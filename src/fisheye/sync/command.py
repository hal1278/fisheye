from __future__ import annotations

import csv
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path

import av
import numpy as np

from .. import __version__
from ..media.timeline import build_virtual_video
from ..timeutil import parse_elapsed, parse_roi
from .led import detect_reference_edges, extract_scores
from .model import assign_utc_epochs, fit_affine


LOGGER = logging.getLogger("fisheye.sync")


def _read_session(capture: Path) -> dict:
    try:
        return json.loads((capture / "session.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read session.json: {exc}") from exc


def _frame_size(session: dict) -> tuple[int, int]:
    try:
        width, height = session["configured"]["camera"]["video_size"].lower().split("x", 1)
        return int(width), int(height)
    except (KeyError, AttributeError, TypeError, ValueError) as exc:
        raise ValueError("session.json does not contain a valid configured camera video_size") from exc


def _ffmpeg(session: dict) -> str:
    try:
        return str(session["configured"]["camera"].get("ffmpeg", "ffmpeg"))
    except (KeyError, AttributeError):
        return "ffmpeg"


def _timepulse(session: dict) -> tuple[float, str]:
    try:
        config = session["configured"]["gnss"]["timepulse"]
        frequency_hz = float(config["frequency_hz"])
        polarity = str(config["polarity"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(
            "session.json does not contain a valid configured GNSS TIMEPULSE"
        ) from exc
    if frequency_hz <= 0:
        raise ValueError("configured TIMEPULSE frequency must be positive")
    if polarity not in {"rising", "falling"}:
        raise ValueError(
            "configured TIMEPULSE polarity must be 'rising' or 'falling'"
        )
    return frequency_hz, polarity


def _parse_utc(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def _coarse_offset(capture: Path) -> tuple[float, float, set[int], tuple[float, float]]:
    gnss_rows: list[tuple[float, float]] = []
    locked_epochs: set[int] = set()
    with (capture / "gnss.csv").open(newline="", encoding="utf-8") as stream:
        for row in csv.DictReader(stream):
            if row.get("tp_locked", "").lower() != "true" or not row.get("tp_utc"):
                continue
            utc = _parse_utc(row["tp_utc"])
            host = int(row["host_monotonic_ns"]) / 1e9
            gnss_rows.append((host, utc))
            locked_epochs.add(round(utc))
    if len(gnss_rows) < 10:
        raise RuntimeError("gnss.csv has too few locked UTC TIMEPULSE rows")

    gh = np.asarray([row[0] for row in gnss_rows], dtype=np.float64)
    gu = np.asarray([row[1] for row in gnss_rows], dtype=np.float64)
    h_ref = float(np.median(gh))
    u_ref = float(np.median(gu))
    matrix = np.column_stack((np.ones_like(gh), gh - h_ref))
    intercept, rate = np.linalg.lstsq(matrix, gu - u_ref, rcond=None)[0]

    offsets: list[float] = []
    with (capture / "camera.csv").open(newline="", encoding="utf-8") as stream:
        for row in csv.DictReader(stream):
            host = int(row["host_monotonic_ns"]) / 1e9
            video = int(row["video_time_ns"]) / 1e9
            utc_est = u_ref + float(intercept) + float(rate) * (host - h_ref)
            offsets.append(utc_est - video)
    if len(offsets) < 10:
        raise RuntimeError("camera.csv has too few coarse anchors")

    array = np.asarray(offsets, dtype=np.float64)
    initial = float(np.median(array))
    inlier = np.abs(array - initial) <= 0.4
    if int(inlier.sum()) < max(10, round(0.5 * len(array))):
        raise RuntimeError("camera.csv coarse anchors do not contain a stable majority")
    stable = array[inlier]
    median = float(np.median(stable))
    mad = float(np.median(np.abs(stable - median)))
    return median, mad, locked_epochs, (float(rate), float(intercept))


def _run_dir(capture: Path) -> Path:
    processing = capture.parent / "processing"
    processing.mkdir(exist_ok=True)
    run = processing / datetime.now().strftime("%Y%m%d_%H%M%S")
    run.mkdir(exist_ok=False)
    return run


def _configure_log(path: Path) -> None:
    LOGGER.handlers.clear()
    LOGGER.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    file_handler = logging.FileHandler(path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    LOGGER.addHandler(file_handler)
    stderr_handler = logging.StreamHandler()
    stderr_handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    LOGGER.addHandler(stderr_handler)


def _write_scores(path: Path, extraction, detection=None) -> None:
    candidates = (
        detection.candidate_sample_indices if detection is not None else set()
    )
    selected = (
        detection.selected_sample_indices if detection is not None else set()
    )
    similarity = (
        detection.template_similarity
        if detection is not None
        else np.full(len(extraction.samples), np.nan, dtype=np.float64)
    )
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(
            [
                "frame_index",
                "pts_s",
                "delta_valid",
                "rough_score",
                "template_similarity",
                "candidate",
                "selected",
            ]
        )
        for index, sample in enumerate(extraction.samples):
            writer.writerow(
                [
                    sample.frame_index,
                    f"{sample.pts_seconds:.9f}",
                    str(sample.delta_valid).lower(),
                    f"{sample.score:.6f}",
                    ""
                    if not np.isfinite(similarity[index])
                    else f"{similarity[index]:.6f}",
                    str(index in candidates).lower(),
                    str(index in selected).lower(),
                ]
            )


def _write_edges(path: Path, edges) -> None:
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(
            [
                "frame_off",
                "frame_on",
                "pts_off_s",
                "pts_on_s",
                "pts_mid_s",
                "on_frames",
                "logical_index",
                "tp_utc",
                "used",
                "residual_ms",
            ]
        )
        for edge in edges:
            utc = ""
            if edge.utc_epoch is not None:
                utc = datetime.fromtimestamp(edge.utc_epoch, timezone.utc).isoformat(
                    timespec="seconds"
                ).replace("+00:00", "Z")
            writer.writerow(
                [
                    edge.frame_off,
                    edge.frame_on,
                    f"{edge.pts_off:.9f}",
                    f"{edge.pts_on:.9f}",
                    f"{edge.pts_mid:.9f}",
                    edge.on_frames,
                    "" if edge.logical_index is None else edge.logical_index,
                    utc,
                    str(edge.used).lower(),
                    "" if edge.residual_ms is None else f"{edge.residual_ms:.6f}",
                ]
            )


def _write_json(path: Path, data: dict) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temp.replace(path)


def run(
    capture: Path,
    sync_start: str,
    roi: str,
    workers: int | None = None,
) -> Path:
    capture = capture.resolve()
    if not capture.is_dir():
        raise ValueError(f"capture directory not found: {capture}")
    required = ["session.json", "gnss.csv", "camera.csv"]
    missing = [name for name in required if not (capture / name).is_file()]
    if missing:
        raise ValueError(f"missing capture files: {', '.join(missing)}")

    start_seconds = parse_elapsed(sync_start)
    roi_value = parse_roi(roi)
    session = _read_session(capture)
    width, height = _frame_size(session)
    x, y, roi_width, roi_height = roi_value
    frequency_hz, polarity = _timepulse(session)
    if frequency_hz != 1.0:
        raise ValueError(
            "current GNSS UTC epoch association supports 1 Hz TIMEPULSE only"
        )
    expected_period = 1.0 / frequency_hz
    if x + roi_width > width or y + roi_height > height:
        raise ValueError(f"ROI {roi} exceeds configured {width}x{height} frame")

    run_dir = _run_dir(capture)
    _configure_log(run_dir / "sync.log")
    LOGGER.info("capture=%s", capture)
    LOGGER.info("sync_start=%.3f roi=%s", start_seconds, roi)

    try:
        video = build_virtual_video(capture)
        if (video.width, video.height) != (width, height):
            raise RuntimeError(
                f"media dimensions {video.width}x{video.height} do not match session.json {width}x{height}"
            )
        if start_seconds > float(video.duration):
            raise ValueError("--sync-start is beyond the end of the video")

        frame_times = np.asarray([float(frame.time) for frame in video.frames], dtype=np.float64)
        frame_interval = float(np.median(np.diff(frame_times)))
        LOGGER.info(
            "indexed %d frames in %d segments; median frame interval %.6f s",
            video.frame_count,
            len(video.segments),
            frame_interval,
        )

        extraction = extract_scores(
            video,
            _ffmpeg(session),
            roi_value,
            start_seconds,
            polarity=polarity,
            workers=workers,
        )
        LOGGER.info(
            "LED extraction workers=%d",
            extraction.workers,
        )
        samples = extraction.samples
        if extraction.skipped_frame_indices:
            LOGGER.warning(
                "%d indexed source frames could not be decoded in the synchronization interval",
                len(extraction.skipped_frame_indices),
            )
        for segment_name, message in extraction.decoder_messages:
            LOGGER.warning("decoder messages for %s:\n%s", segment_name, message)

        scores_path = run_dir / "led_scores.csv"
        _write_scores(scores_path, extraction)
        detection = detect_reference_edges(
            extraction,
            frame_interval,
            expected_period,
        )
        edges = detection.edges
        _write_scores(scores_path, extraction, detection)
        _write_edges(run_dir / "led_edges.csv", edges)
        LOGGER.info(
            "reference-edge candidates=%d selected=%d fitted_period=%.9f s "
            "rough_threshold=%.3f onset_threshold=%.3f template_similarity_floor=%.3f",
            detection.candidate_count,
            detection.selected_count,
            detection.fitted_period_s,
            detection.rough_threshold,
            detection.onset_threshold,
            detection.template_similarity_floor,
        )

        logical = [
            edge.logical_index
            for edge in edges
            if edge.logical_index is not None
        ]
        if len(logical) != len(edges):
            raise RuntimeError(
                "detected reference edges are missing logical pulse indices"
            )
        coarse_offset, coarse_mad, locked_epochs, host_model = _coarse_offset(capture)
        first_epoch, first_estimate = assign_utc_epochs(
            edges, logical, coarse_offset, coarse_mad, locked_epochs
        )
        model, metrics = fit_affine(edges, frame_interval)
        _write_edges(run_dir / "led_edges.csv", edges)

        source_capture = os.path.relpath(capture, run_dir)
        sync_json = {
            "schema_version": 1,
            "status": "completed",
            "software": {
                "fisheye_version": __version__,
                "pyav_version": av.__version__,
                "numpy_version": np.__version__,
            },
            "capture": source_capture,
            "source": {
                "frame_count": video.frame_count,
                "segment_count": len(video.segments),
                "width": video.width,
                "height": video.height,
                "codec": video.codec,
                "nominal_fps": video.nominal_fps,
                "median_frame_interval_s": frame_interval,
                "undecodable_frame_count": len(extraction.skipped_frame_indices),
                "undecodable_frame_indices": extraction.skipped_frame_indices,
            },
            "sync_start_s": start_seconds,
            "roi": {"x": x, "y": y, "width": roi_width, "height": roi_height},
            "led": {
                "method": (
                    "capture-adaptive local luma change; robust candidates; "
                    "periodic lattice; learned spatial template"
                ),
                "polarity": polarity,
                "configured_frequency_hz": frequency_hz,
                "rough_threshold": detection.rough_threshold,
                "onset_threshold": detection.onset_threshold,
                "template_similarity_floor": detection.template_similarity_floor,
                "fitted_period_s": detection.fitted_period_s,
                "candidate_events": detection.candidate_count,
                "detected_edges": detection.selected_count,
                "template_bins": detection.template_bins,
                "used_edges": int(metrics["used_edges"]),
            },
            "coarse": {
                "video_to_utc_offset_s": coarse_offset,
                "offset_mad_s": coarse_mad,
                "first_epoch_estimate": first_estimate,
                "first_epoch_utc": datetime.fromtimestamp(first_epoch, timezone.utc)
                .isoformat(timespec="seconds")
                .replace("+00:00", "Z"),
                "host_to_utc_rate": host_model[0],
            },
            "model": {
                "type": "affine",
                "pts_reference_s": model.pts_reference,
                "utc_reference_s": model.utc_reference,
                "offset_s": model.offset,
                "rate": model.rate,
            },
            "quality": metrics,
        }
        _write_json(run_dir / "sync.json", sync_json)
        if metrics["piecewise_recommended"]:
            LOGGER.warning(
                "binned residual drift exceeds half a median frame interval; inspect sync.json before export"
            )
        LOGGER.info("synchronization completed")
        return run_dir
    except BaseException:
        LOGGER.exception("synchronization failed")
        raise
