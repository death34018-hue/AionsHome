import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def read_static(name: str) -> str:
    return (ROOT / "static" / name).read_text(encoding="utf-8")


class ImageLongPressSaveTests(unittest.TestCase):
    def test_private_chat_images_have_long_press_save_actions(self):
        js = read_static("chat.js")

        self.assertIn("function imageInteractionAttrs()", js)
        self.assertIn('onpointerdown="startImageLongPress(event, ChatImagePreview.original(this))"', js)
        self.assertIn('oncontextmenu="showImageSaveMenu(ChatImagePreview.original(this)); return false;"', js)
        self.assertGreaterEqual(js.count("imageInteractionAttrs()"), 3)

    def test_chatroom_images_have_long_press_save_actions(self):
        js = read_static("chatroom.js")

        self.assertIn("function imageInteractionAttrs()", js)
        self.assertIn('onpointerdown="startImageLongPress(event, ChatImagePreview.original(this))"', js)
        self.assertIn('oncontextmenu="showImageSaveMenu(ChatImagePreview.original(this)); return false;"', js)
        self.assertGreaterEqual(js.count("imageInteractionAttrs()"), 3)

    def test_both_pages_use_android_image_saver_bridge(self):
        self.assertIn("AionImageSaver", read_static("chat.js"))
        self.assertIn("AionImageSaver", read_static("chatroom.js"))

    def test_large_viewer_images_also_support_long_press_save(self):
        chat_js = read_static("chat.js")
        chatroom_js = read_static("chatroom.js")
        self.assertIn("function bindImageSaveOnly", chat_js)
        self.assertIn("Date.now() < imageLongPressSuppressClickUntil", chat_js)
        self.assertIn("bindImageSaveOnly(overlay.querySelector('img'), () => overlay.remove())", chat_js)
        self.assertIn("function bindImageSaveOnly", chatroom_js)
        self.assertIn("Date.now() < imageLongPressSuppressClickUntil", chatroom_js)
        self.assertIn("bindImageSaveOnly(document.getElementById('viewerImg'), closeImageViewer)", chatroom_js)


if __name__ == "__main__":
    unittest.main()
