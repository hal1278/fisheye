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
timestamps_simple.csv
export.json
ffmpeg.log
```

A successful Export performs only lightweight output validation. Full frame-by-frame decoding is an explicit, separate verification step:

```text
fisheye verify PROCESSING/export/YYYYMMDD_HHMMSS
```

which writes `verification.json` in the export directory.

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
Estimated UTC: 2026-09-25T01:23:45.27Z
```

Two fractional digits are a display-resolution choice suitable for an approximately 15 fps source. They do not imply 10 ms accuracy. The displayed value is derived from each corrected output PTS, so source-frame PTS jitter remains visible up to the 0.01 s display resolution; it is not reconstructed from frame index and nominal frame rate.

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
- encoder time base of 1/90000 s;
- an x264 nominal frame rate derived from the configured source nominal rate and fitted clock rate;
- `+faststart`;
- 90 kHz MP4 video track timescale.

The original MJPEG Matroska segments remain the analysis masters.

FFmpeg is used for rendering/encoding rather than reimplementing a video encoder in Python. Export opens only source segments that overlap the retained output range. For an MJPEG first input segment it also performs input seeking to approximately one second before the retained cut, while the filter graph still performs the exact PTS trim. These optimizations do not change CRF, preset, pixel format, frame cadence, or the retained-frame set. Source segments retain their global timestamps and are temporally interleaved inside the filter graph. The retained source PTS are rebased and scaled by the fitted clock rate before `drawtext` formats the corrected frame PTS as UTC.

The `interleave` filter deliberately marks its output frame rate as variable/unknown. Export therefore sets a fine encoder time base independently and supplies libx264 with a nominal frame rate of `source_nominal_fps / fitted_rate`. This nominal value is encoder metadata/parameterization only: Export does not use `-r` or an `fps` filter, and `-fps_mode passthrough` keeps the corrected per-frame PTS rather than snapping frames to a constant-rate grid.

## Timestamp CSV files

Export writes a detailed timestamp table, `timestamps.csv`, with one row per retained output frame:

```text
output_frame_index,output_pts_s,source_frame_index,source_pts_s,source_elapsed_s,estimated_utc,estimated_utc_epoch_s
```

`output_pts_s` follows the same affine synchronization model as the rendered video. `estimated_utc` is ISO 8601 UTC text with microsecond formatting for machine-readable interchange; the extra digits are representation precision and do not imply corresponding absolute timing accuracy. Known undecodable source frames are omitted so the CSV row count matches the decoded output frame count.

For sharing, Export also writes `timestamps_simple.csv`:

```text
frame_index,time_s,estimated_utc
```

Here `frame_index` is the zero-based output-video frame index and `time_s` is the corresponding zero-based output-video PTS in seconds. The simplified file formats `time_s` to six decimal places and `estimated_utc` to milliseconds (three fractional digits). These are presentation choices for the sharing-oriented table; they do not imply higher absolute timing accuracy. The values are generated from the same synchronization model as the detailed table.

## `export.json`

`export.json` records Export provenance and options, including at least:

- schema version;
- source `sync.json`;
- requested and actual output-start information;
- first/last output UTC;
- overlay format/position;
- codec and encoding parameters;
- FFmpeg version;
- expected output frame count and last output PTS;
- lightweight output validation results;
- retained input-segment range and first-input seek;
- detailed and simplified timestamp CSV paths and row count;
- validation result.

## Validation

Export and full verification are deliberately separate responsibilities.

A normal Export run performs lightweight validation without decoding the completed H.264 file again. It requires:

- FFmpeg to exit successfully and report `progress=end`;
- FFmpeg's encoded frame count to agree with the retained indexed source-frame count after known undecodable source frames are excluded;
- FFmpeg to report no duplicated or dropped frames;
- the MP4 to exist, be non-empty, and open as a container with exactly one video stream;
- the stream start PTS to be zero;
- container frame-count metadata, when present, to agree with the expected frame count;
- stream duration to be consistent with the synchronized UTC span;
- both timestamp CSV files to contain one row per expected output frame.

The timestamp CSV files are generated from the source timeline and synchronization model before encoding and are published only after lightweight validation succeeds.

For explicit end-to-end verification, run:

```text
fisheye verify PROCESSING/export/YYYYMMDD_HHMMSS
```

Verify fully decodes `video.mp4` and checks:

- every decoded frame has a usable PTS;
- output PTS are strictly increasing;
- decoded frame count equals `expected_frame_count`;
- first output PTS is zero;
- last output PTS agrees with the synchronized expected last PTS;
- each decoded output PTS agrees with the corresponding `timestamps.csv` row within output time-base tolerance.

A successful verification writes `verification.json`. Verification does not re-encode the video and may be rerun independently of Export.

Visual verification of the burned text on representative first/middle/last frames remains an empirical check for the first real-data export.

A failed Export run remains in its own run directory for diagnostics and must not overwrite a previous successful export.
