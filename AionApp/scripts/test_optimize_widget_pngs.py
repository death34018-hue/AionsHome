import tempfile
import unittest
from pathlib import Path
import sys

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
from optimize_widget_pngs import optimize_png


class OptimizeWidgetPngTests(unittest.TestCase):
    def test_large_character_is_resized_to_requested_long_edge(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "Character-状态.png"
            image = Image.new("RGBA", (1600, 800), (30, 80, 120, 180))
            image.save(path, compress_level=0)

            result = optimize_png(path, max_edge=1000)

            with Image.open(path) as optimized:
                self.assertEqual(optimized.size, (1000, 500))
                pixel = optimized.getpixel((500, 250))
                self.assertTrue(all(abs(actual - expected) <= 1
                                    for actual, expected in zip(pixel[:3], (30, 80, 120))))
                self.assertEqual(pixel[3], 180)
            self.assertLess(result["after_bytes"], result["before_bytes"])

    def test_small_image_keeps_resolution_and_exact_pixels(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "small.png"
            image = Image.new("RGBA", (40, 30), (1, 2, 3, 4))
            image.putpixel((10, 12), (200, 100, 50, 250))
            expected = image.tobytes()
            image.save(path, compress_level=0)

            optimize_png(path, max_edge=1000)

            with Image.open(path) as optimized:
                self.assertEqual(optimized.size, (40, 30))
                self.assertEqual(optimized.convert("RGBA").tobytes(), expected)


if __name__ == "__main__":
    unittest.main()
