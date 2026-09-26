import asyncio
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from contextlib import closing
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from repair_store import RepairStore, Conflict
from repair_files import export_file, file_record
from repair_context import work_history
import repair_routes


class RepairRoomTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = RepairStore(Path(self.tmp.name) / "repair")
        self.task = self.store.create("测试小修")
        self.task_id = self.task["id"]

    def test_model_label_follows_config_but_keeps_running_turn_model(self):
        with patch('repair_codex.model_name', return_value='configured-model'), patch('repair_worker.ensure_worker'), self.client(True) as client:
            url = '/api/repair/tasks/' + self.task_id
            self.assertEqual(client.get(url).json()['work_model'], {'name': 'configured-model', 'running': False})
            self.store.enqueue(self.task_id, 'discuss', {'text': '检查一下'})
            job = self.store.claim()
            self.store.runtime('active_model', json.dumps({'job': job['id'], 'name': 'turn-model'}))
            self.assertEqual(client.get(url).json()['work_model'], {'name': 'turn-model', 'running': True})
            self.store.finish(job)
            self.assertEqual(client.get(url).json()['work_model'], {'name': 'configured-model', 'running': False})

    def test_repair_model_setting_persists_and_keeps_running_model(self):
        from repair_codex import model_name
        with patch('repair_worker.ensure_worker'), self.client(True) as client:
            self.assertEqual(client.get('/api/repair/model').json()['name'], 'gpt-6-sol')
            self.store.enqueue(self.task_id, 'discuss', {'text': '检查'})
            job = self.store.claim()
            self.store.runtime('active_model', json.dumps({'job': job['id'], 'name': 'gpt-5.6-sol'}))
            response = client.put('/api/repair/model', json={'name': '  future-model  '})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(model_name(RepairStore(self.store.directory)), 'future-model')
            url = '/api/repair/tasks/' + self.task_id
            self.assertEqual(client.get(url).json()['work_model'], {'name': 'gpt-5.6-sol', 'running': True})
            self.store.finish(job)
            self.assertEqual(client.get(url).json()['work_model'], {'name': 'future-model', 'running': False})
            self.assertEqual(client.put('/api/repair/model', json={'name': '   '}).status_code, 400)
            self.assertEqual(model_name(self.store), 'future-model')

    def test_repair_model_setting_requires_pairing_and_mutation_guard(self):
        with patch.object(repair_routes, 'default_store', return_value=self.store), self.client() as client:
            self.assertEqual(client.get('/api/repair/model').status_code, 401)
            self.assertEqual(client.put('/api/repair/model', json={'name': 'other'}).status_code, 403)
            self.assertEqual(client.put('/api/repair/model', json={'name': 'other'}, headers={'X-Repair-Request': '1'}).status_code, 401)

    def test_summary_supports_model_not_in_home_model_list(self):
        from repair_summary import summarize_repair
        from stream_safety import StreamActivity
        self.store.runtime('model_name', 'future-model')
        async def reply(messages, model):
            self.assertEqual(model, 'future-model')
            yield StreamActivity()
            yield '已完成维修并核对结果。'
        with patch('repair_summary.call_codex_cli', side_effect=reply):
            self.assertEqual(asyncio.run(summarize_repair(self.store, self.task)), '已完成维修并核对结果。')

    def test_repair_disables_discovered_skills_without_rewriting_already_disabled_ones(self):
        from repair_runtime import prepare_repair_skills, RUNTIME_FLAGS
        request = AsyncMock(side_effect=[{'data': [{'skills': [
            {'name': 'brainstorming', 'path': 'C:/skills/brainstorming/SKILL.md', 'enabled': True},
            {'name': 'video', 'path': 'C:/skills/video/SKILL.md', 'enabled': False},
        ]}]}, {}])
        self.assertEqual(asyncio.run(prepare_repair_skills(request, 'F:/home')), ['brainstorming', 'video'])
        self.assertEqual(request.await_count, 2)
        request.assert_awaited_with('skills/config/write', {'path': 'C:/skills/brainstorming/SKILL.md', 'enabled': False})
        for flag in ['features.plugins=false', 'features.apps=false', 'features.multi_agent=false']:
            self.assertIn(flag, RUNTIME_FLAGS)

    def test_confirmed_version_required_and_new_discussion_invalidates_it(self):
        with self.assertRaises(Conflict):
            self.store.enqueue(self.task_id, "execute", {"revision": 0})
        plan = self.store.save_plan(self.task_id, "从配置读取显示名", 0)
        self.store.enqueue(self.task_id, "discuss", {"text": "还需要兼容群聊"})
        job = self.store.claim()
        self.store.finish(job)
        with self.assertRaises(Conflict):
            self.store.enqueue(self.task_id, "execute", {"revision": plan["revision"]})
        current = self.store.task(self.task_id)
        with self.assertRaises(Conflict):
            self.store.enqueue(self.task_id, "execute", {"revision": current["revision"]})
        updated = self.store.save_plan(self.task_id, "从配置读取显示名，并兼容群聊", current['revision'])
        self.store.enqueue(self.task_id, "execute", {"revision": updated["revision"]})
        self.assertEqual(self.store.claim()["payload"]["plan"], "从配置读取显示名，并兼容群聊")

    def test_chat_confirmation_requires_current_proposal_and_explicit_whole_message(self):
        with patch('repair_worker.ensure_worker'), self.client(True) as client:
            for text, expected in [('嗯，就这样改吧！', 'execute'), ('按这个方案开始', 'execute'),
                                   ('先不要开始', 'discuss'), ('能开始了吗？', 'discuss'),
                                   ('他说“就这样改”', 'discuss'), ('就这样改，顺便删掉数据库', 'discuss')]:
                with self.subTest(text=text):
                    task = self.store.create('确认识别')
                    task = self.store.save_plan(task['id'], '只调整按钮间距', 0)
                    result = client.post('/api/repair/tasks/'+task['id']+'/send', json={'text': text, 'revision': task['revision']})
                    self.assertEqual(result.status_code, 200)
                    self.assertEqual(self.store.active(task['id'])['kind'], expected)
            task = self.store.create('无方案')
            client.post('/api/repair/tasks/'+task['id']+'/send', json={'text':'开始吧','revision':0})
            self.assertEqual(self.store.active(task['id'])['kind'], 'discuss')
            stale = self.store.save_plan(self.task_id, '旧方案', 0)
            self.store.save_plan(self.task_id, '新方案', stale['revision'])
            result = client.post('/api/repair/tasks/'+self.task_id+'/send', json={'text':'就这样改','revision':stale['revision']})
            self.assertEqual(result.status_code, 409)
            self.assertIsNone(self.store.active(self.task_id))

    def test_discussion_proposes_visible_scope_without_executing_or_leaking_protocol(self):
        from repair_worker import discuss
        from test_codex_app_server import _FakeProcess, _happy_messages
        self.store.enqueue(self.task_id, 'discuss', {'text':'帮我调整按钮间距','recipient':'connor'})
        job = self.store.claim()
        answer = '准备只调整按钮间距。你确认后我再改。\n\n```repair-plan\n只调整按钮间距\n```'
        messages = _happy_messages(answer)
        messages = [m for m in messages if m.get('method') != 'item/agentMessage/delta']
        for chunk in [answer[:18], answer[18:35], answer[35:]]:
            messages.insert(-2, {'method':'item/agentMessage/delta', 'params':{'itemId':'msg_1','delta':chunk}})
        process = _FakeProcess(messages)
        with patch('repair_context.build_context', new_callable=AsyncMock, return_value=('','')), patch('repair_codex.command', return_value=['codex']), patch('repair_codex.model_name', return_value='test'), patch('repair_codex.prepare_repair_skills', new_callable=AsyncMock, return_value=[]), patch('ai_providers._CODEX_HOME', self.tmp.name), patch('repair_codex.asyncio.create_subprocess_exec', new_callable=AsyncMock, return_value=process):
            asyncio.run(discuss(self.store, job))
        turn = next(m['params'] for m in process.stdin.messages if m.get('method') == 'turn/start')
        self.assertEqual(json.loads(self.store.runtime('active_model'))['name'], 'test')
        self.assertEqual(turn['sandboxPolicy']['type'], 'dangerFullAccess')
        task = self.store.task(self.task_id)
        self.assertEqual(task['proposal_valid'], 1)
        self.assertEqual(task['plan'], self.store.messages(self.task_id)[-1]['text'])
        self.assertNotIn('repair-plan', ''.join(e['data'].get('delta','') for e in self.store.events(self.task_id)))
        self.assertEqual(self.store.active(self.task_id)['kind'], 'discuss')

    def test_inspect_empty_input_continues_user_request_with_attachments(self):
        source = Path(self.tmp.name) / '线索.png'
        source.write_bytes(b'image fixture')
        attachment = export_file(self.store, self.task_id, source)
        self.store.message(self.task_id, 'user', '把刚刚私聊的图片发给我', [attachment])
        self.store.message(self.task_id, 'connor', '点加号取文件。')
        with patch('repair_worker.ensure_worker'), self.client(True) as client:
            result = client.post(f'/api/repair/tasks/{self.task_id}/send', json={'kind': 'inspect'})
        self.assertEqual(result.status_code, 200)
        job = self.store.claim()
        self.assertEqual(job['kind'], 'inspect')
        self.assertEqual(job['payload']['text'], '把刚刚私聊的图片发给我')
        self.assertEqual(job['payload']['attachments'], [attachment])

    def test_uploaded_screenshot_reaches_native_turn_as_image_input(self):
        from io import BytesIO
        from PIL import Image
        from repair_worker import discuss
        from test_codex_app_server import _FakeProcess, _happy_messages
        image = BytesIO()
        Image.new('RGB', (24, 24), 'red').save(image, format='PNG')
        with patch('repair_worker.ensure_worker'), self.client(True) as client:
            root = f'/api/repair/tasks/{self.task_id}'
            uploaded = client.post(root+'/upload', files={'file': ('报错截图.png', image.getvalue(), 'image/png')})
            self.assertEqual(uploaded.status_code, 200)
            attachment = uploaded.json()
            preview = client.get(f"/api/repair/files/{attachment['id']}?preview=true")
            self.assertEqual(preview.headers['content-type'], 'image/png')
            self.assertEqual(preview.content, image.getvalue())
            # A screenshot alone is also a valid message.
            sent = client.post(root+'/send', json={'attachments': [attachment['id']], 'recipient': 'connor'})
            self.assertEqual(sent.status_code, 200)
        job = self.store.claim()
        process = _FakeProcess(_happy_messages('看到了截图。'))
        with patch('repair_context.build_context', new_callable=AsyncMock, return_value=('', '')), patch('repair_codex.command', return_value=['codex']), patch('repair_codex.model_name', return_value='test'), patch('repair_codex.prepare_repair_skills', new_callable=AsyncMock, return_value=[]), patch('ai_providers._CODEX_HOME', self.tmp.name), patch('repair_codex.asyncio.create_subprocess_exec', new_callable=AsyncMock, return_value=process):
            asyncio.run(discuss(self.store, job))
        turn = next(m['params'] for m in process.stdin.messages if m.get('method') == 'turn/start')
        images = [item for item in turn['input'] if item['type'] == 'localImage']
        self.assertEqual(len(images), 1)
        self.assertEqual(Path(images[0]['path']).read_bytes(), image.getvalue())

    def test_inspect_requires_request_and_prefers_new_input(self):
        url = f'/api/repair/tasks/{self.task_id}/send'
        with patch('repair_worker.ensure_worker'), self.client(True) as client:
            self.assertEqual(client.post(url, json={'kind': 'inspect'}).status_code, 400)
            self.store.message(self.task_id, 'user', '就这样改')
            self.assertEqual(client.post(url, json={'kind': 'inspect'}).status_code, 400)
            result = client.post(url, json={'kind': 'inspect', 'text': '找今天的错误日志'})
            self.assertEqual(result.status_code, 200)
        self.assertEqual(self.store.claim()['payload']['text'], '找今天的错误日志')

    def test_running_job_cannot_be_replaced_or_marked_complete_by_stop(self):
        self.store.enqueue(self.task_id, "inspect", {"text": "查看文件"})
        job = self.store.claim()
        with self.assertRaises(Conflict):
            self.store.save_plan(self.task_id, "别的方案", 1)
        self.store.steer(self.task_id, "先看看日期")
        self.store.stop(self.task_id)
        self.assertTrue(self.store.cancelled(job["id"]))
        with self.assertRaises(Conflict):
            self.store.enqueue(self.task_id, "discuss", {"text": "新问题"})
        self.store.finish(job, "interrupted")
        self.assertEqual(self.store.task(self.task_id)["phase"], "paused")

    def test_ordinary_chat_can_deliver_file_but_cannot_restart(self):
        from repair_worker import handle
        from test_codex_app_server import _FakeProcess, _happy_messages
        source = Path(self.tmp.name) / '找到的文件.txt'
        source.write_text('原文件内容', encoding='utf-8')
        self.store.enqueue(self.task_id, 'discuss', {'recipient':'connor', 'text':'找找那个文件发给我'})
        job = self.store.claim()
        messages = _happy_messages('已找到文件并交付。')
        messages.insert(3, {'id':900, 'method':'item/tool/call', 'params':{'tool':'repair_deliver_file','arguments':{'path':str(source)}}})
        messages.insert(4, {'id':901, 'method':'item/tool/call', 'params':{'tool':'repair_restart_home','arguments':{}}})
        process = _FakeProcess(messages)
        with patch('repair_context.build_context', new_callable=AsyncMock, return_value=('','')), patch('repair_codex.command', return_value=['codex']), patch('repair_codex.model_name', return_value='test'), patch('repair_codex.prepare_repair_skills', new_callable=AsyncMock, return_value=[]), patch('ai_providers._CODEX_HOME', self.tmp.name), patch('repair_codex.asyncio.create_subprocess_exec', new_callable=AsyncMock, return_value=process), patch('repair_worker.restart_home', new_callable=AsyncMock) as restart:
            asyncio.run(handle(self.store, job))
            restart.assert_not_awaited()
        replies = {m['id']:m['result'] for m in process.stdin.messages if 'result' in m}
        self.assertTrue(replies[900]['success'])
        self.assertFalse(replies[901]['success'])
        self.store.finish(job)
        self.assertEqual(self.store.task(self.task_id)['phase'], 'review')
        attachment = next(m['attachments'][0] for m in self.store.messages(self.task_id) if m['attachments'])
        self.assertEqual(Path(file_record(self.store, attachment['id'])['path']).read_text(encoding='utf-8'), '原文件内容')

    def test_chat_investigation_accepts_followup_but_aion_only_chat_does_not(self):
        self.store.enqueue(self.task_id, 'discuss', {'recipient':'both','text':'找一下那个文件'})
        job = self.store.claim()
        self.assertTrue(self.store.active(self.task_id)['can_steer'])
        self.store.steer(self.task_id, '是昨天的那份')
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT text FROM controls WHERE job_id=?', (job['id'],)).fetchone()[0], '是昨天的那份')
        self.store.finish(job)
        self.store.enqueue(self.task_id, 'discuss', {'recipient':'aion','text':'先聊聊'})
        self.store.claim()
        self.assertFalse(self.store.active(self.task_id)['can_steer'])
        with self.assertRaises(Conflict):
            self.store.steer(self.task_id, '补充')

    def test_both_discussion_keeps_aion_then_tool_capable_connor(self):
        from repair_worker import discuss
        async def response(*args, **kwargs):
            yield '我建议先检查相关代码。'
        async def investigate(store, job, instructions, context):
            self.assertEqual(store.messages(job['task_id'])[-1]['who'], 'aion')
        self.store.enqueue(self.task_id, 'discuss', {'recipient':'both','text':'这个按钮好像不对'})
        job = self.store.claim()
        with patch('repair_context.build_context', new_callable=AsyncMock, return_value=('','')), patch('ai_providers.stream_ai', response), patch('repair_codex.run_codex', new_callable=AsyncMock, side_effect=investigate) as run:
            asyncio.run(discuss(self.store, job))
            run.assert_awaited_once()

    def test_aion_disconnect_or_timeout_does_not_block_connor(self):
        from repair_worker import discuss
        import httpx
        async def disconnected(*args, **kwargs):
            raise httpx.RemoteProtocolError('Server disconnected without sending a response.')
            yield
        async def stalled(*args, **kwargs):
            await asyncio.Event().wait()
            yield
        for response in (disconnected, stalled):
            with self.subTest(response=response.__name__):
                task = self.store.create('讨论线路故障')
                self.store.enqueue(task['id'], 'discuss', {'recipient':'both', 'text':'找那个文件夹里的最新照片'})
                job = self.store.claim()
                with patch('repair_context.build_context', new_callable=AsyncMock, return_value=('','')), patch('ai_providers.stream_ai', response), patch('repair_worker.AION_DISCUSSION_TIMEOUT', 0.01), patch('repair_codex.run_codex', new_callable=AsyncMock) as run:
                    asyncio.run(discuss(self.store, job))
                    run.assert_awaited_once()
                self.assertTrue(any(m['who'] == 'system' and '继续处理' in m['text'] for m in self.store.messages(task['id'])))
                self.store.finish(job)

    def test_aion_only_failure_and_user_stop_do_not_launch_connor(self):
        from repair_worker import discuss
        for recipient, error in [('aion', RuntimeError('断线')), ('both', asyncio.CancelledError())]:
            task = self.store.create('不得擅自继续')
            self.store.enqueue(task['id'], 'discuss', {'recipient':recipient,'text':'测试'})
            job = self.store.claim()
            with patch('repair_worker.reply_aion', new_callable=AsyncMock, side_effect=error), patch('repair_codex.run_codex', new_callable=AsyncMock) as run:
                with self.assertRaises(type(error)):
                    asyncio.run(discuss(self.store, job))
                run.assert_not_awaited()
            self.store.finish(job, 'failed')

    def test_default_send_goes_directly_to_connor(self):
        from repair_worker import discuss
        with patch('repair_worker.ensure_worker'), self.client(True) as client:
            response = client.post(f'/api/repair/tasks/{self.task_id}/send', json={'text':'帮我找那份文件'})
            self.assertEqual(response.status_code, 200)
        job = self.store.claim()
        self.assertEqual(job['payload']['recipient'], 'connor')
        with patch('repair_worker.reply_aion', new_callable=AsyncMock) as aion, patch('repair_context.build_context', new_callable=AsyncMock, return_value=('','')), patch('repair_codex.run_codex', new_callable=AsyncMock) as connor:
            asyncio.run(discuss(self.store, job))
            aion.assert_not_awaited()
            connor.assert_awaited_once()

    def test_worker_restart_preserves_history_and_requires_manual_continuation(self):
        self.store.message(self.task_id, "user", "之前已经确认的要求")
        self.store.enqueue(self.task_id, "inspect", {"text": "排查"})
        self.store.claim()
        reopened = RepairStore(self.store.directory)
        reopened.recover()
        self.assertIsNone(reopened.active(self.task_id))
        self.assertEqual(reopened.task(self.task_id)["phase"], "paused")
        self.assertIn("之前已经确认", reopened.messages(self.task_id)[0]["text"])

    def test_event_and_message_cursors_do_not_replay_duplicates(self):
        first = self.store.event(self.task_id, "output", {"delta": "first"})
        self.store.event(self.task_id, "output", {"delta": "second"})
        first_msg = self.store.message(self.task_id, "connor", "发现原因")
        self.store.message(self.task_id, "connor", "下一步")
        self.assertEqual([e["data"]["delta"] for e in self.store.events(self.task_id, first)], ["second"])
        self.assertEqual([m["text"] for m in self.store.messages(self.task_id, first_msg)], ["下一步"])

    def test_image_only_message_is_kept_in_work_history(self):
        attachment = {"id": "image1", "name": "问题截图.png"}
        self.store.enqueue(self.task_id, "discuss", {"text": "", "attachments": [attachment]})
        self.assertEqual(self.store.messages(self.task_id)[0]["attachments"], [attachment])

    def test_full_access_applies_to_every_turn_including_resumed_old_tasks(self):
        from repair_codex import run_codex
        from test_codex_app_server import _FakeProcess, _happy_messages
        with patch("repair_codex.command", return_value=["node", "codex.js"]), patch("repair_codex.model_name", return_value="test-model"), patch("repair_codex.prepare_repair_skills", new_callable=AsyncMock, return_value=[]), patch("ai_providers._CODEX_HOME", self.tmp.name):
            for kind in ("discuss", "execute", "inspect", "plan"):
                if kind == "execute":
                    task = self.store.task(self.task_id)
                    self.store.save_plan(self.task_id, "只修改项目内测试文件", task["revision"])
                self.store.enqueue(self.task_id, kind, {"revision": self.store.task(self.task_id)["revision"], "text": "验证"})
                job = self.store.claim()
                messages = _happy_messages("本轮结果")
                messages.insert(3, {"method": "item/completed", "params": {"item": {"type": "reasoning", "text": "hidden-content"}}})
                process = _FakeProcess(messages)
                with patch("repair_codex.asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=process) as spawn:
                    asyncio.run(run_codex(self.store, job, "验证", "验证"))
                requests = process.stdin.messages
                turn = next(m["params"] for m in requests if m.get("method") == "turn/start")
                self.assertEqual(turn["sandboxPolicy"], {"type": "dangerFullAccess"})
                self.assertEqual(turn["approvalPolicy"], "never")
                thread = next(m['params'] for m in requests if m.get('method') in ('thread/start','thread/resume'))
                self.assertEqual(thread['sandbox'], 'danger-full-access')
                if kind == "execute":
                    self.assertTrue(any(m.get("method") == "thread/resume" for m in requests))
                self.assertNotIn("hidden-content", json.dumps(self.store.events(self.task_id)))
                self.store.finish(job)

    def test_files_are_task_scoped_and_do_not_expose_arbitrary_download_paths(self):
        source = Path(self.tmp.name) / "报告.txt"
        source.write_text("可领取", encoding="utf-8")
        file = export_file(self.store, self.task_id, source)
        self.assertEqual(Path(file_record(self.store, file["id"], self.task_id)["path"]).read_text(encoding="utf-8"), "可领取")
        with self.assertRaises(KeyError):
            file_record(self.store, file["id"], "other-task")
        with self.store.connect() as db:
            db.execute("UPDATE files SET path=? WHERE id=?", (str(source), file["id"]))
        with self.assertRaises(ValueError):
            file_record(self.store, file["id"])

    def test_context_keeps_identity_and_recent_work_when_long(self):
        messages = [{"who": "user", "text": "早期" * 500, "attachments": []} for _ in range(100)]
        messages.append({"who": "connor", "text": "当前决定：不能硬编码", "attachments": []})
        text = work_history(messages, {"user": "测试用户", "connor": "测试伴侣"}, budget=18000)
        self.assertIn("测试伴侣: 当前决定：不能硬编码", text)
        self.assertIn("节选", text)
        self.assertLess(len(text), 19000)

    def client(self, authorize=False):
        app = FastAPI()
        app.include_router(repair_routes.router)
        if authorize:
            app.dependency_overrides[repair_routes.access] = lambda: self.store
        return TestClient(app)

    def test_pairing_required_and_foreign_origin_rejected(self):
        with patch.object(repair_routes, "default_store", return_value=self.store):
            with self.client() as client:
                self.assertEqual(client.get("/api/repair/tasks").status_code, 401)
                code = repair_routes.pairing_code(self.store)
                self.assertEqual(client.post("/api/repair/pair", json={"code": code}).status_code, 403)
                self.assertEqual(client.post("/api/repair/pair", json={"code": code}, headers={"X-Repair-Request": "1", "Origin": "https://elsewhere.test"}).status_code, 403)
                self.assertEqual(client.post("/api/repair/pair", json={"code": code}, headers={"X-Repair-Request": "1"}).status_code, 200)
                self.assertEqual(client.get("/api/repair/tasks").status_code, 200)

    def test_acceptance_only_after_work_and_publishes_once(self):
        home = Path(self.tmp.name) / "home.sqlite3"
        with closing(sqlite3.connect(home)) as db:
            db.executescript("""
              CREATE TABLE chatroom_rooms(id TEXT PRIMARY KEY,type TEXT,updated_at REAL);
              INSERT INTO chatroom_rooms VALUES('group1','group',1);
              CREATE TABLE chatroom_messages(id TEXT PRIMARY KEY,room_id TEXT,sender TEXT,content TEXT,created_at REAL,attachments TEXT);
            """)
        with patch("config.DB_PATH", home), patch("chatroom.get_chatroom_names", return_value=("测试用户", "甲", "乙")), patch("sync_events.broadcast_synced", new_callable=AsyncMock) as broadcast, patch("chatroom.connor_1v1_on_message"), patch('repair_routes.summarize_repair', new_callable=AsyncMock, return_value='已完成文件查看、文档写入和图片交付，并核对交付结果。') as summarize:
            with self.client(True) as client:
                url = f"/api/repair/tasks/{self.task_id}/accept"
                self.assertEqual(client.post(url).status_code, 409)
                self.store.enqueue(self.task_id, "inspect", {"text": "排查"})
                job = self.store.claim()
                self.assertEqual(client.post(url).status_code, 409)
                self.store.message(self.task_id, "connor", "检查通过。")
                self.store.finish(job)
                self.assertEqual(client.post(url).status_code, 200)
                self.assertEqual(client.post(url).status_code, 200)
                self.assertEqual(broadcast.await_count, 1)
                self.assertEqual(summarize.await_count, 1)
        with closing(sqlite3.connect(home)) as db:
            rows = db.execute("SELECT content,attachments FROM chatroom_messages").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertIn("测试用户已验收通过", rows[0][0])
        self.assertIn('文档写入和图片交付', rows[0][0])
        self.assertNotIn('检查通过。', rows[0][0])
        from context_builder import _is_model_visible_timeline_message
        self.assertTrue(_is_model_visible_timeline_message({"sender": "system", "content": rows[0][0], "attachments": rows[0][1]}))

    def test_summary_failure_or_changed_task_does_not_publish_or_accept(self):
        home = Path(self.tmp.name) / 'summary-home.sqlite3'
        with closing(sqlite3.connect(home)) as db:
            db.executescript("CREATE TABLE chatroom_rooms(id TEXT PRIMARY KEY,type TEXT,updated_at REAL); INSERT INTO chatroom_rooms VALUES('g','group',1); CREATE TABLE chatroom_messages(id TEXT PRIMARY KEY,room_id TEXT,sender TEXT,content TEXT,created_at REAL,attachments TEXT);")
        self.store.enqueue(self.task_id, 'inspect', {'text':'检查'})
        self.store.finish(self.store.claim())
        async def changed(*_):
            self.store.message(self.task_id, 'user', '补充新要求')
            return '旧的成果摘要'
        with patch('config.DB_PATH', home), patch('repair_routes.summarize_repair', new_callable=AsyncMock) as summary, self.client(True) as client:
            url = f'/api/repair/tasks/{self.task_id}/accept'
            summary.side_effect = TimeoutError()
            self.assertEqual(client.post(url).status_code, 503)
            summary.side_effect = changed
            self.assertEqual(client.post(url).status_code, 409)
        self.assertEqual(self.store.task(self.task_id)['phase'], 'review')
        with closing(sqlite3.connect(home)) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM chatroom_messages').fetchone()[0], 0)

    def test_summary_uses_earlier_work_and_delivery_not_just_last_reply(self):
        from repair_summary import summary_context
        self.store.message(self.task_id, 'user', '先创建文档')
        self.store.message(self.task_id, 'connor', '文档已写入并读回核对。', [{'name':'说明.txt'}])
        self.store.message(self.task_id, 'connor', '图片也发给你了。', [{'name':'照片.png'}])
        context = summary_context(self.store, self.task)
        for expected in ['先创建文档', '读回核对', '说明.txt', '照片.png']:
            self.assertIn(expected, context)

    def test_actor_context_uses_own_recent_timeline_without_life_tools(self):
        from repair_context import build_context
        with patch("chatroom.get_chatroom_names", return_value=("用户甲", "伴侣甲", "伴侣乙")), patch("chatroom._read_connor_persona", return_value="专属人设"), patch("config.load_worldbook", return_value={"ai_persona": "另一个人设", "user_persona": "用户设定"}), patch("context_builder.fetch_merged_timeline", new_callable=AsyncMock, return_value=[]) as fetch:
            instructions, context = asyncio.run(build_context(self.store, self.task_id, "connor"))
        self.assertIn("专属人设", instructions)
        self.assertNotIn("另一个人设", instructions)
        self.assertEqual(fetch.call_args.args, ("connor", 30))
        self.assertAlmostEqual(fetch.call_args.kwargs["until_ts"] - fetch.call_args.kwargs["since_ts"], 7200)
        self.assertIn("维修室工作记录", context)

    def test_worker_does_not_inherit_desktop_task_or_permissions(self):
        from repair_codex import work_environment
        with patch.dict("os.environ", {"CODEX_APP_TOOLS_PIPE_PATH": "desktop-pipe", "CODEX_THREAD_ID": "desktop-task", "CODEX_PERMISSION_PROFILE": "desktop-profile"}):
            env = work_environment(self.store.directory)
        self.assertNotIn("CODEX_APP_TOOLS_PIPE_PATH", env)
        self.assertNotIn("CODEX_THREAD_ID", env)
        self.assertNotIn("CODEX_PERMISSION_PROFILE", env)
        self.assertEqual(env["CODEX_HOME"], str(self.store.directory))


if __name__ == "__main__":
    unittest.main()
