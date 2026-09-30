from __future__ import annotations

import csv
from datetime import datetime, timezone
import json
from pathlib import Path

import av


class VerifyError(RuntimeError):
    pass


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise VerifyError(f"cannot read {path.name}: {exc}") from exc


def _write_json(path: Path, data: dict) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    temp.replace(path)


def _full_decode(
    video_path: Path,
    timestamps_path: Path,
    expected_frames: int,
    expected_last_pts: float,
) -> dict:
    tolerance = max(2 / 90000, 0.002)
    count = 0
    first_pts: float | None = None
    last_pts: float | None = None
    previous: float | None = None
    max_timestamp_error = 0.0

    try:
        timestamp_stream = timestamps_path.open(
            "r",
            encoding="utf-8",
            newline="",
        )
    except OSError as exc:
        raise VerifyError(f"cannot read timestamps.csv: {exc}") from exc

    with timestamp_stream:
        rows = csv.DictReader(timestamp_stream)
        try:
            with av.open(str(video_path), mode="r") as container:
                if len(container.streams.video) != 1:
                    raise VerifyError(
                        f"output has {len(container.streams.video)} video streams; expected 1"
                    )
                for frame in container.decode(video=0):
                    if frame.pts is None or frame.time_base is None:
                        raise VerifyError("output frame has no usable PTS")
                    current = float(frame.pts * frame.time_base)
                    if previous is not None and current <= previous:
                        raise VerifyError("output PTS are not strictly increasing")
                    try:
                        row = next(rows)
                    except StopIteration as exc:
                        raise VerifyError(
                            "video contains more frames than timestamps.csv"
                        ) from exc

                    try:
                        row_index = int(row["output_frame_index"])
                        expected_pts = float(row["output_pts_s"])
                    except (KeyError, TypeError, ValueError) as exc:
                        raise VerifyError(
                            "timestamps.csv contains an invalid output frame row"
                        ) from exc
                    if row_index != count:
                        raise VerifyError(
                            f"timestamps.csv frame index {row_index} != expected {count}"
                        )

                    timestamp_error = abs(current - expected_pts)
                    max_timestamp_error = max(max_timestamp_error, timestamp_error)
                    if timestamp_error > tolerance:
                        raise VerifyError(
                            f"frame {count} PTS {current:.9f} differs from "
                            f"timestamps.csv {expected_pts:.9f} s"
                        )

                    if first_pts is None:
                        first_pts = current
                    previous = current
                    last_pts = current
                    count += 1
        except av.error.FFmpegError as exc:
            raise VerifyError(f"cannot fully decode video.mp4: {exc}") from exc

        try:
            extra_row = next(rows)
        except StopIteration:
            extra_row = None
        if extra_row is not None:
            raise VerifyError("timestamps.csv contains more rows than video frames")

    if count != expected_frames:
        raise VerifyError(f"decoded frame count {count} != expected {expected_frames}")
    if first_pts is None or last_pts is None:
        raise VerifyError("output contains no video frames")
    if abs(first_pts) > 1 / 90000 + 1e-9:
        raise VerifyError(f"first output PTS is not zero: {first_pts:.9f} s")
    if abs(last_pts - expected_last_pts) > tolerance:
        raise VerifyError(
            f"last output PTS {last_pts:.6f} differs from expected "
            f"{expected_last_pts:.6f} s"
        )

    return {
        "decoded_frame_count": count,
        "pts_strictly_increasing": True,
        "first_pts_s": first_pts,
        "last_pts_s": last_pts,
        "max_timestamp_csv_pts_error_s": max_timestamp_error,
    }


def run(export: Path) -> Path:
    export = export.resolve()
    export_json_path = export / "export.json"
    video_path = export / "video.mp4"
    timestamps_path = export / "timestamps.csv"

    if not export_json_path.is_file():
        raise VerifyError(f"export.json not found in: {export}")
    if not video_path.is_file():
        raise VerifyError(f"video.mp4 not found in: {export}")
    if not timestamps_path.is_file():
        raise VerifyError(f"timestamps.csv not found in: {export}")

    metadata = _read_json(export_json_path)
    if metadata.get("status") != "completed":
        raise VerifyError("export.json does not describe a completed export")

    try:
        expected_frames = int(metadata["expected_frame_count"])
        expected_last_pts = float(metadata["expected_last_pts_s"])
    except (KeyError, TypeError, ValueError) as exc:
        raise VerifyError(
            "export.json is missing full-verification metadata; regenerate the export"
        ) from exc

    started = datetime.now(timezone.utc)
    result = _full_decode(
        video_path,
        timestamps_path,
        expected_frames,
        expected_last_pts,
    )
    completed = datetime.now(timezone.utc)

    verification = {
        "schema_version": 1,
        "status": "passed",
        "method": "full_decode",
        "export": ".",
        "started_utc": started.isoformat().replace("+00:00", "Z"),
        "completed_utc": completed.isoformat().replace("+00:00", "Z"),
        "expected_frame_count": expected_frames,
        "expected_last_pts_s": expected_last_pts,
        **result,
    }
    output = export / "verification.json"
    _write_json(output, verification)
    return output
