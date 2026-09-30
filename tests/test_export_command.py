from unittest import TestCase

from fractions import Fraction
from pathlib import Path
from tempfile import TemporaryDirectory

from fisheye.export.command import (
    ExportError,
    _decodable_span,
    _input_seek_seconds,
    _input_segments,
    _output_nominal_fps,
    _timestamp_overlay_text,
    _undecodable_indices,
    _write_timestamps_csv,
)
from fisheye.media.timeline import FrameRef, SegmentInfo, VirtualVideo


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


    def _video_fixture(self):
        segments = (
            SegmentInfo(0, Path("video_000.mkv"), 0, 3, "mjpeg", 960, 720, 1, 1000),
            SegmentInfo(1, Path("video_001.mkv"), 3, 3, "mjpeg", 960, 720, 1, 1000),
            SegmentInfo(2, Path("video_002.mkv"), 6, 3, "mjpeg", 960, 720, 1, 1000),
        )
        frames = []
        pts = [0, 100, 200, 1800000, 1800100, 1800200, 3600000, 3600100, 3600200]
        for index, value in enumerate(pts):
            segment = 0 if index < 3 else 1 if index < 6 else 2
            local = index if segment == 0 else index - 3 if segment == 1 else index - 6
            frames.append(
                FrameRef(
                    index=index,
                    segment_index=segment,
                    local_index=local,
                    path=segments[segment].path,
                    pts=value,
                    time_base_num=1,
                    time_base_den=1000,
                )
            )
        return VirtualVideo(
            capture_dir=Path("."),
            frames=tuple(frames),
            segments=segments,
            width=960,
            height=720,
            codec="mjpeg",
            nominal_fps=15.0,
        )

    def test_timestamp_overlay_uses_corrected_pts_for_hundredths(self):
        text = _timestamp_overlay_text(1000.123)
        self.assertTrue(text.startswith("Estimated UTC: "))
        self.assertNotIn("%2N", text)
        self.assertIn(
            "%{pts:gmtime:1000.128000000:%Y-%m-%dT%H\\:%M\\:%S}",
            text,
        )
        self.assertIn(
            "%{eif:mod(floor((t+1000.128000000)*100),100):d:2}",
            text,
        )

    def test_output_nominal_fps_accounts_for_clock_rate(self):
        self.assertEqual(_output_nominal_fps(15.0, 1.0), Fraction(15, 1))
        self.assertEqual(
            _output_nominal_fps(15.0, 1.0001),
            Fraction(150000, 10001),
        )

    def test_input_segments_exclude_unneeded_prefix(self):
        video = self._video_fixture()
        selected = _input_segments(video, 4, 8)
        self.assertEqual([item.index for item in selected], [1, 2])

    def test_input_seek_is_relative_to_first_retained_segment(self):
        video = self._video_fixture()
        self.assertEqual(_input_seek_seconds(video, 4), 0.0)

        frame = video.frames[5]
        shifted = list(video.frames)
        shifted[5] = FrameRef(
            index=frame.index,
            segment_index=frame.segment_index,
            local_index=frame.local_index,
            path=frame.path,
            pts=1802500,
            time_base_num=1,
            time_base_den=1000,
        )
        video = VirtualVideo(
            capture_dir=video.capture_dir,
            frames=tuple(shifted),
            segments=video.segments,
            width=video.width,
            height=video.height,
            codec=video.codec,
            nominal_fps=video.nominal_fps,
        )
        self.assertAlmostEqual(_input_seek_seconds(video, 5), 1.5)

    def test_timestamp_csv_omits_undecodable_source_frames(self):
        video = self._video_fixture()
        model = {
            "type": "affine",
            "utc_reference_s": 1000.0,
            "offset_s": 0.0,
            "rate": 1.0,
            "pts_reference_s": 0.0,
        }
        with TemporaryDirectory() as directory:
            path = Path(directory) / "timestamps.csv"
            rows = _write_timestamps_csv(
                path,
                video,
                3,
                5,
                {4},
                model,
                2800.0,
            )
            content = path.read_text(encoding="utf-8").splitlines()

        self.assertEqual(rows, 2)
        self.assertEqual(len(content), 3)
        self.assertIn("source_frame_index", content[0])
        self.assertTrue(content[1].startswith("0,0.000000000,3,"))
        self.assertTrue(content[2].startswith("1,0.200000000,5,"))
