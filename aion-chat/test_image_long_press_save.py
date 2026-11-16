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
        for page in ("chat.js", "chatroom.js"):
            self.assertIn("ChatImagePreview.showSaveMenu", read_static(page))
        self.assertIn("AionImageViewer.saveOriginal(url)", read_static("chat-image-preview.js"))
        self.assertIn("AionImageSaver", read_static("image-viewer.js"))



if __name__ == "__main__":
    unittest.main()
