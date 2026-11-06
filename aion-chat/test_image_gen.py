import base64
import io
import json
import sys
import tempfile
import unittest
from email.parser import BytesParser
from email.policy import default
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from PIL import Image

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

    async def test_gif_chat_command_preserves_animation_and_reference_retry(self):
        frames = [Image.new('RGB', (16, 16), color) for color in ('red', 'blue')]
        output = io.BytesIO()
        frames[0].save(output, 'GIF', save_all=True, append_images=frames[1:], duration=200, loop=0)
        gif = output.getvalue()
        for with_reference in (False, True):
            with self.subTest(with_reference=with_reference), tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir)
                store = AlbumStore(root / 'album')
                reference = root / 'reference.png'
                reference.write_bytes(sample_image())
                cast = [{'id': 'anchor_test', 'name': '角色甲', 'aliases': '', 'identity': '',
                         'appearance': '', 'image': '/uploads/reference.png'}]
                requests, snapshots = [], []

                def respond(request):
                    requests.append(request)
                    return httpx.Response(200, json={'output_format': 'gif', 'data': [
                        {'b64_json': base64.b64encode(gif).decode('ascii')}]})

                client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
                with (
                    patch.dict(image_gen.SETTINGS, image_gen_provider='existing',
                               image_gen_base_url='https://api.openai.com/v1',
                               image_gen_api_key='test-key', image_gen_model='gpt-image-2.6-sunburst'),
                    patch('image_gen.httpx.AsyncClient', return_value=client),
                    patch('album.get_album_store', return_value=store),
                    patch('image_anchors.load_anchors', AsyncMock(return_value=cast)),
                    patch('image_anchors.UPLOADS_DIR', root),
                ):
                    filename = await image_gen.generate_chat_image(
                        'format=gif | characters=' + ('anchor_test' if with_reference else 'none')
                        + ' | Blink slowly in a seamless loop.', source_identity='connor',
                        on_prepared=snapshots.append if with_reference else None)

                request = requests[0]
                if with_reference:
                    self.assertEqual(request.url.path, '/v1/images/edits')
                    mime = BytesParser(policy=default).parsebytes(
                        ('Content-Type: ' + request.headers['content-type'] + '\r\n\r\n').encode() + request.content)
                    fields = {part.get_param('name', header='content-disposition'): part.get_payload(decode=True).decode()
                              for part in mime.iter_parts() if not part.get_filename()}
                    self.assertTrue(snapshots)
                    self.assertIn(sample_image(), request.content)
                else:
                    self.assertEqual(request.url.path, '/v1/images/generations')
                    fields = json.loads(request.content)
                self.assertEqual(fields['output_format'], 'gif')
                self.assertEqual(int(fields['frame_count']), 9)
                self.assertEqual(int(fields['frame_duration_ms']), 200)
                self.assertNotIn('format=gif', fields['prompt'])
                self.assertTrue(filename.endswith('.gif'))
                saved = store.images_dir / Path(filename).name
                self.assertEqual(saved.read_bytes(), gif)
                with Image.open(saved) as animation:
                    self.assertEqual(animation.n_frames, 2)

    async def test_relay_gpt_image_uses_generations_and_saves_base64(self):
        requests = []
        model = '[图-次-0.02￥]gpt-image-2.5-flare'

        def respond(request):
            requests.append(request)
            return httpx.Response(200, json={'data': [
                {'b64_json': base64.b64encode(sample_image()).decode('ascii')}]})

        with tempfile.TemporaryDirectory() as tmpdir:
            store = AlbumStore(Path(tmpdir))
            client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
            with (
                patch.dict(image_gen.SETTINGS, image_gen_provider='existing',
                           image_gen_base_url='https://relay.invalid/v1/',
                           image_gen_api_key='test-key', image_gen_model=model),
                patch('image_gen.httpx.AsyncClient', return_value=client),
                patch('album.get_album_store', return_value=store),
            ):
                filename = await image_gen.generate_image('画一盏小灯')

            self.assertEqual(str(requests[0].url), 'https://relay.invalid/v1/images/generations')
            self.assertEqual(json.loads(requests[0].content), {'model': model, 'prompt': '画一盏小灯', 'n': 1})
            self.assertEqual(len(requests), 1)
            self.assertTrue(filename)
            self.assertEqual((store.images_dir / Path(filename).name).read_bytes(), sample_image())

    async def test_retry_from_codex_to_relay_edits_uploads_original_anchors_in_order(self):
        import chatroom_image_retry as retry
        requests = []
        model = '[图-次-0.02￥]gpt-image-2.5-flare'

        def respond(request):
            requests.append(request)
            if request.method == 'POST':
                return httpx.Response(200, json={'data': [{'url': 'https://cdn.invalid/result.png'}]})
            return httpx.Response(200, content=sample_image())

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            store = AlbumStore(root / 'album')
            refs = []
            for index, name in enumerate(['角色甲', '角色乙']):
                path = root / f'{index}.png'
                path.write_bytes(name.encode('utf-8'))
                refs.append({'name': name, 'path': str(path)})
            with patch.dict(image_gen.SETTINGS, image_gen_provider='codex'):
                payload = await retry.snapshot('两人在窗边喝茶', refs, False, 'connor')
            for ref in refs:
                Path(ref['path']).unlink()
            client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
            with (
                patch.dict(image_gen.SETTINGS, image_gen_provider='existing',
                           image_gen_base_url='https://relay.invalid',
                           image_gen_api_key='test-key', image_gen_model=model),
                patch('image_gen.httpx.AsyncClient', return_value=client),
                patch('album.get_album_store', return_value=store),
            ):
                filename = await retry.generate(payload)

            self.assertEqual(str(requests[0].url), 'https://relay.invalid/v1/images/edits')
            mime = BytesParser(policy=default).parsebytes(
                ('Content-Type: ' + requests[0].headers['content-type'] + '\r\n\r\n').encode() + requests[0].content)
            fields, images = {}, []
            for part in mime.iter_parts():
                name = part.get_param('name', header='content-disposition')
                value = part.get_payload(decode=True)
                if part.get_filename():
                    images.append((name, value))
                else:
                    fields[name] = value.decode('utf-8')
            self.assertEqual(images, [('image[]', '角色甲'.encode()), ('image[]', '角色乙'.encode())])
            self.assertEqual(fields['model'], model)
            self.assertIn('参考图 1 对应人物：角色甲', fields['prompt'])
            self.assertIn('参考图 2 对应人物：角色乙', fields['prompt'])
            self.assertTrue(fields['prompt'].endswith('两人在窗边喝茶'))
            self.assertEqual(len(requests), 2)
            self.assertNotIn('authorization', requests[1].headers)
            self.assertTrue(filename)
            self.assertEqual((store.images_dir / Path(filename).name).read_bytes(), sample_image())
            self.assertEqual(store.list_photos()['photos'][0]['prompt'], '两人在窗边喝茶')

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
    async def test_api_rejection_shows_reason_without_response_secrets(self):
        for code, visible in (('moderation_blocked', '内容审核拒绝'), ('invalid_value', 'invalid_value')):
            with self.subTest(code=code):
                client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(
                    400, json={'error': {'code': code, 'message': 'private prompt and secret-test-key'}})))
                with (
                    patch.dict(image_gen.SETTINGS, {
                        'image_gen_provider': 'existing',
                        'image_gen_base_url': 'https://api.openai.com/v1',
                        'image_gen_api_key': 'secret-test-key',
                        'image_gen_model': 'gpt-image-2.6-sunburst',
                    }, clear=True),
                    patch('image_gen.httpx.AsyncClient', return_value=client),
                    patch('image_anchors.resolve_chat_references', AsyncMock(return_value=('Blink slowly.', []))),
                    patch.object(chatroom_routes, '_save_msg', new=AsyncMock()) as save,
                    patch('builtins.print') as output,
                    patch('system_logs._publish', new=AsyncMock()) as publish,
                ):
                    await chatroom_routes._chatroom_image_gen('room', 'aion', 'format=gif | Blink slowly.', False)

                message = save.call_args.args[2]
                self.assertIn('HTTP 400', message)
                self.assertIn(visible, message)
                self.assertIn('retry_inputs', save.call_args.kwargs)
                diagnostics = message + str(output.call_args_list) + publish.call_args.args[0]['error_text']
                self.assertNotIn('private prompt', diagnostics)
                self.assertNotIn('secret-test-key', diagnostics)

    async def test_api_timeout_waits_ten_minutes_and_keeps_retry_reason(self):
        import chatroom_image_retry as retry
        client_type = httpx.AsyncClient
        requests = []

        def respond(request):
            requests.append(request)
            raise httpx.ReadTimeout('test timeout', request=request)

        def client_factory(*args, **kwargs):
            return client_type(*args, transport=httpx.MockTransport(respond), **kwargs)

        for prefix in ('', 'format=gif | '):
            with (
                self.subTest(format='gif' if prefix else 'png'),
                patch.dict(image_gen.SETTINGS, {
                    'image_gen_provider': 'existing',
                    'image_gen_base_url': 'https://api.openai.com/v1',
                    'image_gen_api_key': 'test-key',
                    'image_gen_model': 'gpt-image-2.6-sunburst',
                }, clear=True),
                patch('image_gen.httpx.AsyncClient', new=client_factory),
                patch('image_anchors.resolve_chat_references', AsyncMock(return_value=('Blink slowly.', []))),
                patch.object(chatroom_routes, '_save_msg', new=AsyncMock()) as save,
            ):
                await chatroom_routes._chatroom_image_gen('room', 'connor', prefix + 'Blink slowly.', False)

                self.assertEqual(save.call_args.args[2], '图片生成超时')
                self.assertEqual(requests[-1].extensions['timeout']['read'], 600)
                payload = save.call_args.kwargs['retry_inputs']
                with self.assertRaisesRegex(TimeoutError, '600'):
                    await retry.generate(payload)

    async def test_chatroom_image_gen_forwards_internal_sender_identity(self):
        calls = []

        async def fake_generate_image(prompt, is_selfie=False, source_identity="", *, raise_errors=False):
            calls.append({
                "prompt": prompt,
                "is_selfie": is_selfie,
                "source_identity": source_identity,
            })
            return None

        with patch.dict(image_gen.SETTINGS, {'image_gen_provider': 'existing'}), \
                patch("image_gen.generate_image", new=fake_generate_image), \
                patch.object(chatroom_routes, '_save_msg', new=AsyncMock()):
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
