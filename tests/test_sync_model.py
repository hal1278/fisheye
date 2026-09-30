from unittest import TestCase

from fisheye.sync.led import LedEdge
from fisheye.sync.model import assign_utc_epochs, fit_affine, logical_pulse_indices


class SyncModelTest(TestCase):
    @staticmethod
    def _edges(count: int = 100):
        return [
            LedEdge(
                frame_off=i * 15,
                frame_on=i * 15 + 1,
                pts_off=5.0 + i - 0.03,
                pts_on=5.0 + i + 0.03,
                pts_mid=5.0 + i,
                on_frames=2,
            )
            for i in range(count)
        ]

    def test_logical_indices(self):
        edges = self._edges(5)
        edges[3].pts_off += 1.0
        edges[3].pts_on += 1.0
        edges[3].pts_mid += 1.0
        edges[4].pts_off += 1.0
        edges[4].pts_on += 1.0
        edges[4].pts_mid += 1.0
        self.assertEqual(logical_pulse_indices(edges, 1 / 15), [0, 1, 2, 4, 5])

    def test_assign_and_fit(self):
        edges = self._edges()
        logical = logical_pulse_indices(edges, 1 / 15)
        base_epoch = 1_790_000_000
        coarse_offset = base_epoch - edges[0].pts_mid + 0.05
        locked = {base_epoch + i for i in range(len(edges))}
        first, _ = assign_utc_epochs(edges, logical, coarse_offset, 0.02, locked)
        self.assertEqual(first, base_epoch)
        model, metrics = fit_affine(edges, 1 / 15)
        self.assertAlmostEqual(model.rate, 1.0, places=12)
        self.assertEqual(metrics["used_edges"], len(edges))


    def test_interval_metrics_do_not_misclassify_frame_quantization_as_drift(self):
        frame_interval = 1 / 15
        base_epoch = 1_790_000_000
        edges = []
        for i in range(1200):
            phase = 0.1 if (i // 300) % 2 == 0 else 0.9
            pulse_pts = 5.0 + i
            pts_off = pulse_pts - phase * frame_interval
            pts_on = pts_off + frame_interval
            edge = LedEdge(
                frame_off=i * 15,
                frame_on=i * 15 + 1,
                pts_off=pts_off,
                pts_on=pts_on,
                pts_mid=(pts_off + pts_on) * 0.5,
                logical_index=i,
                utc_epoch=base_epoch + i,
            )
            edges.append(edge)

        _, metrics = fit_affine(edges, frame_interval)

        self.assertGreater(
            metrics["binned_median_span_ms"],
            0.5 * frame_interval * 1000.0,
        )
        self.assertLess(
            metrics["binned_interval_median_span_ms"],
            0.5 * frame_interval * 1000.0,
        )
        self.assertFalse(metrics["piecewise_recommended"])
        self.assertGreater(metrics["interval_covered_fraction"], 0.75)
