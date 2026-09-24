# fisheye-logging

Fisheye-camera and u-blox M8T timing logger. The logger records primary camera/GNSS data and coarse host-clock anchors; LED detection and video/GNSS synchronization are intentionally offline post-processing tasks.

## Requirements

- Python 3.11+
- [uv](https://docs.astral.sh/uv/)
- FFmpeg available on `PATH`
- u-blox M8T accessible as a serial port

## Run

Copy `fisheye.example.toml` to `fisheye.toml`, edit the device identifiers, then run:

```text
uv run fisheye-logging
```

or:

```text
uv run fisheye-logging --config path/to/fisheye.toml
```

See `docs/design.md` for the logging design and file semantics.
