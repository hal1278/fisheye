from __future__ import annotations

from dataclasses import dataclass
import subprocess
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from ..media.timeline import SegmentInfo, VirtualVideo


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


def _read_exact(stream, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _segment_scores(
    ffmpeg: str,
    segment: "SegmentInfo",
    roi: tuple[int, int, int, int],
    frame_refs,
) -> list[LedSample]:
    x, y, width, height = roi
    frame_bytes = width * height * 3
    cmd = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(segment.path),
        "-map",
        "0:v:0",
        "-vf",
        f"crop={width}:{height}:{x}:{y},format=rgb24",
        "-fps_mode",
        "passthrough",
        "-f",
        "rawvideo",
        "pipe:1",
    ]
    process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert process.stdout is not None and process.stderr is not None
    samples: list[LedSample] = []
    try:
        for ref in frame_refs:
            raw = _read_exact(process.stdout, frame_bytes)
            if len(raw) != frame_bytes:
                stderr = process.stderr.read().decode("utf-8", errors="replace")
                raise LedError(
                    f"FFmpeg ended before all frames were decoded from {segment.path.name}: "
                    f"expected frame {ref.local_index}; {stderr.strip()}"
                )
            rgb = np.frombuffer(raw, dtype=np.uint8).reshape(height, width, 3)
            values = (
                rgb[..., 0].astype(np.float32)
                - (rgb[..., 1].astype(np.float32) + rgb[..., 2].astype(np.float32)) * 0.5
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

        extra = process.stdout.read(1)
        stderr = process.stderr.read().decode("utf-8", errors="replace")
        returncode = process.wait()
        if extra:
            raise LedError(f"FFmpeg decoded more frames than indexed in {segment.path.name}")
        if returncode != 0:
            raise LedError(
                f"FFmpeg ROI decode failed for {segment.path.name} with status {returncode}: "
                f"{stderr.strip()}"
            )
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
    return samples


def extract_scores(
    video: "VirtualVideo",
    ffmpeg: str,
    roi: tuple[int, int, int, int],
    sync_start: float,
) -> list[LedSample]:
    x, y, width, height = roi
    if x + width > video.width or y + height > video.height:
        raise LedError(
            f"ROI {x},{y},{width},{height} exceeds {video.width}x{video.height} frame"
        )

    first_time = video.first_time
    samples: list[LedSample] = []
    for segment in video.segments:
        refs = video.frames[segment.first_frame : segment.first_frame + segment.frame_count]
        segment_samples = _segment_scores(ffmpeg, segment, roi, refs)
        for sample in segment_samples:
            if sample.pts_seconds - float(first_time) >= sync_start:
                samples.append(sample)
    if not samples:
        raise LedError("no frames remain after --sync-start")
    return samples


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
