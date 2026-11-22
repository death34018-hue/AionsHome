import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from contextlib import ExitStack

import aiosqlite
import active_memory_search as search
import memory_compression as compression


class MemoryEvidenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'evidence.db'
        self.connect = lambda: aiosqlite.connect(self.path)
        self.patches = [patch.object(module, 'get_db', self.connect) for module in (search, compression)]
        for item in self.patches:
            item.start()
        async with self.connect() as db:
            for table in ('memories', 'chatroom_memories'):
                await db.execute(f'''CREATE TABLE {table} (
                    id TEXT PRIMARY KEY, content TEXT, type TEXT, scope TEXT DEFAULT 'connor',
                    source_conv TEXT, room_id TEXT, created_at REAL DEFAULT 100,
                    source_start_ts REAL, source_end_ts REAL, source_msg_id TEXT,
                    keywords TEXT DEFAULT '', importance REAL DEFAULT 0.5, embedding BLOB,
                    evidence_summary TEXT DEFAULT '', archive_state TEXT DEFAULT 'active',
                    compression_batch_id TEXT DEFAULT '', source_memory_ids TEXT)''')
            await db.execute('CREATE TABLE messages (id TEXT, conv_id TEXT, role TEXT, content TEXT, created_at REAL)')
            await db.execute('CREATE TABLE chatroom_rooms (id TEXT, type TEXT)')
            await db.execute('CREATE TABLE chatroom_messages (id TEXT, room_id TEXT, sender TEXT, content TEXT, created_at REAL)')
            await db.executemany('INSERT INTO chatroom_rooms VALUES (?,?)', [('group','group'), ('private','connor_1v1')])
            await db.executemany('INSERT INTO messages VALUES (?,?,?,?,?)', [
                ('a','one','user','那家店叫星河面馆',100), ('b','one','assistant','就在车站旁边',101),
                ('other','two','user','不应展开别的窗口',101)])
            await db.executemany('INSERT INTO chatroom_messages VALUES (?,?,?,?,?)', [
                ('c','private','user','私聊提过星河面馆',100),
                ('g','group','system','点歌：星河面馆',102)])
            await db.execute('CREATE TABLE memory_compression_batch_inputs (batch_id TEXT, store TEXT, memory_id TEXT)')
            await db.commit()

    async def asyncTearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.tmp.cleanup()

    async def test_new_lineage_excludes_unrelated_batch_and_legacy_still_traces(self):
        async with self.connect() as db:
            for mem_id, source, parents in [('old','["private:a"]',None), ('noise','["private:other"]',None), ('new','[]','["old"]'), ('legacy','[]',None)]:
                await db.execute('INSERT INTO memories (id,content,source_msg_id,source_memory_ids,compression_batch_id) VALUES (?,?,?,?,?)',
                                 (mem_id, '星河面馆' if mem_id != 'noise' else '浇花', source, parents, 'batch' if mem_id in ('new','legacy') else ''))
            await db.executemany('INSERT INTO memory_compression_batch_inputs VALUES (?,?,?)', [('batch','main','old'),('batch','main','noise'),('batch','chatroom','c')])
            await db.commit()
        self.assertEqual(await compression.resolve_source_message_ids('main','new'), ['private:a'])
        self.assertEqual(set(await compression.resolve_source_message_ids('main','legacy')), {'private:a','private:other'})
        result = search.MemorySearchResult('legacy','aion','吃饭',100,1,[],True,raw={'id':'legacy','compression_batch_id':'batch'})
        await search._attach_source_details([result], 'aion', [search.MemorySearchRequest('星河面馆',include_detail=True)])
        self.assertIn('星河面馆', '\n'.join(result.sources))
        self.assertNotIn('不应展开', '\n'.join(result.sources))

    async def test_history_fallback_and_open_enforce_actor_and_window(self):
        with patch.object(search, 'get_embedding', AsyncMock(return_value=None)):
            aion = await search.search_actor_memories('aion', [search.MemorySearchRequest('星河面馆')])
            connor = await search.search_actor_memories('connor', [search.MemorySearchRequest('星河面馆')])
        self.assertEqual({r.memory_id for r in aion}, {'private:a','chatroom:g'})
        self.assertEqual({r.memory_id for r in connor}, {'chatroom:c','chatroom:g'})
        opened = await search.search_actor_memories('aion', [search.MemorySearchRequest('面馆', source_id='private:a')])
        self.assertIn('车站旁边', '\n'.join(opened[0].sources))
        self.assertNotIn('别的窗口', '\n'.join(opened[0].sources))
        self.assertEqual(await search.search_actor_memories('connor', [search.MemorySearchRequest('面馆',source_id='private:a')]), [])
        self.assertEqual(await search.search_actor_memories('aion', [search.MemorySearchRequest('面馆',source_id='chatroom:c')]), [])

    async def test_followup_search_is_bounded_and_preserves_evidence(self):
        generate = AsyncMock(side_effect=['[MEMORY_SEARCH:面馆|history]', '[MEMORY_SEARCH:面馆|open=private:a]', '就在车站旁边。'])
        messages = [{'role':'user','content':'原始问题和第一轮结果'}]
        with patch.object(search, 'search_actor_memories', AsyncMock(return_value=[])) as lookup:
            answer = await search.continue_memory_search('aion', messages, generate, '店在哪里？')
        self.assertEqual(answer, '就在车站旁边。')
        self.assertEqual(lookup.await_count, 2)
        self.assertEqual(generate.await_count, 3)
        self.assertEqual(messages[0]['content'], '原始问题和第一轮结果')
        stubborn = AsyncMock(return_value='[MEMORY_SEARCH:面馆|history]')
        with patch.object(search, 'search_actor_memories', AsyncMock(return_value=[])) as lookup:
            answer = await search.continue_memory_search('aion', [], stubborn, '店在哪里？')
        self.assertLessEqual(lookup.await_count, 2)
        self.assertNotIn('MEMORY_SEARCH', answer)
        self.assertTrue(answer.strip())

    async def test_peer_dm_keeps_participants_in_recent_history_and_memory_evidence(self):
        import chatroom
        import context_builder
        import memory
        from routes.memory_library import _source_rows

        async with self.connect() as db:
            await db.execute("ALTER TABLE messages ADD COLUMN attachments TEXT DEFAULT '[]'")
            await db.execute("ALTER TABLE chatroom_messages ADD COLUMN attachments TEXT DEFAULT '[]'")
            await db.execute("INSERT INTO chatroom_rooms VALUES ('cr_peer_dm','peer_dm')")
            await db.execute("INSERT INTO chatroom_messages VALUES ('dm','cr_peer_dm','connor','一起去找北极星',110,'[]')")
            await db.commit()
        names = ('Mira', 'Sol', 'Nova')
        with ExitStack() as stack:
            for module in (context_builder, memory, chatroom):
                stack.enter_context(patch.object(module, 'get_db', self.connect))
            stack.enter_context(patch.object(context_builder, '_timeline_display_names', return_value=names))
            stack.enter_context(patch.object(chatroom, 'get_chatroom_names', return_value=names))
            stack.enter_context(patch.object(memory, '_connor_display_name', return_value='Nova'))
            stack.enter_context(patch.object(memory, 'load_worldbook', return_value={'user_name': 'Mira', 'ai_name': 'Sol'}))
            for actor, recall in (('aion', memory.fetch_source_details), ('connor', chatroom.fetch_chatroom_source_details)):
                with self.subTest(actor=actor):
                    recent = await context_builder.fetch_merged_timeline(actor, 1)
                    self.assertEqual([(m['source'], m['sender']) for m in recent], [('peer_dm', 'connor')])
                    rendered = context_builder.render_merged_timeline(recent, actor)[-1]
                    self.assertEqual(rendered['_timeline_source'], 'peer_dm')
                    line = rendered['content']
                    self.assertIn('Nova：一起去找北极星', line)
                    self.assertIn('Sol与Nova私聊', line)
                    hits = await search.search_chat_history(actor, [search.MemorySearchRequest('北极星')])
                    self.assertEqual([r.memory_id for r in hits], ['chatroom:dm'])
                    opened = await search.open_memory_source(actor, hits[0].memory_id)
                    for source in (hits[0].sources[0], opened[0].sources[0]):
                        self.assertIn('Sol与Nova私聊', source)
                        self.assertIn('Nova', source)
                    evidence = await recall([{'id': 'memory', 'source_msg_id': '["chatroom:dm"]'}], [])
                    self.assertIn('Sol与Nova私聊', evidence)
                    self.assertIn('Nova: 一起去找北极星', evidence)
                    dated = await recall([{'id': 'memory', 'source_start_ts': 100, 'source_end_ts': 111}], ['北极星'])
                    self.assertIn('Sol与Nova私聊', dated)
                    async with self.connect() as db:
                        db.row_factory = aiosqlite.Row
                        rows = await _source_rows(db, 'main' if actor == 'aion' else 'chatroom', ids=['chatroom:dm'])
                    self.assertEqual([(r['source'], r['window_id'], r['role']) for r in rows], [('peer_dm', 'cr_peer_dm', 'connor')])

    def test_parser_accepts_history_and_open(self):
        _, requests = search.extract_memory_search_requests('[MEMORY_SEARCH:面馆|history] [MEMORY_SEARCH:店名|open=private:a]')
        self.assertTrue(requests[0].history)
        self.assertEqual(requests[1].source_id, 'private:a')

    async def test_multiple_compressions_trace_each_store_independently(self):
        async with self.connect() as db:
            for table, source in [('memories','private:a'), ('chatroom_memories','chatroom:c')]:
                for mem_id, parents, ids in [('leaf',None,[source]), ('day',['leaf'],[]), ('week',['day'],[])]:
                    await db.execute(f'INSERT INTO {table} (id,content,source_memory_ids,source_msg_id) VALUES (?,?,?,?)',
                                     (mem_id, '事件', json.dumps(parents) if parents is not None else None, json.dumps(ids)))
            await db.commit()
        self.assertEqual(await compression.resolve_source_message_ids('main','week'), ['private:a'])
        self.assertEqual(await compression.resolve_source_message_ids('chatroom','week'), ['chatroom:c'])

    async def test_reply_routes_can_continue_search_and_save_only_final_answer(self):
        from routes import chat, chatroom
        import schedule

        async with self.connect() as db:
            await db.execute("ALTER TABLE messages ADD COLUMN attachments TEXT DEFAULT '[]'")
            await db.execute('CREATE TABLE conversations (id TEXT, updated_at REAL)')
            await db.commit()
        async def process(text, **kwargs):
            return text

        for actor in ('aion', 'connor'):
            prompts = []
            async def stream(messages, *args, **kwargs):
                prompts.append(messages)
                yield '[MEMORY_SEARCH:面馆|history]' if len(prompts) == 1 else '找到了，是星河面馆。'

            with ExitStack() as stack:
                stack.enter_context(patch.object(schedule, '_process_background_reply_commands', process))
                lookup = stack.enter_context(patch.object(search, 'search_actor_memories', AsyncMock(return_value=[])))
                module = chat if actor == 'aion' else chatroom
                stack.enter_context(patch.object(module, 'search_actor_memories', AsyncMock(return_value=[])))
                stack.enter_context(patch.object(module, 'build_ability_block', AsyncMock(return_value='')))
                stack.enter_context(patch('config.load_worldbook', return_value={'user_name':'用户','ai_name':'AI'}))
                if actor == 'aion':
                    stack.enter_context(patch.object(chat, 'load_worldbook', return_value={'user_name':'用户','ai_name':'AI'}))
                    stack.enter_context(patch.object(chat, 'get_db', self.connect))
                    stack.enter_context(patch.object(chat, 'stream_ai', stream))
                    stack.enter_context(patch.object(chat, '_private_memory_recent_messages', AsyncMock(return_value=[])))
                    stack.enter_context(patch.object(chat.manager, 'any_tts_enabled', return_value=False))
                    stack.enter_context(patch.object(chat.manager, 'broadcast', AsyncMock()))
                    stack.enter_context(patch.object(chat, 'export_conversation', AsyncMock()))
                    stack.enter_context(patch.object(chat, 'with_band_vibration_attachment', AsyncMock(return_value=[])))
                    await chat.perform_private_memory_search('one','test',[search.MemorySearchRequest('店名')],original_question='那家店叫什么？')
                    async with self.connect() as db:
                        row = await (await db.execute("SELECT content FROM messages WHERE id LIKE '%memory_reply'")).fetchone()
                    self.assertEqual(row[0], '找到了，是星河面馆。')
                else:
                    stack.enter_context(patch.object(chatroom, '_stream_connor_model', stream))
                    stack.enter_context(patch.object(chatroom, '_load_room_and_messages', AsyncMock(return_value=({},[]))))
                    stack.enter_context(patch.object(chatroom, '_complete_chatroom_memory_search_status', AsyncMock()))
                    save = stack.enter_context(patch.object(chatroom, '_save_msg', AsyncMock()))
                    await chatroom._chatroom_memory_search('private','connor','test',{'requests':[search.MemorySearchRequest('店名')], 'original_question':'那家店叫什么？'})
                    self.assertEqual(save.await_args.args[2], '找到了，是星河面馆。')
                self.assertEqual(len(prompts), 2)
                self.assertEqual(lookup.await_args.args[0], actor)


if __name__ == '__main__':
    unittest.main()
