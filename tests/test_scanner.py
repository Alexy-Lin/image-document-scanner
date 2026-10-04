import re
import unittest

import numpy as np

from scanner import ScanOptions, enhance_document, make_pdf, order_points, perspective_warp


class ScannerTests(unittest.TestCase):
    def test_order_points_handles_normal_and_45_degree_pages(self):
        rectangle = order_points([(100, 100), (0, 0), (0, 100), (100, 0)])
        np.testing.assert_array_equal(
            rectangle,
            np.array([[0, 0], [100, 0], [100, 100], [0, 100]], dtype=np.float32),
        )

        diamond = order_points([(50, 100), (100, 50), (50, 0), (0, 50)])
        np.testing.assert_array_equal(
            diamond,
            np.array([[50, 0], [100, 50], [50, 100], [0, 50]], dtype=np.float32),
        )

    def test_order_points_rejects_duplicate_and_concave_points(self):
        with self.assertRaises(ValueError):
            order_points([(0, 0), (100, 0), (100, 100), (100, 100)])
        with self.assertRaises(ValueError):
            order_points([(0, 0), (100, 0), (50, 40), (0, 100)])

    def test_perspective_warp_returns_rgb_rectangle(self):
        image = np.zeros((160, 120, 3), dtype=np.uint8)
        result = perspective_warp(image, [(10, 10), (109, 10), (109, 149), (10, 149)])
        self.assertEqual(result.shape, (139, 99, 3))
        self.assertEqual(result.dtype, np.uint8)

    def test_higher_ink_retention_keeps_lighter_gray_marks(self):
        image = np.full((240, 240, 3), 245, dtype=np.uint8)
        image[116:124, 116:124] = 215

        low = enhance_document(image, ScanOptions(mode="纯白文档", ink_threshold=5))
        high = enhance_document(image, ScanOptions(mode="纯白文档", ink_threshold=60))

        np.testing.assert_array_equal(low[120, 120], [255, 255, 255])
        np.testing.assert_array_equal(high[120, 120], [215, 215, 215])

    def test_pdf_a4_orientation_follows_page_rotation(self):
        portrait = np.full((120, 80, 3), 255, dtype=np.uint8)
        pdf = make_pdf(
            [portrait, portrait],
            dpi=200,
            page_size="A4（按页方向）",
            page_rotations=[0, 90],
        )
        self.assertTrue(pdf.startswith(b"%PDF"))
        boxes = re.findall(rb"/MediaBox\s*\[([^]]+)\]", pdf)
        self.assertEqual(len(boxes), 2)
        dimensions = [[float(value) for value in box.split()[2:4]] for box in boxes]
        self.assertLess(dimensions[0][0], dimensions[0][1])
        self.assertGreater(dimensions[1][0], dimensions[1][1])


if __name__ == "__main__":
    unittest.main()
