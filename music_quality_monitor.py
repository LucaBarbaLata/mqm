#!/usr/bin/env python3
"""
Music Quality Monitor
=====================

Live analysis of whatever is playing on your Windows PC to estimate the *real*
quality of the audio: does Spotify's "Lossless" actually deliver full
bandwidth, or is an app quietly serving a lossy stream?

How it works
------------
* The system audio output is tapped with a WASAPI loopback capture, so nothing
  needs to be re-routed and any app can be checked.
* Lossy codecs (Ogg Vorbis, MP3, AAC, Opus ...) discard the highest
  frequencies to save bits: MP3 320 stops at ~20.4 kHz, MP3/AAC 128 at ~17 kHz,
  Vorbis 96k at ~17 kHz, while a lossless 44.1 kHz source keeps content up to
  ~22 kHz.  The monitor averages the spectrum over each interval and finds
  where the energy falls into the noise floor ("bandwidth").
* Modern codecs at 160-320 kbps keep full bandwidth, so a second test looks
  for *spectral holes*: on a frame-by-frame basis a lossy encoder zeroes
  individual treble coefficients, leaving bins 40 dB below their neighbours.
  Lossless audio shows ~0 % holes, Spotify's 320k Vorbis ~3-4 %, 160k ~15-20 %.
* Windows Core Audio session meters tell us which application is actually
  producing the sound, so the verdict is attributed to Spotify.exe, a browser,
  etc.
* If the samples reaching the mixer sit exactly on a 16-bit (or 24-bit) grid,
  the bit depth of the source is reported as well.

Usage
-----
    python music_quality_monitor.py                 # live monitor, Ctrl+C to stop
    python music_quality_monitor.py --seconds 30    # measure for 30 s, print a summary
    python music_quality_monitor.py --file song.flac
    python music_quality_monitor.py --list-devices

Caveats (please read!)
----------------------
* Bandwidth analysis can only tell you that a stream is *consistent with*
  lossless.  A lossless file made from a lossy master (or an old recording with
  little treble) will look band-limited; a 320 kbps stream is only ~1.5 kHz
  short of lossless.  Judge across several tracks, not a single second.
* Windows resamples everything to the output device's sample rate.  For the
  bit-depth check to work, keep the device at the source rate (44.1 kHz for
  Spotify), disable "Normalize volume" in the player and set both the app and
  Windows volume to 100 %.
* Apps that use exclusive-mode / ASIO bypass the shared mixer and are invisible
  to the loopback capture.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import wave
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import numpy as np

APP_NAME = "Music Quality Monitor"
__version__ = "1.0.0"

# --------------------------------------------------------------------------- #
# Tunables
# --------------------------------------------------------------------------- #
FFT_SIZE = 8192                 # frequency resolution: 5.4 Hz/bin @ 44.1 kHz
SMOOTH_HZ = 100.0               # spectral smoothing applied before edge detection
RUN_HZ = 150.0                  # contiguous bandwidth that must exceed the floor
FLOOR_PERCENTILE = 2.0          # quietest bins above 4 kHz define the noise floor
FLOOR_MARGIN_DB = 12.0          # content must clear the floor by this much
DYNAMIC_RANGE_DB = 95.0         # ...and be within this range of the spectrum peak
SILENCE_DBFS = -70.0            # blocks quieter than this are ignored
HISTORY_SECONDS = 12.0          # rolling window used for the verdict

# Spectral-hole detection: lossy codecs zero individual MDCT coefficients in the
# treble on a frame-by-frame basis.  Short Blackman-windowed frames (the length
# of a Vorbis/AAC long block) reveal bins far below their neighbours.
HOLE_FFT = 2048
HOLE_BAND_LO = 14_000.0         # search band: 14 kHz up to just below the cutoff
HOLE_BAND_HI = 21_000.0
HOLE_DEPTH_DB = 40.0            # a bin this far under the band median is a hole
HOLE_MIN_BAND_HZ = 2_000.0      # need at least this much band to judge
HOLE_LIGHT = 0.5                # % holes: below = clean, above = codec artefacts
HOLE_HEAVY = 8.0                # % holes: above = <= ~160 kbps class

# Bandwidth thresholds (Hz).  Measured with real encoders (libvorbis, LAME,
# ffmpeg-aac, libopus) at the bitrates streaming services use.
BW_HIRES = 23_000
BW_FULL = 20_700                # >= : full CD bandwidth (lossless, Vorbis >=160k, AAC >=256k)
BW_HIGH = 18_800                # >= : MP3 320 (20.4k), Opus (20.0k), MP3 192 (19.0k)
BW_MID = 16_300                 # >= : Vorbis 96k (18.7k), AAC 128 (17.4k), MP3 128 (16.9k)
BW_LOW = 14_500

TIER_ICON = {
    "hires": "✔", "lossless": "✔", "vhigh": "▲", "high": "▲",
    "mid": "▼", "low": "✖", "verylow": "✖",
}


# --------------------------------------------------------------------------- #
# Spectrum analysis (pure numpy; works on any OS)
# --------------------------------------------------------------------------- #
@dataclass
class Analysis:
    cutoff_hz: float
    rms_dbfs: float
    peak_dbfs: float
    floor_db: float
    hole_pct: Optional[float]       # % of treble bins that are codec holes (None = not enough treble)
    holey_frames: Optional[float]   # % of frames containing holes
    bit_depth: Optional[int]        # 16 / 24 when samples sit on that grid, else None
    spectrum_db: np.ndarray         # smoothed averaged spectrum (dBFS per bin)
    freqs: np.ndarray
    silent: bool


def classify(cutoff_hz: float, hole_pct: Optional[float]) -> tuple[str, str]:
    """Combine bandwidth and spectral-hole evidence into a verdict."""
    holes = "unknown" if hole_pct is None else (
        "clean" if hole_pct < HOLE_LIGHT else "light" if hole_pct < HOLE_HEAVY else "heavy")
    if cutoff_hz >= BW_HIRES and holes != "heavy":
        return "hires", "Hi-res: content above 22 kHz (>44.1 kHz source)"
    if cutoff_hz >= BW_FULL:
        if holes == "clean":
            return "lossless", "Lossless-consistent: full bandwidth, no codec holes"
        if holes == "light":
            return "vhigh", "Lossy ~320 kbps (Vorbis: Spotify 'Very High') - full bandwidth but codec holes"
        if holes == "heavy":
            return "mid", "Lossy ~160 kbps (Vorbis: Spotify 'High') - full bandwidth, heavy codec holes"
        return "lossless", "Full bandwidth (treble too quiet to check for codec holes)"
    if cutoff_hz >= BW_HIGH:
        if holes == "heavy":
            return "mid", "Lossy ~160-192 kbps (MP3 192 / lowpassed Vorbis)"
        return "high", "Lossy ~256-320 kbps class (MP3 320, Opus, AAC) - lowpass under 20.7 kHz"
    if cutoff_hz >= BW_MID:
        return "mid", "Lossy ~128-192 kbps (MP3 128-192, AAC 128, Vorbis 96-128)"
    if cutoff_hz >= BW_LOW:
        return "low", "Lossy ~96-128 kbps (Spotify 'Normal' 96k, MP3 128)"
    return "verylow", "Very low bitrate (<= 64-96 kbps) or heavily band-limited source"


def to_db(x: np.ndarray | float, floor: float = 1e-20) -> np.ndarray | float:
    return 10.0 * np.log10(np.maximum(x, floor))


def estimate_bit_depth(x: np.ndarray) -> Optional[int]:
    """Return 16 or 24 if the samples land on that integer grid, else None.

    A 16-bit source that reaches the mixer untouched (no volume scaling, no
    resampling, no normalisation) produces float samples that are exact
    multiples of 1/32768.  Any DSP in the path smears them off the grid.
    """
    x = x[np.abs(x) > 1e-6].astype(np.float64)
    if x.size < 20_000 or np.unique(x).size < 500:
        return None
    for bits in (16, 24):
        scaled = x * float(2 ** (bits - 1))
        on_grid = np.abs(scaled - np.rint(scaled)) < 1e-3
        if np.mean(on_grid) > 0.999:
            return bits
    return None


class SpectrumAnalyzer:
    def __init__(self, sample_rate: int, fft_size: int = FFT_SIZE):
        self.rate = sample_rate
        self.n = fft_size
        self.window = np.hanning(fft_size).astype(np.float64)
        self.coherent_gain = self.window.sum() / 2.0     # full-scale sine -> 0 dBFS
        self.freqs = np.fft.rfftfreq(fft_size, 1.0 / sample_rate)
        self.bin_hz = self.freqs[1]
        self.smooth_bins = max(1, int(round(SMOOTH_HZ / self.bin_hz)))
        self.run_bins = max(1, int(round(RUN_HZ / self.bin_hz)))
        self._hole_window = np.blackman(HOLE_FFT)

    def analyze(self, audio: np.ndarray) -> Optional[Analysis]:
        """`audio` is (frames, channels) or (frames,) float32 in [-1, 1]."""
        if audio.ndim == 2:
            mono = audio.mean(axis=1)
        else:
            mono = audio
        if mono.size < self.n:
            return None

        peak = float(np.max(np.abs(mono)))
        rms = float(np.sqrt(np.mean(mono.astype(np.float64) ** 2)))
        rms_db = float(to_db(rms ** 2))
        peak_db = float(to_db(peak ** 2))
        silent = rms_db < SILENCE_DBFS

        # Welch-style average of |FFT|^2 over 50 %-overlapping Hann frames.
        hop = self.n // 2
        n_frames = 1 + (mono.size - self.n) // hop
        idx = np.arange(self.n)[None, :] + hop * np.arange(n_frames)[:, None]
        frames = mono[idx].astype(np.float64) * self.window
        power = (np.abs(np.fft.rfft(frames, axis=1)) / self.coherent_gain) ** 2
        spec_db = to_db(power.mean(axis=0))

        # Light smoothing so a single quiet bin doesn't break the edge search.
        if self.smooth_bins > 1:
            kernel = np.ones(self.smooth_bins) / self.smooth_bins
            spec_db = np.convolve(spec_db, kernel, mode="same")

        hf = self.freqs >= 4000.0
        floor_db = float(np.percentile(spec_db[hf], FLOOR_PERCENTILE))
        # Content must clear the noise floor AND sit within the useful dynamic
        # range of the signal; the second rule stops an ultra-deep floor (e.g.
        # the empty 22-24 kHz region of a 48 kHz mix) from promoting residue.
        threshold = max(floor_db + FLOOR_MARGIN_DB, float(spec_db.max()) - DYNAMIC_RANGE_DB)
        cutoff_hz = self._find_cutoff(spec_db, threshold)

        hole_pct = holey = None
        if not silent:
            hole_pct, holey = self._spectral_holes(mono, cutoff_hz)
        bits = None if silent else estimate_bit_depth(audio.reshape(-1))
        return Analysis(cutoff_hz, rms_db, peak_db, floor_db, hole_pct, holey, bits,
                        spec_db, self.freqs, silent)

    def _spectral_holes(self, mono: np.ndarray, cutoff_hz: float):
        """% of treble bins (14 kHz .. cutoff) that a codec zeroed out."""
        n = HOLE_FFT
        hi = min(HOLE_BAND_HI, cutoff_hz - 300.0)
        if hi - HOLE_BAND_LO < HOLE_MIN_BAND_HZ or mono.size < 4 * n:
            return None, None
        n_frames = mono.size // n
        frames = mono[: n_frames * n].reshape(n_frames, n).astype(np.float64)
        loud = np.sqrt((frames ** 2).mean(axis=1)) > 10 ** (SILENCE_DBFS / 20)
        frames = frames[loud]
        if frames.shape[0] < 4:
            return None, None
        power = np.abs(np.fft.rfft(frames * self._hole_window, axis=1)) ** 2
        freqs = np.fft.rfftfreq(n, 1.0 / self.rate)
        band = (freqs >= HOLE_BAND_LO) & (freqs < hi)
        pb = power[:, band]
        median = np.median(pb, axis=1, keepdims=True)
        # frames whose treble is essentially absent cannot be judged
        usable = median[:, 0] > 10 ** ((SILENCE_DBFS - 50) / 10)
        if usable.sum() < 4:
            return None, None
        holes = (pb[usable] < median[usable] * 10 ** (-HOLE_DEPTH_DB / 10)).mean(axis=1)
        return float(holes.mean() * 100.0), float((holes > 0.01).mean() * 100.0)

    def _find_cutoff(self, spec_db: np.ndarray, threshold: float) -> float:
        """Highest frequency below which a RUN_HZ-wide band stays above threshold."""
        above = spec_db > threshold
        run = np.convolve(above.astype(np.float64), np.ones(self.run_bins), mode="valid")
        good = np.nonzero(run >= 0.9 * self.run_bins)[0]
        if good.size == 0:
            return 0.0
        top_bin = good[-1] + self.run_bins - 1
        return float(self.freqs[min(top_bin, spec_db.size - 1)])


# --------------------------------------------------------------------------- #
# Windows: which app is playing, what format is the device in
# --------------------------------------------------------------------------- #
IS_WINDOWS = sys.platform == "win32"

if IS_WINDOWS:
    from ctypes import wintypes

    class _BLOB(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.ULONG), ("pBlobData", ctypes.POINTER(ctypes.c_ubyte))]

    class _PROPVARIANT_UNION(ctypes.Union):
        _fields_ = [("blob", _BLOB), ("uhVal", ctypes.c_ulonglong),
                    ("pwszVal", wintypes.LPWSTR), ("_pad", ctypes.c_ubyte * 16)]

    class _PROPVARIANT(ctypes.Structure):  # full 24-byte layout, blob-capable
        _fields_ = [("vt", wintypes.USHORT), ("r1", wintypes.WORD), ("r2", wintypes.WORD),
                    ("r3", wintypes.WORD), ("union", _PROPVARIANT_UNION)]


@dataclass
class SessionInfo:
    name: str
    pid: int
    peak: float
    active: bool

    @property
    def peak_dbfs(self) -> float:
        return float(to_db(self.peak ** 2))


@dataclass
class DeviceInfo:
    name: str
    id: str
    mix_rate: int
    mix_bits: int
    device_rate: Optional[int]
    device_bits: Optional[int]      # container bits (16 / 24 / 32)
    device_valid_bits: Optional[int]


class WindowsAudio:
    """Thin wrapper around pycaw / Core Audio."""

    def __init__(self):
        from comtypes import CLSCTX_ALL, GUID  # noqa: F401  (import check)
        from pycaw.pycaw import (AudioUtilities, IAudioClient, IAudioMeterInformation,
                                 PROPERTYKEY, STGM, WAVEFORMATEX)
        self._au = AudioUtilities
        self._IAudioClient = IAudioClient
        self._IMeter = IAudioMeterInformation
        self._PROPERTYKEY = PROPERTYKEY
        self._STGM = STGM
        self._WAVEFORMATEX = WAVEFORMATEX
        self._CLSCTX_ALL = CLSCTX_ALL
        self._GUID = GUID

    def default_device(self) -> DeviceInfo:
        spk = self._au.GetSpeakers()
        dev = spk._dev
        mix_rate, mix_bits = 0, 0
        try:
            iface = dev.Activate(self._IAudioClient._iid_, self._CLSCTX_ALL, None)
            client = ctypes.cast(iface, ctypes.POINTER(self._IAudioClient))
            wfx = client.GetMixFormat().contents
            mix_rate, mix_bits = int(wfx.nSamplesPerSec), int(wfx.wBitsPerSample)
        except Exception:
            pass
        dev_rate = dev_bits = dev_valid = None
        try:
            dev_rate, dev_bits, dev_valid = self._device_format(dev)
        except Exception:
            pass
        return DeviceInfo(spk.FriendlyName, spk.id, mix_rate, mix_bits, dev_rate, dev_bits, dev_valid)

    def _device_format(self, dev):
        """PKEY_AudioEngine_DeviceFormat via a raw vtable call (pycaw's
        PROPVARIANT cannot hold a VT_BLOB)."""
        store = dev.OpenPropertyStore(self._STGM.STGM_READ.value)
        key = self._PROPERTYKEY()
        key.fmtid = self._GUID("{f19f064d-082c-4e27-bc73-6882a1bb8e4c}")
        key.pid = 0
        pv = _PROPVARIANT()
        raw = ctypes.cast(store, ctypes.c_void_p).value
        vtbl = ctypes.cast(raw, ctypes.POINTER(ctypes.c_void_p)).contents.value
        get_value = ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p,
                                       ctypes.POINTER(self._PROPERTYKEY),
                                       ctypes.POINTER(_PROPVARIANT))
        fn = get_value(ctypes.cast(vtbl, ctypes.POINTER(ctypes.c_void_p))[5])  # IPropertyStore::GetValue
        try:
            fn(raw, ctypes.byref(key), ctypes.byref(pv))
            if pv.vt != 65 or pv.union.blob.cbSize < 18:   # VT_BLOB
                return None, None, None
            data = bytes(ctypes.string_at(pv.union.blob.pBlobData, pv.union.blob.cbSize))
        finally:
            ctypes.windll.ole32.PropVariantClear(ctypes.byref(pv))
        tag = int.from_bytes(data[0:2], "little")
        rate = int.from_bytes(data[4:8], "little")
        bits = int.from_bytes(data[14:16], "little")
        valid = bits
        if tag == 0xFFFE and len(data) >= 20:            # WAVEFORMATEXTENSIBLE
            valid = int.from_bytes(data[18:20], "little") or bits
        return rate, bits, valid

    def sessions(self) -> list[SessionInfo]:
        out = []
        own_pid = os.getpid()
        for s in self._au.GetAllSessions():
            if s.Process and s.ProcessId == own_pid:
                continue        # the loopback capture registers a session of its own
            try:
                meter = s._ctl.QueryInterface(self._IMeter)
                peak = float(meter.GetPeakValue())
            except Exception:
                peak = 0.0
            name = s.Process.name() if s.Process else "System sounds"
            pid = s.ProcessId if s.Process else 0
            out.append(SessionInfo(name, pid, peak, s.State == 1))
        return out

    def playing(self) -> list[SessionInfo]:
        return sorted((s for s in self.sessions() if s.active and s.peak > 1e-4),
                      key=lambda s: s.peak, reverse=True)


# --------------------------------------------------------------------------- #
# WASAPI loopback capture
# --------------------------------------------------------------------------- #
class LoopbackCapture:
    def __init__(self, device_index: Optional[int] = None, frames_per_buffer: int = 2048):
        import pyaudiowpatch as pyaudio
        self._pa_mod = pyaudio
        self.pa = pyaudio.PyAudio()
        if device_index is None:
            info = self.pa.get_default_wasapi_loopback()
        else:
            info = self.pa.get_device_info_by_index(device_index)
        self.info = info
        self.name = info["name"]
        self.rate = int(info["defaultSampleRate"])
        self.channels = int(info["maxInputChannels"])
        self._chunks: deque[np.ndarray] = deque()
        self._lock = threading.Lock()
        self.overflows = 0
        self.stream = self.pa.open(format=pyaudio.paFloat32, channels=self.channels,
                                   rate=self.rate, input=True,
                                   input_device_index=info["index"],
                                   frames_per_buffer=frames_per_buffer,
                                   stream_callback=self._callback)
        self.stream.start_stream()

    def _callback(self, in_data, frame_count, time_info, status):
        if status:
            self.overflows += 1
        arr = np.frombuffer(in_data, dtype=np.float32).reshape(-1, self.channels)
        with self._lock:
            self._chunks.append(arr.copy())
        return (None, self._pa_mod.paContinue)

    def drain(self) -> np.ndarray:
        with self._lock:
            chunks = list(self._chunks)
            self._chunks.clear()
        if not chunks:
            return np.zeros((0, self.channels), dtype=np.float32)
        return np.concatenate(chunks, axis=0)

    def close(self):
        try:
            self.stream.stop_stream()
            self.stream.close()
        finally:
            self.pa.terminate()

    @staticmethod
    def list_devices() -> list[dict]:
        import pyaudiowpatch as pyaudio
        pa = pyaudio.PyAudio()
        try:
            return [d for d in pa.get_loopback_device_info_generator()]
        finally:
            pa.terminate()


# --------------------------------------------------------------------------- #
# Rolling verdict
# --------------------------------------------------------------------------- #
class History:
    def __init__(self, seconds: float, interval: float):
        self.items: deque[Analysis] = deque(maxlen=max(1, int(round(seconds / interval))))
        self.all_cutoffs: list[float] = []
        self.all_holes: list[float] = []
        self.apps: dict[str, int] = {}

    def add(self, a: Analysis, apps: list[str]):
        if a.silent:
            return
        self.items.append(a)
        self.all_cutoffs.append(a.cutoff_hz)
        if a.hole_pct is not None:
            self.all_holes.append(a.hole_pct)
        for name in apps:
            self.apps[name] = self.apps.get(name, 0) + 1

    @property
    def cutoff(self) -> Optional[float]:
        return float(np.median([a.cutoff_hz for a in self.items])) if self.items else None

    @property
    def holes(self) -> Optional[float]:
        h = [a.hole_pct for a in self.items if a.hole_pct is not None]
        return float(np.median(h)) if h else None

    @property
    def holey_frames(self) -> Optional[float]:
        h = [a.holey_frames for a in self.items if a.holey_frames is not None]
        return float(np.median(h)) if h else None

    @property
    def spread(self) -> Optional[float]:
        if len(self.items) < 3:
            return None
        c = np.array([a.cutoff_hz for a in self.items])
        return float(np.percentile(c, 90) - np.percentile(c, 10))

    @property
    def bit_depth(self) -> Optional[int]:
        bits = [a.bit_depth for a in self.items if a.bit_depth]
        if not bits:
            return None
        return max(set(bits), key=bits.count)


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
BLOCKS = " ▁▂▃▄▅▆▇█"


def enable_ansi():
    if IS_WINDOWS:
        try:
            k32 = ctypes.windll.kernel32
            h = k32.GetStdHandle(-11)
            mode = wintypes.DWORD()
            if k32.GetConsoleMode(h, ctypes.byref(mode)):
                k32.SetConsoleMode(h, mode.value | 0x0004)
        except Exception:
            pass
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def spectrum_bar(a: Analysis, columns: int = 56, top_db: float = -10.0, span_db: float = 110.0) -> str:
    """One-line spectrogram column view from 0 Hz to Nyquist."""
    edges = np.linspace(0, a.freqs[-1], columns + 1)
    out = []
    for lo, hi in zip(edges[:-1], edges[1:]):
        sel = (a.freqs >= lo) & (a.freqs < hi)
        level = float(a.spectrum_db[sel].max()) if sel.any() else -200.0
        frac = (level - (top_db - span_db)) / span_db
        out.append(BLOCKS[int(np.clip(frac, 0, 1) * (len(BLOCKS) - 1))])
    return "".join(out)


def spectrum_axis(nyquist: float, columns: int = 56) -> str:
    axis = [" "] * columns
    for khz in range(0, int(nyquist // 1000) + 1, 5):
        pos = int(round(khz * 1000 / nyquist * (columns - 1)))
        label = f"{khz}k"
        for i, ch in enumerate(label):
            if pos + i < columns:
                axis[pos + i] = ch
    return "".join(axis)


def fmt_khz(hz: Optional[float]) -> str:
    return "  --  " if hz is None else f"{hz / 1000:5.1f}k"


class LiveView:
    def __init__(self):
        self.lines_drawn = 0

    def draw(self, lines: list[str]):
        if self.lines_drawn:
            sys.stdout.write(f"\x1b[{self.lines_drawn}A")
        for line in lines:
            sys.stdout.write("\x1b[2K" + line + "\n")
        # blank out leftovers from a previously taller frame
        for _ in range(self.lines_drawn - len(lines)):
            sys.stdout.write("\x1b[2K\n")
        if self.lines_drawn > len(lines):
            sys.stdout.write(f"\x1b[{self.lines_drawn - len(lines)}A")
        self.lines_drawn = len(lines)
        sys.stdout.flush()


def holes_text(pct: Optional[float], frames: Optional[float]) -> str:
    if pct is None:
        return "-- (not enough treble energy to judge)"
    if pct < HOLE_LIGHT:
        kind = "clean: no lossy-codec artefacts"
    elif pct < HOLE_HEAVY:
        kind = "light: typical of ~320 kbps Vorbis"
    else:
        kind = "heavy: typical of <= 160 kbps"
    return f"{pct:4.1f} % of treble bins empty in {frames:3.0f} % of frames  ({kind})"


def bit_depth_text(bits: Optional[int], dev: Optional[DeviceInfo]) -> str:
    if bits:
        return f"{bits}-bit (samples sit exactly on the {bits}-bit grid: untouched integer path)"
    hint = "volume <100 %, normalisation, EQ or resampling in the path"
    if dev and dev.device_rate and dev.mix_rate and dev.device_rate != 44100:
        hint = f"device runs at {dev.mix_rate} Hz so 44.1 kHz sources are resampled"
    return f"float / processed ({hint})"


def render(dev: Optional[DeviceInfo], capture_name: str, playing: list[SessionInfo],
           latest: Optional[Analysis], hist: History, elapsed: float, overflow: int) -> list[str]:
    now = datetime.now().strftime("%H:%M:%S")
    width = 72
    L = [f" {APP_NAME} v{__version__}".ljust(width - 22) + f"{now}   {elapsed:6.0f} s",
         " " + "─" * (width - 2)]
    if dev:
        fmt = "unknown format"
        if dev.device_rate:
            vb = f"{dev.device_valid_bits}-bit" if dev.device_valid_bits else ""
            fmt = f"{vb} / {dev.device_rate} Hz".strip(" /")
        L.append(f" Output device : {dev.name}")
        L.append(f" Device format : {fmt}   (shared mix {dev.mix_rate} Hz, {dev.mix_bits}-bit float)")
    else:
        L.append(f" Capture       : {capture_name}")

    if playing:
        apps = ", ".join(f"{s.name} ({s.peak_dbfs:.0f} dBFS)" for s in playing[:3])
        L.append(f" Now playing   : {apps}")
        if len(playing) > 1:
            L.append("                 ⚠ several apps are audible: verdict applies to the mix")
    else:
        L.append(" Now playing   : (no app is producing audio)")
    L.append("")

    if latest is None or latest.silent:
        state = "waiting for audio ..."
        if playing and latest is not None:
            state = ("app is playing but the shared mix is silent - exclusive mode / ASIO / "
                     "other device?")
        L.append(f" Level         : {state}")
        L.append(" Bandwidth     : --")
        L.append(" Bit depth     : --")
        L.append(" Verdict       : --")
    else:
        L.append(f" Level         : RMS {latest.rms_dbfs:6.1f} dBFS   peak {latest.peak_dbfs:6.1f} dBFS"
                 f"   floor {latest.floor_db:6.0f} dB")
        med = hist.cutoff
        spread = hist.spread
        stab = ""
        if spread is not None:
            stab = f"   ({'steady' if spread < 600 else 'varying'} ±{spread / 2000:.1f} kHz over {len(hist.items)} s)"
        L.append(f" Bandwidth     : now {fmt_khz(latest.cutoff_hz)}   median {fmt_khz(med)}{stab}")
        L.append(f" Codec holes   : {holes_text(hist.holes, hist.holey_frames)}")
        L.append(f" Bit depth     : {bit_depth_text(hist.bit_depth, dev)}")
        key, label = classify(med if med is not None else latest.cutoff_hz, hist.holes)
        note = ""
        nyq = latest.freqs[-1]
        if med is not None and med > 0.97 * nyq and key != "hires":
            note = f"  [reaches device Nyquist {nyq / 1000:.1f} kHz: raise the device sample rate to test hi-res]"
        L.append(f" Verdict       : {TIER_ICON[key]} {label}{note}")
    L.append("")
    if latest is not None:
        cut = hist.cutoff if hist.cutoff is not None else latest.cutoff_hz
        cols = 56
        marker = [" "] * cols
        if not latest.silent and cut > 0:
            marker[min(cols - 1, int(round(cut / latest.freqs[-1] * (cols - 1))))] = "▼"
        L.append(" Spectrum       " + "".join(marker) + "  (▼ = bandwidth)")
        L.append(" 0 dB..-120 dB  " + spectrum_bar(latest, cols))
        L.append("                " + spectrum_axis(latest.freqs[-1], cols))
    if overflow:
        L.append(f" ⚠ {overflow} capture overflow(s)")
    return L


def summary_lines(hist: History, title: str) -> list[str]:
    if not hist.all_cutoffs:
        return [f"{title}: no audio was analysed."]
    c = np.array(hist.all_cutoffs)
    med = float(np.median(c))
    holes = float(np.median(hist.all_holes)) if hist.all_holes else None
    key, label = classify(med, holes)
    L = [f"{title}",
         f"  analysed intervals : {c.size}",
         f"  bandwidth median   : {med / 1000:.2f} kHz  (10-90 %: {np.percentile(c, 10) / 1000:.1f} - "
         f"{np.percentile(c, 90) / 1000:.1f} kHz)",
         f"  codec holes        : {'--' if holes is None else f'{holes:.2f} % of treble bins'}",
         f"  bit depth          : {hist.bit_depth or 'float / processed'}",
         f"  verdict            : {TIER_ICON[key]} {label}"]
    if hist.apps:
        top = sorted(hist.apps.items(), key=lambda kv: kv[1], reverse=True)
        L.append("  audible apps       : " + ", ".join(f"{n} ({k}s)" for n, k in top[:5]))
    return L


# --------------------------------------------------------------------------- #
# Modes
# --------------------------------------------------------------------------- #
def run_live(args) -> int:
    enable_ansi()
    try:
        win = WindowsAudio() if IS_WINDOWS else None
    except Exception as e:  # pycaw missing / COM failure: still allow capture
        print(f"(per-app attribution unavailable: {e})")
        win = None

    try:
        cap = LoopbackCapture(args.device)
    except Exception as e:
        print(f"Could not open loopback capture: {e}")
        print("Install dependencies with:  pip install -r requirements.txt")
        return 1

    analyzer = SpectrumAnalyzer(cap.rate)
    hist = History(HISTORY_SECONDS, args.interval)
    view = LiveView()
    dev = win.default_device() if win else None
    recorder = None
    if args.record:
        recorder = wave.open(args.record, "wb")
        recorder.setnchannels(cap.channels)
        recorder.setsampwidth(4)
        recorder.setframerate(cap.rate)

    print(f"{APP_NAME}: capturing '{cap.name}' at {cap.rate} Hz. Press Ctrl+C to stop.\n")
    t0 = time.monotonic()
    latest = None
    last_dev_check = t0
    try:
        while True:
            time.sleep(args.interval)
            elapsed = time.monotonic() - t0
            audio = cap.drain()
            if recorder is not None and audio.size:
                recorder.writeframes((np.clip(audio, -1, 1) * 2147483647.0).astype("<i4").tobytes())
            playing = win.playing() if win else []
            result = analyzer.analyze(audio)
            if result is not None:
                latest = result
                hist.add(result, [s.name for s in playing])
            view.draw(render(dev, cap.name, playing, latest, hist, elapsed, cap.overflows))

            # follow the default device if the user switches outputs
            if win and time.monotonic() - last_dev_check > 5:
                last_dev_check = time.monotonic()
                try:
                    new_dev = win.default_device()
                    if new_dev.id != dev.id:
                        dev = new_dev
                        cap.close()
                        cap = LoopbackCapture(args.device)
                        analyzer = SpectrumAnalyzer(cap.rate)
                        hist = History(HISTORY_SECONDS, args.interval)
                        latest = None
                except Exception:
                    pass
            if args.seconds and elapsed >= args.seconds:
                break
    except KeyboardInterrupt:
        pass
    finally:
        cap.close()
        if recorder is not None:
            recorder.close()
    print()
    print("\n".join(summary_lines(hist, "Summary")))
    if args.json:
        print(json.dumps(summary_json(hist), indent=2))
    return 0


def summary_json(hist: History) -> dict:
    if not hist.all_cutoffs:
        return {"analysed_intervals": 0}
    c = np.array(hist.all_cutoffs)
    holes = float(np.median(hist.all_holes)) if hist.all_holes else None
    key, label = classify(float(np.median(c)), holes)
    return {"analysed_intervals": int(c.size), "bandwidth_hz_median": float(np.median(c)),
            "bandwidth_hz_p10": float(np.percentile(c, 10)), "bandwidth_hz_p90": float(np.percentile(c, 90)),
            "codec_holes_pct": holes, "bit_depth": hist.bit_depth, "tier": key, "verdict": label,
            "apps": hist.apps}


def decode_file(path: str) -> tuple[np.ndarray, int, dict]:
    """Decode any audio file with ffmpeg (falls back to the wave module for .wav)."""
    meta: dict = {}
    if shutil.which("ffprobe") and shutil.which("ffmpeg"):
        probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
                                "stream=codec_name,sample_rate,channels,bits_per_raw_sample,"
                                "bits_per_sample,bit_rate", "-of", "json", path],
                               capture_output=True, text=True)
        if probe.returncode == 0:
            streams = json.loads(probe.stdout or "{}").get("streams", [])
            if streams:
                meta = streams[0]
        rate = int(meta.get("sample_rate", 44100))
        channels = int(meta.get("channels", 2))
        raw = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-vn", "-f", "f32le",
                              "-acodec", "pcm_f32le", "-ar", str(rate), "-ac", str(channels), "-"],
                             capture_output=True)
        if raw.returncode != 0:
            raise RuntimeError(raw.stderr.decode(errors="replace").strip())
        audio = np.frombuffer(raw.stdout, dtype=np.float32).reshape(-1, channels)
        return audio, rate, meta

    with wave.open(path, "rb") as w:
        channels, width, rate = w.getnchannels(), w.getsampwidth(), w.getframerate()
        data = w.readframes(w.getnframes())
    if width == 2:
        audio = np.frombuffer(data, "<i2").astype(np.float32) / 32768.0
    elif width == 3:
        b = np.frombuffer(data, np.uint8).reshape(-1, 3).astype(np.int32)
        audio = ((b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)) << 8 >> 8).astype(np.float32) / 8388608.0
    elif width == 4:
        audio = np.frombuffer(data, "<i4").astype(np.float32) / 2147483648.0
    else:
        raise RuntimeError(f"unsupported sample width {width}")
    meta = {"codec_name": "wav", "sample_rate": rate, "channels": channels, "bits_per_sample": width * 8}
    return audio.reshape(-1, channels), rate, meta


def run_file(args) -> int:
    enable_ansi()
    audio, rate, meta = decode_file(args.file)
    analyzer = SpectrumAnalyzer(rate)
    hist = History(HISTORY_SECONDS, args.interval)
    step = int(rate * args.interval)
    print(f"{APP_NAME}: {os.path.basename(args.file)}")
    desc = f"{meta.get('codec_name', '?')} {rate} Hz {meta.get('channels', '?')} ch"
    bits = meta.get("bits_per_raw_sample") or meta.get("bits_per_sample")
    if bits and str(bits) != "0":
        desc += f" {bits}-bit"
    if meta.get("bit_rate"):
        desc += f" {int(meta['bit_rate']) // 1000} kbps"
    print(f"  container says     : {desc}")
    n_blocks = max(1, (audio.shape[0] - FFT_SIZE) // step + 1)
    for i in range(n_blocks):
        block = audio[i * step:(i + 1) * step + FFT_SIZE]
        a = analyzer.analyze(block)
        if a is None:
            continue
        hist.add(a, [])
        if args.verbose:
            t = i * args.interval
            holes = "  holes   --  " if a.hole_pct is None else f"  holes {a.hole_pct:5.1f} %"
            state = "silent" if a.silent else (
                f"bandwidth {a.cutoff_hz / 1000:5.1f} kHz{holes}  rms {a.rms_dbfs:6.1f} dBFS")
            print(f"  {t:7.1f}s  {state}")
    print("\n".join(summary_lines(hist, "  analysis")[1:]))
    if args.json:
        print(json.dumps(summary_json(hist), indent=2))
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="music_quality_monitor", description=APP_NAME,
                                formatter_class=argparse.RawDescriptionHelpFormatter,
                                epilog=__doc__.split("Usage")[0])
    p.add_argument("--file", "-f", help="analyse an audio file instead of live playback")
    p.add_argument("--seconds", "-s", type=float, default=0, help="stop after N seconds and print a summary")
    p.add_argument("--interval", "-i", type=float, default=1.0, help="analysis interval in seconds (default 1)")
    p.add_argument("--device", "-d", type=int, help="loopback device index (see --list-devices)")
    p.add_argument("--list-devices", action="store_true", help="list WASAPI loopback devices and exit")
    p.add_argument("--record", metavar="OUT.wav", help="also save the captured audio (32-bit PCM WAV)")
    p.add_argument("--json", action="store_true", help="print a JSON summary at the end")
    p.add_argument("--verbose", "-v", action="store_true", help="per-interval lines in --file mode")
    p.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    args = p.parse_args(argv)

    if args.list_devices:
        for d in LoopbackCapture.list_devices():
            print(f"[{d['index']:2d}] {d['name']}  {int(d['defaultSampleRate'])} Hz, {d['maxInputChannels']} ch")
        return 0
    if args.file:
        return run_file(args)
    if not IS_WINDOWS:
        print("Live monitoring needs Windows (WASAPI loopback). Use --file on other platforms.")
        return 1
    return run_live(args)


if __name__ == "__main__":
    sys.exit(main())
