import asyncio
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import aiosqlite
import httpx
from fastapi import FastAPI


class ChatroomImageRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_edit_replaces_prompt_and_resubmits_frozen_anchors_with_a_new_ticket(self):
        import chatroom_image_retry as retry
        import image_gen
        from routes import chatroom

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = lambda: aiosqlite.connect(root / 'chat.db')
            paths = [root / 'a.png', root / 'b.png']
            for path in paths:
                path.write_bytes(path.name.encode())
            refs = [{'name': path.stem, 'path': str(path)} for path in paths]
            with patch.dict(image_gen.SETTINGS, image_gen_provider='flowai', flowai_image_model='seedream_5_0_flash'):
                snapshot = await retry.snapshot('old scene', refs, True, 'connor')
            original = io.BytesIO(snapshot)
            with zipfile.ZipFile(original, 'a') as archive:
                archive.comment = b'old-ticket'
            snapshot = original.getvalue()
            for path in paths:
                path.unlink()
            async with db() as conn:
                await conn.execute('''CREATE TABLE chatroom_messages (
                    id TEXT PRIMARY KEY,room_id,sender,content,attachments,created_at,reasoning_content)''')
                await retry.init_storage(conn)
                await conn.execute('INSERT INTO chatroom_messages VALUES (?,?,?,?,?,?,?)',
                                   ('msg', 'room', 'connor', '生成超时', json.dumps([retry.marker()]), 1, None))
                await conn.execute('INSERT INTO chatroom_image_retries VALUES (?,?)', ('msg', snapshot))
                await conn.commit()

            calls = []
            async def generate(prompt, *, references, is_selfie, source_identity, raise_errors, **flow):
                calls.append((prompt, is_selfie, source_identity, flow['flow_job_id'],
                              [(ref['name'], Path(ref['path']).read_bytes()) for ref in references]))
                if len(calls) == 1:
                    await flow['on_flow_job']('new-ticket')
                    raise TimeoutError()
                return 'album/new.png'

            app = FastAPI()
            app.include_router(chatroom.router)
            route = '/api/chatroom/rooms/room/messages/msg/retry-image'
            edited_prompt = '  新场景\n窗边喝茶  '
            with patch.object(chatroom, 'get_db', db), \
                    patch.object(chatroom, 'broadcast_synced', new_callable=AsyncMock), \
                    patch.object(image_gen, 'generate_image', generate), \
                    patch.dict(image_gen.SETTINGS, image_gen_enabled=True,
                               image_gen_provider='flowai', flowai_image_model='seedream_5_0_flash'):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
                    self.assertEqual((await client.get(route)).json(), {'prompt': 'old scene', 'attempt': 0})
                    unchanged = await client.put(route, json={'prompt': 'old scene', 'attempt': 0})
                    self.assertEqual(unchanged.status_code, 200)
                    async with db() as conn:
                        saved = await (await conn.execute('SELECT snapshot FROM chatroom_image_retries')).fetchone()
                    self.assertEqual(saved[0], snapshot)
                    self.assertEqual((await client.put(route, json={'prompt': ' \n ', 'attempt': 0})).status_code, 400)
                    response = await client.put(route, json={'prompt': edited_prompt, 'attempt': 0})
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.json()['message']['attachments'], [retry.marker(1)])
                    self.assertEqual((await client.get(route)).json(), {'prompt': edited_prompt, 'attempt': 1})
                    async with db() as conn:
                        saved = await (await conn.execute('SELECT snapshot FROM chatroom_image_retries')).fetchone()
                    with zipfile.ZipFile(io.BytesIO(saved[0])) as archive:
                        self.assertEqual(archive.comment, b'')
                        self.assertEqual(archive.namelist().count('request.json'), 1)
                        self.assertEqual(json.loads(archive.read('request.json'))['prompt'], edited_prompt)
                    self.assertEqual((await client.put(route, json={'prompt': 'stale scene', 'attempt': 0})).status_code, 409)
                    self.assertFalse((await client.post(route, json={'attempt': 0})).json()['accepted'])
                    self.assertEqual((await client.get(route.replace('/room/', '/another-room/'))).status_code, 404)
                    for attempt in (1, 2):
                        response = await client.post(route, json={'attempt': attempt})
                        self.assertTrue(response.json()['accepted'])
                        await asyncio.gather(*list(chatroom._image_retry_tasks))
                    self.assertEqual((await client.get(route)).status_code, 409)
                    async with db() as conn:
                        row = await (await conn.execute(
                            'SELECT created_at,attachments FROM chatroom_messages WHERE id=?', ('msg',))).fetchone()
                    self.assertEqual(row[0], 1)
                    self.assertGreater(json.loads(row[1])[0]['generated_at'], 1)
            self.assertEqual(calls, [
                (edited_prompt, True, 'connor', None, [('a', b'a.png'), ('b', b'b.png')]),
                (edited_prompt, True, 'connor', 'new-ticket', [('a', b'a.png'), ('b', b'b.png')]),
            ])

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
                if any(isinstance(a, dict) and a.get('url') == '/uploads/album/new.png'
                       for a in event['data']['attachments']):
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
                with self.assertRaises(chatroom.HTTPException) as blocked:
                    await chatroom.update_room_image_prompt('room', msg_id,
                        chatroom.ImageRetryPrompt(attempt=1, prompt='cannot edit a running request'))
                self.assertEqual(blocked.exception.status_code, 409)
                finish.set()
                await asyncio.wait_for(delivered.wait(), 2)
                self.assertEqual(len(calls), 2)
                self.assertEqual(calls[1], calls[0])
                resolve.assert_awaited_once()
                async with db() as conn:
                    rows=await (await conn.execute('SELECT id,content,attachments FROM chatroom_messages')).fetchall()
                    self.assertEqual([(row[0], row[1]) for row in rows], [(msg_id, '')])
                    image = json.loads(rows[0][2])[0]
                    self.assertEqual(image['url'], '/uploads/album/new.png')
                    self.assertEqual(image['type'], 'image')
                    self.assertGreater(image['generated_at'], 0)
                    self.assertEqual(await (await conn.execute('SELECT count(*) FROM chatroom_image_retries')).fetchone(), (0,))
