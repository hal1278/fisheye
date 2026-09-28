from unittest import TestCase

from fisheye.roi.state import RoiState


class RoiStateTest(TestCase):
    def test_pointer_position_is_clamped(self):
        roi = RoiState(960, 720, width=32, height=32)
        roi.position_at(0, 0)
        self.assertEqual((roi.x, roi.y), (0, 0))
        roi.position_at(960, 720)
        self.assertEqual((roi.x, roi.y), (928, 688))

    def test_xy_moves_only_when_locked(self):
        roi = RoiState(100, 100, x=10, y=10, width=20, height=20)
        roi.move_x(1)
        self.assertEqual(roi.x, 10)
        roi.lock()
        roi.move_x(1)
        roi.move_y(-1)
        self.assertEqual((roi.x, roi.y), (11, 9))

    def test_resize_preserves_center_when_possible(self):
        roi = RoiState(100, 100, x=40, y=40, width=20, height=20)
        roi.resize_width(10)
        roi.resize_height(10)
        self.assertEqual((roi.x, roi.y, roi.width, roi.height), (35, 35, 30, 30))

    def test_resize_clamps_to_frame(self):
        roi = RoiState(100, 80, x=90, y=70, width=10, height=10)
        roi.resize_width(1000)
        roi.resize_height(1000)
        self.assertEqual((roi.x, roi.y, roi.width, roi.height), (0, 0, 100, 80))

    def test_copy_text(self):
        roi = RoiState(960, 720, x=512, y=608, width=32, height=32, locked=True)
        self.assertEqual(roi.copy_text(), "--roi 512,608,32,32")
