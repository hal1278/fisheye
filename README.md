# fisheye

Fisheye-camera acquisition and GNSS time synchronization workflow.

The user-facing workflow is:

```text
fisheye record
fisheye roi
fisheye sync
fisheye export
```

`record` acquires camera/GNSS data, `roi` selects the fixed TIMEPULSE LED region, `sync` estimates the video-PTS-to-UTC mapping, and `export` generates a synchronized shareable video. Optional full-decode verification of a completed export is available separately through `fisheye verify`.

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
uv run fisheye sync recordings/<session>/capture --roi X,Y,W,H
```

## Export

```text
uv run fisheye export recordings/<session>/processing/<sync-run> --start HH:MM:SS
```

Normal export performs lightweight validation and writes `video.mp4`, detailed `timestamps.csv`, simplified `timestamps_simple.csv`, `export.json`, and `ffmpeg.log`.

For explicit full-decode verification:

```text
uv run fisheye verify recordings/<session>/processing/<sync-run>/export/<export-run>
```

See `docs/design.md` and the stage-specific documents under `docs/` for the current design.
