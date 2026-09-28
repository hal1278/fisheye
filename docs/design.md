# Fisheye workflow design

Status: agreed design as of 2026-09-28.

## Scope and terminology

The project is named **Fisheye**.

The collective term used in this documentation is **Fisheye workflow**. The workflow covers acquisition, interactive ROI selection, synchronization analysis, and synchronized-video export. "Workflow" is preferred to "pipeline" because ROI selection is an interactive stage rather than only a data transformation.

The workflow has four user-visible stages:

1. **Record** — acquire camera video, GNSS timing data, coarse host-clock anchors, metadata, and diagnostics.
2. **ROI** — interactively select the fixed image region containing the TIMEPULSE LED/fiber spot.
3. **Sync** — detect LED rising edges, associate them with GNSS TIMEPULSE epochs, estimate the source-PTS-to-UTC mapping, and validate the synchronization solution.
4. **Export** — use a validated synchronization solution to cut, retime, timestamp, encode, and validate the shareable output video.

The corresponding CLI is a single command with subcommands:

```text
fisheye record
fisheye roi
fisheye sync
fisheye export
```

No compatibility aliases are provided.

Detailed stage documents:

- [Record](record.md)
- [ROI](roi.md)
- [Sync](sync.md)
- [Export](export.md)

## Repository, distribution, and package naming

The repository, Python project/distribution, import package, and CLI command are all named `fisheye`.

The source tree is organized as:

```text
src/
└── fisheye/
    ├── cli.py
    ├── record/
    ├── media/
    ├── roi/
    ├── sync/
    └── export/
```

The exact internal module split may change as implementation proceeds. The stable architectural boundary is the four workflow stages and their artifact interfaces, not each Python file.

## Repository structure

Keep one repository and one Python distribution.

Current implementation follows this structure:

```text
src/
└── fisheye/
    ├── cli.py
    ├── timeutil.py
    ├── record/
    │   ├── camera.py
    │   ├── command.py
    │   ├── config.py
    │   ├── gnss.py
    │   └── session.py
    ├── media/
    │   ├── timeline.py
    │   └── decode.py
    ├── roi/
    │   ├── gui.py
    │   └── state.py
    ├── sync/
    │   ├── command.py
    │   ├── led.py
    │   └── model.py
    └── export/
        └── command.py
```

`media/` owns common media primitives used by ROI, Sync, and Export, including segment discovery, source PTS handling, virtual frame indexing, seeking, and decoding.

## Session layout and artifact ownership

A session separates immutable acquisition data from derived processing data:

```text
recordings/
└── YYYYMMDD_HHMMSS/
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
        └── YYYYMMDD_HHMMSS/
            ├── led_edges.csv
            ├── sync.json
            ├── sync.log
            └── export/
                └── YYYYMMDD_HHMMSS/
                    ├── video.mp4
                    ├── export.json
                    └── ffmpeg.log
```

`capture/` is the primary acquisition record and is read-only to later stages.

ROI does not create derived files or a processing directory. It reads a selected `capture/` directory and copies `--roi X,Y,W,H` to the clipboard.

Sync creates `processing/<sync-start-time>/` after command-line parsing and basic capture-input validation have succeeded but before expensive media analysis begins. That directory represents one synchronization solution.

Export consumes a synchronization directory and creates `export/<export-start-time>/` inside it. This allows one synchronization solution to be rendered multiple times with different output ranges or encoding/display settings without rerunning LED detection and model fitting.

Existing run directories are errors rather than overwrite targets.

## Stage interfaces

The principal interfaces are:

```text
Record
  output: capture/

ROI
  input:  capture/
  output: --roi X,Y,W,H copied to clipboard

Sync
  input:  capture/, --roi, --sync-start
  output: processing/<time>/led_edges.csv
          processing/<time>/sync.json

Export
  input:  capture/ referenced by sync.json
          processing/<time>/sync.json
          output-range/encoding options
  output: processing/<time>/export/<time>/video.mp4
          processing/<time>/export/<time>/export.json
```

This is a real artifact boundary: Export does not rerun LED detection or GNSS epoch association.

## Dependencies

FFmpeg remains an external executable for live camera capture, high-throughput ROI extraction during Sync, and final rendering/encoding.

Use:

- `pyserial` and `pyubx2` for GNSS recording;
- `av` (PyAV) for offline demuxing, seeking, frame PTS/time-base access, ROI frame display, and output validation;
- `numpy` for LED image statistics and synchronization calculations;
- `Pillow` for ROI GUI image scaling and Tk image conversion;
- `tkinter` / `ttk` for the ROI GUI.

For Sync, FFmpeg crops the small ROI and streams RGB frames to Python. This avoids converting complete 960x720 frames to NumPy arrays solely to inspect a small LED region.

`tkinter` is provided by the Python/runtime environment rather than as a normal PyPI dependency.

Do not add OpenCV, SciPy, or a Qt binding without a demonstrated need. The current operations do not justify those dependencies.

## General timing policy

Video presentation timestamps stored in the Matroska files are the primary camera-side time axis. Frame number divided by nominal frame rate is not a time source.

The synchronization target is the physical 1 Hz rising TIMEPULSE visible in the video. `camera.csv` and `gnss.csv.host_monotonic_ns` are coarse association aids and are not fine timing measurements.

The workflow must not manufacture a constant-frame-rate timeline by duplicating or dropping frames merely to match the configured nominal frame rate.
