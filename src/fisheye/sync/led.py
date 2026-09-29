from __future__ import annotations

import bisect
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
import os
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
    delta_valid: bool


@dataclass(slots=True)
class LedEdge:
    frame_off: int
    frame_on: int
    pts_off: float
    pts_on: float
    pts_mid: float
    on_frames: int = 0
    utc_epoch: int | None = None
    used: bool = True
    residual_ms: float | None = None


@dataclass(slots=True)
class LedExtraction:
    samples: list[LedSample]
    signatures: np.ndarray
    skipped_frame_indices: list[int]
    decoder_messages: list[tuple[str, str]]
    workers: int


@dataclass(slots=True)
class EdgeDetection:
    edges: list[LedEdge]
    candidate_sample_indices: set[int]
    selected_sample_indices: set[int]
    template_similarity: np.ndarray
    rough_threshold: float
    onset_threshold: float
    fitted_period_s: float
    candidate_count: int
    selected_count: int
    template_bins: int


@dataclass(slots=True)
class _CandidateEvent:
    sample_index: int
    time: float
    score: float
    signature: np.ndarray


_SPATIAL_BINS = 4


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


def _luma(rgb: np.ndarray) -> np.ndarray:
    # Only local frame-to-frame change is used, so a standard luma projection is sufficient.
    return (
        0.299 * rgb[..., 0].astype(np.float32)
        + 0.587 * rgb[..., 1].astype(np.float32)
        + 0.114 * rgb[..., 2].astype(np.float32)
    )


def _spatial_signature(
    signal: np.ndarray, bins: int = _SPATIAL_BINS
) -> np.ndarray:
    height, width = signal.shape
    yy = np.minimum(np.arange(height) * bins // height, bins - 1)
    xx = np.minimum(np.arange(width) * bins // width, bins - 1)
    labels = (yy[:, None] * bins + xx[None, :]).reshape(-1)
    pooled = np.bincount(
        labels,
        weights=signal.reshape(-1),
        minlength=bins * bins,
    ).astype(np.float32)
    total = float(pooled.sum())
    if total > 0.0:
        pooled /= total
    return pooled



def _segment_scores(
    ffmpeg: str,
    segment: "SegmentInfo",
    roi: tuple[int, int, int, int],
    frame_refs: tuple["FrameRef", ...] | list["FrameRef"],
    polarity: str,
) -> tuple[list[LedSample], np.ndarray, list[int], str]:
    import av

    x, y, width, height = roi
    cmd = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "warning",
        "-copyts",
        "-threads",
        "1",
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
    signatures: list[np.ndarray] = []
    sign = 1.0 if polarity == "rising" else -1.0

    source_times = np.asarray([float(ref.time) for ref in refs], dtype=np.float64)
    positive_deltas = np.diff(source_times)
    positive_deltas = positive_deltas[positive_deltas > 0]
    if positive_deltas.size == 0:
        raise LedError(f"cannot determine frame interval for {segment.path.name}")
    median_interval = float(np.median(positive_deltas))
    max_transition = 2.5 * median_interval

    previous_ref: "FrameRef" | None = None
    previous_luma: np.ndarray | None = None

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

                        current_luma = _luma(_rgb_view(frame))
                        delta_valid = False
                        score = 0.0
                        signature = np.zeros(
                            _SPATIAL_BINS**2, dtype=np.float32
                        )

                        if previous_ref is not None and previous_luma is not None:
                            dt = float(ref.time - previous_ref.time)
                            contiguous = (
                                ref.local_index == previous_ref.local_index + 1
                            )
                            if contiguous and 0.0 < dt <= max_transition:
                                delta = sign * (current_luma - previous_luma)
                                delta -= float(np.median(delta))
                                signal = np.maximum(delta, 0.0)
                                values = signal.reshape(-1)
                                top_count = max(1, values.size // 20)
                                top = np.partition(
                                    values, values.size - top_count
                                )[-top_count:]
                                score = float(top.mean())
                                signature = _spatial_signature(signal)
                                delta_valid = True

                        samples.append(
                            LedSample(
                                frame_index=ref.index,
                                pts_seconds=float(ref.time),
                                score=score,
                                delta_valid=delta_valid,
                            )
                        )
                        signatures.append(signature)
                        previous_ref = ref
                        previous_luma = current_luma
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
            stderr = stderr_file.read().decode(
                "utf-8", errors="replace"
            ).strip()

    if failure is not None:
        if isinstance(failure, LedError):
            raise failure
        raise LedError(
            f"failed to read timestamped ROI stream from {segment.path.name}: "
            f"{failure}; {stderr}"
        ) from failure
    if returncode != 0:
        raise LedError(
            f"FFmpeg ROI decode failed for {segment.path.name} with status "
            f"{returncode}: {stderr}"
        )

    missing = [ref.index for ref in refs if ref.index not in seen]
    signature_array = (
        np.stack(signatures).astype(np.float32, copy=False)
        if signatures
        else np.empty((0, _SPATIAL_BINS**2), dtype=np.float32)
    )
    return samples, signature_array, missing, stderr

def _resolve_workers(requested: int | None, task_count: int) -> int:
    if task_count <= 1:
        return 1
    if requested is not None:
        if requested <= 0:
            raise ValueError("LED extraction worker count must be positive")
        return min(requested, task_count)
    cpu_count = os.cpu_count() or 1
    return min(task_count, cpu_count, 4)


def extract_scores(
    video: "VirtualVideo",
    ffmpeg: str,
    roi: tuple[int, int, int, int],
    sync_start: float,
    polarity: str,
    workers: int | None = None,
) -> LedExtraction:
    x, y, width, height = roi
    if x + width > video.width or y + height > video.height:
        raise LedError(
            f"ROI {x},{y},{width},{height} exceeds "
            f"{video.width}x{video.height} frame"
        )
    if polarity not in {"rising", "falling"}:
        raise LedError(f"unsupported TIMEPULSE polarity: {polarity!r}")

    first_time = video.first_time
    tasks: list[tuple["SegmentInfo", tuple["FrameRef", ...]]] = []
    for segment in video.segments:
        refs = video.frames[
            segment.first_frame : segment.first_frame + segment.frame_count
        ]
        if float(refs[-1].time - first_time) < sync_start:
            continue
        tasks.append((segment, refs))

    if not tasks:
        raise LedError("no source segments remain after --sync-start")

    worker_count = _resolve_workers(workers, len(tasks))
    results: dict[
        int, tuple[list[LedSample], np.ndarray, list[int], str]
    ] = {}

    if worker_count == 1:
        for segment, refs in tasks:
            results[segment.index] = _segment_scores(
                ffmpeg, segment, roi, refs, polarity
            )
    else:
        with ThreadPoolExecutor(
            max_workers=worker_count,
            thread_name_prefix="fisheye-led",
        ) as executor:
            futures = {
                executor.submit(
                    _segment_scores,
                    ffmpeg,
                    segment,
                    roi,
                    refs,
                    polarity,
                ): segment.index
                for segment, refs in tasks
            }
            for future in as_completed(futures):
                segment_index = futures[future]
                results[segment_index] = future.result()

    samples: list[LedSample] = []
    signature_blocks: list[np.ndarray] = []
    skipped: list[int] = []
    decoder_messages: list[tuple[str, str]] = []

    for segment, refs in tasks:
        (
            segment_samples,
            segment_signatures,
            segment_missing,
            stderr,
        ) = results[segment.index]
        if stderr:
            decoder_messages.append((segment.path.name, stderr))

        keep = np.fromiter(
            (
                sample.pts_seconds - float(first_time) >= sync_start
                for sample in segment_samples
            ),
            dtype=bool,
            count=len(segment_samples),
        )
        samples.extend(
            sample for sample, use in zip(segment_samples, keep) if use
        )
        signature_blocks.append(segment_signatures[keep])
        skipped.extend(
            frame_index
            for frame_index in segment_missing
            if float(video.elapsed(frame_index)) >= sync_start
        )

    if not samples:
        raise LedError("no decodable frames remain after --sync-start")

    order = np.argsort(
        np.fromiter(
            (sample.frame_index for sample in samples), dtype=np.int64
        )
    )
    samples = [samples[int(index)] for index in order]
    signatures = np.concatenate(signature_blocks, axis=0)[order]
    skipped.sort()
    return LedExtraction(
        samples,
        signatures,
        skipped,
        decoder_messages,
        worker_count,
    )

def _robust_threshold(
    scores: np.ndarray, sigma: float
) -> tuple[float, float, float]:
    median = float(np.median(scores))
    mad = float(np.median(np.abs(scores - median)))
    scale = 1.4826 * mad
    if scale <= 1e-9:
        # Keep the threshold capture-adaptive even for nearly quantized data.
        scale = max(float(np.percentile(scores, 75) - median), 1e-6)
    return median + sigma * scale, median, scale


def _cluster_candidates(
    samples: list[LedSample],
    signatures: np.ndarray,
    threshold: float,
    cluster_window: float,
) -> list[_CandidateEvent]:
    raw = [
        index
        for index, sample in enumerate(samples)
        if sample.delta_valid and sample.score >= threshold
    ]
    if not raw:
        return []

    clusters: list[list[int]] = [[raw[0]]]
    for index in raw[1:]:
        previous = clusters[-1][-1]
        if (
            samples[index].pts_seconds - samples[previous].pts_seconds
            <= cluster_window
        ):
            clusters[-1].append(index)
        else:
            clusters.append([index])

    events: list[_CandidateEvent] = []
    for cluster in clusters:
        peak = max(cluster, key=lambda index: samples[index].score)
        events.append(
            _CandidateEvent(
                sample_index=peak,
                time=samples[peak].pts_seconds,
                score=samples[peak].score,
                signature=signatures[peak],
            )
        )
    return events


def _initial_chain(
    events: list[_CandidateEvent],
    expected_period: float,
    frame_interval: float,
) -> tuple[list[int], list[int]]:
    if len(events) < 10:
        raise LedError(f"too few reference-edge candidates: {len(events)}")

    times = np.asarray([event.time for event in events], dtype=np.float64)
    strengths = np.asarray(
        [event.score for event in events], dtype=np.float64
    )
    _, strength_median, strength_scale = _robust_threshold(strengths, 0.0)
    strength_z = np.clip(
        (strengths - strength_median) / strength_scale, 0.0, 12.0
    )

    tolerance = max(2.5 * frame_interval, 0.18 * expected_period)
    max_step = 10
    score = np.ones(len(events), dtype=np.float64) + 0.05 * strength_z
    predecessor = np.full(len(events), -1, dtype=np.int64)
    pulse_index = np.zeros(len(events), dtype=np.int64)

    left = 0
    for current in range(len(events)):
        while (
            times[current] - times[left]
            > (max_step + 0.5) * expected_period
        ):
            left += 1
        for previous in range(left, current):
            delta = times[current] - times[previous]
            step = int(round(delta / expected_period))
            if not 1 <= step <= max_step:
                continue
            residual = abs(delta - step * expected_period)
            if residual > tolerance:
                continue
            value = (
                score[previous]
                + 1.0
                + 0.05 * strength_z[current]
                - 0.35 * (residual / tolerance)
                - 0.08 * (step - 1)
            )
            if value > score[current]:
                score[current] = value
                predecessor[current] = previous
                pulse_index[current] = pulse_index[previous] + step

    current = int(np.argmax(score))
    chain: list[int] = []
    indices: list[int] = []
    while current >= 0:
        chain.append(current)
        indices.append(int(pulse_index[current]))
        current = int(predecessor[current])
    chain.reverse()
    indices.reverse()
    if len(chain) < 10:
        raise LedError(
            f"periodic reference-edge chain is too short: {len(chain)}"
        )
    offset = indices[0]
    indices = [index - offset for index in indices]
    return chain, indices


def _fit_lattice(
    events: list[_CandidateEvent],
    chain: list[int],
    indices: list[int],
    expected_period: float,
    frame_interval: float,
) -> tuple[float, float, np.ndarray]:
    n = np.asarray(indices, dtype=np.float64)
    t = np.asarray(
        [events[index].time for index in chain], dtype=np.float64
    )
    n_ref = float(np.median(n))
    matrix = np.column_stack((np.ones_like(n), n - n_ref))
    intercept, period = np.linalg.lstsq(
        matrix, t, rcond=None
    )[0]
    if abs(float(period) - expected_period) > max(
        0.01 * expected_period, frame_interval
    ):
        raise LedError(
            f"fitted pulse period {float(period):.6f} s is inconsistent "
            f"with configured period {expected_period:.6f} s"
        )

    predicted = float(intercept) + float(period) * (n - n_ref)
    residual = t - predicted
    limit = max(2.5 * frame_interval, 0.18 * expected_period)
    keep = (
        np.abs(residual - np.median(residual)) <= limit
    )
    if int(keep.sum()) < 10:
        raise LedError(
            "too few periodic candidates remain after lattice refit"
        )
    matrix = matrix[keep]
    t_used = t[keep]
    intercept, period = np.linalg.lstsq(
        matrix, t_used, rcond=None
    )[0]
    return (
        float(intercept - period * n_ref),
        float(period),
        keep,
    )


def _cosine_similarity(
    signatures: np.ndarray, template: np.ndarray
) -> np.ndarray:
    template_norm = float(np.linalg.norm(template))
    row_norm = np.linalg.norm(signatures, axis=1)
    denominator = row_norm * template_norm
    similarity = np.zeros(signatures.shape[0], dtype=np.float64)
    valid = denominator > 0
    similarity[valid] = (
        signatures[valid] @ template / denominator[valid]
    )
    return similarity


def detect_reference_edges(
    extraction: LedExtraction,
    frame_interval: float,
    expected_period: float,
) -> EdgeDetection:
    samples = extraction.samples
    signatures = extraction.signatures
    if expected_period <= 0:
        raise LedError("configured TIMEPULSE period must be positive")
    if len(samples) != signatures.shape[0]:
        raise LedError("LED sample/signature length mismatch")

    valid_scores = np.asarray(
        [
            sample.score
            for sample in samples
            if sample.delta_valid
        ],
        dtype=np.float64,
    )
    if valid_scores.size < 10:
        raise LedError(
            "too few valid frame transitions for edge detection"
        )

    rough_threshold, baseline, scale = _robust_threshold(
        valid_scores, 6.0
    )
    onset_threshold = baseline + 3.0 * scale
    cluster_window = max(
        2.5 * frame_interval, 0.20 * expected_period
    )
    events = _cluster_candidates(
        samples,
        signatures,
        rough_threshold,
        cluster_window,
    )
    if len(events) < 10:
        raise LedError(
            f"too few reference-edge candidate events: {len(events)}"
        )

    chain, chain_indices = _initial_chain(
        events, expected_period, frame_interval
    )
    phase, period, keep = _fit_lattice(
        events,
        chain,
        chain_indices,
        expected_period,
        frame_interval,
    )
    kept_chain = [
        event_index
        for event_index, use in zip(chain, keep)
        if use
    ]
    if len(kept_chain) < 10:
        raise LedError(
            "too few candidates available for spatial-template learning"
        )

    template = np.median(
        np.stack(
            [events[index].signature for index in kept_chain]
        ),
        axis=0,
    ).astype(np.float32)
    total = float(template.sum())
    if total <= 0.0:
        raise LedError("reference-edge spatial template is empty")
    template /= total

    event_signatures = np.stack(
        [event.signature for event in events]
    )
    event_similarity = _cosine_similarity(
        event_signatures, template
    )
    all_similarity = _cosine_similarity(
        signatures, template
    )

    times = np.asarray(
        [event.time for event in events], dtype=np.float64
    )
    strengths = np.asarray(
        [event.score for event in events], dtype=np.float64
    )
    _, strength_median, strength_scale = _robust_threshold(
        strengths, 0.0
    )
    strength_z = np.clip(
        (strengths - strength_median) / strength_scale,
        0.0,
        12.0,
    )

    first_n = int(np.floor((times[0] - phase) / period)) - 1
    last_n = int(np.ceil((times[-1] - phase) / period)) + 1
    search_window = max(
        2.5 * frame_interval, 0.18 * expected_period
    )

    selected_events: list[int] = []
    selected_indices: list[int] = []
    left = 0
    for pulse_index in range(first_n, last_n + 1):
        predicted = phase + period * pulse_index
        while (
            left < len(events)
            and times[left] < predicted - search_window
        ):
            left += 1
        right = left
        best: int | None = None
        best_score = -np.inf
        while (
            right < len(events)
            and times[right] <= predicted + search_window
        ):
            residual = abs(times[right] - predicted) / search_window
            value = (
                2.0 * event_similarity[right]
                + 0.08 * strength_z[right]
                - 0.8 * residual
            )
            if value > best_score:
                best_score = value
                best = right
            right += 1
        if best is not None:
            selected_events.append(best)
            selected_indices.append(pulse_index)

    if len(selected_events) < 10:
        raise LedError(
            f"too few periodic edges selected: {len(selected_events)}"
        )

    n = np.asarray(selected_indices, dtype=np.float64)
    t = np.asarray(
        [events[index].time for index in selected_events],
        dtype=np.float64,
    )
    n_ref = float(np.median(n))
    matrix = np.column_stack((np.ones_like(n), n - n_ref))
    intercept, period = np.linalg.lstsq(
        matrix, t, rcond=None
    )[0]
    phase = float(intercept - period * n_ref)
    period = float(period)
    del phase

    edges: list[LedEdge] = []
    selected_sample_indices: set[int] = set()
    for event_index in selected_events:
        peak = events[event_index].sample_index
        onset = peak
        while onset > 0:
            current = samples[onset]
            previous = samples[onset - 1]
            if not current.delta_valid:
                break
            if current.score < onset_threshold:
                break
            if (
                onset != peak
                and current.pts_seconds - previous.pts_seconds
                > 2.5 * frame_interval
            ):
                break
            if previous.frame_index + 1 != current.frame_index:
                break
            onset -= 1
        if (
            onset < peak
            and samples[onset].score < onset_threshold
        ):
            onset += 1
        if onset <= 0:
            continue
        on = samples[onset]
        off = samples[onset - 1]
        if (
            not on.delta_valid
            or off.frame_index + 1 != on.frame_index
        ):
            continue
        edges.append(
            LedEdge(
                frame_off=off.frame_index,
                frame_on=on.frame_index,
                pts_off=off.pts_seconds,
                pts_on=on.pts_seconds,
                pts_mid=(
                    off.pts_seconds + on.pts_seconds
                )
                * 0.5,
            )
        )
        selected_sample_indices.add(onset)

    if len(edges) < 10:
        raise LedError(
            "too few valid reference-edge brackets after onset "
            f"refinement: {len(edges)}"
        )

    candidate_sample_indices = {
        event.sample_index for event in events
    }
    return EdgeDetection(
        edges=edges,
        candidate_sample_indices=candidate_sample_indices,
        selected_sample_indices=selected_sample_indices,
        template_similarity=all_similarity,
        rough_threshold=rough_threshold,
        onset_threshold=onset_threshold,
        fitted_period_s=period,
        candidate_count=len(events),
        selected_count=len(edges),
        template_bins=_SPATIAL_BINS,
    )
