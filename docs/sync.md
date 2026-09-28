# Sync stage

## Purpose

Sync analyzes a capture and selected LED ROI to produce a validated mapping from source video PTS to UTC.

User-facing command:

```text
fisheye sync CAPTURE --sync-start HH:MM:SS --roi X,Y,W,H
```

Sync does not generate the final shareable video. Export is a separate stage that consumes the synchronization solution.

## Required inputs

Normal processing uses:

- `video_*.mkv` — source frames and PTS;
- `gnss.csv` — normalized TIMEPULSE UTC epochs and lock status;
- `camera.csv` — coarse video/GNSS association;
- `session.json` — session and TIMEPULSE configuration validation;
- `--roi` — fixed LED ROI selected by the ROI stage;
- `--sync-start` — beginning of the interval after LED/fiber position adjustment.

Normally not required by the algorithm:

- `gnss.ubx` — retained as the lossless audit/recovery source;
- `ffmpeg.log` — acquisition diagnostics.

## Processing run directory

After argument parsing and basic capture validation succeed, Sync creates:

```text
recordings/YYYYMMDD_HHMMSS/processing/YYYYMMDD_HHMMSS/
```

before expensive media analysis begins.

Basic validation includes at least:

- capture directory exists;
- required files exist and are readable;
- `session.json` parses;
- at least one video segment is discovered;
- ROI and `--sync-start` are syntactically and geometrically valid.

This avoids empty runs for obvious invocation errors while preserving logs and partial artifacts if indexing, LED detection, epoch matching, or model fitting later fails.

Existing processing-run directories are never overwritten.

Expected outputs:

```text
led_edges.csv
sync.json
sync.log
```

The processing directory represents one synchronization solution and is the input unit for Export.

## Source video timeline

Treat all segments as one virtual source timeline.

For every source frame, preserve its media PTS/time base. Source elapsed time is:

```text
source_elapsed = frame_pts_time - first_frame_pts_time
```

Never use `frame_index / nominal_fps` as a synchronization time.

Validate:

- segment order;
- consistent stream properties;
- strictly increasing frame PTS in the virtual source;
- absence of duplicate frame timestamps.

Timestamp gaps are recorded but are not filled with synthetic frames.

## Synchronization interval

`--sync-start` marks the beginning of the interval in which the LED/fiber position is considered fixed and usable for automatic LED detection.

Frames before `--sync-start` are not used to estimate LED edges or the synchronization model.

There is no output-video cut option in Sync; that belongs to Export.

## LED measurement

Build the source packet/PTS index with the shared media layer. For LED extraction, run FFmpeg sequentially over each source segment, decode the MJPEG stream, crop the fixed ROI before RGB conversion, set a microsecond filter time base, and carry the ROI frames through a NUT pipe as uncompressed RGB with their PTS. PyAV demuxes that timestamped ROI stream. Each decoded ROI frame is matched back to the indexed source PTS rather than being associated by ordinal frame number. A malformed MJPEG packet may therefore be recorded as an undecodable source-frame index without shifting the PTS association of every subsequent LED sample.

The NUT layer is used only to preserve timestamps across the pipe; it does not add image compression. For a 32 x 31 RGB ROI the payload is only 2976 bytes per decoded frame, so the extra pipe/mux/demux overhead is small compared with MJPEG decoding, while avoiding transfer or RGB conversion of the full 960 x 720 frame in Python.

Use NumPy to compute a scalar LED score from the fixed ROI. The initial score is a red-excess statistic such as:

```text
R - (G + B) / 2
```

aggregated over the ROI in a way that remains sensitive to the LED spot.

The threshold is derived from the observed score distribution rather than hard-coded to an absolute pixel value. A simple histogram/Otsu-style two-state threshold is sufficient initially.

Configured TIMEPULSE properties provide sanity checks. For the current system:

- frequency: 1 Hz;
- reference edge: rising;
- duty cycle: approximately 0.1;
- time grid: UTC.

Do not require an exact count of ON frames because the nominal frame interval is comparable to the pulse width.

## Rising-edge observations

Represent each detected rising edge by the interval between:

- the last OFF frame PTS;
- the first ON frame PTS.

Store both bounds.

For fitting, use the midpoint as the initial representative observation:

```text
P_edge = (P_off + P_on) / 2
```

This retains the finite-frame observation interval rather than pretending the pulse is known exactly at the first ON frame.

Falling edges are used only for pulse-shape/duty sanity checks.

## Pulse sequence

Consecutive rising-edge separations determine logical pulse indices.

Approximately 1 s indicates adjacent pulses; approximately 2 s indicates one missed visible pulse, etc.

Allow frame-scale timing quantization when deciding which integer pulse separation is plausible. Large inconsistencies cause the edge sequence to be rejected rather than silently repaired.

## Absolute UTC epoch association

Fine timing comes from the physical LED edge in the video.

The host timestamps in `camera.csv` and `gnss.csv` are only coarse association data.

Do not associate each LED rise independently with the nearest `gnss.csv.host_monotonic_ns` value. Acquisition/progress-report latency can temporarily shift camera coarse anchors by seconds.

Instead:

1. derive a coarse host-monotonic-to-UTC relation from many valid `gnss.csv` rows;
2. map many `camera.csv` anchors through that coarse relation;
3. estimate the video-to-UTC integer-second offset robustly, initially by a median-based estimator;
4. use that coarse absolute offset to choose the integer UTC second for the LED pulse sequence;
5. verify selected epochs against `gnss.csv` and lock status.

A 1 Hz unlabeled pulse train cannot by itself distinguish a solution shifted by an exact integer second. If the coarse anchors cannot resolve that ambiguity safely, Sync must fail instead of choosing arbitrarily.

## PTS-to-UTC model

After pulse-to-UTC correspondence is established, fit an affine clock model:

```text
UTC(P) = UTC_ref + a + b * (P - P_ref)
```

where `P` is source-video PTS expressed as time.

Use a reference near the middle of the observation to avoid unnecessary numerical loss from fitting large absolute epoch values.

Initial fitting may use ordinary least squares followed by explicit residual-based rejection and refitting. NumPy is sufficient initially.

Keep rejected edges in `led_edges.csv` with `used=false`.

## Global versus piecewise model

Start with one global affine model for the usable synchronization interval.

Evaluate LED residuals versus time, including:

- median;
- median absolute deviation;
- RMS;
- absolute P95;
- maximum;
- binned median residual over time.

Do not perform independent interpolation between every pair of 1 Hz edges. Frame-scale edge-observation error would be converted directly into artificial local clock-rate changes.

Use a piecewise model only if residuals show systematic drift or a step large relative to the frame observation scale.

The current initial implementation fits a global affine model and reports `piecewise_recommended` when the span of 300 s binned median residuals exceeds half the median frame interval. It does not yet generate a piecewise model automatically; that threshold and piecewise implementation remain empirical follow-up items after the first real-data run.

For ordinary slow drift, a future piecewise implementation should preserve UTC continuity at boundaries. A real source-PTS discontinuity may justify a discontinuous model region.

## `led_edges.csv`

`led_edges.csv` contains event-level evidence, not one row per source frame.

It should include enough information to reproduce and inspect each edge, including:

- source segment/frame identity;
- OFF/ON PTS bounds;
- midpoint;
- assigned UTC TIMEPULSE epoch;
- whether the edge was used;
- fitted residual.

## `sync.json`

`sync.json` is the machine-readable contract between Sync and Export.

It records at least:

- schema version;
- source capture identity/path information sufficient to reopen the capture;
- `sync_start`;
- ROI;
- LED threshold/method;
- detected and used edge counts;
- coarse absolute-offset estimate and spread;
- GNSS epoch range;
- model type and parameters;
- residual statistics;
- software/PyAV versions needed for reproducibility.

It does not contain Export-specific cut, codec, CRF, or overlay-format choices.

## Timestamp uncertainty

The fitted model yields an estimated UTC frame time.

Regression residual is not by itself an absolute timestamp uncertainty because exposure timing, rolling-shutter/readout behavior, threshold crossing, and other systematic offsets may remain.

Sync therefore records residual statistics but does not claim a `±X ms` absolute uncertainty until those systematic effects have been characterized.
