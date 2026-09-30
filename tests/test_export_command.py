from unittest import TestCase

from fisheye.export.command import (
    ExportError,
    _decodable_span,
    _undecodable_indices,
)


class ExportCommandTest(TestCase):
    def test_undecodable_indices_validate_count_and_range(self):
        source = {
            "undecodable_frame_count": 3,
            "undecodable_frame_indices": [2, 5, 9],
        }
        self.assertEqual(
            _undecodable_indices(source, 10),
            {2, 5, 9},
        )

        with self.assertRaises(ExportError):
            _undecodable_indices(
                {
                    "undecodable_frame_count": 2,
                    "undecodable_frame_indices": [2],
                },
                10,
            )

        with self.assertRaises(ExportError):
            _undecodable_indices(
                {
                    "undecodable_frame_count": 1,
                    "undecodable_frame_indices": [10],
                },
                10,
            )

    def test_decodable_span_skips_cut_and_trailing_missing_frames(self):
        cut, last, count = _decodable_span(
            frame_count=10,
            requested_cut_index=2,
            undecodable={2, 5, 9},
        )
        self.assertEqual(cut, 3)
        self.assertEqual(last, 8)
        self.assertEqual(count, 5)

    def test_decodable_span_rejects_missing_tail(self):
        with self.assertRaises(ExportError):
            _decodable_span(
                frame_count=5,
                requested_cut_index=3,
                undecodable={3, 4},
            )
