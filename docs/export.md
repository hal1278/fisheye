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
timestamps.csv
export.json
ffmpeg.log
```

Existing export-run directories are never overwritten.

This permits multiple exports from one synchronization solution without rerunning Sync, for example with a different cut point or encoding setting.

## Output range

`--start` is source-video elapsed time, measured from the first real source frame.

For a requested start of 3180 s, choose the first real source frame whose source elapsed time is at least 3180 s. If that indexed source frame is known from Sync to be undecodable, advance to the first later indexed frame not listed in `sync.json.source.undecodable_frame_indices`.

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

The current implementation accepts the global affine model emitted by Sync. Piecewise-model export will be added together with piecewise fitting if real-data residuals show that it is necessary.

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

FFmpeg is used for rendering/encoding rather than reimplementing a video encoder in Python. Export opens only source segments that overlap the retained output range. For an MJPEG first input segment it also performs input seeking to approximately one second before the retained cut, while the filter graph still performs the exact PTS trim. These optimizations do not change CRF, preset, pixel format, frame cadence, or the retained-frame set. Source segments retain their global timestamps and are temporally interleaved inside the filter graph. The retained source PTS are rebased and scaled by the fitted clock rate before `drawtext` formats the corrected frame PTS as UTC.

## `timestamps.csv`

Export also writes one row per retained output frame:

```text
output_frame_index,output_pts_s,source_frame_index,source_pts_s,source_elapsed_s,estimated_utc,estimated_utc_epoch_s
```

`output_pts_s` follows the same affine synchronization model as the rendered video. `estimated_utc` is ISO 8601 UTC text with microsecond formatting for machine-readable interchange; the extra digits are representation precision and do not imply corresponding absolute timing accuracy. Known undecodable source frames are omitted so the CSV row count matches the decoded output frame count.

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
- retained input-segment range and first-input seek;
- `timestamps.csv` provenance and row count;
- validation result.

## Validation

An Export run is successful only if validation passes, including:

- rendered frame count agrees with the retained indexed source-frame count after excluding source frames that Sync recorded as undecodable;
- output PTS are strictly increasing;
- first output PTS is zero;
- output duration agrees with synchronized UTC span within muxing time-base resolution;
- generated MP4 decodes to completion;
- decoded output frame count and PTS endpoints agree with the values predicted from `sync.json` within output time-base resolution.

Visual verification of the burned text on representative first/middle/last frames remains an empirical check for the first real-data export.

A failed Export run remains in its own run directory for diagnostics and must not overwrite a previous successful export.
