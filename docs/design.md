# Fisheye logging design

Status: initial design decisions as of 2026-09-25.

## Scope

This project records the data required to synchronize fisheye-camera video with GNSS time in later offline processing.

The current scope is **logging only**.

Included:

- camera recording through FFmpeg;
- GNSS timing logging from a u-blox M8T;
- configuration and validation of the M8T for the logging session;
- coarse host-clock anchors between the camera and GNSS acquisition paths;
- session metadata and diagnostic logs.

Not included:

- LED detection;
- matching LED pulses to `UBX-TIM-TP`;
- assigning absolute timestamps to video frames;
- online or offline synchronization algorithms;
- position/velocity processing from the M8T.

Synchronization is intentionally a separate post-processing problem. The logger only has to preserve sufficient primary and auxiliary information so that synchronization remains recoverable even when some LED pulses are not observable.

## Application structure

The application is a Python packaged application managed by `uv`.

Expected command:

```text
uv run fisheye-logging
```

Configuration is stored in TOML. The package should use a `src` layout and provide a CLI entry point through `pyproject.toml`.

FFmpeg remains an external executable rather than a Python package dependency. Python is responsible for orchestration, GNSS serial I/O, metadata, coarse timing anchors, process lifecycle, and error handling. FFmpeg is responsible for camera capture, encoding/copying, muxing, and segmentation.

The architecture should not depend on Windows except at the camera-backend boundary. The initial backend is DirectShow. Other backends such as V4L2 may be added without changing the GNSS or session logic.

## Configuration

Human-edited configuration is stored in TOML.

Configuration should contain at least:

- output directory;
- camera backend;
- camera device identifier;
- camera mode/preset;
- segment duration;
- GNSS serial port;
- GNSS measurement rate;
- GNSS message output configuration.

Camera preset and segment duration are required configuration. They should not silently fall back to implicit defaults.

GNSS message selection is a receiver configuration concern. The logger must not hard-code an allow-list such as `TIM-TP`/`NAV-PVT` when deciding which received UBX bytes to preserve.

A representative GNSS message configuration is:

```toml
[gnss.messages]
TIM-TP = 1
NAV-PVT = 1
```

The exact TOML schema is not yet frozen.

## M8T role and configuration

The M8T is dedicated to fisheye-camera time synchronization. Positioning at higher rate is performed by another receiver.

Target operating configuration:

- measurement rate: 1 Hz;
- TIMEPULSE: 1 Hz;
- TIMEPULSE aligned to UTC integer-second boundaries;
- periodic `UBX-TIM-TP` enabled;
- `UBX-NAV-PVT` may also be enabled as a low-rate trace/diagnostic message;
- unnecessary NMEA and UBX periodic outputs disabled.

Observed receiver used during design:

- model: LEA-M8T-0;
- firmware: TIM 1.10;
- protocol version: 22.00.

Session startup should apply or verify the intended RAM configuration and poll the resulting actual configuration. Persistent BBR/flash modification is not required by the logging design.

The actual receiver configuration, not only the desired TOML values, is recorded in session metadata.

## Session output

A session directory separates acquisition output from later derived data:

```text
<session>/
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
    └── <processing-time>/
        └── ...
```

The session directory name is the acquisition start time. `capture/` contains the
primary acquisition record and should be treated as read-only by post-processing.
`processing/` is reserved for derived output; each post-processing execution writes
to a directory named by its processing start time. Existing output directories are
errors rather than overwrite targets.

The capture names are source-oriented:

- `gnss.*`: data derived from or received from the GNSS receiver;
- `camera.csv`: coarse anchors for the camera/video time axis;
- `video_*.mkv`: primary camera data;
- `ffmpeg.log`: FFmpeg diagnostic output;
- `session.json`: non-epoch acquisition metadata.

## Video data and PTS

The video container is the primary camera record.

Video presentation timestamps (PTS) are preserved in the Matroska files and are the primary continuous camera-side time axis used by post-processing.

Requirements:

- do not reset timestamps at segment boundaries;
- do not intentionally convert the stream to a synthetic constant-frame-rate timeline by duplicating or dropping frames;
- do not create a second per-frame CSV containing all PTS values;
- post-processing should inspect the timestamps stored in the media container, e.g. with `ffprobe`, rather than relying on a media player's displayed playback counter.

The precise origin of DirectShow sample timestamps is device/driver dependent. They are treated as device-provided video timestamps, not assumed to be a direct readout of a camera hardware counter.

## `gnss.ubx`

`gnss.ubx` is the primary lossless GNSS trace.

The serial bytes received during the observation phase are stored without logger-side message selection. Receiver-side configuration controls which periodic messages are emitted.

This preserves the original UBX representation, including fields whose native precision is intentionally not reproduced in `gnss.csv`.

Configuration/poll traffic may be kept outside the observation trace; the actual configuration is recorded separately in `session.json`.

## `gnss.csv`

`gnss.csv` is a normalized convenience view for timing post-processing. It is not the lossless source record.

One row corresponds to one received `UBX-TIM-TP` message.

Current columns:

```csv
tp_utc,q_err_ps,tp_locked,host_monotonic_ns
```

### `tp_utc`

UTC time of the corresponding TIMEPULSE, derived from the `UBX-TIM-TP` time fields.

Representation:

- ISO 8601;
- UTC (`Z`);
- millisecond fractional precision is sufficient for this camera system.

Example:

```text
2026-09-25T01:23:45.000Z
```

The native `TIM-TP` representation has finer resolution. That information is not discarded from the session because it remains available bit-for-bit in `gnss.ubx`.

The camera system currently records at approximately 15 fps and uses Matroska PTS as its video time axis. The synchronization accuracy is therefore dominated by camera/frame/LED observation characteristics rather than the sub-millisecond representation of the TIMEPULSE target time.

### `q_err_ps`

The `UBX-TIM-TP.qErr` value in picoseconds.

It is retained because it describes TIMEPULSE quantization error for that epoch. If the corresponding UBX validity flag marks it invalid, the CSV value should be empty/null rather than requiring a separate `q_err_invalid` column.

### `tp_locked`

Boolean derived from `UBX-TIM-TP.TpNotLocked`:

- `true`: the TIMEPULSE is locked to GNSS time;
- `false`: it is not.

This is retained per epoch because TIMEPULSE lock may change during an observation and directly affects whether the pulse can be used as a timing reference.

### `host_monotonic_ns`

Host monotonic-clock value recorded when the logger receives/processes the `TIM-TP` message.

This is not the physical TIMEPULSE timestamp and must not be used as a fine synchronization measurement.

Its purpose is to provide a coarse host-side axis shared with camera acquisition. It can be used to resolve integer-second ambiguity and to recover correspondence when LED observations are temporarily unavailable.

### Fields intentionally not duplicated per epoch

The following are not currently separate CSV columns:

- GPS/GNSS week and TOW raw fields: represented in normalized form by `tp_utc`, with exact originals retained in `gnss.ubx`;
- time base: session-level configuration;
- UTC availability/standard: used for validation or session metadata rather than duplicated every row unless later evidence shows epoch-level changes must be retained in the convenience CSV;
- GNSS reference system: not needed when operating on UTC time base;
- RAIM status: not required for the current synchronization workflow;
- position and velocity: outside the M8T's role;
- `NAV-PVT.gnssFixOk`: not a substitute for TIMEPULSE lock and would require joining a different message epoch.

`NAV-PVT` may still be present in `gnss.ubx` for diagnostics and later inspection.

## `camera.csv`

`camera.csv` records **coarse** correspondences between the FFmpeg/video time axis and the same host monotonic clock used by `gnss.csv`.

It is not a per-frame log.

Current conceptual columns:

```csv
video_time_ns,host_monotonic_ns
```

The exact source of `video_time_ns` within FFmpeg progress/output reporting is an implementation detail still to be verified. The anchor is allowed to contain buffering/reporting latency because it is not used for fine synchronization.

Anchors may be written every one to several seconds.

Purpose:

- give the camera and GNSS acquisition paths a coarse common host-side axis;
- help determine which integer-second TIMEPULSE corresponds to a visible LED pulse;
- detect/recover across temporary LED-observation gaps;
- provide evidence of video PTS discontinuities or gross timing anomalies.

Fine synchronization is always based on the physical LED pulse visible in the video, not on `camera.csv`.

## Handling missing LED observations

The logging design does not require every 1 Hz LED pulse to be observable.

Post-processing can use:

1. observed LED edges as fine physical synchronization points;
2. video PTS continuity between observed edges;
3. `camera.csv` and `gnss.csv` host-monotonic anchors as a coarse bridge;
4. manual adjustment when necessary.

A temporary loss of LED visibility therefore does not by itself destroy synchronization. Ambiguity becomes difficult only when LED observations, video PTS continuity, and coarse host anchors all fail over the same interval.

## `ffmpeg.log`

FFmpeg diagnostics are stored.

Initial logging level should preserve `info` output rather than only warnings/errors, so that the session can later be inspected for:

- actual selected input mode;
- timestamp warnings;
- buffer overruns;
- malformed packets;
- segment/muxer events;
- termination reason.

FFmpeg stderr and machine-readable progress output have separate purposes:

- stderr -> `ffmpeg.log`;
- progress/timing information -> parser -> `camera.csv`.

## `session.json`

`session.json` contains non-epoch metadata and actual observed configuration.

Expected categories include:

- schema version;
- camera backend/device/mode;
- segment configuration;
- video timestamp policy;
- receiver identity and firmware;
- actual `CFG-RATE` values;
- actual `CFG-TP5` values;
- GNSS time base;
- configured periodic GNSS messages;
- FFmpeg version;
- fisheye-logging version;
- session start/end status and relevant failure information.

It is machine-generated. The human-edited configuration remains in TOML.

## Error handling and observation integrity

The logger should fail explicitly on configuration/open/startup errors rather than silently changing modes or falling back to different presets.

During acquisition:

- GNSS raw data should be append-only;
- CSV rows should correspond to actually received/observed events and must not synthesize missing epochs;
- video timestamps should not be rewritten merely to make them continuous;
- child-process failure and serial failure must be visible in the session result.

## Non-blocking implementation/verification items

The following remain implementation or empirical-verification items and do not block the initial logging design:

- exact `camera.csv.video_time_ns` source from FFmpeg progress reporting;
- measured relationship between DirectShow/device timestamps and actual exposure timing;
- final TOML schema;
- exact session JSON schema;
- graceful termination details on Windows and other supported platforms;
- offline LED detection and synchronization algorithms, which are explicitly out of scope for the logger.
