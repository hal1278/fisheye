# Export stage

## Purpose

Export converts a validated synchronization solution into a single shareable video.

User-facing command:

```text
fisheye export PROCESSING --start HH:MM:SS
```

`PROCESSING` is a `processing/YYYYMMDD_HHMMSS/` synchronization directory containing `sync.json`.

Export does not redetect LED pulses and does not refit the synchronization model.

## Inputs

Export reads:

- `sync.json`;
- the capture referenced by `sync.json`;
- source `video_*.mkv` files;
- Export-specific options such as output start and encoding settings.

The initial use case starts output at approximately 53 minutes source elapsed time.

## Export run directory

Each Export invocation creates:

```text
PROCESSING/export/YYYYMMDD_HHMMSS/
```

after `sync.json`, referenced capture data, and Export arguments have passed basic validation.

Expected outputs:

```text
video.mp4
export.json
ffmpeg.log
```

Existing export-run directories are never overwritten.

This permits multiple exports from one synchronization solution without rerunning Sync, for example with a different cut point or encoding setting.

## Output range

`--start` is source-video elapsed time, measured from the first real source frame.

For a requested start of 3180 s, choose the first real source frame whose source elapsed time is at least 3180 s.

Do not manufacture a frame exactly at 3180.000 s and do not require the cut to coincide with a keyframe.

Record in `export.json`:

- requested start;
- actual selected source frame/PTS;
- actual selected UTC.

## Output PTS

Map each retained source PTS through the synchronization model.

For a global affine model:

```text
UTC(P) = UTC_ref + a + b * (P - P_ref)
```

and for the first retained frame `P0`:

```text
PTS_out(P) = UTC(P) - UTC(P0)
```

Thus:

- the first output frame has playback PTS zero;
- output elapsed time follows synchronized UTC rate;
- source frames are not duplicated or dropped;
- variable frame cadence is preserved.

Piecewise synchronization models are applied according to the regions recorded in `sync.json`.

## UTC overlay

Burn an estimated UTC frame timestamp into a lens-external region.

Initial display:

```text
UTC(est.) 2026-09-25T01:23:45.27Z
```

Two fractional digits are a display-resolution choice suitable for an approximately 15 fps source. They do not imply 10 ms accuracy.

Do not burn a `±X ms` uncertainty into the video until exposure timing, rolling-shutter/readout effects, LED thresholding, and other systematic offsets have been characterized. Regression residual alone is not an absolute timestamp uncertainty.

The exact overlay position is fixed relative to source pixels and must avoid the fisheye image.

## Encoding

Initial output format:

- H.264/AVC via `libx264`;
- MP4;
- `yuv420p` for broad playback compatibility;
- CRF 12;
- `preset slow`;
- passthrough/VFR frame cadence;
- `+faststart`;
- sufficiently fine MP4 video track timescale.

The original MJPEG Matroska segments remain the analysis masters.

FFmpeg is used for rendering/encoding rather than reimplementing a video encoder in Python.

## `export.json`

`export.json` records Export provenance and options, including at least:

- schema version;
- source `sync.json`;
- requested and actual output-start information;
- first/last output UTC;
- overlay format/position;
- codec and encoding parameters;
- FFmpeg version;
- output frame count and duration;
- validation result.

## Validation

An Export run is successful only if validation passes, including:

- retained source and rendered frame counts agree;
- output PTS are strictly increasing;
- first output PTS is zero;
- output duration agrees with synchronized UTC span within muxing time-base resolution;
- generated MP4 decodes to completion;
- representative first/middle/last timestamps agree with `sync.json` and `export.json`.

A failed Export run remains in its own run directory for diagnostics and must not overwrite a previous successful export.
