from __future__ import annotations

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
    logical_index: int | None = None
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
    template_similarity_floor: float
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


class _PtsMatcher:
    """Match monotonic decoded PTS to the monotonic source timeline."""

    def __init__(
        self,
        frame_refs: tuple["FrameRef", ...] | list["FrameRef"],
    ):
        self._refs = frame_refs
        self._keys = [round(ref.time * 1_000_000) for ref in frame_refs]
        for previous, current in zip(self._keys, self._keys[1:]):
            if current <= previous:
                raise LedError(
                    "source frame timestamps collide at AVTB resolution"
                )
        self._next = 0
        self._last_decoded_pts: int | None = None
        self._missing: list[int] = []

    def match(self, pts_us: int) -> "FrameRef":
        if (
            self._last_decoded_pts is not None
            and pts_us <= self._last_decoded_pts
        ):
            raise LedError(
                f"decoded ROI timestamps are not increasing: "
                f"{pts_us} <= {self._last_decoded_pts} us"
            )
        self._last_decoded_pts = pts_us

        while (
            self._next < len(self._keys)
            and self._keys[self._next] < pts_us - 1
        ):
            self._missing.append(self._refs[self._next].index)
            self._next += 1

        if self._next >= len(self._keys):
            raise LedError(
                f"decoded ROI frame PTS {pts_us} us has no source-frame match"
            )

        key = self._keys[self._next]
        if abs(key - pts_us) > 1:
            raise LedError(
                f"decoded ROI frame PTS {pts_us} us does not match source "
                f"timeline (next {key} us)"
            )

        ref = self._refs[self._next]
        self._next += 1
        return ref

    def missing_indices(self) -> list[int]:
        return self._missing + [
            ref.index for ref in self._refs[self._next :]
        ]


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


def _spatial_labels(
    height: int,
    width: int,
    bins: int = _SPATIAL_BINS,
) -> np.ndarray:
    yy = np.minimum(np.arange(height) * bins // height, bins - 1)
    xx = np.minimum(np.arange(width) * bins // width, bins - 1)
    return (yy[:, None] * bins + xx[None, :]).reshape(-1)


def _spatial_signature(
    signal: np.ndarray,
    labels: np.ndarray,
    bins: int = _SPATIAL_BINS,
) -> np.ndarray:
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

    refs = frame_refs
    matcher = _PtsMatcher(refs)
    samples: list[LedSample] = []
    signatures: list[np.ndarray] = []
    sign = 1.0 if polarity == "rising" else -1.0
    spatial_labels = _spatial_labels(height, width)

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
                        ref = matcher.match(pts_us)

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
                                signature = _spatial_signature(
                                    signal, spatial_labels
                                )
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

    missing = matcher.missing_indices()
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

    for previous, current in zip(samples, samples[1:]):
        if current.frame_index <= previous.frame_index:
            raise LedError(
                "segment merge produced non-increasing source frame indices"
            )
    signatures = np.concatenate(signature_blocks, axis=0)
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

    event_times = np.asarray(
        [event.time for event in events], dtype=np.float64
    )
    event_strengths = np.asarray(
        [event.score for event in events], dtype=np.float64
    )
    _, event_strength_median, event_strength_scale = _robust_threshold(
        event_strengths, 0.0
    )
    event_strength_z = np.clip(
        (event_strengths - event_strength_median) / event_strength_scale,
        0.0,
        12.0,
    )

    search_window = max(
        2.5 * frame_interval, 0.18 * expected_period
    )

    # First extend the bootstrap lattice across the capture using only rough
    # candidates. This pass is used to estimate how much the learned template
    # similarity varies over time; rough_threshold is not a final gate.
    calibration_events: list[int] = []
    calibration_indices: list[int] = []
    first_n = int(np.floor((event_times[0] - phase) / period)) - 1
    last_n = int(np.ceil((event_times[-1] - phase) / period)) + 1
    left = 0
    for pulse_index in range(first_n, last_n + 1):
        predicted = phase + period * pulse_index
        while (
            left < len(events)
            and event_times[left] < predicted - search_window
        ):
            left += 1
        right = left
        best: int | None = None
        best_score = -np.inf
        while (
            right < len(events)
            and event_times[right] <= predicted + search_window
        ):
            residual = (
                abs(event_times[right] - predicted) / search_window
            )
            value = (
                2.0 * event_similarity[right]
                + 0.08 * event_strength_z[right]
                - 0.8 * residual
            )
            if value > best_score:
                best_score = value
                best = right
            right += 1
        if best is not None:
            calibration_events.append(best)
            calibration_indices.append(pulse_index)

    if len(calibration_events) < 10:
        raise LedError(
            "too few periodic rough candidates for template calibration"
        )

    calibration_similarity = event_similarity[
        np.asarray(calibration_events, dtype=np.int64)
    ]
    similarity_median = float(np.median(calibration_similarity))
    similarity_mad = float(
        np.median(
            np.abs(calibration_similarity - similarity_median)
        )
    )
    if similarity_mad <= 1e-9:
        template_similarity_floor = float(
            np.percentile(calibration_similarity, 5)
        )
    else:
        template_similarity_floor = (
            similarity_median - 4.0 * 1.4826 * similarity_mad
        )
    template_similarity_floor = float(
        np.clip(template_similarity_floor, 0.0, 1.0)
    )

    sample_times = np.asarray(
        [sample.pts_seconds for sample in samples],
        dtype=np.float64,
    )
    sample_scores = np.asarray(
        [sample.score for sample in samples],
        dtype=np.float64,
    )
    sample_valid = np.asarray(
        [sample.delta_valid for sample in samples],
        dtype=bool,
    )
    sample_strength_z = np.clip(
        (sample_scores - baseline) / scale,
        0.0,
        12.0,
    )

    def select_samples(
        lattice_phase: float,
        lattice_period: float,
    ) -> list[tuple[int, int]]:
        selected: list[tuple[int, int]] = []
        first = int(
            np.ceil(
                (sample_times[0] - lattice_phase) / lattice_period
            )
        )
        last = int(
            np.floor(
                (sample_times[-1] - lattice_phase) / lattice_period
            )
        )
        for pulse_index in range(first, last + 1):
            predicted = lattice_phase + lattice_period * pulse_index
            lo = int(
                np.searchsorted(
                    sample_times,
                    predicted - search_window,
                    side="left",
                )
            )
            hi = int(
                np.searchsorted(
                    sample_times,
                    predicted + search_window,
                    side="right",
                )
            )
            if lo >= hi:
                continue
            indices = np.arange(lo, hi, dtype=np.int64)
            usable = (
                sample_valid[indices]
                & (sample_scores[indices] >= onset_threshold)
                & (
                    all_similarity[indices]
                    >= template_similarity_floor
                )
            )
            indices = indices[usable]
            if indices.size == 0:
                continue

            residual = np.abs(
                sample_times[indices] - predicted
            ) / search_window
            quality = (
                2.0 * all_similarity[indices]
                + 0.08 * sample_strength_z[indices]
                - 0.8 * residual
            )
            best = int(indices[int(np.argmax(quality))])
            selected.append((pulse_index, best))
        return selected

    def refit_selected(
        selected: list[tuple[int, int]],
    ) -> tuple[float, float, list[tuple[int, int]]]:
        if len(selected) < 10:
            raise LedError(
                f"too few periodic frame transitions selected: "
                f"{len(selected)}"
            )
        pulse_indices = np.asarray(
            [item[0] for item in selected], dtype=np.float64
        )
        selected_times = np.asarray(
            [sample_times[item[1]] for item in selected],
            dtype=np.float64,
        )
        n_ref = float(np.median(pulse_indices))
        matrix = np.column_stack(
            (
                np.ones_like(pulse_indices),
                pulse_indices - n_ref,
            )
        )
        intercept, fitted_period = np.linalg.lstsq(
            matrix, selected_times, rcond=None
        )[0]
        if abs(float(fitted_period) - expected_period) > max(
            0.01 * expected_period, frame_interval
        ):
            raise LedError(
                f"fitted pulse period {float(fitted_period):.6f} s "
                f"is inconsistent with configured period "
                f"{expected_period:.6f} s"
            )

        predicted = (
            float(intercept)
            + float(fitted_period) * (pulse_indices - n_ref)
        )
        residual = selected_times - predicted
        keep = (
            np.abs(residual - np.median(residual))
            <= search_window
        )
        if int(keep.sum()) < 10:
            raise LedError(
                "too few periodic frame transitions remain after refit"
            )

        filtered = [
            item
            for item, use in zip(selected, keep)
            if use
        ]
        pulse_indices = pulse_indices[keep]
        selected_times = selected_times[keep]
        n_ref = float(np.median(pulse_indices))
        matrix = np.column_stack(
            (
                np.ones_like(pulse_indices),
                pulse_indices - n_ref,
            )
        )
        intercept, fitted_period = np.linalg.lstsq(
            matrix, selected_times, rcond=None
        )[0]
        fitted_phase = float(
            intercept - fitted_period * n_ref
        )
        return fitted_phase, float(fitted_period), filtered

    # Search all valid frame transitions after the template has been learned.
    # This can recover real pulses whose rough score fell below rough_threshold.
    selected_samples = select_samples(phase, period)
    phase, period, selected_samples = refit_selected(
        selected_samples
    )
    selected_samples = select_samples(phase, period)
    phase, period, selected_samples = refit_selected(
        selected_samples
    )

    edges: list[LedEdge] = []
    selected_sample_indices: set[int] = set()
    max_onset_span = min(
        search_window, 2.5 * frame_interval
    )
    for pulse_index, peak in selected_samples:
        predicted = phase + period * pulse_index
        onset = peak
        while onset > 0:
            previous_index = onset - 1
            previous = samples[previous_index]
            current = samples[onset]
            if (
                not previous.delta_valid
                or previous.score < onset_threshold
                or all_similarity[previous_index]
                < template_similarity_floor
            ):
                break
            if previous.frame_index + 1 != current.frame_index:
                break
            if (
                sample_times[peak] - previous.pts_seconds
                > max_onset_span
            ):
                break
            if previous_index == 0:
                break
            earlier = samples[previous_index - 1]
            midpoint = (
                earlier.pts_seconds + previous.pts_seconds
            ) * 0.5
            if abs(midpoint - predicted) > search_window:
                break
            onset = previous_index

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
                logical_index=pulse_index,
            )
        )
        selected_sample_indices.add(onset)

    if len(edges) < 10:
        raise LedError(
            "too few valid reference-edge brackets after onset "
            f"refinement: {len(edges)}"
        )

    first_logical = edges[0].logical_index
    assert first_logical is not None
    for edge in edges:
        assert edge.logical_index is not None
        edge.logical_index -= first_logical

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
        template_similarity_floor=template_similarity_floor,
        fitted_period_s=period,
        candidate_count=len(events),
        selected_count=len(edges),
        template_bins=_SPATIAL_BINS,
    )
