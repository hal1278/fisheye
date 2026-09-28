# Synchronization stage

## Purpose

Synchronization converts a captured session and selected LED ROI into a single shareable video with a UTC estimate burned into every output frame.

User-facing command:

```text
fisheye sync CAPTURE --sync-start HH:MM:SS --roi X,Y,W,H --output-start HH:MM:SS
```

The initial use case selects `recordings/YYYYMMDD_HHMMSS/capture/`, excludes the LED-position-adjustment period from synchronization, and starts shared-video output at approximately 53 minutes source elapsed time.

The top-level stage contains two internal phases:

1. synchronization analysis;
2. synchronized-video rendering.

They remain one user-facing command initially.

## Required capture inputs

Normal processing uses:

- `video_*.mkv` — source frames and PTS;
- `gnss.csv` — normalized TIMEPULSE UTC epochs and lock status;
- `camera.csv` — coarse video/GNSS association;
- `session.json` — session and TIMEPULSE configuration validation.

Normally not required by the algorithm:

- `gnss.ubx` — retained as the lossless audit/recovery source;
- `ffmpeg.log` — acquisition diagnostics.

## Processing run directory

After argument parsing and basic capture validation succeed, `fisheye sync` creates:

```text
recordings/YYYYMMDD_HHMMSS/processing/YYYYMMDD_HHMMSS/
```

before expensive video analysis begins.

Basic validation includes at least:

- capture directory exists;
- required files exist and are readable;
- `session.json` parses;
- at least one video segment is discovered;
- requested ROI and time arguments are syntactically valid.

This timing avoids empty run directories for obvious invocation errors, while ensuring that failures during indexing, LED detection, model fitting, or rendering can leave a diagnostic run record.

Existing processing-run directories are never overwritten.

Expected derived outputs are:

```text
led_edges.csv
sync.json
process.log
video.mp4
```

`video.mp4` is written only after synchronization analysis passes validation.

## Source video timeline

Treat all segments as one virtual source timeline.

For every decoded source frame, preserve its media PTS/time base. Source elapsed time is:

```text
source_elapsed = frame_pts_time - first_frame_pts_time
```

Never use `frame_index / nominal_fps` as a synchronization time.

Validate:

- segment order;
- consistent stream properties;
- strictly increasing frame PTS within the virtual source;
- absence of duplicate frame timestamps.

Timestamp gaps are recorded but are not filled with synthetic frames.

The current capture configuration uses passthrough timing and does not reset timestamps at segment boundaries, so segment boundaries should not require special synchronization treatment unless the media itself proves otherwise.

## Synchronization interval and output interval

`--sync-start` and `--output-start` are independent source-elapsed-time parameters.

`--sync-start` marks the beginning of the interval in which the LED/fiber position is considered fixed and usable for automatic LED detection.

Frames before `--sync-start` are not used to estimate LED edges or the synchronization model.

`--output-start` controls only the shared-video cut.

For a requested start of 3180 s, select the first real source frame whose source elapsed time is at least 3180 s. Do not create an artificial frame exactly at 3180.000 s and do not require the cut to coincide with a keyframe.

Synchronization analysis may use valid LED observations before `--output-start`.

## LED measurement

Use the fixed ROI selected by the ROI Selection stage.

Decode source frames sequentially over the synchronization interval using PyAV. Use NumPy to compute a scalar LED score from the ROI.

The initial score is a red-excess statistic such as:

```text
R - (G + B) / 2
```

aggregated over the small ROI in a way that remains sensitive to the LED spot.

The threshold is derived from the observed score distribution rather than hard-coded to an absolute pixel value. A simple histogram/Otsu-style two-state threshold is sufficient initially; no general machine-learning library is required.

Configured TIMEPULSE properties provide sanity checks. For the current system:

- frequency: 1 Hz;
- reference edge: rising;
- duty cycle: approximately 0.1;
- time grid: UTC.

Do not require an exact count of ON frames because the nominal frame interval is comparable to the pulse width.

## Rising-edge observations

Represent a detected rising edge by the interval between:

- the last OFF frame PTS;
- the first ON frame PTS.

Store both bounds.

For fitting, use the midpoint as the initial representative observation:

```text
P_edge = (P_off + P_on) / 2
```

This retains the finite-frame observation interval rather than pretending the pulse is known exactly at the first ON frame.

Falling edges are used only for pulse-shape/duty sanity checks, not as synchronization references.

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
5. verify the chosen epochs exist in `gnss.csv` and are locked where required.

A 1 Hz unlabeled pulse train cannot by itself distinguish a solution shifted by an exact integer second. If the coarse anchors cannot resolve that ambiguity safely, synchronization must fail instead of choosing arbitrarily.

## PTS-to-UTC model

After pulse-to-UTC correspondence is established, fit an affine clock model:

```text
UTC(P) = UTC_ref + a + b * (P - P_ref)
```

where `P` is source-video PTS expressed as time.

Use a reference near the middle of the observation to avoid unnecessary numerical loss from fitting large absolute epoch values.

Initial fitting may use ordinary least squares followed by explicit residual-based rejection and refitting. NumPy is sufficient; SciPy is not required initially.

Keep rejected edges in `led_edges.csv` with a `used=false` indication rather than deleting them.

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

Use a piecewise model only if residuals show systematic drift or a step that is large relative to the frame observation scale.

For ordinary slow drift, piecewise sections preserve UTC continuity at boundaries. A real source-PTS discontinuity may justify a discontinuous model region.

The exact automatic threshold for switching from global to piecewise remains an empirical validation item; approximately half a nominal frame interval is a useful scale for deciding whether added model complexity is meaningful, not a statement of absolute timestamp accuracy.

## Analysis artifacts

`led_edges.csv` contains event-level evidence, not one row per source frame.

It should include enough information to reproduce and inspect each edge, including:

- source segment/frame identity;
- OFF/ON PTS bounds;
- midpoint;
- assigned UTC TIMEPULSE epoch;
- whether the edge was used;
- fitted residual.

`sync.json` records the synchronization model and quality metrics, including at least:

- schema version;
- source capture identity;
- `sync_start`;
- ROI;
- LED threshold/method;
- detected and used edge counts;
- coarse absolute-offset estimate and spread;
- GNSS epoch range;
- model type and parameters;
- residual statistics;
- requested and actual output-start information;
- software/FFmpeg/PyAV versions needed for reproducibility.

## Timestamp semantics

The burned timestamp is an **estimated UTC frame time**.

Initial display:

```text
UTC(est.) 2026-09-25T01:23:45.27Z
```

Two fractional digits are a display resolution choice suitable for an approximately 15 fps source. They do not imply 10 ms accuracy.

Do not burn a `±X ms` uncertainty into the video until exposure timing, rolling-shutter/readout effects, LED thresholding, and other systematic offsets have been characterized. Regression residual alone is not an absolute timestamp uncertainty.

## Rendering

After a valid synchronization model is available:

1. choose the first real frame at or after `--output-start`;
2. map each retained source PTS through the synchronization model;
3. rebase synchronized output PTS so the first output frame is at playback time zero;
4. burn the estimated UTC text into a lens-external region;
5. encode one shareable MP4;
6. validate the complete result.

Do not duplicate or drop frames to force constant frame rate.

Initial output format:

- H.264/AVC via `libx264`;
- MP4;
- `yuv420p` for broad playback compatibility;
- CRF 12;
- `preset slow`;
- passthrough/VFR frame cadence;
- `+faststart`;
- a sufficiently fine video track timescale.

The original MJPEG Matroska segments remain the analysis masters.

## Rendering validation

A synchronization run is successful only if validation passes, including:

- source and decoded frame identities/counts are internally consistent;
- output frame count equals retained source-frame count;
- output PTS are strictly increasing;
- first output PTS is zero;
- output duration agrees with synchronized UTC span within muxing time-base resolution;
- generated MP4 decodes to completion;
- representative first/middle/last timestamps agree with `sync.json`.

A failed render must not overwrite prior successful processing runs.
