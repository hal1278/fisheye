from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess

import av

from .. import __version__
from ..media.timeline import build_virtual_video, discover_segments
from ..timeutil import parse_elapsed


class ExportError(RuntimeError):
    pass


def _undecodable_indices(source_meta: dict, frame_count: int) -> set[int]:
    raw = source_meta.get("undecodable_frame_indices")
    declared = source_meta.get("undecodable_frame_count", 0)

    if raw is None:
        try:
            declared_count = int(declared)
        except (TypeError, ValueError) as exc:
            raise ExportError(
                "sync.json has an invalid undecodable-frame count"
            ) from exc
        if declared_count:
            raise ExportError(
                "sync.json records undecodable source frames without their indices"
            )
        return set()

    if not isinstance(raw, list):
        raise ExportError(
            "sync.json undecodable_frame_indices must be a list"
        )

    indices: set[int] = set()
    for value in raw:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ExportError(
                "sync.json contains a non-integer undecodable frame index"
            )
        if not 0 <= value < frame_count:
            raise ExportError(
                f"sync.json undecodable frame index {value} is out of range"
            )
        if value in indices:
            raise ExportError(
                f"sync.json repeats undecodable frame index {value}"
            )
        indices.add(value)

    try:
        declared_count = int(declared)
    except (TypeError, ValueError) as exc:
        raise ExportError(
            "sync.json has an invalid undecodable-frame count"
        ) from exc
    if declared_count != len(indices):
        raise ExportError(
            "sync.json undecodable-frame count does not match its index list"
        )
    return indices


def _decodable_span(
    frame_count: int,
    requested_cut_index: int,
    undecodable: set[int],
) -> tuple[int, int, int]:
    cut_index = next(
        (
            index
            for index in range(requested_cut_index, frame_count)
            if index not in undecodable
        ),
        None,
    )
    if cut_index is None:
        raise ExportError(
            "no decodable source frame remains at or after --start"
        )

    last_index = next(
        index
        for index in range(frame_count - 1, cut_index - 1, -1)
        if index not in undecodable
    )
    missing_in_range = sum(
        cut_index <= index <= last_index for index in undecodable
    )
    expected_frames = last_index - cut_index + 1 - missing_in_range
    return cut_index, last_index, expected_frames


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ExportError(f"cannot read {path.name}: {exc}") from exc


def _write_json(path: Path, data: dict) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temp.replace(path)


def _run_dir(processing: Path) -> Path:
    parent = processing / "export"
    parent.mkdir(exist_ok=True)
    run = parent / datetime.now().strftime("%Y%m%d_%H%M%S")
    run.mkdir(exist_ok=False)
    return run


def _ffmpeg(capture: Path) -> str:
    session = _read_json(capture / "session.json")
    try:
        return str(session["configured"]["camera"].get("ffmpeg", "ffmpeg"))
    except (KeyError, AttributeError):
        return "ffmpeg"


def _ffmpeg_version(executable: str) -> str:
    result = subprocess.run(
        [executable, "-version"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )
    return result.stdout.splitlines()[0] if result.stdout else ""


def _model_utc(model: dict, pts_seconds: float) -> float:
    if model.get("type") != "affine":
        raise ExportError(f"unsupported synchronization model: {model.get('type')!r}")
    return (
        float(model["utc_reference_s"])
        + float(model["offset_s"])
        + float(model["rate"]) * (pts_seconds - float(model["pts_reference_s"]))
    )


def _utc_text(value: float) -> str:
    seconds = int(value)
    hundredths = int(round((value - seconds) * 100.0))
    if hundredths == 100:
        seconds += 1
        hundredths = 0
    base = datetime.fromtimestamp(seconds, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    return f"{base}.{hundredths:02d}Z"


def _filter_graph(segment_count: int, cut_pts_seconds: float, rate: float) -> str:
    setup = ";".join(
        f"[{index}:v]settb=AVTB[v{index}]" for index in range(segment_count)
    )
    inputs = "".join(f"[v{index}]" for index in range(segment_count))
    return (
        f"{setup};{inputs}interleave=nb_inputs={segment_count}:duration=longest,"
        f"trim=start={cut_pts_seconds:.9f},"
        f"setpts=(PTS-STARTPTS)*{rate:.15g},"
        "drawtext=textfile=.timestamp.txt:fontcolor=white:bordercolor=black:"
        "borderw=2:fix_bounds=1:x=16:y=h-text_h-16[vout]"
    )


def _validate_output(path: Path, expected_frames: int, expected_last_pts: float) -> dict:
    count = 0
    first_pts: float | None = None
    last_pts: float | None = None
    previous: float | None = None
    with av.open(str(path), mode="r") as container:
        if not container.streams.video:
            raise ExportError("output has no video stream")
        for frame in container.decode(video=0):
            if frame.pts is None or frame.time_base is None:
                raise ExportError("output frame has no usable PTS")
            current = float(frame.pts * frame.time_base)
            if previous is not None and current <= previous:
                raise ExportError("output PTS are not strictly increasing")
            if first_pts is None:
                first_pts = current
            previous = current
            last_pts = current
            count += 1
    if count != expected_frames:
        raise ExportError(f"output frame count {count} != expected {expected_frames}")
    if first_pts is None or last_pts is None:
        raise ExportError("output contains no video frames")
    if abs(first_pts) > 1 / 90000 + 1e-9:
        raise ExportError(f"first output PTS is not zero: {first_pts:.9f} s")
    tolerance = max(2 / 90000, 0.002)
    if abs(last_pts - expected_last_pts) > tolerance:
        raise ExportError(
            f"last output PTS {last_pts:.6f} differs from expected {expected_last_pts:.6f} s"
        )
    return {
        "frame_count": count,
        "first_pts_s": first_pts,
        "last_pts_s": last_pts,
    }


def run(processing: Path, start: str) -> Path:
    processing = processing.resolve()
    sync_path = processing / "sync.json"
    if not sync_path.is_file():
        raise ExportError(f"sync.json not found in: {processing}")
    sync = _read_json(sync_path)
    if sync.get("status") != "completed":
        raise ExportError("sync.json does not describe a completed synchronization run")
    start_seconds = parse_elapsed(start)

    try:
        capture = (processing / sync["capture"]).resolve()
        model = sync["model"]
    except (KeyError, TypeError) as exc:
        raise ExportError("sync.json is missing required fields") from exc
    if not capture.is_dir():
        raise ExportError(f"capture referenced by sync.json not found: {capture}")

    video = build_virtual_video(capture)
    source_meta = sync.get("source", {})
    if int(source_meta.get("frame_count", -1)) != video.frame_count:
        raise ExportError("capture frame count no longer matches sync.json")
    if start_seconds > float(video.duration):
        raise ExportError("--start is beyond the end of the video")

    requested_cut_index = next(
        index
        for index in range(video.frame_count)
        if float(video.elapsed(index)) >= start_seconds
    )
    undecodable = _undecodable_indices(source_meta, video.frame_count)
    cut_index, last_index, expected_frames = _decodable_span(
        video.frame_count,
        requested_cut_index,
        undecodable,
    )
    cut_frame = video.frames[cut_index]
    cut_pts = float(cut_frame.time)
    first_utc = _model_utc(model, cut_pts)
    last_pts = float(video.frames[last_index].time)
    last_utc = _model_utc(model, last_pts)
    expected_last_pts = last_utc - first_utc

    run_dir = _run_dir(processing)
    timestamp_file = run_dir / ".timestamp.txt"
    output = run_dir / "video.mp4"
    log_path = run_dir / "ffmpeg.log"
    ffmpeg = _ffmpeg(capture)
    segments = discover_segments(capture)

    utc_format = "%Y-%m-%dT%H\\:%M\\:%S.%2NZ"
    timestamp_file.write_text(
        f"UTC(est.) %{{pts:gmtime:{first_utc:.9f}:{utc_format}}}\n",
        encoding="utf-8",
    )

    command = [ffmpeg, "-hide_banner", "-loglevel", "info", "-copyts"]
    for segment in segments:
        command += ["-i", str(segment)]
    command += [
        "-filter_complex",
        _filter_graph(len(segments), cut_pts, float(model["rate"])),
        "-map",
        "[vout]",
        "-an",
        "-c:v",
        "libx264",
        "-crf",
        "12",
        "-preset",
        "slow",
        "-pix_fmt",
        "yuv420p",
        "-fps_mode",
        "passthrough",
        "-video_track_timescale",
        "90000",
        "-movflags",
        "+faststart",
        str(output),
    ]

    try:
        with log_path.open("w", encoding="utf-8", errors="replace") as log:
            result = subprocess.run(
                command,
                cwd=run_dir,
                stdout=subprocess.DEVNULL,
                stderr=log,
                check=False,
            )
        if result.returncode != 0:
            raise ExportError(f"FFmpeg export failed with status {result.returncode}")

        validation = _validate_output(output, expected_frames, expected_last_pts)
        export_json = {
            "schema_version": 1,
            "status": "completed",
            "software": {
                "fisheye_version": __version__,
                "ffmpeg": _ffmpeg_version(ffmpeg),
            },
            "sync": str(Path("..") / ".." / "sync.json"),
            "requested_start_s": start_seconds,
            "requested_start_frame_index": requested_cut_index,
            "actual_start": {
                "frame_index": cut_index,
                "source_pts_s": cut_pts,
                "source_elapsed_s": float(video.elapsed(cut_index)),
                "utc": _utc_text(first_utc),
                "utc_epoch_s": first_utc,
            },
            "last_source_frame_index": last_index,
            "last_utc": _utc_text(last_utc),
            "undecodable_source_frames_after_start": sum(
                index >= cut_index for index in undecodable
            ),
            "overlay": {
                "format": "UTC(est.) YYYY-MM-DDTHH:MM:SS.hhZ",
                "x": 16,
                "y": "h-text_h-16",
            },
            "encoding": {
                "codec": "libx264",
                "crf": 12,
                "preset": "slow",
                "pixel_format": "yuv420p",
                "video_track_timescale": 90000,
                "movflags": "+faststart",
            },
            "expected_frame_count": expected_frames,
            "validation": validation,
            "ffmpeg_command": command,
        }
        _write_json(run_dir / "export.json", export_json)
        return run_dir
    finally:
        try:
            timestamp_file.unlink()
        except FileNotFoundError:
            pass
