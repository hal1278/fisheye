# Fisheye workflow design

Status: design decisions as of 2026-09-28.

## Scope and terminology

The project covers the acquisition and offline synchronization of fisheye-camera video with GNSS time.

The collective term used in this documentation is **acquisition and synchronization workflow**. "Workflow" is preferred to "pipeline" because ROI selection is an interactive step rather than only a data transformation.

The workflow has three top-level stages:

1. **Logging** — acquire the camera video, GNSS timing data, coarse host-clock anchors, metadata, and diagnostics.
2. **ROI Selection** — interactively select the fixed image region containing the TIMEPULSE LED/fiber spot.
3. **Synchronization** — detect LED rising edges, match them to GNSS TIMEPULSE epochs, estimate the video-PTS-to-UTC mapping, validate it, and generate the shareable synchronized video.

The corresponding CLI is a single `fisheye` command with subcommands:

```text
fisheye record
fisheye roi
fisheye sync
```

There are no compatibility aliases for separate executables.

Detailed stage designs:

- [Logging](logging.md)
- [ROI Selection](roi-selection.md)
- [Synchronization](synchronization.md)

## Project and repository naming

The project name used by the CLI and documentation is **Fisheye**.

The repository has grown beyond logging, so `fisheye-logging` no longer describes its full scope. The intended repository name is `fisheye` if that name is available and unambiguous for the owner. The current repository may retain its existing name until the rename is performed; source paths and documentation should not depend on the remote repository name.

The Python distribution/package name is an implementation detail and need not be identical to the repository name. It may be changed when the CLI/package refactor is implemented.

## Repository structure

Keep a single repository and a single Python distribution. Separate reusable implementation by responsibility rather than by executable:

```text
src/
└── fisheye/
    ├── cli.py
    ├── capture/
    │   ├── camera.py
    │   ├── config.py
    │   ├── gnss.py
    │   └── session.py
    ├── media/
    │   ├── timeline.py
    │   └── decode.py
    ├── roi/
    │   └── gui.py
    └── sync/
        ├── led.py
        ├── epochs.py
        ├── model.py
        └── export.py
```

The exact module split may change as implementation proceeds. The stable architectural boundary is the three workflow stages, not each Python file.

`media/` owns common media primitives used by ROI selection and synchronization, including segment discovery, source PTS handling, virtual frame indexing, seeking, and decoding.

## Session layout

A recording session separates immutable acquisition data from derived processing output:

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
            ├── process.log
            └── video.mp4
```

`capture/` is the primary acquisition record and is read-only to ROI and synchronization code.

ROI Selection does not create a processing run directory. It reads a selected `capture/` directory and copies a command-line ROI value to the clipboard.

A `processing/<processing-time>/` directory is created by `fisheye sync`, after command-line parsing and basic capture-input validation have succeeded but before expensive media analysis begins. This avoids empty runs for an invalid path or invalid arguments while still preserving logs and partial artifacts if analysis, model fitting, or rendering later fails.

The processing directory name is the synchronization-run start time. Existing directories are errors rather than overwrite targets.

## Synchronization-stage phases

Synchronization is one top-level workflow stage and one user-facing subcommand, but it contains two internal phases:

1. **Analysis**
   - build and validate the source video timeline;
   - detect LED edges;
   - resolve GNSS TIMEPULSE epochs;
   - estimate and validate the PTS-to-UTC model;
   - write `led_edges.csv` and `sync.json`.
2. **Rendering**
   - select the requested output range;
   - map source PTS to synchronized output PTS;
   - burn estimated UTC into each frame;
   - encode one shareable video;
   - validate the resulting media.

The phases are separate modules and leave an explicit intermediate synchronization model, but they are not separate top-level commands in the initial interface. This permits analysis to be inspected and reused without expanding the user-facing workflow prematurely.

## Media and Python dependencies

FFmpeg remains an external executable for camera capture and final rendering/encoding.

For offline media access, use **PyAV** rather than starting a new FFmpeg process for each GUI seek. PyAV provides in-process access to FFmpeg demuxing, seeking, decoding, frame PTS, and stream time bases.

Use:

- `pyserial` and `pyubx2` for GNSS logging;
- `av` (PyAV) for offline video indexing/seeking/decoding;
- `numpy` for LED score extraction and synchronization calculations;
- `Pillow` for GUI image scaling and Tk image conversion;
- `tkinter` / `ttk` for the ROI Selection GUI.

`tkinter` is supplied by the Python/runtime environment rather than as a normal PyPI dependency.

Do not add OpenCV, SciPy, or a Qt binding unless a concrete requirement appears that the selected stack cannot satisfy. The current operations are simple enough that the additional dependency and packaging cost is not justified.

## General timing policy

Video presentation timestamps stored in the Matroska files are the primary camera-side time axis. Frame number divided by nominal frame rate is not a time source.

The synchronization target is the physical 1 Hz rising TIMEPULSE visible in the video. `camera.csv` and `gnss.csv.host_monotonic_ns` are coarse association aids and must not be treated as fine timing measurements.

The workflow must not manufacture a constant-frame-rate timeline by duplicating or dropping frames merely to match the configured nominal frame rate.
