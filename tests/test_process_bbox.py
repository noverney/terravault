"""Tests for process API bbox helpers."""

from __future__ import annotations

import unittest

from terravault.process_api import bbox_from_center


class TestBBoxFromCenter(unittest.TestCase):
    def test_bbox_wraps_center_point(self):
        bbox = bbox_from_center(
            center_lon=7.5886,
            center_lat=47.5596,
            width=1024,
            height=1024,
            resolution_m=10,
        )

        self.assertEqual(len(bbox), 4)
        self.assertLess(bbox[0], 7.5886)
        self.assertLess(bbox[1], 47.5596)
        self.assertGreater(bbox[2], 7.5886)
        self.assertGreater(bbox[3], 47.5596)


if __name__ == "__main__":
    unittest.main()
