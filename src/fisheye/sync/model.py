from __future__ import annotations

from dataclasses import dataclass
import numpy as np

from .led import LedEdge


class SyncModelError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class AffineModel:
    pts_reference: float
    utc_reference: float
    offset: float
    rate: float

    def utc(self, pts_seconds: float) -> float:
        return self.utc_reference + self.offset + self.rate * (
            pts_seconds - self.pts_reference
        )


def logical_pulse_indices(edges: list[LedEdge], frame_interval: float) -> list[int]:
    indices = [0]
    tolerance = max(0.25, 2.0 * frame_interval)
    for previous, current in zip(edges, edges[1:]):
        delta = current.pts_mid - previous.pts_mid
        step = round(delta)
        if step < 1 or abs(delta - step) > tolerance:
            raise SyncModelError(
                f"LED pulse interval {delta:.3f} s cannot be assigned to an integer number of seconds"
            )
        indices.append(indices[-1] + step)
    return indices


def assign_utc_epochs(
    edges: list[LedEdge],
    logical_indices: list[int],
    coarse_offset: float,
    coarse_mad: float,
    locked_epochs: set[int],
) -> tuple[int, float]:
    estimates = np.asarray(
        [edge.pts_mid + coarse_offset - index for edge, index in zip(edges, logical_indices)],
        dtype=np.float64,
    )
    estimate = float(np.median(estimates))
    first_epoch = int(round(estimate))
    distance = abs(estimate - first_epoch)
    if distance + 3.0 * coarse_mad >= 0.5:
        raise SyncModelError(
            "coarse anchors do not resolve the integer-second ambiguity safely: "
            f"estimate={estimate:.3f}, MAD={coarse_mad:.3f} s"
        )

    present = 0
    for edge, index in zip(edges, logical_indices):
        epoch = first_epoch + index
        edge.utc_epoch = epoch
        if epoch in locked_epochs:
            present += 1
    if present < max(10, round(0.9 * len(edges))):
        raise SyncModelError(
            f"only {present}/{len(edges)} assigned LED epochs are present and locked in gnss.csv"
        )
    return first_epoch, estimate


def fit_affine(
    edges: list[LedEdge], frame_interval: float
) -> tuple[AffineModel, dict[str, float | int | bool]]:
    candidates = [edge for edge in edges if edge.utc_epoch is not None]
    if len(candidates) < 10:
        raise SyncModelError("too few UTC-assigned LED edges for model fitting")

    p = np.asarray([edge.pts_mid for edge in candidates], dtype=np.float64)
    t = np.asarray([edge.utc_epoch for edge in candidates], dtype=np.float64)
    middle = len(candidates) // 2
    p_ref = float(p[middle])
    t_ref = float(t[middle])

    def fit(mask: np.ndarray) -> tuple[float, float, np.ndarray]:
        x = p[mask] - p_ref
        y = t[mask] - t_ref
        matrix = np.column_stack((np.ones_like(x), x))
        offset, rate = np.linalg.lstsq(matrix, y, rcond=None)[0]
        predicted = t_ref + offset + rate * (p - p_ref)
        residual = t - predicted
        return float(offset), float(rate), residual

    mask = np.ones(len(candidates), dtype=bool)
    offset, rate, residual = fit(mask)
    median = float(np.median(residual))
    mad = float(np.median(np.abs(residual - median)))
    reject_limit = max(2.0 * frame_interval, 6.0 * mad)
    mask = np.abs(residual - median) <= reject_limit
    if int(mask.sum()) < max(10, round(0.8 * len(candidates))):
        raise SyncModelError(
            f"too many LED edges rejected by affine model: {int(mask.sum())}/{len(candidates)} remain"
        )
    offset, rate, residual = fit(mask)

    off_pts = np.asarray(
        [edge.pts_off for edge in candidates], dtype=np.float64
    )
    on_pts = np.asarray(
        [edge.pts_on for edge in candidates], dtype=np.float64
    )
    predicted_off = t_ref + offset + rate * (off_pts - p_ref)
    predicted_on = t_ref + offset + rate * (on_pts - p_ref)
    lower = np.minimum(predicted_off, predicted_on)
    upper = np.maximum(predicted_off, predicted_on)
    interval_residual = np.where(
        t < lower,
        t - lower,
        np.where(t > upper, t - upper, 0.0),
    )

    for edge, keep, value, interval_value in zip(
        candidates, mask, residual, interval_residual
    ):
        edge.used = bool(keep)
        edge.residual_ms = float(value * 1000.0)
        edge.interval_residual_ms = float(interval_value * 1000.0)

    used_residual = residual[mask]
    abs_used = np.abs(used_residual)
    used_interval_residual = interval_residual[mask]
    abs_interval = np.abs(used_interval_residual)
    metrics: dict[str, float | int | bool] = {
        "detected_edges": len(edges),
        "used_edges": int(mask.sum()),
        "residual_median_ms": float(np.median(used_residual) * 1000.0),
        "residual_mad_ms": float(
            np.median(np.abs(used_residual - np.median(used_residual))) * 1000.0
        ),
        "residual_rms_ms": float(np.sqrt(np.mean(used_residual**2)) * 1000.0),
        "residual_p95_abs_ms": float(np.percentile(abs_used, 95) * 1000.0),
        "residual_max_abs_ms": float(abs_used.max() * 1000.0),
        "interval_covered_edges": int(
            np.count_nonzero(used_interval_residual == 0.0)
        ),
        "interval_covered_fraction": float(
            np.mean(used_interval_residual == 0.0)
        ),
        "interval_residual_median_ms": float(
            np.median(used_interval_residual) * 1000.0
        ),
        "interval_residual_mad_ms": float(
            np.median(
                np.abs(
                    used_interval_residual
                    - np.median(used_interval_residual)
                )
            )
            * 1000.0
        ),
        "interval_residual_rms_ms": float(
            np.sqrt(np.mean(used_interval_residual**2)) * 1000.0
        ),
        "interval_residual_p95_abs_ms": float(
            np.percentile(abs_interval, 95) * 1000.0
        ),
        "interval_residual_max_abs_ms": float(
            abs_interval.max() * 1000.0
        ),
    }

    bin_ids = np.floor((p[mask] - p[mask][0]) / 300.0).astype(int)
    midpoint_medians = [
        float(np.median(used_residual[bin_ids == value]))
        for value in np.unique(bin_ids)
    ]
    midpoint_span = (
        max(midpoint_medians) - min(midpoint_medians)
        if len(midpoint_medians) >= 2
        else 0.0
    )
    interval_medians = [
        float(np.median(used_interval_residual[bin_ids == value]))
        for value in np.unique(bin_ids)
    ]
    interval_span = (
        max(interval_medians) - min(interval_medians)
        if len(interval_medians) >= 2
        else 0.0
    )
    metrics["binned_median_span_ms"] = midpoint_span * 1000.0
    metrics["binned_interval_median_span_ms"] = interval_span * 1000.0
    metrics["piecewise_recommended"] = (
        interval_span > 0.5 * frame_interval
    )

    return (
        AffineModel(
            pts_reference=p_ref,
            utc_reference=t_ref,
            offset=offset,
            rate=rate,
        ),
        metrics,
    )
