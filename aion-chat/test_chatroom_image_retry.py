import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import aiosqlite


class ChatroomImageRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_restart_restores_interrupted_button_and_deleting_message_removes_inputs(self):
        import chatroom_image_retry as retry
        async with aiosqlite.connect(':memory:') as conn:
            await conn.execute('CREATE TABLE chatroom_messages(id PRIMARY KEY,content,attachments)')
            await retry.init_storage(conn)
            await conn.execute('INSERT INTO chatroom_messages VALUES (?,?,?)',
                               ('msg','正在重新生成图片',json.dumps([retry.marker(1, True)])))
            await conn.execute('INSERT INTO chatroom_image_retries VALUES (?,?)', ('msg',b'original'))
            await retry.init_storage(conn)
            row = await (await conn.execute('SELECT attachments FROM chatroom_messages')).fetchone()
            self.assertEqual(json.loads(row[0]), [retry.marker(1)])
            await conn.execute("DELETE FROM chatroom_messages WHERE id='msg'")
            self.assertIsNone(await (await conn.execute('SELECT snapshot FROM chatroom_image_retries')).fetchone())

    async def test_retry_keeps_original_inputs_and_replaces_only_the_failed_message(self):
        import chatroom_image_retry as retry
        import image_gen
        from routes import chatroom
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = lambda: aiosqlite.connect(root / 'chat.db')
            async with db() as conn:
                await conn.executescript("""
                    CREATE TABLE chatroom_messages(id TEXT PRIMARY KEY,room_id,sender,content,attachments,created_at,reasoning_content);
                    CREATE TABLE chatroom_rooms(id PRIMARY KEY,type,updated_at);
                    INSERT INTO chatroom_rooms VALUES ('room','group',0);
                """)
                await retry.init_storage(conn)
                await conn.commit()
            paths = [root/'a.png', root/'b.png']
            for path in paths:
                path.write_bytes(path.name.encode())
            refs = [{'name': p.stem, 'path': str(p)} for p in paths]
            calls = []
            started, finish, delivered = asyncio.Event(), asyncio.Event(), asyncio.Event()
            async def generate(prompt, *, references, is_selfie, source_identity, raise_errors, **flow):
                calls.append((prompt,is_selfie,source_identity,[(r['name'],Path(r['path']).read_bytes()) for r in references]))
                if len(calls) == 1:
                    raise TimeoutError()
                self.assertEqual(flow['flow_model'], 'grok_imagine_image_2_0')
                self.assertIsNone(flow['flow_job_id'])
                started.set()
                await finish.wait()
                return 'album/new.png'
            async def broadcast(manager, event, **kwargs):
                if event['data']['attachments'] == ['/uploads/album/new.png']:
                    delivered.set()
            with patch.object(chatroom, 'get_db', db), patch.object(chatroom, 'broadcast_synced', broadcast), \
                    patch('image_anchors.resolve_chat_references', new=AsyncMock(return_value=('exact scene',refs))) as resolve, \
                    patch.object(chatroom, 'with_band_vibration_attachment', side_effect=lambda _,a:a), \
                    patch.object(chatroom, 'connor_1v1_on_message'), patch.object(image_gen, 'generate_image', generate), \
                    patch.dict(image_gen.SETTINGS, {'image_gen_enabled':True, 'image_gen_provider':'codex'}):
                await chatroom._chatroom_image_gen('room','connor','raw command',True)
                async with db() as conn:
                    msg_id, attachments = await (await conn.execute('SELECT id,attachments FROM chatroom_messages')).fetchone()
                self.assertEqual(json.loads(attachments)[0]['type'], 'chatroom_image_retry')
                for path in paths:
                    path.unlink()
                image_gen.SETTINGS.update(image_gen_provider='flowai', flowai_image_model='grok_imagine_image_2_0')
                body = chatroom.ImageRetryClick(attempt=0)
                await asyncio.gather(*(chatroom.retry_room_image('room',msg_id,body) for _ in range(2)))
                await asyncio.wait_for(started.wait(), 2)
                finish.set()
                await asyncio.wait_for(delivered.wait(), 2)
                self.assertEqual(len(calls), 2)
                self.assertEqual(calls[1], calls[0])
                resolve.assert_awaited_once()
                async with db() as conn:
                    rows=await (await conn.execute('SELECT id,content,attachments FROM chatroom_messages')).fetchall()
                    self.assertEqual(rows, [(msg_id,'',json.dumps(['/uploads/album/new.png']))])
                    self.assertEqual(await (await conn.execute('SELECT count(*) FROM chatroom_image_retries')).fetchone(), (0,))
