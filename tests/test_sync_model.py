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
