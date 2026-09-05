import base64
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import image_gen
from album import AlbumStore
from test_album import sample_image
from routes import chatroom as chatroom_routes


class FakeImageGenResponse:
    status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        image_data = base64.b64encode(sample_image()).decode("ascii")
        return {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {
                                "inlineData": {
                                    "mimeType": "image/png",
                                    "data": image_data,
                                }
                            }
                        ]
                    }
                }
            ]
        }


class FakeAsyncClient:
    def __init__(self, *args, **kwargs):
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return FakeImageGenResponse()


class ImageGenerationModelTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.enterContext(patch.dict(image_gen.SETTINGS, {}, clear=True))

    async def test_generate_image_uses_gemini_flash_lite_image_endpoint(self):
        clients = []

        def client_factory(*args, **kwargs):
            client = FakeAsyncClient(*args, **kwargs)
            clients.append(client)
            return client

        with tempfile.TemporaryDirectory() as tmpdir:
            store = AlbumStore(Path(tmpdir))
            with (
                patch("image_gen.get_key", return_value="test-key"),
                patch("album.get_album_store", return_value=store),
                patch("image_gen.httpx.AsyncClient", new=client_factory),
            ):
                filename = await image_gen.generate_image("draw a tiny lantern")

            self.assertTrue(filename.startswith("album/"))
            self.assertEqual((store.images_dir / Path(filename).name).read_bytes(), sample_image())
            self.assertEqual(store.list_photos()["photos"][0]["prompt"], "draw a tiny lantern")

        url, kwargs = clients[0].calls[0]
        self.assertIn(
            "/v1beta/models/gemini-3.1-flash-lite-image:generateContent",
            url,
        )
        self.assertEqual(kwargs["json"]["contents"][0]["parts"], [{"text": "draw a tiny lantern"}])

    async def test_connor_selfie_uses_secondary_reference_image(self):
        clients = []

        def client_factory(*args, **kwargs):
            client = FakeAsyncClient(*args, **kwargs)
            clients.append(client)
            return client

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            default_ref = tmp_path / "default.jpg"
            secondary_ref = tmp_path / "secondary.jpg"
            default_ref.write_bytes(b"default reference")
            secondary_ref.write_bytes(b"secondary reference")

            with (
                patch("image_gen.get_key", return_value="test-key"),
                patch("album.get_album_store", return_value=AlbumStore(tmp_path)),
                patch("image_gen.REFERENCE_IMAGE_PATH", default_ref),
                patch("image_gen.SECONDARY_REFERENCE_IMAGE_PATH", secondary_ref),
                patch("image_gen.httpx.AsyncClient", new=client_factory),
            ):
                await image_gen.generate_image(
                    "take a warm selfie",
                    is_selfie=True,
                    source_identity="connor",
                )

        parts = clients[0].calls[0][1]["json"]["contents"][0]["parts"]
        self.assertEqual(parts[0], {"text": "take a warm selfie"})
        self.assertEqual(
            parts[1]["inlineData"]["data"],
            base64.b64encode(b"secondary reference").decode("ascii"),
        )

    async def test_connor_selfie_does_not_fall_back_to_default_reference_image(self):
        clients = []

        def client_factory(*args, **kwargs):
            client = FakeAsyncClient(*args, **kwargs)
            clients.append(client)
            return client

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            default_ref = tmp_path / "default.jpg"
            missing_secondary_ref = tmp_path / "missing-secondary.jpg"
            default_ref.write_bytes(b"default reference")

            with (
                patch("image_gen.get_key", return_value="test-key"),
                patch("album.get_album_store", return_value=AlbumStore(tmp_path)),
                patch("image_gen.REFERENCE_IMAGE_PATH", default_ref),
                patch("image_gen.SECONDARY_REFERENCE_IMAGE_PATH", missing_secondary_ref),
                patch("image_gen.httpx.AsyncClient", new=client_factory),
            ):
                await image_gen.generate_image(
                    "take a warm selfie",
                    is_selfie=True,
                    source_identity="connor",
                )

        parts = clients[0].calls[0][1]["json"]["contents"][0]["parts"]
        self.assertEqual(parts, [{"text": "take a warm selfie"}])


class ChatroomImageGenerationTests(unittest.IsolatedAsyncioTestCase):
    async def test_chatroom_image_gen_forwards_internal_sender_identity(self):
        calls = []

        async def fake_generate_image(prompt, is_selfie=False, source_identity=""):
            calls.append({
                "prompt": prompt,
                "is_selfie": is_selfie,
                "source_identity": source_identity,
            })
            return None

        with patch("image_gen.generate_image", new=fake_generate_image):
            await chatroom_routes._chatroom_image_gen(
                "room-1",
                "connor",
                "take a warm selfie",
                True,
            )

        self.assertEqual(calls, [{
            "prompt": "take a warm selfie",
            "is_selfie": True,
            "source_identity": "connor",
        }])


if __name__ == "__main__":
    unittest.main()
