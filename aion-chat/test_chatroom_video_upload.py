import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import UploadFile
from starlette.datastructures import Headers

from routes.chatroom import chatroom_upload


class ChatroomVideoUploadTests(unittest.IsolatedAsyncioTestCase):
    async def test_chatroom_accepts_phone_video_larger_than_old_image_limit(self):
        payload = b"v" * (21 * 1024 * 1024)
        upload = UploadFile(
            io.BytesIO(payload),
            filename="latest.mp4",
            headers=Headers({"content-type": "video/mp4"}),
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            upload_dir = Path(tmpdir)
            with patch("routes.chatroom._cr_upload_dir", return_value=upload_dir):
                result = await chatroom_upload(upload)

            self.assertNotIn("error", result)
            self.assertEqual(result["type"], "video/mp4")
            self.assertEqual((upload_dir / Path(result["url"]).name).read_bytes(), payload)


if __name__ == "__main__":
    unittest.main()
