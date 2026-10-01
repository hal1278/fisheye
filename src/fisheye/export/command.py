from __future__ import annotations

from datetime import datetime, timezone
from fractions import Fraction
import csv
import json
from pathlib import Path
import subprocess

import av

from .. import __version__
from ..media.timeline import build_virtual_video
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


def _utc_text_precise(value: float) -> str:
    return datetime.fromtimestamp(value, timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )


def _timestamp_overlay_text(first_utc: float) -> str:
    # Shift by half a hundredth so both the whole-second and fractional
    # components round to the displayed 0.01 s resolution.
    rounded_origin = first_utc + 0.005
    utc_format = "%Y-%m-%dT%H\\:%M\\:%S"
    hundredths = (
        f"%{{eif:mod(floor((t+{rounded_origin:.9f})*100),100):d:2}}"
    )
    return (
        f"Estimated UTC: %{{pts:gmtime:{rounded_origin:.9f}:{utc_format}}}."
        f"{hundredths}Z\n"
    )


def _output_nominal_fps(nominal_fps: float, rate: float) -> Fraction:
    if nominal_fps <= 0 or rate <= 0:
        raise ExportError("nominal frame rate and synchronization rate must be positive")
    return (
        Fraction(str(nominal_fps)) / Fraction(str(rate))
    ).limit_denominator(1_000_000)


def _input_segments(video, cut_index: int, last_index: int):
    first_segment = video.frames[cut_index].segment_index
    last_segment = video.frames[last_index].segment_index
    return video.segments[first_segment : last_segment + 1]


def _input_seek_seconds(video, cut_index: int) -> float:
    segment = video.segment_for_frame(cut_index)
    if segment.codec != "mjpeg":
        return 0.0
    segment_first_pts = float(video.frames[segment.first_frame].time)
    cut_pts = float(video.frames[cut_index].time)
    # Keep a small preroll while avoiding decoding the long prefix of the
    # first retained MJPEG segment. trim=start still enforces the exact cut.
    return max(0.0, cut_pts - segment_first_pts - 1.0)


def _write_timestamp_csvs(
    detailed_path: Path,
    simple_path: Path,
    video,
    cut_index: int,
    last_index: int,
    undecodable: set[int],
    model: dict,
    first_utc: float,
) -> int:
    count = 0
    with (
        detailed_path.open("w", encoding="utf-8", newline="") as detailed_stream,
        simple_path.open("w", encoding="utf-8", newline="") as simple_stream,
    ):
        detailed = csv.writer(detailed_stream)
        simple = csv.writer(simple_stream)
        detailed.writerow(
            [
                "output_frame_index",
                "output_pts_s",
                "source_frame_index",
                "source_pts_s",
                "source_elapsed_s",
                "estimated_utc",
                "estimated_utc_epoch_s",
            ]
        )
        simple.writerow(["frame_index", "time_s", "estimated_utc"])

        for source_index in range(cut_index, last_index + 1):
            if source_index in undecodable:
                continue
            source_pts = float(video.frames[source_index].time)
            estimated_utc = _model_utc(model, source_pts)
            output_pts = estimated_utc - first_utc
            estimated_utc_text = _utc_text_precise(estimated_utc)

            detailed.writerow(
                [
                    count,
                    f"{output_pts:.9f}",
                    source_index,
                    f"{source_pts:.9f}",
                    f"{float(video.elapsed(source_index)):.9f}",
                    estimated_utc_text,
                    f"{estimated_utc:.6f}",
                ]
            )
            simple.writerow(
                [
                    count,
                    f"{output_pts:.9f}",
                    estimated_utc_text,
                ]
            )
            count += 1
    return count


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


def _parse_progress(text: str) -> dict[str, int | str]:
    block: dict[str, str] = {}
    final: dict[str, str] | None = None
    for raw_line in text.splitlines():
        if "=" not in raw_line:
            continue
        key, value = raw_line.split("=", 1)
        block[key] = value.strip()
        if key == "progress":
            if value.strip() == "end":
                final = dict(block)
            block.clear()

    if final is None:
        raise ExportError("FFmpeg progress did not report normal completion")

    try:
        frame_count = int(final["frame"])
        dup_frames = int(final.get("dup_frames", "0"))
        drop_frames = int(final.get("drop_frames", "0"))
    except (KeyError, ValueError) as exc:
        raise ExportError("FFmpeg progress is missing valid frame counters") from exc

    if dup_frames or drop_frames:
        raise ExportError(
            f"FFmpeg reported duplicated/dropped frames: dup={dup_frames}, drop={drop_frames}"
        )

    return {
        "frame_count": frame_count,
        "dup_frames": dup_frames,
        "drop_frames": drop_frames,
        "progress": "end",
    }


def _validate_output_light(
    path: Path,
    expected_frames: int,
    expected_last_pts: float,
    output_frame_interval: float,
    progress: dict[str, int | str],
) -> dict:
    if not path.is_file() or path.stat().st_size <= 0:
        raise ExportError("output video is missing or empty")

    progress_frames = int(progress["frame_count"])
    if progress_frames != expected_frames:
        raise ExportError(
            f"FFmpeg output frame count {progress_frames} != expected {expected_frames}"
        )

    try:
        with av.open(str(path), mode="r") as container:
            if len(container.streams.video) != 1:
                raise ExportError(
                    f"output has {len(container.streams.video)} video streams; expected 1"
                )
            stream = container.streams.video[0]
            if stream.start_time is None or stream.time_base is None:
                raise ExportError("output video stream has no usable start time")
            first_pts = float(stream.start_time * stream.time_base)
            stream_frames = int(stream.frames) if stream.frames else None
            duration = (
                float(stream.duration * stream.time_base)
                if stream.duration is not None
                else None
            )
    except av.error.FFmpegError as exc:
        raise ExportError(f"cannot open generated MP4: {exc}") from exc

    pts_tolerance = 1 / 90000 + 1e-9
    if abs(first_pts) > pts_tolerance:
        raise ExportError(f"first output PTS is not zero: {first_pts:.9f} s")

    if stream_frames is not None and stream_frames != expected_frames:
        raise ExportError(
            f"container frame count {stream_frames} != expected {expected_frames}"
        )

    if duration is not None:
        tail = duration - expected_last_pts
        if tail < -max(0.002, 2 / 90000):
            raise ExportError(
                f"output duration {duration:.6f} ends before expected last PTS "
                f"{expected_last_pts:.6f} s"
            )
        if tail > max(0.250, 2.0 * output_frame_interval):
            raise ExportError(
                f"output duration {duration:.6f} is inconsistent with expected "
                f"last PTS {expected_last_pts:.6f} s"
            )

    return {
        "mode": "lightweight",
        "ffmpeg_frame_count": progress_frames,
        "container_frame_count": stream_frames,
        "first_pts_s": first_pts,
        "duration_s": duration,
        "dup_frames": int(progress["dup_frames"]),
        "drop_frames": int(progress["drop_frames"]),
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
    rate = float(model["rate"])
    nominal_output_fps = _output_nominal_fps(video.nominal_fps, rate)

    run_dir = _run_dir(processing)
    timestamp_file = run_dir / ".timestamp.txt"
    timestamps_path = run_dir / "timestamps.csv"
    timestamps_temp = run_dir / ".timestamps.csv.tmp"
    timestamps_simple_path = run_dir / "timestamps_simple.csv"
    timestamps_simple_temp = run_dir / ".timestamps_simple.csv.tmp"
    output = run_dir / "video.mp4"
    log_path = run_dir / "ffmpeg.log"
    ffmpeg = _ffmpeg(capture)
    segments = _input_segments(video, cut_index, last_index)
    first_input_seek_s = _input_seek_seconds(video, cut_index)

    timestamp_rows = _write_timestamp_csvs(
        timestamps_temp,
        timestamps_simple_temp,
        video,
        cut_index,
        last_index,
        undecodable,
        model,
        first_utc,
    )
    if timestamp_rows != expected_frames:
        raise ExportError(
            f"timestamp row count {timestamp_rows} != expected {expected_frames}"
        )

    timestamp_file.write_text(
        _timestamp_overlay_text(first_utc),
        encoding="utf-8",
    )

    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "info",
        "-nostats",
        "-stats_period",
        "60",
        "-progress",
        "pipe:1",
        "-copyts",
    ]
    for input_index, segment in enumerate(segments):
        if input_index == 0 and first_input_seek_s > 0.0:
            command += ["-ss", f"{first_input_seek_s:.9f}"]
        command += ["-i", str(segment.path)]
    command += [
        "-filter_complex",
        _filter_graph(len(segments), cut_pts, rate),
        "-map",
        "[vout]",
        "-an",
        "-c:v",
        "libx264",
        "-enc_time_base:v",
        "1:90000",
        "-x264-params",
        f"fps={nominal_output_fps.numerator}/{nominal_output_fps.denominator}",
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
                stdout=subprocess.PIPE,
                stderr=log,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
        if result.returncode != 0:
            raise ExportError(f"FFmpeg export failed with status {result.returncode}")

        progress = _parse_progress(result.stdout)
        output_frame_interval = float(sync["source"]["median_frame_interval_s"]) * rate
        validation = _validate_output_light(
            output,
            expected_frames,
            expected_last_pts,
            output_frame_interval,
            progress,
        )
        validation["timestamp_rows"] = timestamp_rows
        timestamps_temp.replace(timestamps_path)
        timestamps_simple_temp.replace(timestamps_simple_path)
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
            "inputs": {
                "first_segment_index": segments[0].index,
                "last_segment_index": segments[-1].index,
                "segment_count": len(segments),
                "first_input_seek_s": first_input_seek_s,
            },
            "timestamps": {
                "path": "timestamps.csv",
                "simple_path": "timestamps_simple.csv",
                "row_count": timestamp_rows,
                "estimated_utc": True,
            },
            "overlay": {
                "format": "Estimated UTC: YYYY-MM-DDTHH:MM:SS.hhZ",
                "x": 16,
                "y": "h-text_h-16",
            },
            "encoding": {
                "codec": "libx264",
                "crf": 12,
                "preset": "slow",
                "pixel_format": "yuv420p",
                "encoder_time_base": "1/90000",
                "encoder_nominal_fps": (
                    f"{nominal_output_fps.numerator}/{nominal_output_fps.denominator}"
                ),
                "video_track_timescale": 90000,
                "movflags": "+faststart",
            },
            "expected_frame_count": expected_frames,
            "expected_last_pts_s": expected_last_pts,
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
        try:
            timestamps_temp.unlink()
        except FileNotFoundError:
            pass
        try:
            timestamps_simple_temp.unlink()
        except FileNotFoundError:
            pass
