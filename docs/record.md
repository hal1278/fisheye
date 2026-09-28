# Record stage

## Purpose

Record acquires all primary and auxiliary information required for later synchronization of fisheye-camera video to GNSS UTC.

User-facing command:

```text
fisheye record
```

The current implementation uses FFmpeg camera capture and a u-blox M8T timing receiver.

## Responsibilities

Record includes:

- camera recording through FFmpeg;
- GNSS timing recording from the M8T;
- applying and verifying the intended receiver RAM configuration;
- coarse host-clock anchors between camera and GNSS acquisition paths;
- acquisition metadata;
- FFmpeg diagnostics.

Record does not perform:

- ROI selection;
- LED detection;
- LED-to-TIMEPULSE epoch association;
- frame UTC assignment;
- synchronized-video generation.

## Configuration

Human-edited configuration is stored in TOML.

Configuration includes at least:

- session output directory;
- camera backend and device identifier;
- camera mode;
- segment duration;
- GNSS serial port;
- GNSS measurement rate;
- GNSS periodic-message configuration;
- TIMEPULSE configuration.

Camera mode and segment duration must not silently fall back to implicit values.

GNSS message selection is a receiver configuration concern. Raw observation recording must not discard received UBX messages because they are not part of a recorder-side allow-list.

## M8T role

The M8T is dedicated to time synchronization of the fisheye video.

Current target operation:

- measurement rate: 1 Hz;
- TIMEPULSE: 1 Hz;
- TIMEPULSE aligned to UTC integer-second boundaries;
- rising edge as the timing edge;
- periodic `UBX-TIM-TP` enabled;
- `UBX-NAV-PVT` permitted as a diagnostic trace;
- unnecessary periodic NMEA/UBX output disabled.

The session records the actual polled receiver configuration in addition to the desired configuration.

Persistent BBR/flash modification is not required.

## Capture directory

Each Record run creates:

```text
recordings/YYYYMMDD_HHMMSS/
├── capture/
│   ├── video_000.mkv
│   ├── video_001.mkv
│   ├── ...
│   ├── gnss.ubx
│   ├── gnss.csv
│   ├── camera.csv
│   ├── ffmpeg.log
│   └── session.json
└── processing/
```

The session directory is named from acquisition start time.

`capture/` is immutable input to later stages. ROI, Sync, and Export must not rewrite or delete capture files.

## Video and PTS

The Matroska video files are the primary camera record.

Requirements:

- preserve source presentation timestamps;
- do not reset timestamps at segment boundaries;
- use passthrough frame timing rather than synthetic CFR duplication/drop;
- do not write a redundant per-frame PTS CSV during acquisition;
- later stages obtain frame timing from the media container.

Segment files are storage units only. Later stages treat them as one virtual source timeline.

The origin of DirectShow sample timestamps is device/driver dependent and is not assumed to be a direct camera hardware-clock readout.

## `gnss.ubx`

`gnss.ubx` is the primary lossless GNSS trace.

Received serial bytes during observation are stored without recorder-side periodic-message selection. Receiver configuration determines which periodic messages are emitted.

The raw trace retains native UBX precision and fields intentionally normalized or omitted from `gnss.csv`.

## `gnss.csv`

`gnss.csv` is a normalized convenience view for synchronization processing, not the lossless source.

Current columns:

```csv
tp_utc,q_err_ps,tp_locked,host_monotonic_ns
```

`tp_utc` is the UTC time associated with the TIMEPULSE as derived from `UBX-TIM-TP`.

`q_err_ps` retains the TIMEPULSE quantization-error field when valid.

`tp_locked` records whether the TIMEPULSE is locked to GNSS time for the epoch.

`host_monotonic_ns` is the host monotonic-clock value when the recorder receives/processes the TIM-TP message. It is not the physical TIMEPULSE timestamp and is used only as a coarse association axis.

Exact UBX representation remains recoverable from `gnss.ubx`.

## `camera.csv`

`camera.csv` stores coarse correspondences between FFmpeg's reported video time and the same host monotonic clock used by GNSS recording.

Current columns:

```csv
video_time_ns,host_monotonic_ns
```

It is not a per-frame timing log and not a fine synchronization measurement.

Its purposes are:

- coarse association between video and GNSS time domains;
- resolving integer-second ambiguity for observed LED pulses;
- gross anomaly diagnostics.

Observed FFmpeg progress-report latency may temporarily shift these anchors by seconds; Sync therefore uses many anchors robustly rather than nearest-neighbor matching of individual rows.

## `ffmpeg.log`

FFmpeg stderr is retained at `info` level for diagnostics including:

- actual selected input mode;
- timestamp warnings;
- buffer overruns;
- corrupt/malformed packets;
- segment/muxer events;
- termination reason.

Machine-readable FFmpeg progress output is parsed separately into `camera.csv`.

## `session.json`

`session.json` contains machine-generated session metadata, including:

- schema version and acquisition status;
- configured camera and GNSS values;
- actual receiver identity and firmware;
- actual `CFG-RATE` and `CFG-TP5` values;
- configured periodic GNSS messages;
- FFmpeg version;
- application/Python/platform versions;
- camera command;
- session start/end and failure information.

The TOML configuration remains the human-edited source of requested settings.

## Integrity and failure behavior

Record must fail explicitly on configuration/open/startup errors rather than switching modes silently.

During acquisition:

- GNSS raw data is append-only;
- CSV rows represent actually observed events and do not synthesize missing epochs;
- video timestamps are not rewritten merely to make them look continuous;
- child-process and serial failures are visible in session metadata.
