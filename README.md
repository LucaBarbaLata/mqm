# Music Quality Monitor

**Find out what quality your music is *really* playing at.**

Music Quality Monitor taps the Windows audio output, works out which
application is producing the sound, and analyses the signal to tell whether it
is consistent with lossless audio or with a lossy stream — and at roughly what
bitrate. Use it to check whether Spotify's *Lossless* setting actually delivers
lossless, whether a "HD" stream is really 128 kbps, or what a browser tab is
serving.

```
 Music Quality Monitor v1.0.0                     01:10:23       20 s
 ──────────────────────────────────────────────────────────────────────
 Output device : Headphones (ACCENTUM Plus)
 Device format : 16-bit / 44100 Hz   (shared mix 44100 Hz, 32-bit float)
 Now playing   : Spotify.exe (-13 dBFS)

 Level         : RMS  -22.8 dBFS   peak   -8.6 dBFS   floor   -110 dB
 Bandwidth     : now  21.1k   median  21.1k   (steady ±0.1 kHz over 12 s)
 Codec holes   :  0.0 % of treble bins empty in   0 % of frames  (clean: no lossy-codec artefacts)
 Bit depth     : float / processed (volume <100 %, normalisation, EQ or resampling in the path)
 Verdict       : ✔ Lossless-consistent: full bandwidth, no codec holes

 Spectrum                                                            ▼    (▼ = bandwidth)
 0 dB..-120 dB  ▆▅▅▅▅▅▄▄▅▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▃▃▃▃▃▃▃▃▃▃▃▂▂▂▂▂▂▂▂▂▂▂▂▂▂▂▂▂▂▁▁▃
                0k          5k           10k         15k          20k
```

## Features

- **Works with any app** – captures the Windows shared mix through WASAPI
  loopback; no virtual cables, no re-routing.
- **Per-app attribution** – reads the Windows audio session meters, so the
  verdict names `Spotify.exe`, `brave.exe`, `Tidal.exe`, …
- **Two independent quality tests** – spectral bandwidth *and* codec
  "spectral holes", calibrated against real encoders (see below). This is what
  separates Spotify *Very High* (320 kbps Vorbis) from true *Lossless*.
- **Bit-depth detection** – reports 16-bit / 24-bit when the samples reach the
  mixer untouched.
- **Device format** – shows the output device's configured bit depth and
  sample rate and the shared-mode mix format.
- **Live text UI** with a one-line spectrum, rolling 12-second median and a
  stability indicator, plus an end-of-run summary (optionally JSON).
- **File mode** – analyse local files (`.flac`, `.wav`, `.mp3`, `.ogg`,
  `.m4a`, … via ffmpeg) to spot fake FLACs.
- **Record mode** – save the captured audio to WAV for inspection in Spek,
  Audacity, etc.

## Requirements

- Windows 10 / 11 (live monitoring uses WASAPI loopback and Core Audio)
- Python 3.9 or newer
- Packages from `requirements.txt`:
  `numpy`, `PyAudioWPatch`, `pycaw`, `comtypes`, `psutil`
- Optional: `ffmpeg` / `ffprobe` on the `PATH` for `--file` with formats
  other than WAV

`--file` mode is pure numpy and also runs on macOS / Linux.

## Installation

```bash
git clone <this repository>
cd mqm
pip install -r requirements.txt
```

## Usage

```bash
python music_quality_monitor.py                    # live monitor, Ctrl+C to stop
python music_quality_monitor.py --seconds 30       # measure 30 s, then print a summary
python music_quality_monitor.py --seconds 30 --json
python music_quality_monitor.py --record capture.wav
python music_quality_monitor.py --file track.flac --verbose
python music_quality_monitor.py --list-devices
python music_quality_monitor.py --device 32
```

| Option | Description |
|---|---|
| `-f`, `--file PATH` | Analyse an audio file instead of live playback |
| `-s`, `--seconds N` | Stop after *N* seconds and print a summary (default: run until Ctrl+C) |
| `-i`, `--interval S` | Analysis interval in seconds (default `1.0`) |
| `-d`, `--device N` | Loopback device index from `--list-devices` (default: current output device) |
| `--list-devices` | List WASAPI loopback devices and exit |
| `--record OUT.wav` | Also save the captured audio as a 32-bit PCM WAV |
| `--json` | Print a JSON summary at the end |
| `-v`, `--verbose` | One line per interval in `--file` mode |
| `--version` | Show the version |

Play music, let the monitor run for 10–20 seconds and read the **median**
values and the verdict. Test a few different tracks: a single track may be
band-limited by its master rather than by the codec.

## Reading the output

| Line | Meaning |
|---|---|
| **Output device** | The Windows default output the monitor is listening to. It follows you when you switch devices. |
| **Device format** | Bit depth / sample rate set in Windows Sound settings, and the shared-mode mix format (always 32-bit float). |
| **Now playing** | Apps whose audio session is active and audible, with their peak level. If more than one is listed, the verdict applies to the *mix*. |
| **Level** | RMS and peak of the last interval and the estimated noise floor. Intervals below −70 dBFS are treated as silence and ignored. |
| **Bandwidth** | Highest frequency that still carries real content. `now` is the last interval, `median` the rolling 12-second median; *steady* means a hard codec lowpass, *varying* means natural content. |
| **Codec holes** | Percentage of treble bins (14 kHz → cutoff) that a lossy encoder zeroed out, and how many frames contain such holes. |
| **Bit depth** | `16-bit` / `24-bit` when samples sit exactly on that integer grid; otherwise the path applies gain, EQ or resampling. |
| **Verdict** | Combination of bandwidth and holes (see next section). `✔` lossless-consistent, `▲` high-bitrate lossy, `▼` mid, `✖` low. |
| **Spectrum** | Averaged spectrum from 0 Hz to the device's Nyquist frequency, `▼` marks the measured bandwidth. |

## How it works

### 1. Bandwidth

Every lossy encoder lowpasses the audio to spend its bits where they matter.
The monitor averages 8192-point spectra over each interval, estimates the
noise floor, and finds the highest frequency below which a 150 Hz-wide band
still rises clearly above it.

| Measured bandwidth | Typical source |
|---|---|
| > 23 kHz | Hi-res source (device must run at 88.2 / 96 kHz or more to show it) |
| 20.7 – 22 kHz | Lossless, Vorbis ≥ 160 kbps, AAC ≥ 256 kbps |
| 18.8 – 20.7 kHz | MP3 320 (≈ 20.4 kHz), Opus (≈ 20 kHz), MP3 192 (≈ 19 kHz) |
| 16.3 – 18.8 kHz | Vorbis 96 kbps (Spotify *Normal*), AAC 128, MP3 128 |
| < 16.3 kHz | ≤ 96 kbps or a heavily band-limited master |

### 2. Codec holes

Bandwidth alone cannot tell Spotify's 160 / 320 kbps Vorbis streams from FLAC:
modern codecs keep the full 21–22 kHz. What they *cannot* hide is that, frame
by frame, they zero out individual MDCT coefficients in the treble.

The monitor cuts the signal into 2048-sample Blackman-windowed frames (the
length of a Vorbis/AAC long block) and, between 14 kHz and the measured
cutoff, counts the bins that sit more than 40 dB below the band median.
Lossless audio essentially never produces such bins.

Measured on real music (same track, encoded with libvorbis / LAME / ffmpeg-aac):

| Source | Bandwidth | Holes | Verdict |
|---|---|---|---|
| FLAC / WAV | 21.3 kHz | 0.0 % | ✔ Lossless-consistent |
| Vorbis 320 kbps (Spotify *Very High*) | 21.4 kHz | ~3–4 % | ▲ Lossy ~320 kbps |
| Vorbis 160 kbps (Spotify *High*) | 20.9 kHz | ~15–20 % | ▼ Lossy ~160 kbps |
| Vorbis 96 kbps (Spotify *Normal*) | 16.9 kHz | – | ▼ Lossy ~128 kbps class |
| MP3 320 / 192 / 128 kbps | 20.2 / 18.8 / 16.8 kHz | 0.2 / 9 / 9 % | correct tier each |
| AAC 128 kbps | 17.3 kHz | – | ▼ Lossy ~128 kbps class |
| Opus 96 / 160 kbps | 20.5 kHz | – | ▲ Lossy 256–320 kbps class |

The hole test still works if the player dithers its output to 16-bit
(Vorbis 320 → 2.6 %, Vorbis 160 → 14 %).

Verdict thresholds: holes < 0.5 % = *clean*, 0.5–8 % = *light* (≈ 320 kbps
Vorbis), > 8 % = *heavy* (≤ 160 kbps).

### 3. Bit depth

A 16-bit source that reaches the mixer untouched produces float samples that
are exact multiples of 1/32768; a 24-bit source, multiples of 1/8388608. Any
gain, normalisation, EQ or resampling in the path smears the values off the
grid, in which case the line reads *float / processed*.

### 4. Which app is playing

The Windows Core Audio session API exposes one session per process with a
peak meter. Sessions that are active and audible are listed, loudest first.
The monitor's own loopback session is excluded.

## Checking Spotify step by step

1. In Spotify: *Settings → Audio quality* – set *Streaming quality* to the
   tier you want to verify. For the bit-depth check also switch off
   *Normalize volume* and set Spotify's volume to 100 %.
2. In Windows: set the output device to **44100 Hz** (Sound settings → device
   → Format) and volume to 100 % if you want the bit-depth check to work.
3. Run `python music_quality_monitor.py` and play a modern, well-produced track.
4. Expected results:

   | Spotify setting | Bandwidth | Holes | Verdict |
   |---|---|---|---|
   | Lossless | ~21–22 kHz | ~0 % | ✔ Lossless-consistent |
   | Very High (320 kbps) | ~21–22 kHz | ~3–4 % | ▲ Lossy ~320 kbps |
   | High (160 kbps) | ~21 kHz | > 8 % | ▼ Lossy ~160 kbps |
   | Normal (96 kbps) | ~17 kHz | – | ▼ Lossy ~128 kbps class |

5. Switch tiers while the monitor runs and watch the median move (Spotify may
   need a track change or a few seconds to re-buffer at the new quality).

## Limitations

- **Consistency, not proof.** The monitor reports what the signal is
  *consistent with*. A lossless file made from a lossy or band-limited master
  looks lossy; judge across several tracks and trust the median.
- **AAC ≥ 256 kbps** (Apple Music, YouTube Music Premium) is near-transparent,
  keeps full bandwidth and fills gaps with noise – it reads as
  *lossless-consistent*. Detecting it is out of scope for this method.
- **Resampling.** Windows resamples every app to the device's sample rate.
  Hi-res content above 22 kHz is only visible with the device at 88.2 / 96 kHz
  or more; the bit-depth check needs the device at the source rate.
- **Exclusive mode / ASIO.** Players using WASAPI exclusive mode or ASIO
  bypass the shared mixer and are invisible to loopback capture. The monitor
  warns when an app is active but the mix is silent.
- **Several apps at once** – the verdict applies to the mix; pause the others.
- **Quiet or dark material** – if there is too little treble energy the hole
  test is skipped and the verdict rests on bandwidth alone.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Could not open loopback capture` | `pip install -r requirements.txt`; run `--list-devices` and pick one with `--device N`. |
| *waiting for audio …* while music plays | The app may play on a different device than the Windows default (`--list-devices`), or use exclusive mode. |
| Verdict flips between tiers | Wait for the 12-second median to settle; skip intros/outros with little treble. |
| *Bit depth: float / processed* | Expected unless the whole path is at unity gain (see *Checking Spotify*). |
| `--file` fails on MP3/FLAC | Install ffmpeg and make sure `ffmpeg` and `ffprobe` are on the `PATH`; WAV works without it. |
| Capture overflow warnings | Increase `--interval` or close CPU-heavy programs. |

## Project layout

```
mqm/
├── music_quality_monitor.py   # the whole application (analysis, capture, UI, CLI)
├── requirements.txt
└── README.md
```

All tuning constants (FFT sizes, thresholds, tier boundaries) are grouped at
the top of `music_quality_monitor.py` under *Tunables*.
