import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent))

from tts import _has_unclosed_tag, _strip_tags


class TtsInnerMonologueFallbackTests(unittest.TestCase):
    def test_mixed_ascii_and_corner_brackets_are_a_complete_strippable_tag(self):
        text = "笑死了。【心里嘀咕：睡饱了才有力气窝进怀里。]"

        self.assertFalse(_has_unclosed_tag(text))
        self.assertEqual(_strip_tags(text), "笑死了。")


if __name__ == "__main__":
    unittest.main()
