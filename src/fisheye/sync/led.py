from __future__ import annotations

import bisect
from dataclasses import dataclass
import subprocess
import tempfile
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from ..media.timeline import FrameRef, SegmentInfo, VirtualVideo


class LedError(RuntimeError):
    pass


@dataclass(slots=True)
class LedSample:
    frame_index: int
    pts_seconds: float
    score: float


@dataclass(slots=True)
class LedEdge:
    frame_off: int
    frame_on: int
    pts_off: float
    pts_on: float
    pts_mid: float
    on_frames: int
    utc_epoch: int | None = None
    used: bool = True
    residual_ms: float | None = None


@dataclass(slots=True)
class LedExtraction:
    samples: list[LedSample]
    skipped_frame_indices: list[int]
    decoder_messages: list[tuple[str, str]]


def _reference_by_avtime(
    frame_refs: tuple["FrameRef", ...] | list["FrameRef"],
) -> tuple[list[int], list["FrameRef"]]:
    keys: list[int] = []
    refs: list["FrameRef"] = []
    for ref in frame_refs:
        key = round(ref.time * 1_000_000)
        if keys and key <= keys[-1]:
            raise LedError("source frame timestamps collide at AVTB resolution")
        keys.append(key)
        refs.append(ref)
    return keys, refs


def _match_reference(
    pts_us: int,
    keys: list[int],
    refs: list["FrameRef"],
) -> "FrameRef":
    position = bisect.bisect_left(keys, pts_us)
    candidates: list[int] = []
    if position < len(keys):
        candidates.append(position)
    if position:
        candidates.append(position - 1)
    if not candidates:
        raise LedError(f"decoded ROI frame PTS {pts_us} us has no source-frame match")
    best = min(candidates, key=lambda index: abs(keys[index] - pts_us))
    if abs(keys[best] - pts_us) > 1:
        raise LedError(
            f"decoded ROI frame PTS {pts_us} us does not match source timeline "
            f"(nearest {keys[best]} us)"
        )
    return refs[best]


def _rgb_view(frame) -> np.ndarray:
    if frame.format.name != "rgb24" or len(frame.planes) != 1:
        raise LedError(f"unexpected ROI pixel format: {frame.format.name}")
    plane = frame.planes[0]
    rows = np.frombuffer(
        plane,
        dtype=np.uint8,
        count=plane.line_size * plane.height,
    ).reshape(plane.height, plane.line_size)
    return rows[:, : frame.width * 3].reshape(frame.height, frame.width, 3)


def _segment_scores(
    ffmpeg: str,
    segment: "SegmentInfo",
    roi: tuple[int, int, int, int],
    frame_refs: tuple["FrameRef", ...] | list["FrameRef"],
) -> tuple[list[LedSample], list[int], str]:
    import av

    x, y, width, height = roi
    cmd = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "warning",
        "-copyts",
        "-i",
        str(segment.path),
        "-map",
        "0:v:0",
        "-vf",
        f"crop={width}:{height}:{x}:{y},format=rgb24,settb=AVTB",
        "-c:v",
        "rawvideo",
        "-enc_time_base",
        "filter",
        "-fps_mode",
        "passthrough",
        "-f",
        "nut",
        "pipe:1",
    ]

    keys, refs = _reference_by_avtime(frame_refs)
    seen: set[int] = set()
    samples: list[LedSample] = []

    with tempfile.TemporaryFile() as stderr_file:
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=stderr_file,
        )
        assert process.stdout is not None
        failure: BaseException | None = None
        try:
            try:
                with av.open(process.stdout, mode="r", format="nut") as container:
                    if not container.streams.video:
                        raise LedError(
                            f"NUT ROI stream has no video stream for {segment.path.name}"
                        )
                    for frame in container.decode(video=0):
                        if frame.pts is None or frame.time_base is None:
                            raise LedError(
                                f"decoded ROI frame has no timestamp in {segment.path.name}"
                            )
                        pts_us = round(frame.pts * frame.time_base * 1_000_000)
                        ref = _match_reference(pts_us, keys, refs)
                        if ref.index in seen:
                            raise LedError(
                                f"duplicate decoded ROI timestamp at source frame {ref.index}"
                            )
                        seen.add(ref.index)

                        rgb = _rgb_view(frame)
                        values = (
                            rgb[..., 0].astype(np.float32)
                            - (
                                rgb[..., 1].astype(np.float32)
                                + rgb[..., 2].astype(np.float32)
                            )
                            * 0.5
                        ).reshape(-1)
                        top_count = max(1, values.size // 20)
                        top = np.partition(values, values.size - top_count)[-top_count:]
                        samples.append(
                            LedSample(
                                frame_index=ref.index,
                                pts_seconds=float(ref.time),
                                score=float(top.mean()),
                            )
                        )
            except BaseException as exc:
                failure = exc
            finally:
                process.stdout.close()

            returncode = process.wait()
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            stderr_file.seek(0)
            stderr = stderr_file.read().decode("utf-8", errors="replace").strip()

    if failure is not None:
        if isinstance(failure, LedError):
            raise failure
        raise LedError(
            f"failed to read timestamped ROI stream from {segment.path.name}: {failure}; "
            f"{stderr}"
        ) from failure
    if returncode != 0:
        raise LedError(
            f"FFmpeg ROI decode failed for {segment.path.name} with status {returncode}: "
            f"{stderr}"
        )

    missing = [ref.index for ref in refs if ref.index not in seen]
    return samples, missing, stderr


def extract_scores(
    video: "VirtualVideo",
    ffmpeg: str,
    roi: tuple[int, int, int, int],
    sync_start: float,
) -> LedExtraction:
    x, y, width, height = roi
    if x + width > video.width or y + height > video.height:
        raise LedError(
            f"ROI {x},{y},{width},{height} exceeds {video.width}x{video.height} frame"
        )

    first_time = video.first_time
    samples: list[LedSample] = []
    skipped: list[int] = []
    decoder_messages: list[tuple[str, str]] = []

    for segment in video.segments:
        refs = video.frames[
            segment.first_frame : segment.first_frame + segment.frame_count
        ]
        if float(refs[-1].time - first_time) < sync_start:
            continue

        segment_samples, segment_missing, stderr = _segment_scores(
            ffmpeg, segment, roi, refs
        )
        if stderr:
            decoder_messages.append((segment.path.name, stderr))

        for sample in segment_samples:
            if sample.pts_seconds - float(first_time) >= sync_start:
                samples.append(sample)
        for frame_index in segment_missing:
            if float(video.elapsed(frame_index)) >= sync_start:
                skipped.append(frame_index)

    if not samples:
        raise LedError("no decodable frames remain after --sync-start")
    return LedExtraction(samples, skipped, decoder_messages)


def otsu_threshold(scores: np.ndarray) -> float:
    if scores.size < 2:
        raise LedError("not enough LED samples")
    low = float(scores.min())
    high = float(scores.max())
    if not high > low:
        raise LedError("LED score is constant; ON/OFF states cannot be separated")
    hist, edges = np.histogram(scores, bins=256, range=(low, high))
    centers = (edges[:-1] + edges[1:]) * 0.5
    total = hist.sum()
    cumulative_weight = np.cumsum(hist)
    cumulative_mean = np.cumsum(hist * centers)
    global_mean = cumulative_mean[-1]
    left = cumulative_weight[:-1]
    right = total - left
    valid = (left > 0) & (right > 0)
    between = np.full(255, -1.0, dtype=np.float64)
    numerator = (global_mean * left - cumulative_mean[:-1] * total) ** 2
    between[valid] = numerator[valid] / (left[valid] * right[valid])
    index = int(np.argmax(between))
    return float(edges[index + 1])


def detect_rising_edges(samples: list[LedSample]) -> tuple[float, float, list[LedEdge]]:
    values = np.asarray([sample.score for sample in samples], dtype=np.float64)
    threshold = otsu_threshold(values)
    on = values > threshold
    on_fraction = float(on.mean())
    if not 0.005 <= on_fraction <= 0.5:
        raise LedError(
            f"implausible LED ON fraction {on_fraction:.3f}; ROI or threshold is likely invalid"
        )

    edges: list[LedEdge] = []
    i = 0
    while i < len(samples):
        if not on[i]:
            i += 1
            continue
        start = i
        while i + 1 < len(samples) and on[i + 1]:
            i += 1
        end = i
        if start > 0:
            off = samples[start - 1]
            first_on = samples[start]
            edges.append(
                LedEdge(
                    frame_off=off.frame_index,
                    frame_on=first_on.frame_index,
                    pts_off=off.pts_seconds,
                    pts_on=first_on.pts_seconds,
                    pts_mid=(off.pts_seconds + first_on.pts_seconds) * 0.5,
                    on_frames=end - start + 1,
                )
            )
        i += 1

    if len(edges) < 10:
        raise LedError(f"too few LED rising edges detected: {len(edges)}")
    return threshold, on_fraction, edges
