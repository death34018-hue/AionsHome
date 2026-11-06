import base64
import io
import json
import random
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import aiosqlite
from PIL import Image

import ai_providers
import context_builder
from database import ensure_timeline_indexes


def message(sender, timestamp, attachments=(), channel='group'):
    return {'source': channel, 'sender': sender, 'content': '', 'created_at': timestamp,
            'attachments': list(attachments)}


def image_urls(history):
    return [attachment for entry in history for attachment in entry.get('attachments', [])]


class GeneratedImageContextTests(unittest.TestCase):
    def test_all_generated_images_share_one_user_turn_for_both_actors(self):
        first = '/uploads/album/a.png'
        second = '/uploads/album/b.png'
        third = '/uploads/album/c.png'
        rows = [
            message('user', 1),
            message('aion', 2, [first]),
            message('connor', 3, [
                {'type': 'image', 'url': second, 'generated_at': 3},
                {'type': 'image', 'url': third, 'generated_at': 3}]),
        ]
        with patch('ai_providers.prepare_image_context_attachment',
                   side_effect=ai_providers._attachment_file_ref):
            self.assertEqual(image_urls(context_builder.render_merged_timeline(rows, 'aion')), [])
            rows.append(message('user', 4))
            for actor in ('aion', 'connor'):
                self.assertEqual(image_urls(context_builder.render_merged_timeline(rows, actor)),
                                 [first, second, third])
            rows.extend([message('aion', 5), message('connor', 6), message('user', 7, channel='private')])
            self.assertEqual(image_urls(context_builder.render_merged_timeline(rows, 'connor')),
                             [first, second, third])
            self.assertEqual(image_urls(context_builder.render_merged_timeline(
                rows, 'aion', include_image_attachments=False)), [])
            rows.append(message('user', 8))
            for actor in ('aion', 'connor'):
                history = context_builder.render_merged_timeline(rows, actor)
                self.assertEqual(image_urls(history), [])
                self.assertTrue(any('发送了2张图片' in entry['content'] for entry in history))

    def test_large_picture_uses_one_compressed_copy_in_api_gemini_and_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'large.png'
            image = Image.frombytes('RGB', (1800, 1400), random.Random(7).randbytes(1800 * 1400 * 3))
            image.save(source)
            original = source.read_bytes()
            self.assertGreater(len(original), 2 * 1024 * 1024)
            with patch.object(ai_providers, 'ALBUM_IMAGES_DIR', root), \
                    patch.object(ai_providers, 'UPLOADS_DIR', root):
                rows = [message('connor', 1, ['/uploads/album/large.png']), message('user', 2)]
                history = context_builder.render_merged_timeline(rows, 'aion')
                urls = image_urls(history)
                self.assertEqual(len(urls), 1)
                self.assertTrue(urls[0].startswith('/uploads/image-context/'))
                compressed = ai_providers._resolve_attachment_path(urls[0])
                content = compressed.read_bytes()
                self.assertLessEqual(len(content), 350 * 1024)
                with Image.open(io.BytesIO(content)) as picture:
                    self.assertEqual(picture.format, 'JPEG')
                    self.assertLessEqual(max(picture.size), 1280)
                api = ai_providers.build_multimodal_messages(history)
                api_images = [part['image_url']['url'] for entry in api if isinstance(entry['content'], list)
                              for part in entry['content'] if part.get('type') == 'image_url']
                self.assertEqual(base64.b64decode(api_images[0].split(',', 1)[1]), content)
                gemini = ai_providers.build_gemini_contents(history)
                gemini_images = [part['inline_data'] for entry in gemini for part in entry['parts']
                                 if 'inline_data' in part]
                self.assertEqual(base64.b64decode(gemini_images[0]['data']), content)
                self.assertEqual(ai_providers._collect_cli_image_paths(history), (str(compressed.resolve()),))
                self.assertIn(str(compressed.resolve()).replace('\\', '/'),
                              ai_providers._build_cli_prompt(history))
                cached_at = compressed.stat().st_mtime_ns
                self.assertEqual(image_urls(context_builder.render_merged_timeline(rows, 'connor')), urls)
                self.assertEqual(compressed.stat().st_mtime_ns, cached_at)
                self.assertEqual(source.read_bytes(), original)


class RetriedImageContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_old_retry_enters_the_context_window_at_completion_and_expires_next_turn(self):
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / 'chat.db'
            db = lambda: aiosqlite.connect(database)
            photo = {'type': 'image', 'url': '/uploads/album/retried.png', 'generated_at': 100}
            async with db() as conn:
                await conn.executescript('''
                    CREATE TABLE messages(id,conv_id,role,content,created_at,attachments);
                    CREATE TABLE chatroom_rooms(id,type);
                    CREATE TABLE chatroom_messages(id,room_id,sender,content,created_at,attachments);
                    INSERT INTO chatroom_rooms VALUES ('room','group'),('private','connor_1v1');
                ''')
                await ensure_timeline_indexes(conn)
                await conn.execute('INSERT INTO chatroom_messages VALUES (?,?,?,?,?,?)',
                                   ('old', 'room', 'connor', '', 1, json.dumps([photo])))
                await conn.executemany('INSERT INTO chatroom_messages VALUES (?,?,?,?,?,?)', [
                    (f'msg-{i}', 'room', 'user', '早些时候的消息', i, '[]') for i in range(2, 50)])
                await conn.execute('INSERT INTO chatroom_messages VALUES (?,?,?,?,?,?)',
                                   ('next', 'room', 'user', '看到刚出的图了', 101, '[]'))
                await conn.commit()
            with patch.object(context_builder, 'get_db', db), \
                    patch('ai_providers.prepare_image_context_attachment',
                          side_effect=ai_providers._attachment_file_ref):
                for actor in ('aion', 'connor'):
                    merged = await context_builder.fetch_merged_timeline(actor, 3, room_id='room')
                    self.assertEqual([row['id'] for row in merged], ['msg-49', 'old', 'next'])
                    self.assertEqual(image_urls(context_builder.render_merged_timeline(merged, actor)),
                                     ['/uploads/album/retried.png'])
                async with db() as conn:
                    self.assertEqual((await (await conn.execute(
                        "SELECT created_at FROM chatroom_messages WHERE id='old'")).fetchone())[0], 1)
                    await conn.execute('INSERT INTO chatroom_messages VALUES (?,?,?,?,?,?)',
                                       ('later', 'room', 'user', '聊下一件事', 102, '[]'))
                    await conn.commit()
                for actor in ('aion', 'connor'):
                    merged = await context_builder.fetch_merged_timeline(actor, 3, room_id='room')
                    self.assertEqual(image_urls(context_builder.render_merged_timeline(merged, actor)), [])
