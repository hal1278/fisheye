# Sync stage

## Purpose

Sync analyzes a capture and selected LED ROI to produce a validated mapping from source video PTS to UTC.

User-facing command:

```text
fisheye sync CAPTURE --roi X,Y,W,H [--sync-start HH:MM:SS] [--workers N]
```

Sync does not generate the final shareable video. Export is a separate stage that consumes the synchronization solution.

## Required inputs

Normal processing uses:

- `video_*.mkv` — source frames and PTS;
- `gnss.csv` — normalized TIMEPULSE UTC epochs and lock status;
- `camera.csv` — coarse video/GNSS association;
- `session.json` — session and TIMEPULSE configuration validation;
- `--roi` — fixed LED ROI selected by the ROI stage;
- `--sync-start` — optional beginning of the usable interval after LED/fiber position adjustment; default `0`.

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
led_scores.csv
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

`--sync-start` marks the beginning of the interval in which the LED/fiber position is considered fixed and usable for automatic LED detection. It defaults to `0`; when the ROI is fixed from the first frame, the normal invocation does not specify it.

Frames before `--sync-start` are not used to estimate LED edges or the synchronization model.

There is no output-video cut option in Sync; that belongs to Export.

## LED measurement

Build the source packet/PTS index with the shared media layer. For LED extraction, run independent FFmpeg decoders for source segments in parallel, decode the MJPEG stream, crop the fixed ROI before RGB conversion, set a microsecond filter time base, and carry the ROI frames through a NUT pipe as uncompressed RGB with their PTS. PyAV demuxes that timestamped ROI stream. Each decoded ROI frame is matched back to the indexed source PTS rather than being associated by ordinal frame number. A malformed MJPEG packet may therefore be recorded as an undecodable source-frame index without shifting the PTS association of every subsequent LED sample.

By default Sync uses up to four segment workers, capped by the number of usable segments and available CPUs. `--workers N` overrides that count. Each FFmpeg process is limited to one decoder thread so that process-level segment parallelism does not multiply FFmpeg's internal thread count uncontrollably. Segment results are merged deterministically by source order before LED edge detection.

The NUT layer is used only to preserve timestamps across the pipe; it does not add image compression. For a 32 x 31 RGB ROI the payload is only 2976 bytes per decoded frame, so the extra pipe/mux/demux overhead is small compared with MJPEG decoding, while avoiding transfer or RGB conversion of the full 960 x 720 frame in Python.

For each valid pair of consecutive decoded frames, compute a luma difference in the configured TIMEPULSE edge direction. Subtract the spatial median of that difference to suppress ROI-wide exposure changes, keep only the positive local change, and form a scalar rough score from the strongest few percent of ROI pixels.

The detector is capture-adaptive. It does not hard-code LED color, absolute brightness, pixel location inside the ROI, or an absolute threshold. A robust median/MAD threshold produces deliberately permissive edge candidates. Candidate events are then selected using the configured TIMEPULSE period. High-confidence periodic events learn a coarse spatial template inside the user-selected ROI; that template is used to re-score candidates and select the final periodic edge sequence.

The template is learned independently for each capture. The ROI is therefore a coarse search region, not a manually specified LED shape.

Transitions across missing/undecodable frames or abnormally large PTS gaps are invalid and cannot define an edge bracket.

Configured TIMEPULSE properties provide sanity checks. For the current system:

- frequency: 1 Hz;
- reference edge: rising;
- duty cycle: approximately 0.1;
- time grid: UTC.

Do not require an exact count of ON frames because the nominal frame interval is comparable to the pulse width.

## Reference-edge observations

Represent each detected configured reference edge by the interval between:

- the last OFF frame PTS;
- the first ON frame PTS.

Store both bounds.

For fitting, use the midpoint as the initial representative observation:

```text
P_edge = (P_off + P_on) / 2
```

This retains the finite-frame observation interval rather than pretending the pulse is known exactly at the first ON frame.

The configured polarity determines whether positive or negative local luma change is used. Duty cycle is a sanity-check parameter rather than the primary detector.

## Pulse sequence

The periodic candidate sequence determines logical pulse indices. False candidates are discarded and missing visible pulses are represented by skipped pulse indices.

The configured `frequency_hz` in `session.json` provides the expected period. The current UTC epoch-association implementation supports the recorded 1 Hz configuration; the detector itself uses the configured period rather than a hard-coded image-specific value.

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


## `led_scores.csv`

`led_scores.csv` is written immediately after ROI extraction, before periodic detection. It contains one row per decoded frame in the synchronization interval:

- source frame index and PTS;
- whether the frame-to-frame transition is valid;
- adaptive local-change rough score;
- learned-template similarity when detection succeeds;
- candidate/final-selection flags.

The first version of the file is therefore still available if later candidate selection or model fitting fails, avoiding repeated video decoding while tuning or diagnosing the detector.
