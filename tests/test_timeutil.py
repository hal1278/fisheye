from unittest import TestCase

from fisheye.timeutil import parse_elapsed, parse_roi


class TimeUtilTest(TestCase):
    def test_parse_elapsed(self):
        self.assertEqual(parse_elapsed("00:53:00"), 3180)
        self.assertEqual(parse_elapsed("53:00"), 3180)
        self.assertAlmostEqual(parse_elapsed("1:02:03.5"), 3723.5)

    def test_parse_elapsed_rejects_invalid(self):
        for value in ("1:60:00", "-1", "a:b"):
            with self.assertRaises(ValueError):
                parse_elapsed(value)

    def test_parse_roi(self):
        self.assertEqual(parse_roi("512,608,32,32"), (512, 608, 32, 32))
        with self.assertRaises(ValueError):
            parse_roi("1,2,0,4")
