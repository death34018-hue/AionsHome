"""Shared portraits: select only the cast, retain legacy selfies, never mix faces."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import image_gen


class ChatAnchorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.assertTrue(callable(getattr(image_gen, 'generate_chat_image', None)),
                        'Chat generation needs shared character selection')
        import image_anchors
        self.anchors_module = image_anchors
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cast = []
        for key, name, identity in [('u', '月亮', 'user'), ('a', '星星', 'aion'), ('c', '云朵', 'connor')]:
            (self.root / f'{key}.png').write_bytes(b'portrait')
            self.cast.append(dict(id=f'anchor_{key}', name=name, aliases='', appearance='银色头发',
                                  identity=identity, image=f'/uploads/{key}.png'))
        self.enterContext(patch.object(image_anchors, 'UPLOADS_DIR', self.root))
        self.enterContext(patch.object(image_anchors, 'load_anchors', AsyncMock(return_value=self.cast)))
        self.enterContext(patch.object(image_anchors, 'identity_names', return_value={
            'user': '月亮', 'aion': '星星', 'connor': '云朵'}))
        self.generate = self.enterContext(patch.object(image_gen, 'generate_image', AsyncMock(return_value='album/test.png')))

    async def test_explicit_single_pair_trio_and_no_people(self):
        for ids in [('u',), ('u', 'c'), ('u', 'a', 'c'), ()]:
            with self.subTest(ids=ids):
                self.generate.reset_mock()
                cast = ','.join('anchor_'+key for key in ids) or 'none'
                await image_gen.generate_chat_image(f'characters={cast} | At the beach', source_identity='connor')
                kwargs = self.generate.call_args.kwargs
                self.assertEqual([Path(ref['path']).stem for ref in kwargs.get('references', [])], list(ids))
                self.assertNotIn('characters=', self.generate.call_args.args[0])
                for key in ids:
                    self.assertIn(next(a['name'] for a in self.cast if a['id'] == 'anchor_'+key), self.generate.call_args.args[0])
                self.assertEqual(kwargs['source_identity'], 'connor')

    async def test_selfie_uses_bound_identity_or_legacy_only_when_unbound(self):
        await image_gen.generate_chat_image('A warm selfie', is_selfie=True, source_identity='connor')
        self.assertEqual(self.generate.call_args.kwargs['references'][0]['name'], '云朵')
        self.assertTrue(self.generate.call_args.kwargs['is_selfie'])
        self.cast.clear()
        self.generate.reset_mock()
        await image_gen.generate_chat_image('A warm selfie', is_selfie=True, source_identity='connor')
        self.generate.assert_awaited_once_with('A warm selfie', is_selfie=True, source_identity='connor', raise_errors=True)

    async def test_names_select_the_same_photos_as_ids_and_ambiguous_names_fail(self):
        await image_gen.generate_chat_image('characters=云朵,星星 | Walking together', is_selfie=True)
        self.assertEqual([r['name'] for r in self.generate.call_args.kwargs['references']], ['云朵', '星星'])
        self.cast[0]['name'] = '云朵'
        with self.assertRaises(ValueError):
            await image_gen.generate_chat_image('characters=云朵 | Walking')
        await image_gen.generate_chat_image('characters=anchor_c | Walking')
        self.assertEqual(Path(self.generate.call_args.kwargs['references'][0]['path']).stem, 'c')

    async def test_missing_bound_photo_and_unknown_character_do_not_generate(self):
        (self.root/'c.png').unlink()
        for prompt, selfie in [('A selfie', True), ('characters=anchor_unknown | A portrait', False)]:
            with self.subTest(prompt=prompt), self.assertRaises(ValueError):
                await image_gen.generate_chat_image(prompt, is_selfie=selfie, source_identity='connor')
        self.generate.assert_not_awaited()

    async def test_prompt_resolves_configured_names_aliases_and_speaker(self):
        self.cast[2].pop('identity')
        self.cast[2].update(name='Cloud', aliases='云朵，阿云')
        from capabilities import build_capability_prompt_items
        with patch('capabilities.is_capability_enabled', side_effect=lambda key: key == 'image_gen'):
            for who, label in [('connor', 'Cloud（别名：云朵，阿云'), ('aion', '星星（别名：无')]:
                items = await build_capability_prompt_items('月亮', who=who)
                images = [item for item in items if '[SELFIE:' in item or '[DRAW:' in item]
                self.assertEqual(len(images), 1)
                self.assertIn(label + '；当前发言者（你）', images[0])
                self.assertNotIn('anchor_', images[0])
                self.assertIn('characters=none', images[0])
                self.assertIn('[SELFIE: English scene prompt]', images[0])
                await image_gen.generate_chat_image('A selfie', is_selfie=True, source_identity=who)
                references = self.generate.call_args.kwargs['references']
                self.assertEqual([ref['name'] for ref in references], ['Cloud' if who == 'connor' else '星星'])
            with patch.object(self.anchors_module, 'load_anchors', AsyncMock(return_value=[])):
                items = await build_capability_prompt_items('月亮')
                images = [item for item in items if '[SELFIE:' in item or '[DRAW:' in item]
                self.assertEqual(len(images), 1)
                self.assertIn('[SELFIE: English scene prompt]', images[0])
                self.assertIn('[DRAW: English scene prompt]', images[0])
                self.assertNotIn('characters=', images[0])

    async def test_private_and_group_routes_forward_character_selection(self):
        from routes import chat, chatroom
        with patch('image_gen.generate_chat_image', AsyncMock(return_value=None)) as generate, \
                patch.object(chat.manager, 'broadcast', AsyncMock()), \
                patch.object(chatroom, '_save_msg', AsyncMock()):
            await chat._do_image_gen('private', 'message', 'characters=anchor_u | Portrait', False)
            await chatroom._chatroom_image_gen('group', 'connor', 'characters=anchor_u,anchor_c | Together', False)
        self.assertEqual(generate.await_count, 2)
        self.assertEqual(generate.call_args.kwargs['source_identity'], 'connor')

    async def test_missing_reference_is_visible_in_private_and_group_chat(self):
        from routes import chat, chatroom
        with patch('image_gen.generate_chat_image', AsyncMock(side_effect=ValueError('人物锚点照片失效'))), \
                patch.object(chat.manager, 'broadcast', AsyncMock()) as broadcast, \
                patch.object(chatroom, '_save_msg', AsyncMock()) as save:
            await chat._do_image_gen('private', 'message', 'Portrait', True)
            await chatroom._chatroom_image_gen('group', 'connor', 'Portrait', True)
        self.assertIn('照片失效', broadcast.call_args.args[0]['data']['error'])
        self.assertIn('照片失效', save.call_args.args[2])

    async def test_generation_timeout_and_empty_result_are_visible(self):
        from routes import chat, chatroom
        for failure in (TimeoutError(), None):
            with self.subTest(failure=failure), \
                    patch('image_gen.generate_chat_image', AsyncMock(side_effect=failure, return_value=None)), \
                    patch.object(chat.manager, 'broadcast', AsyncMock()) as broadcast, \
                    patch.object(chatroom, '_save_msg', AsyncMock()) as save:
                await chat._do_image_gen('private', 'message', 'Portrait', True)
                await chatroom._chatroom_image_gen('group', 'connor', 'Portrait', True)
                save.assert_awaited_once()
                if failure:
                    self.assertIn('超时', broadcast.call_args.args[0]['data']['error'])
                    self.assertIn('超时', save.call_args.args[2])

    async def test_new_selfie_references_suppress_the_old_extra_photo(self):
        from test_image_gen import FakeAsyncClient
        from test_album import sample_image
        client = FakeAsyncClient()
        # Exercise the real shared provider entry, outside the generation mock.
        with patch.dict(image_gen.SETTINGS, {}, clear=True), \
                patch('image_gen.get_key', return_value='test-key'), \
                patch('image_gen.httpx.AsyncClient', return_value=client), \
                patch('image_gen.save_generated_image', AsyncMock(return_value='album/test.png')), \
                patch('image_gen._selfie_reference_path', side_effect=AssertionError('must not append legacy face')):
            # The decorated real function is saved before the fixture patches it.
            (self.root/'u.png').write_bytes(sample_image())
            await REAL_GENERATE('Portrait', is_selfie=True,
                                references=[{'name': '月亮', 'path': str(self.root/'u.png')}])
        parts = client.calls[0][1]['json']['contents'][0]['parts']
        self.assertEqual(sum('inlineData' in part for part in parts), 1)


REAL_GENERATE = image_gen.generate_image


class AnchorStorageTests(unittest.IsolatedAsyncioTestCase):
    async def test_upload_and_replace_anchor_without_chat_uploads(self):
        import aiosqlite
        import httpx
        import io
        import image_anchors
        import theater_studio as studio
        from fastapi import FastAPI
        from PIL import Image
        from test_album import sample_image
        app = FastAPI()
        app.include_router(studio.router)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            connect = lambda: aiosqlite.connect(root/'test.db')
            with patch.object(studio, 'get_db', connect), patch.object(image_anchors, 'get_db', connect), \
                    patch.object(image_anchors, 'IMAGE_ANCHORS_DIR', root/'portraits'), \
                    patch.object(studio, 'UPLOADS_DIR', root/'absent-uploads'):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
                    fields = {'fields': '{"name":"人物","identity":"connor"}'}
                    photo = {'file': ('ignore-this-name.png', sample_image(), 'image/png')}
                    r = await client.put('/studio/anchors/one/photo', data=fields, files=photo)
                    self.assertEqual(r.status_code, 200, r.text)
                    self.assertEqual(r.json()['image'], '/image-anchors/人物.png')
                    bad = {'file': ('invalid.png', b'broken', 'image/png')}
                    self.assertEqual((await client.put('/studio/anchors/one/photo', data=fields, files=bad)).status_code, 400)
                    self.assertEqual((await studio.get_anchors())[0], r.json())
                    new = io.BytesIO()
                    Image.new('RGB', (6, 4), 'blue').save(new, 'PNG')
                    changed = await client.put('/studio/anchors/one/photo', data=fields,
                                               files={'file': ('next.png', new.getvalue(), 'image/png')})
                    self.assertEqual(changed.status_code, 200, changed.text)
                    updated, = await studio.get_anchors()
                    self.assertEqual(updated['id'], r.json()['id'])
                    self.assertEqual(studio.reference_path(updated['image']).read_bytes(), new.getvalue())
                    self.assertFalse((root/'absent-uploads').exists())

    async def test_theater_and_chat_read_the_same_records_and_keep_identity(self):
        import aiosqlite
        import image_anchors
        import theater_studio as studio
        from test_album import sample_image
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/'photo.png').write_bytes(sample_image())
            connect = lambda: aiosqlite.connect(root/'test.db')
            with patch.object(studio, 'get_db', connect), \
                    patch.object(image_anchors, 'get_db', connect), \
                    patch.object(image_anchors, 'IMAGE_ANCHORS_DIR', root/'portraits', create=True), \
                    patch.object(studio, 'UPLOADS_DIR', root):
                self.assertEqual(await image_anchors.load_anchors(), [])
                await studio.put_anchor('person', studio.Anchor(name='人物', image='/uploads/photo.png', identity='user'))
                self.assertEqual(await studio.get_anchors(), await image_anchors.load_anchors())
                await studio.put_anchor('person', studio.Anchor(name='新名字', image='/uploads/photo.png'))
                shared = await image_anchors.load_anchors()
                self.assertEqual([(a['name'], a['identity']) for a in shared], [('新名字', 'user')])
                (root/'photo.png').unlink()
                self.assertTrue(shared[0]['image'].startswith('/image-anchors/'))
                self.assertEqual(studio.reference_path(shared[0]['image']).read_bytes(), sample_image())

    async def test_existing_anchors_migrate_once_and_survive_uploads_cleanup(self):
        import aiosqlite
        import image_anchors
        import theater_studio as studio
        from test_album import sample_image
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            uploads = root/'uploads'
            uploads.mkdir()
            source = uploads/'one.png'
            source.write_bytes(sample_image())
            connect = lambda: aiosqlite.connect(root/'test.db')
            with patch.object(image_anchors, 'get_db', connect), patch.object(studio, 'get_db', connect), \
                    patch.object(image_anchors, 'UPLOADS_DIR', uploads), \
                    patch.object(image_anchors, 'IMAGE_ANCHORS_DIR', root/'portraits', create=True):
                await studio.save(dict(id='anchor_a', kind='anchor', conv_id='', name='人物',
                                       identity='connor', image='/uploads/one.png'))
                await image_anchors.migrate_anchor_images()
                source.unlink()
                await image_anchors.migrate_anchor_images()
                record, = await studio.get_anchors()
                self.assertEqual((record['id'], record['identity'], record['image']),
                                 ('anchor_a', 'connor', '/image-anchors/人物.png'))
                self.assertEqual(image_anchors.reference_path(record['image']).read_bytes(), sample_image())
                self.assertEqual(len(list((root/'portraits').iterdir())), 1)

if __name__ == '__main__':
    unittest.main()
