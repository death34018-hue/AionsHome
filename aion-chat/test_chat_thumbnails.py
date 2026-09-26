import tempfile
import unittest
from pathlib import Path

from PIL import Image
from fastapi import FastAPI
from fastapi.testclient import TestClient


class ChatThumbnailTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.uploads = self.root / 'uploads'
        self.uploads.mkdir()
        Image.new('RGB', (3000, 2000), '#789abc').save(self.uploads / 'photo.jpg')

    def client(self):
        import chat_thumbnails
        app = FastAPI()
        app.include_router(chat_thumbnails.create_router({'/uploads/': self.uploads}, self.root / 'cache'))
        return TestClient(app)

    def test_thumbnail_is_small_cached_and_original_is_untouched(self):
        client = self.client()
        original = (self.uploads / 'photo.jpg').read_bytes()
        response = client.get('/api/chat-media/thumbnail', params={'src': '/uploads/photo.jpg'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['content-type'], 'image/webp')
        import io
        with Image.open(io.BytesIO(response.content)) as image:
            self.assertEqual(image.size, (480, 320))
        self.assertLess(len(response.content), len(original))
        self.assertEqual((self.uploads / 'photo.jpg').read_bytes(), original)
        cached = client.get('/api/chat-media/thumbnail', params={'src': '/uploads/photo.jpg'}, headers={'If-None-Match': response.headers['etag']})
        self.assertEqual(cached.status_code, 304)
        self.assertEqual(len(list((self.root / 'cache').glob('*.webp'))), 1)

    def test_only_allowed_image_paths_are_read(self):
        client = self.client()
        (self.root / 'private.jpg').write_bytes((self.uploads / 'photo.jpg').read_bytes())
        for source in ['/uploads/../private.jpg', '/uploads/%2e%2e/private.jpg', 'https://example.com/p.jpg', '/public/wallpaper/p.jpg', '/uploads/missing.jpg']:
            with self.subTest(source=source):
                self.assertIn(client.get('/api/chat-media/thumbnail', params={'src': source}).status_code, (400, 404))

    def test_changed_source_generates_a_new_thumbnail(self):
        client = self.client()
        first = client.get('/api/chat-media/thumbnail', params={'src': '/uploads/photo.jpg'})
        Image.new('RGB', (100, 200), 'red').save(self.uploads / 'photo.jpg')
        second = client.get('/api/chat-media/thumbnail', params={'src': '/uploads/photo.jpg'})
        self.assertNotEqual(first.headers['etag'], second.headers['etag'])
        self.assertNotEqual(first.content, second.content)

    def test_local_only_wallpaper_cannot_be_accessed_via_thumbnail_aliases(self):
        import chat_thumbnails
        public = self.root / 'public'
        (public / 'wallpaper').mkdir(parents=True)
        Image.new('RGB', (100, 100)).save(public / 'wallpaper' / 'private.jpg')
        app = FastAPI()
        app.include_router(chat_thumbnails.create_router({'/public/': public}, self.root / 'cache'))
        client = TestClient(app)
        for src in ['/public/wallpaper/private.jpg', '/public/x/../wallpaper/private.jpg', '/public//wallpaper/private.jpg', '/public/Wallpaper/private.jpg']:
            self.assertEqual(client.get('/api/chat-media/thumbnail', params={'src': src}).status_code, 404)


if __name__ == '__main__':
    unittest.main()
