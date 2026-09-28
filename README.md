# fisheye

Fisheye-camera acquisition and GNSS time synchronization workflow.

The user-facing workflow is:

```text
fisheye record
fisheye roi
fisheye sync
fisheye export
```

`record` acquires camera/GNSS data, `roi` selects the fixed TIMEPULSE LED region, `sync` estimates the video-PTS-to-UTC mapping, and `export` generates a synchronized shareable video.

## Requirements

- Python 3.11+
- [uv](https://docs.astral.sh/uv/)
- FFmpeg available on `PATH`
- u-blox M8T accessible as a serial port for `record`
- Tk available in the Python runtime for `roi`

## Record

Copy `fisheye.example.toml` to `fisheye.toml`, edit the device identifiers, then run:

```text
uv run fisheye record
```

or:

```text
uv run fisheye record --config path/to/fisheye.toml
```

## ROI

```text
uv run fisheye roi
```

Select `recordings/<session>/capture/` in the directory chooser. The selected ROI is copied as `--roi X,Y,W,H`.

## Sync

```text
uv run fisheye sync recordings/<session>/capture --sync-start HH:MM:SS --roi X,Y,W,H
```

## Export

```text
uv run fisheye export recordings/<session>/processing/<sync-run> --start HH:MM:SS
```

See `docs/design.md` and the stage-specific documents under `docs/` for the current design.
