from unittest import TestCase

import numpy as np

from fisheye.sync.led import LedExtraction, LedSample, detect_reference_edges


class LedDetectionTest(TestCase):
    def test_periodic_template_selection_rejects_false_events(self):
        rng = np.random.default_rng(2)
        fps = 15.0
        duration = 120
        frame_count = round(fps * duration)
        samples: list[LedSample] = []
        signatures = np.zeros((frame_count, 16), dtype=np.float32)

        for frame in range(frame_count):
            t = frame / fps
            score = max(0.0, float(rng.normal(2.0, 0.4)))
            signature = rng.random(16).astype(np.float32)
            signature /= signature.sum()
            if (t - 0.2) % 1.0 < 0.09:
                score = float(15.0 + rng.normal(0.0, 1.0))
                signature = np.zeros(16, dtype=np.float32)
                signature[10] = 0.65
                signature[11] = 0.35
            samples.append(
                LedSample(
                    frame_index=frame,
                    pts_seconds=t,
                    score=score,
                    delta_valid=frame > 0,
                )
            )
            signatures[frame] = signature

        for frame in rng.choice(
            np.arange(5, frame_count - 5), size=20, replace=False
        ):
            samples[int(frame)].score = 18.0
            signature = np.zeros(16, dtype=np.float32)
            signature[int(rng.integers(0, 8))] = 1.0
            signatures[int(frame)] = signature

        extraction = LedExtraction(samples, signatures, [], [], 1)
        result = detect_reference_edges(extraction, 1 / fps, 1.0)

        self.assertGreaterEqual(result.candidate_count, duration)
        self.assertGreaterEqual(result.selected_count, duration - 2)
        self.assertLessEqual(result.selected_count, duration + 2)
        self.assertAlmostEqual(result.fitted_period_s, 1.0, places=3)
        mids = np.asarray([edge.pts_mid for edge in result.edges])
        self.assertLess(
            float(np.median(np.abs(np.diff(mids) - 1.0))),
            0.02,
        )


    def test_recovers_below_bootstrap_threshold_pulse(self):
        rng = np.random.default_rng(4)
        fps = 15.0
        duration = 60
        frame_count = round(fps * duration)
        samples: list[LedSample] = []
        signatures = np.zeros((frame_count, 16), dtype=np.float32)

        for frame in range(frame_count):
            t = frame / fps
            score = max(0.0, float(rng.normal(2.0, 0.3)))
            signature = rng.random(16).astype(np.float32)
            signature /= signature.sum()
            phase = (t - 0.2) % 1.0
            if phase < 0.09:
                score = float(16.0 + rng.normal(0.0, 0.5))
                signature = np.zeros(16, dtype=np.float32)
                signature[5] = 0.7
                signature[6] = 0.3
                if 29.0 < t < 31.0:
                    score = 7.0
            samples.append(
                LedSample(
                    frame_index=frame,
                    pts_seconds=t,
                    score=score,
                    delta_valid=frame > 0,
                )
            )
            signatures[frame] = signature

        extraction = LedExtraction(samples, signatures, [], [], 1)
        result = detect_reference_edges(extraction, 1 / fps, 1.0)
        logical = [edge.logical_index for edge in result.edges]

        self.assertIn(30, logical)
        self.assertTrue(all(index is not None for index in logical))
        self.assertEqual(logical, sorted(logical))
