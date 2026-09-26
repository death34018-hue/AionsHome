"""Critical opt-in streaming speech and chapter revision behavior, no provider calls."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, AsyncMock, Mock

import aiosqlite
import theater_studio as studio


class StudioTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = self.root/'test.db'
        self.patches = [patch.object(studio, 'get_db', lambda: aiosqlite.connect(self.db)),
                        patch('routes.theater._load_personas', return_value=[dict(id='', name='Test', persona='温柔细腻，重视彼此的情感')]),
                        patch.object(studio, 'THEATER_TTS_CACHE_DIR', self.root/'audio')]
        for p in self.patches: p.start()
        async with studio.get_db() as db:
            await db.executescript("CREATE TABLE theater_conversations(id TEXT PRIMARY KEY,title TEXT,model TEXT,persona_id TEXT,created_at REAL,updated_at REAL); INSERT INTO theater_conversations(id,title,model,persona_id) VALUES('book','Test','test',''); CREATE TABLE theater_messages(id TEXT,content TEXT,conv_id TEXT,role TEXT);")
        studio._live.clear()
        studio._locks.clear()
        await studio.save(dict(id='book',kind='book',conv_id='book',**studio.BookPatch(mode='novel',illustrations=False).model_dump()))
        await studio.change('book', phase='writing', consensus='雨夜初遇', outline='相遇相知')
        await studio.save(dict(id='chapter',kind='chapter',conv_id='book',number=1,title='Test',plan='Plan',content='',status='planned',revision=1,summary='',images=[],versions=[],audio=None))

    async def asyncTearDown(self):
        import theater_planning as planning
        await planning.cleanup('book')
        for task in list(studio._speech.values())+list(studio._writing.values()):
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        studio._speech.clear(); studio._writing.clear(); studio._live.clear()
        for p in self.patches: p.stop()
        self.tmp.cleanup()

    async def test_copy_outline_creates_independent_empty_story_with_selected_model(self):
        await studio.change('book', anchors=['anchor-one'], min_chars=5000, max_chars=15000,
                            outline_versions=[{'outline':'旧大纲'}])
        await studio.change('chapter', content='原故事正文', summary='原剧情摘要', status='ready',
                            images=[{'url':'old.png'}], audio='old-audio', versions=[{'content':'更早正文'}],
                            min_chars=6000, max_chars=18000, needs_review=True)
        await studio.save(dict(id='chapter-two',kind='chapter',conv_id='book',number=2,title='第二章',plan='后续计划',content='后续正文',status='ready'))
        await studio.save(dict(id='discussion_book',kind='discussion',conv_id='book',messages=[{'content':'讨论记录'}]))
        original = await studio.rows('book')
        with patch('ws.manager.broadcast', AsyncMock()) as broadcast:
            copied = await studio.copy_outline('book', studio.CopyOutlineRequest(title='另一种写法',model='another-model'))
            broadcast.assert_awaited_once()
        self.assertEqual(await studio.rows('book'), original)
        self.assertEqual(copied['title'], '另一种写法')
        self.assertEqual((await studio.conversation(copied['id']))[1:], ('another-model', ''))
        book = await studio.load(copied['id'])
        self.assertEqual((book['outline'],book['consensus'],book['anchors']), ('相遇相知','雨夜初遇',['anchor-one']))
        self.assertEqual((book['min_chars'],book['max_chars'],book['phase'],book['outline_versions']), (5000,15000,'writing',[]))
        chapters = sorted(await studio.rows(copied['id'],'chapter'), key=lambda c:c['number'])
        self.assertEqual([(c['title'],c['plan']) for c in chapters], [('Test','Plan'),('第二章','后续计划')])
        self.assertEqual(chapters[0]['max_chars'],18000)
        for c in chapters:
            self.assertNotIn(c['id'], ['chapter','chapter-two'])
            self.assertEqual((c['content'],c['summary'],c['images'],c['audio'],c['versions'],c['status']), ('','',[],None,[],'planned'))
            self.assertFalse(c.get('needs_review'))
        self.assertEqual(await studio.rows(copied['id'],'discussion'), [])
        with patch.object(studio,'write_chapter',AsyncMock()):
            await studio.start_writing(chapters[0]['id'],studio.WriteRequest())
            await studio._writing[chapters[0]['id']]
        await studio.change(chapters[0]['id'], plan='副本里的新计划')
        self.assertEqual((await studio.load('chapter'))['plan'], 'Plan')

    async def test_copy_outline_requires_an_existing_outline(self):
        await studio.change('book',outline='')
        with self.assertRaises(studio.HTTPException) as error:
            await studio.copy_outline('book',studio.CopyOutlineRequest())
        self.assertEqual(error.exception.status_code,409)
        async with studio.get_db() as db:
            cursor = await db.execute('SELECT COUNT(*) FROM theater_conversations')
            self.assertEqual((await cursor.fetchone())[0],1)

    async def test_rewrite_and_outline_copy_keep_first_chapter_as_generation_target(self):
        await studio.change('chapter', title='雨夜初遇', plan='在车站第一次相遇，止于交换姓名。',
                            content='旧版正文标记', summary='旧版摘要标记', status='draft')
        await studio.save(dict(id='next', kind='chapter', conv_id='book', number=2,
                               title='翌日重逢', plan='后章专属细节：次日在书店重逢并找回遗失的车票。',
                               content='', summary='', status='planned'))
        with patch('ws.manager.broadcast', AsyncMock()):
            copied = await studio.copy_outline('book', studio.CopyOutlineRequest())
        copied_first = next(c for c in await studio.rows(copied['id'], 'chapter') if c['number'] == 1)
        captured = []
        async def stream(messages, *args, **kwargs):
            captured.append(messages)
            yield '雨落在车站前，两人第一次见面。'
        for key, rewrite in [('chapter', True), (copied_first['id'], False)]:
            with self.subTest(rewrite=rewrite):
                with patch.object(studio, 'stream_ai', stream), patch.object(studio, 'summarize', AsyncMock()), \
                        patch.object(studio, 'illustrate', AsyncMock()):
                    await studio.start_writing(key, studio.WriteRequest(rewrite=rewrite))
                    await studio._writing[key]
                prompt = captured[-1][0]['content']
                self.assertIn('本次只写第1章《雨夜初遇》', prompt)
                self.assertIn('尚无已发生剧情', prompt)
                self.assertIn('从本章计划的第一个场景开始', prompt)
                self.assertIn('在车站第一次相遇，止于交换姓名。', prompt)
                self.assertNotIn('旧版正文标记', prompt)
                self.assertNotIn('旧版摘要标记', prompt)
                self.assertNotIn('后章专属细节', prompt)
                self.assertEqual((await studio.load(key))['number'], 1)
        self.assertEqual((await studio.load('chapter'))['versions'][-1]['content'], '旧版正文标记')
        self.assertEqual((await studio.load('next'))['content'], '')

    async def test_discussion_edit_delete_clear_stale_context(self):
        import theater_planning as p
        doc = await p.discussion('book')
        await studio.change('book', premise='旧脑洞')
        await studio.change(doc['id'], messages=[dict(id='u', role='user', content='旧脑洞'),
                            dict(id='a', role='assistant', content='旧建议')], notes='旧摘要', summarized=1)
        edited = await p.edit_discussion_message('book', 'u', p.Talk(content=' 新脑洞 '))
        self.assertEqual(edited['messages'][0]['content'], '新脑洞')
        self.assertEqual((await studio.load('book'))['premise'], '新脑洞')
        self.assertEqual((edited['notes'], edited['summarized']), ('', 0))
        with self.assertRaises(studio.HTTPException):
            await p.edit_discussion_message('book', 'a', p.Talk(content='不能改'))
        with self.assertRaises(studio.HTTPException):
            await p.edit_discussion_message('book', 'u', p.Talk(content=' '))
        deleted = await p.delete_discussion_message('book', 'a')
        self.assertEqual([m['id'] for m in deleted['messages']], ['u'])
        self.assertNotIn('旧建议', p.discussion_context(deleted))
        await p.delete_discussion_message('book', 'u')
        self.assertEqual((await studio.load('book'))['premise'], '')

    async def test_discussion_regenerate_replaces_reply_and_blocks_concurrent_edits(self):
        import theater_planning as p
        doc = await p.discussion('book')
        await studio.change(doc['id'], messages=[dict(id='u', role='user', content='新脑洞'),
                            dict(id='a', role='assistant', content='不要这条回复')], notes='不要这条回复', summarized=1)
        started, release = asyncio.Event(), asyncio.Event()
        async def generate(cid, prompt, on_chunk=None, *, messages=None):
            self.assertNotIn('不要这条回复', prompt+json.dumps(messages, ensure_ascii=False))
            self.assertEqual(messages[-1]['content'], '新脑洞')
            started.set()
            await release.wait()
            await on_chunk('重新想好了')
            return '重新想好了'
        with patch.object(studio, 'generate_text', generate):
            await p.regenerate_discussion_message('book', 'a')
            task = p._discussing['book']
            await asyncio.wait_for(started.wait(), 5)
            with self.assertRaises(studio.HTTPException) as error:
                await p.delete_discussion_message('book', 'u')
            self.assertEqual(error.exception.status_code, 409)
            release.set()
            await task
        saved = await p.discussion('book')
        self.assertEqual(saved['status'], 'idle')
        self.assertEqual(len(saved['messages']), 2)
        self.assertNotEqual(saved['messages'][-1]['id'], 'a')
        self.assertEqual(saved['messages'][-1]['content'], '重新想好了')

    async def test_discussion_sends_only_chat_with_real_roles_and_optional_chat_summary(self):
        import theater_planning as planning
        await studio.change('book', premise='外部脑洞标记', consensus='外部共识标记', outline='外部大纲标记', anchors=['person'])
        await studio.save(dict(id='anchor_person', kind='anchor', conv_id='', name='外部人物标记', aliases='', appearance='外部外貌标记'))
        await studio.change('chapter', content='外部截断正文标记', summary='', status='interrupted', plan='外部章节计划标记')
        await studio.save(dict(id='chapter2', kind='chapter', conv_id='book', number=2, content='外部完整正文标记', summary='外部章节摘要标记'))
        doc = await planning.discussion('book')
        chat = [dict(id='u0', role='user', content='旧聊天原文'),
                dict(id='a0', role='assistant', content='旧聊天回复'),
                dict(id='u1', role='user', content='我们重新聊聊'),
                dict(id='a1', role='assistant', content='好，你来说')]
        captured = []
        async def stream(messages, *args, **kwargs):
            captured.append(messages)
            yield '记住这个偏好了。'
        for summarized in (0, 2):
            with self.subTest(summarized=summarized):
                await studio.change(doc['id'], messages=chat, notes='旧聊天摘要' if summarized else '', summarized=summarized, status='idle')
                with patch.object(studio, 'stream_ai', stream):
                    await planning.send_discussion('book', planning.Talk(content='最后补充一个写作偏好'))
                    await planning._discussing['book']
                sent = captured[-1]
                self.assertEqual(sent[0]['role'], 'system')
                self.assertIn('温柔细腻，重视彼此的情感', sent[0]['content'])
                self.assertEqual('旧聊天摘要' in sent[0]['content'], bool(summarized))
                self.assertEqual(sent[1:], [dict(role=m['role'], content=m['content']) for m in chat[summarized:]]+
                                 [dict(role='user', content='最后补充一个写作偏好')])
                self.assertNotIn('外部', json.dumps(sent, ensure_ascii=False))
                saved = await planning.discussion('book')
                self.assertEqual(saved['status'], 'idle')
                self.assertEqual(saved['messages'][-1]['content'], '记住这个偏好了。')

    async def test_chapter_budget_override_preserves_prose_and_allows_resume(self):
        await studio.change('book',min_chars=5000,max_chars=15000)
        before=await studio.load('book')
        await studio.change('chapter',content='字'*21000,status='interrupted')
        saved=await studio.edit_chapter('chapter',studio.ChapterPatch(min_chars=5000,max_chars=35000))
        self.assertEqual(saved['content'],'字'*21000)
        self.assertEqual(saved['status'],'interrupted')
        self.assertEqual(await studio.load('book'),before)
        self.assertEqual(studio.chapter_limits(before,saved),(5000,35000))
        self.assertEqual(studio.chapter_limits(before,{}),(5000,15000))
        self.assertIn('0至14000字',studio.chapter_constraints(before,saved['content'],chapter=saved))
        with self.assertRaises(studio.HTTPException):
            await studio.edit_chapter('chapter',studio.ChapterPatch(min_chars=40000))
        self.assertEqual((await studio.load('chapter'))['min_chars'],5000)
        with patch.object(studio,'write_chapter',AsyncMock()):
            await studio.start_writing('chapter',studio.WriteRequest())
            await studio._writing['chapter']
        self.assertEqual((await studio.load('chapter'))['content'],'字'*21000)

    async def test_edit_interrupted_prose_then_continue_from_saved_tail(self):
        original = '保留到断点。第二遍重复正文'
        retained = '保留到断点。'
        await studio.change('chapter',content=original,status='interrupted',error='回复内容过长')
        saved = await studio.edit_chapter('chapter',studio.ChapterPatch(content=retained))
        self.assertEqual(saved['status'],'interrupted')
        self.assertEqual(saved['error'],'')
        self.assertEqual(saved['versions'][-1]['content'],original)
        self.assertEqual((await studio.load('book'))['phase'],'writing')
        async def generate(cid,prompt,on_chunk=None):
            if on_chunk:
                self.assertTrue(prompt.endswith(retained))
                self.assertNotIn('第二遍重复正文',prompt)
                await on_chunk('接着写。')
            return '摘要'
        with patch.object(studio,'generate_text',generate),patch.object(studio,'illustrate',AsyncMock()):
            await studio.start_writing('chapter',studio.WriteRequest())
            await studio._writing['chapter']
        completed = await studio.load('chapter')
        self.assertEqual(completed['content'],retained+'接着写。')
        self.assertEqual(completed['status'],'draft')
        await studio.set_chapter_completion('chapter',studio.ChapterCompletion(completed=True))
        edited = await studio.edit_chapter('chapter',studio.ChapterPatch(content='完成后的文字修订。'))
        self.assertEqual(edited['status'],'ready')

    async def test_directory_counts_remain_available_without_chapter_content(self):
        await studio.change('chapter',content='你好，\n 世界！')
        data=await studio.get_book('book',chapter='other')
        item=data['chapters'][0]
        self.assertNotIn('content',item)
        self.assertEqual(item['word_count'],6)

    async def test_length_range_roundtrip_validation_and_remaining_budget(self):
        old = await studio.load('book')
        saved = await studio.put_book('book', studio.BookPatch(**{**old, 'min_chars':5000, 'max_chars':15000}))
        self.assertEqual(studio.chapter_limits(saved), (5000,15000))
        self.assertEqual(saved['phase'], 'writing')
        with self.assertRaises(studio.HTTPException):
            await studio.put_book('book',studio.BookPatch(min_chars=15000,max_chars=5000))
        self.assertEqual(studio.chapter_limits(await studio.load('book')),(5000,15000))
        self.assertEqual(studio.chapter_limits({'target_chars':6000}),(6000,6000))
        prompt=studio.chapter_constraints(saved,'甲 '*4000)
        self.assertIn('1000至11000字',prompt)
        self.assertIn('写完本章结束节点即停笔',prompt)
        await studio.change('chapter',content='甲'*15000,status='interrupted')
        with self.assertRaises(studio.HTTPException) as error:
            await studio.start_writing('chapter',studio.WriteRequest())
        self.assertIn('字数上限',error.exception.detail)

    async def test_writing_receives_range_and_current_chapter_only(self):
        await studio.change('book',min_chars=5000,max_chars=15000)
        await studio.change('chapter',content='旧正文',status='interrupted')
        await studio.save(dict(id='next',kind='chapter',conv_id='book',number=2,title='后台',plan='后台见面',content='',status='planned'))
        prompts=[]
        async def generate(cid,prompt,on_chunk=None):
            prompts.append(prompt)
            if on_chunk: await on_chunk('继续正文')
            return '继续正文'
        with patch.object(studio,'generate_text',generate),patch.object(studio,'illustrate',AsyncMock()):
            await studio.write_chapter('chapter','')
        self.assertIn('5000至15000字',prompts[0])
        self.assertIn('4997至14997字',prompts[0])
        self.assertNotIn('第2章《后台》',prompts[0])
        self.assertNotIn('后台见面',prompts[0])
        self.assertIn('本次续写第1章《Test》',prompts[0])
        self.assertEqual((await studio.load('chapter'))['content'],'旧正文继续正文')

    async def test_navigation_preserves_phase_and_explicit_resume_keeps_outline(self):
        import theater_planning as planning
        before = await studio.load('book')
        await planning.discussion_mode('book')
        self.assertEqual(await studio.load('book'), before)
        await studio.change('book', phase='discussion')
        chapter = await studio.load('chapter')
        await planning.use_current_outline('book')
        self.assertEqual(await studio.load('book'), before)
        self.assertEqual(await studio.load('chapter'), chapter)
        await studio.change('book', phase='discussion', outline='')
        with self.assertRaises(studio.HTTPException):
            await planning.use_current_outline('book')

    async def test_first_outline_confirmation_stays_in_current_story_and_preserves_discussion(self):
        import theater_planning as planning
        async with studio.get_db() as db:
            await db.execute("DELETE FROM theater_studio WHERE kind='chapter'")
            await db.commit()
        await studio.change('book', phase='discussion', outline='', consensus='')
        doc = await planning.discussion('book')
        doc = await studio.change(doc['id'], messages=[dict(id='u', role='user', content='本次脑洞')])
        proposal = dict(consensus='新故事共识', outline='相遇到结局', chapters=[
            dict(title='初遇', plan='雨夜初遇', min_chars=5000, max_chars=8000)])
        with patch.object(studio, 'generate_text', AsyncMock(return_value=json.dumps(proposal))):
            result = await planning.make_outline('book')
        self.assertEqual(await studio.rows('book', 'chapter'), [])
        body = planning.ConfirmOutline(revision=result['outline_draft']['revision'])
        with patch('ws.manager.broadcast', AsyncMock()) as broadcast:
            result = await planning.confirm_outline('book', body)
            self.assertEqual(result['conversation']['id'], 'book')
            self.assertEqual(result['conversation']['title'], 'Test')
            self.assertEqual(await planning.confirm_outline('book', body), result)
            broadcast.assert_not_awaited()
        async with studio.get_db() as db:
            count = await (await db.execute('SELECT COUNT(*) FROM theater_conversations')).fetchone()
        self.assertEqual(count[0], 1)
        self.assertEqual(await studio.load(doc['id']), doc)
        book = await studio.load('book')
        self.assertEqual((book['phase'], book['premise'], book['outline']), ('writing', '本次脑洞', '相遇到结局'))
        chapters = await studio.rows('book', 'chapter')
        self.assertEqual(len(chapters), 1)
        self.assertEqual((chapters[0]['number'], chapters[0]['min_chars'], chapters[0]['max_chars']), (1, 5000, 8000))
        self.assertEqual(chapters[0]['content'], '')
        self.assertIsNone((await studio.get_book('book'))['outline_draft'])

    async def test_outline_draft_uses_only_discussion_and_confirm_creates_independent_story(self):
        import theater_planning as planning
        await studio.change('book', premise='旧脑洞标记', outline='旧大纲标记', consensus='旧共识标记')
        await studio.change('chapter', content='旧正文标记', summary='', status='interrupted', images=[{'url':'old.png'}])
        doc=await planning.discussion('book')
        await studio.change(doc['id'],messages=[dict(id='u',role='user',content='本次讨论：雨夜初遇')])
        original_book=await studio.load('book');original_chapter=await studio.load('chapter')
        proposal=dict(consensus='陌生人慢慢相爱',outline='雨夜相遇后逐渐相知',chapters=[dict(title='雨夜',plan='初见'),dict(title='同行',plan='逐渐相知')])
        async def stream(messages,*args,**kwargs):
            self.assertEqual(messages[0]['role'],'system')
            text=json.dumps(messages,ensure_ascii=False)
            self.assertIn('本次讨论：雨夜初遇',text)
            self.assertNotIn('旧脑洞标记',text);self.assertNotIn('旧大纲标记',text)
            self.assertNotIn('旧共识标记',text);self.assertNotIn('旧正文标记',text)
            yield json.dumps(proposal,ensure_ascii=False)
        with patch.object(studio,'stream_ai',stream),patch.object(studio,'summarize',AsyncMock()) as summary:
            result=await planning.make_outline('book')
            summary.assert_not_awaited()
        draft=result['outline_draft']
        self.assertEqual(len(draft['plans']),2)
        self.assertEqual(await studio.load('book'),original_book)
        self.assertEqual(await studio.load('chapter'),original_chapter)
        self.assertEqual((await studio.get_book('book'))['outline_draft']['revision'],draft['revision'])
        with self.assertRaises(studio.HTTPException):await planning.confirm_outline('book')
        edited=planning.OutlineDocument(**{**draft,'outline':'编辑后的完整走向'})
        draft=await planning.edit_outline_draft('book',edited)
        with self.assertRaises(studio.HTTPException):await planning.edit_outline_draft('book',edited)
        with patch.object(studio,'generate_text',AsyncMock(return_value='并不是大纲JSON')):
            with self.assertRaises(studio.HTTPException) as error:await planning.make_outline('book')
            self.assertIn('模型已回复',error.exception.detail)
        self.assertEqual(await studio.load(draft['id']),draft)
        with patch.object(studio,'generate_text',AsyncMock(return_value='')):
            with self.assertRaises(studio.HTTPException) as error:await planning.make_outline('book')
            self.assertIn('没有返回',error.exception.detail)
        with patch('ws.manager.broadcast',AsyncMock()):
            confirmed=await planning.confirm_outline('book',planning.ConfirmOutline(revision=draft['revision'],title='新版本'))
            again=await planning.confirm_outline('book',planning.ConfirmOutline(revision=draft['revision']))
        self.assertEqual(confirmed,again)
        cid=confirmed['conversation']['id']
        self.assertNotEqual(cid,'book')
        self.assertIsNone((await studio.get_book('book'))['outline_draft'])
        self.assertEqual(await studio.load('book'),original_book)
        self.assertEqual(await studio.load('chapter'),original_chapter)
        copy=await studio.load(cid)
        self.assertEqual((copy['outline'],copy['phase']),('编辑后的完整走向','writing'))
        self.assertEqual(copy['premise'],'本次讨论：雨夜初遇')
        chapters=sorted(await studio.rows(cid,'chapter'),key=lambda c:c['number'])
        self.assertEqual([c['number'] for c in chapters],[1,2])
        for c in chapters:
            self.assertEqual((c['content'],c['summary'],c['images'],c['audio'],c['status']),('','',[],None,'planned'))

    async def test_complete_outline_editor_saves_any_chapter_without_changing_prose(self):
        import theater_planning as planning
        await studio.change('chapter',content='已写正文',summary='已发生事件',audio='recording',status='ready')
        await studio.save(dict(id='two',kind='chapter',conv_id='book',number=2,title='后章',plan='后章计划',content='',status='planned'))
        first=await studio.load('chapter')
        body=planning.OutlineDocument(consensus='新共识',outline='全书走向',plans=[
            dict(id='chapter',title='新标题',plan='第一章新计划',min_chars=5000,max_chars=9000),
            dict(id='two',title='第二章',plan='第二章修改后的计划')])
        await planning.edit_current_outline('book',body)
        updated=await studio.load('chapter')
        for key in ('content','summary','audio','status'):self.assertEqual(updated[key],first[key])
        self.assertEqual(updated['plan'],'第一章新计划')
        self.assertEqual((await studio.load('two'))['plan'],'第二章修改后的计划')
        self.assertEqual((await studio.load('book'))['phase'],'writing')

    async def test_outline_reports_provider_errors_and_keeps_unparseable_response(self):
        import theater_planning as planning
        doc=await planning.discussion('book')
        await studio.change(doc['id'],messages=[dict(role='user',content='规划一个重逢的故事')])
        original=await studio.load('book')
        for response,expected,status in [
            ('[硅基流动错误 429] 请求太频繁','模型接口返回错误','provider_error'),
            ('这里是用普通文字写的大纲，没有JSON结构。','回复中没有JSON大纲','invalid'),
            ('{"consensus":"共识","outline":"总纲","chapters":[', 'JSON无法解析','invalid')]:
            async def stream(*args,**kwargs):
                yield response
            with patch.object(studio,'stream_ai',stream):
                with self.assertRaises(studio.HTTPException) as error:await planning.make_outline('book')
            self.assertIn(expected,error.exception.detail)
            attempt=await studio.load('outline_attempt_book')
            self.assertEqual(attempt['raw_response'],response)
            self.assertEqual(attempt['status'],status)
            self.assertEqual(await studio.load('book'),original)
            self.assertIsNone(await studio.load('outline_draft_book'))

    async def test_outline_punctuation_compatibility_saves_a_valid_draft(self):
        import theater_planning as planning
        doc=await planning.discussion('book')
        await studio.change(doc['id'],messages=[dict(role='user',content='重新规划雨夜相遇')])
        original=await studio.load('book')
        raw='''大纲如下：｛“故事共识”：“平等，慢热；保持自然。”，“全书大纲”：“雨夜初遇，随后重逢。”，
        “章节列表”：［｛“章节标题”：“雨夜”，＂章节计划＂：＂核心变化：认识彼此
必须保留：他说“Wait，别走！”；停在交换名字。＂，｝，］，｝'''
        async def stream(*args,**kwargs):yield raw
        with patch.object(studio,'stream_ai',stream):result=await planning.make_outline('book')
        draft=result['outline_draft']
        self.assertEqual(draft['consensus'],'平等，慢热；保持自然。')
        self.assertEqual(draft['plans'][0]['plan'],'核心变化：认识彼此\n必须保留：他说“Wait，别走！”；停在交换名字。')
        self.assertEqual(await studio.load('book'),original)
        attempt=await studio.load('outline_attempt_book')
        self.assertEqual((attempt['status'],attempt['raw_response']),('ready',raw))

    async def test_detailed_outline_supports_more_chapters_and_reaches_the_writer(self):
        import theater_planning as planning
        await studio.change('book',min_chars=5000,max_chars=15000)
        plan=('核心变化：从客气到愿意保持联系\n期望字数：7500\n'
              '场景与详略：进入后台略写1000字；关心伤势详写3500字；交换联系方式2000字；离开收尾1000字\n'
              '必须保留：询问疼不疼\n结束节点：交换联系方式后离开\n后章边界：下次见面留到后章')
        doc=await planning.discussion('book')
        await studio.change(doc['id'],messages=[dict(role='user',content='一起规划相识的故事')])
        proposal=dict(consensus='保持平等关系',outline='逐步认识彼此',
                      chapters=[dict(title=f'第{i+1}次相处',plan=plan) for i in range(14)])
        calls=[]
        async def stream(messages, model, **kwargs):
            calls.append((messages[0]['content'],kwargs['max_tokens']))
            yield json.dumps(proposal,ensure_ascii=False) if kwargs['max_tokens']==16000 else '新正文。'
        with patch.object(studio,'stream_ai',stream),patch.object(studio,'illustrate',AsyncMock()):
            result=await planning.make_outline('book')
            draft=result['outline_draft']
            self.assertEqual(len(draft['plans']),14)
            self.assertEqual(draft['plans'][0]['plan'],plan)
            self.assertEqual(calls[0][1],16000)
            self.assertIn('5000至15000字',calls[0][0])
            self.assertIn('以约7500字安排单章事件量',calls[0][0])
            confirmed=await planning.confirm_outline('book',planning.ConfirmOutline(revision=draft['revision']))
            new_chapters=sorted(await studio.rows(confirmed['conversation']['id'],'chapter'),key=lambda c:c['number'])
            self.assertEqual([c['number'] for c in new_chapters],list(range(1,15)))
            key=new_chapters[0]['id']
            await studio.start_writing(key,studio.WriteRequest())
            await studio._writing[key]
        self.assertIn(plan,calls[1][0])
        self.assertEqual(calls[1][1],24000)
        self.assertEqual(calls[2][1],6000)
        self.assertEqual((await studio.load(key))['content'],'新正文。')

    async def test_long_discussion_compacts_without_deleting_transcript(self):
        import theater_planning as planning
        doc = await planning.discussion('book')
        messages = [dict(id=str(i), role='user' if i%2==0 else 'assistant', content='脑洞'+str(i)) for i in range(21)]
        doc = await studio.change(doc['id'], messages=messages[:20])
        with patch.object(studio, 'generate_text', AsyncMock(return_value='确认慢热；否定失忆')) as generate:
            unchanged = await planning.compact('book', doc)
            self.assertEqual(unchanged['summarized'], 0)
            generate.assert_not_awaited()
            doc = await studio.change(doc['id'], messages=messages)
            compact = await planning.compact('book', doc)
            generate.assert_awaited_once()
        self.assertEqual(compact['messages'], messages)
        self.assertEqual(compact['summarized'], 13)
        context = planning.discussion_context(compact)
        self.assertIn('确认慢热；否定失忆', context)
        self.assertNotIn('"脑洞0"', context)
        self.assertIn('脑洞20', context)

    async def test_chapter_context_uses_consensus_and_no_raw_discussion(self):
        import theater_planning as planning
        doc = await planning.discussion('book')
        await studio.change(doc['id'], messages=[dict(id='1',role='user',content='不要发送的旧讨论原文')])
        prompts = []
        async def generate(cid,prompt,on_chunk=None):
            prompts.append(prompt)
            if on_chunk: await on_chunk('新的正文。')
            return '本章事实'
        with patch.object(studio,'generate_text',generate):
            await studio.start_writing('chapter',studio.WriteRequest())
            await studio._writing['chapter']
        self.assertIn('雨夜初遇', prompts[0])
        self.assertNotIn('不要发送的旧讨论原文', prompts[0])
        self.assertEqual((await studio.load('chapter'))['context_characters']['历史讨论原文'],0)

    async def test_later_chapter_receives_summaries_without_future_plans_or_previous_prose(self):
        await studio.change('book', outline='全书未来走向标记')
        await studio.change('chapter', content='前章正文标记', summary='前章摘要标记', status='ready')
        await studio.save(dict(id='second', kind='chapter', conv_id='book', number=2,
                               title='重逢', plan='本章计划标记', content='', summary='',
                               status='planned', revision=1, versions=[]))
        await studio.save(dict(id='third', kind='chapter', conv_id='book', number=3,
                               title='后章标题标记', plan='后章计划标记', content='', status='planned'))
        captured = []
        async def stream(messages, *args, **kwargs):
            captured.append(messages)
            yield '新的正文。'
        with patch.object(studio, 'stream_ai', stream), patch.object(studio, 'summarize', AsyncMock()), \
                patch.object(studio, 'illustrate', AsyncMock()):
            await studio.start_writing('second', studio.WriteRequest(instruction='本次指导标记'))
            await studio._writing['second']
        prompt = captured[0][0]['content']
        for kept in ('温柔细腻', '雨夜初遇', '前章摘要标记', '本章计划标记', '本次指导标记', '本次只写第2章《重逢》'):
            self.assertIn(kept, prompt)
        for excluded in ('全书未来走向标记', '后章标题标记', '后章计划标记', '前章正文标记', '尚无已发生剧情'):
            self.assertNotIn(excluded, prompt)
        stats = (await studio.load('second'))['context_characters']
        self.assertEqual((stats['全书大纲'], stats['上一章结尾']), (0, 0))
        self.assertGreater(stats['前章剧情摘要'], 0)

    async def test_persona_reaches_provider_and_missing_persona_blocks_discussion(self):
        import theater_planning as planning
        captured = []
        async def stream(messages, *args, **kwargs):
            captured.extend(messages)
            yield '自然回应。'
        with patch.object(studio, 'stream_ai', stream):
            await studio.generate_text('book', '一起讨论初遇的背景')
        self.assertIn('温柔细腻，重视彼此的情感',captured[0]['content'])
        with patch('routes.theater._load_personas',return_value=[]):
            with self.assertRaises(studio.HTTPException):
                await planning.send_discussion('book',planning.Talk(content='脑洞'))

    async def test_novel_provider_wait_and_errors_are_visible(self):
        async def slow_reply(messages, model, **kwargs):
            self.assertEqual(kwargs['request_timeout'], 300)
            await asyncio.sleep(.02)
            yield '小说正文'

        with patch.object(studio, 'stream_ai', slow_reply):
            self.assertEqual(await studio.generate_text('book', '写一章'), '小说正文')

        async def provider_error(messages, model, **kwargs):
            self.assertEqual(kwargs['request_timeout'], 300)
            kwargs['meta']['provider_error'] = 'HTTP 429: {"error":{"message":"quota exceeded"}}'
            yield '{"error":{"message":"quota exceeded"}}'

        with patch.object(studio, 'stream_ai', provider_error):
            with self.assertRaises(studio.ModelRequestError) as error:
                await studio.generate_text('book', '写一章', lambda chunk: None)
        self.assertIn('HTTP 429：quota exceeded', str(error.exception))
        with patch.object(studio, 'stream_ai', provider_error):
            await studio.start_writing('chapter', studio.WriteRequest())
            await studio._writing['chapter']
        saved = await studio.load('chapter')
        self.assertEqual(saved['content'], '')
        self.assertIn('HTTP 429：quota exceeded', saved['error'])

        async def provider_timeout(messages, model, **kwargs):
            self.assertEqual(kwargs['request_timeout'], 300)
            kwargs['meta']['provider_timeout'] = 300
            raise TimeoutError()
            yield ''

        with patch.object(studio, 'stream_ai', provider_timeout):
            with self.assertRaisesRegex(RuntimeError, '本次等待 5 分钟仍未收到回复'):
                await studio.generate_text('book', '写一章', lambda chunk: None)

    async def test_chapter_replay_prefers_complete_original_over_new_voice_partial(self):
        await studio.change('chapter', content='完整正文', status='ready')
        root = studio.THEATER_TTS_CACHE_DIR
        root.mkdir()
        for aid, voice, status in [('original', 'old', 'ready'), ('partial', 'new', 'stopped')]:
            (root/f'{aid}_0.mp3').write_bytes(b'recorded')
            await studio.save(dict(id=aid, kind='audio', conv_id='book', source='chapter', revision=1,
                                   voice=voice, status=status, offset=4 if status == 'ready' else 1,
                                   segments=[dict(seq=0, url=f'/audio/{aid}/0')]))
        await studio.change('chapter', audio='partial')
        with patch.object(studio, '_request_tts_audio', AsyncMock()) as synth:
            for voice in ['new', '']:
                audio = await studio.start_speech('chapter', studio.SpeechRequest(voice=voice))
                self.assertEqual((audio['id'], audio['voice']), ('original', 'old'))
            self.assertNotIn('chapter', studio._speech)
            synth.assert_not_awaited()
        self.assertEqual((await studio.load('chapter'))['audio'], 'original')
        (root/'original_0.mp3').unlink()
        with self.assertRaises(studio.HTTPException) as error:
            await studio.start_speech('chapter', studio.SpeechRequest(voice='new'))
        self.assertEqual(error.exception.status_code, 409)
        self.assertNotIn('chapter', studio._speech)

    async def test_listening_to_partial_recording_does_not_resume_synthesis(self):
        await studio.change('chapter', content='完整正文', status='ready')
        root = studio.THEATER_TTS_CACHE_DIR
        root.mkdir()
        (root/'partial_0.mp3').write_bytes(b'recorded')
        await studio.save(dict(id='partial', kind='audio', conv_id='book', source='chapter', revision=1,
                               voice='old', status='running', offset=1,
                               segments=[dict(seq=0, url='/audio/partial/0')]))
        with patch.object(studio, '_request_tts_audio', AsyncMock()) as synth:
            audio = await studio.start_speech('chapter', studio.SpeechRequest(voice='new'))
            self.assertEqual((audio['id'], audio['status']), ('partial', 'stopped'))
            self.assertNotIn('chapter', studio._speech)
            synth.assert_not_awaited()

    async def test_new_chapter_speech_requires_explicit_confirmation(self):
        await studio.change('chapter', content='完整正文', status='ready')
        with patch.object(studio, '_request_tts_audio', AsyncMock(return_value=b'mp3')) as synth:
            result = await studio.start_speech('chapter', studio.SpeechRequest(voice='test'))
            self.assertTrue(result['needs_confirmation'])
            self.assertEqual(await studio.rows('book', 'audio'), [])
            self.assertNotIn('chapter', studio._speech)
            synth.assert_not_awaited()
            audio = await studio.start_speech('chapter', studio.SpeechRequest(voice='test', allow_generation=True))
            await studio._speech['chapter']
            self.assertEqual((await studio.load(audio['id']))['status'], 'ready')
            synth.assert_awaited_once()

    async def test_midstream_opt_in_consumes_prefix_and_new_text_exactly_once(self):
        calls=[]
        async def synth(text, voice, **kwargs):
            calls.append(text)
            await asyncio.sleep(.005)
            return b'mp3'
        prefix='初遇的雨夜。'*100
        suffix='后来我们一起回家。'*110
        studio.begin_live('chapter','book');studio.append_live('chapter',prefix)
        await asyncio.sleep(.01)
        self.assertEqual(calls, [])
        with patch.object(studio,'_request_tts_audio',synth):
            a=await studio.start_speech('chapter',studio.SpeechRequest(voice='test', allow_generation=True, prefer_cached=False))
            again=await studio.start_speech('chapter',studio.SpeechRequest(voice='test', allow_generation=True, prefer_cached=False))
            self.assertEqual(a['id'],again['id'])
            studio.append_live('chapter',suffix);studio.finish_live('chapter')
            await asyncio.wait_for(studio._speech['chapter'],2)
        result=await studio.load(a['id'])
        self.assertEqual(''.join(calls),prefix+suffix)
        self.assertEqual(result['offset'],len(prefix+suffix))
        self.assertEqual(result['status'],'ready')
        self.assertTrue(all(len(s)<=500 for s in calls))
        self.assertEqual([(prefix+suffix)[s['start']:s['end']] for s in result['segments']], calls)
        self.assertEqual([s['end']-s['start'] for s in result['segments']],
                         [s['chars'] for s in result['segments']])

    async def test_speech_positions_preserve_skipped_markup_and_unicode(self):
        silent = '<meta>不朗读的内容</meta>\n\n'
        prose = '🌙一起听故事。'*80
        await studio.change('chapter', content=silent+prose, status='ready')
        original_cut = studio._find_cut_position_for_text
        def cut(text, low, high):
            return len(silent)-1 if text.startswith(silent) else original_cut(text, low, high)
        with patch.object(studio, '_find_cut_position_for_text', cut), \
                patch.object(studio, '_request_tts_audio', AsyncMock(return_value=b'mp3')):
            audio = await studio.start_speech('chapter', studio.SpeechRequest(voice='test', allow_generation=True))
            await studio._speech['chapter']
        segments = (await studio.audio_status(audio['id']))['segments']
        self.assertEqual(segments[0]['start'], len(silent))
        self.assertEqual(''.join((silent+prose)[s['start']:s['end']] for s in segments), prose)

    async def test_failure_retries_only_unfinished_segment(self):
        text='这一段要认真听。'*130
        await studio.change('chapter',content=text,status='ready')
        calls=[]
        async def synth(s, voice, **kwargs):
            calls.append(s)
            return None if len(calls)==2 else b'mp3'
        with patch.object(studio,'_request_tts_audio',synth):
            a=await studio.start_speech('chapter',studio.SpeechRequest(voice='test', allow_generation=True, prefer_cached=False))
            await studio._speech['chapter']
            failed=await studio.load(a['id'])
            self.assertEqual(failed['status'],'failed')
            self.assertEqual(len(failed['segments']),1)
            await studio.start_speech('chapter',studio.SpeechRequest(voice='test', allow_generation=True, prefer_cached=False))
            await studio._speech['chapter']
        self.assertEqual(calls[1],calls[2])
        self.assertEqual(''.join([calls[0]]+calls[2:]),text)

    async def test_switching_voice_stops_old_synthesis_and_creates_separate_audio(self):
        started=asyncio.Event()
        async def synth(text, voice, **kwargs):
            if voice=='old':
                started.set(); await asyncio.Event().wait()
            return b'new-edge-audio'
        await studio.change('chapter',content='今晚的故事。'*100,status='ready')
        with patch.object(studio,'_request_tts_audio',synth):
            old=await studio.start_speech('chapter',studio.SpeechRequest(voice='old', allow_generation=True))
            await started.wait()
            new=await studio.start_speech('chapter',studio.SpeechRequest(voice='edge:zh-CN-XiaoxiaoNeural', prefer_cached=False, allow_generation=True))
            self.assertNotEqual(new['id'],old['id'])
            await studio._speech['chapter']
        self.assertEqual((await studio.load(old['id']))['status'],'stopped')
        self.assertEqual((await studio.load(new['id']))['status'],'ready')

    async def test_new_voice_does_not_relabel_legacy_audio_with_unknown_voice(self):
        async with studio.get_db() as db:
            await db.execute('INSERT INTO theater_messages VALUES(?,?,?,?)',('tm_legacy','晚安。','book','assistant'))
            await db.commit()
        studio.THEATER_TTS_CACHE_DIR.mkdir(parents=True,exist_ok=True)
        legacy=studio.THEATER_TTS_CACHE_DIR/'tm_legacy.mp3'
        legacy.write_bytes(b'old-unknown-voice')
        with patch.object(studio,'_request_tts_audio',new=AsyncMock(return_value=b'new-edge-audio')) as synth:
            audio=await studio.start_speech('tm_legacy',studio.SpeechRequest(voice='edge:zh-CN-XiaoxiaoNeural',prefer_cached=False,allow_generation=True))
            self.assertEqual(audio['segments'],[])
            await studio._speech['tm_legacy']
        synth.assert_awaited_once()
        self.assertEqual(legacy.read_bytes(),b'old-unknown-voice')

    async def test_legacy_replay_reuses_original_audio_even_without_or_with_another_voice(self):
        async with studio.get_db() as db:
            await db.execute('INSERT INTO theater_messages VALUES(?,?,?,?)',('tm_cached','旧回复','book','assistant'))
            await db.commit()
        root=studio.THEATER_TTS_CACHE_DIR
        root.mkdir(parents=True,exist_ok=True)
        (root/'tm_cached.mp3').write_bytes(b'original-recording')
        (root/'tm_cached_s0.mp3').write_bytes(b'old-segment')
        with patch.object(studio,'_request_tts_audio',AsyncMock()) as synth:
            audio=await studio.start_speech('tm_cached',studio.SpeechRequest())
            again=await studio.start_speech('tm_cached',studio.SpeechRequest(voice='different-voice'))
            self.assertEqual(audio,again)
            self.assertEqual(audio['status'],'ready')
            self.assertEqual(audio['voice'],'')
            self.assertTrue(audio['legacy'])
            self.assertEqual([s['url'] for s in audio['segments']],['/api/theater/tts/audio/tm_cached'])
            self.assertEqual((await studio.audio_status(audio['id']))['segments'],audio['segments'])
            self.assertNotIn('tm_cached',studio._speech)
            synth.assert_not_awaited()
        self.assertEqual((root/'tm_cached.mp3').read_bytes(),b'original-recording')

    async def test_legacy_segment_replay_preserves_numeric_order_without_filling_gaps(self):
        async with studio.get_db() as db:
            await db.execute('INSERT INTO theater_messages VALUES(?,?,?,?)',('tm_segments','旧回复','book','assistant'))
            await db.commit()
        root=studio.THEATER_TTS_CACHE_DIR
        root.mkdir(parents=True,exist_ok=True)
        for seq in [10,0,2]:
            (root/f'tm_segments_s{seq}.mp3').write_bytes(b'old-audio')
        with patch.object(studio,'_request_tts_audio',AsyncMock()) as synth:
            audio=await studio.start_speech('tm_segments',studio.SpeechRequest(voice='new'))
            self.assertEqual([s['seq'] for s in audio['segments']],[0,1,2])
            self.assertEqual([s['url'] for s in audio['segments']],
                             [f'/api/theater/tts/audio/tm_segments_s{i}' for i in [0,2,10]])
            self.assertNotIn('tm_segments',studio._speech)
            # A missing linked recording must not silently trigger replacement synthesis.
            for path in root.glob('*.mp3'): path.unlink()
            with self.assertRaises(studio.HTTPException):
                await studio.start_speech('tm_segments',studio.SpeechRequest(voice='new'))
            synth.assert_not_awaited()

    async def test_dialogue_replay_reuses_studio_recording_with_its_original_voice(self):
        async with studio.get_db() as db:
            await db.execute('INSERT INTO theater_messages VALUES(?,?,?,?)',('tm_studio','旧回复','book','assistant'))
            await db.commit()
        root=studio.THEATER_TTS_CACHE_DIR
        root.mkdir(parents=True,exist_ok=True)
        aid=studio.uid('na_')
        (root/f'{aid}_0.mp3').write_bytes(b'recorded-voice')
        await studio.save(dict(id=aid,kind='audio',conv_id='book',source='tm_studio',revision=1,voice='original',
                               status='ready',offset=3,segments=[dict(seq=0,url=f'/api/theater/studio/audio/{aid}/0')]))
        with patch.object(studio,'_request_tts_audio',AsyncMock()) as synth:
            audio=await studio.start_speech('tm_studio',studio.SpeechRequest(voice='different'))
            self.assertEqual((audio['id'],audio['voice']),(aid,'original'))
            self.assertNotIn('tm_studio',studio._speech)
            synth.assert_not_awaited()

    async def test_stop_discards_inflight_audio_and_does_not_stop_writing(self):
        started=asyncio.Event()
        async def synth(*args,**kwargs):
            started.set(); await asyncio.Event().wait()
        studio.begin_live('chapter','book');studio.append_live('chapter','长长的故事。'*100)
        with patch.object(studio,'_request_tts_audio',synth):
            a=await studio.start_speech('chapter',studio.SpeechRequest(voice='test', allow_generation=True, prefer_cached=False))
            await started.wait();await studio.cancel_speech('chapter')
        self.assertEqual((await studio.load(a['id']))['status'],'stopped')
        self.assertEqual((await studio.load(a['id']))['segments'],[])
        self.assertFalse(studio._live['chapter']['done'])

    async def test_edit_and_restore_preserve_later_completed_chapter(self):
        await studio.change('chapter',content='旧正文',status='ready')
        studio.begin_live('chapter','book');studio.append_live('chapter','旧正文');studio.finish_live('chapter')
        await studio.save(dict(id='later',kind='chapter',conv_id='book',number=2,content='后文',status='ready'))
        changed=await studio.edit_chapter('chapter',studio.ChapterPatch(content='新正文'))
        self.assertEqual(changed['revision'],2)
        self.assertEqual(changed['versions'][0]['content'],'旧正文')
        self.assertEqual((await studio.source_state('chapter'))['content'],'新正文')
        self.assertEqual((await studio.load('later'))['status'],'ready')
        self.assertFalse((await studio.load('later')).get('needs_review'))
        restored=await studio.restore_chapter('chapter')
        self.assertEqual(restored['content'],'旧正文')
        self.assertEqual(restored['revision'],3)
        self.assertEqual((await studio.load('later'))['content'],'后文')
        self.assertFalse((await studio.load('later')).get('needs_review'))

    async def test_rewriting_chapter_five_does_not_require_reviewing_later_chapters(self):
        await studio.change('chapter', content='第一章正文', status='ready', summary='第一章摘要')
        for number in range(2, 11):
            await studio.save(dict(id=f'chapter-{number}',kind='chapter',conv_id='book',number=number,
                                   title=f'第{number}章',plan=f'第{number}章计划',content=f'第{number}章原文',
                                   summary=f'第{number}章摘要',status='ready',revision=1,images=[],versions=[]))
        async def generate(cid, prompt, on_chunk=None):
            if on_chunk:
                await on_chunk('本章重写正文')
            return '本章新摘要'
        with patch.object(studio, 'generate_text', generate), patch.object(studio, 'illustrate', AsyncMock()):
            await studio.start_writing('chapter-5', studio.WriteRequest(rewrite=True))
            await studio._writing['chapter-5']
        self.assertEqual((await studio.load('chapter-5'))['status'], 'draft')
        await studio.set_chapter_completion('chapter-5', studio.ChapterCompletion(completed=True))
        for number in range(6, 11):
            later=await studio.load(f'chapter-{number}')
            self.assertEqual((later['status'],later['content']),('ready',f'第{number}章原文'))
            self.assertFalse(later.get('needs_review'))
        # Older stories may already have review flags saved by the previous behavior.
        await studio.change('chapter-6', needs_review=True)
        with patch.object(studio, 'generate_text', generate), patch.object(studio, 'illustrate', AsyncMock()):
            await studio.start_writing('chapter-10', studio.WriteRequest(rewrite=True))
            await studio._writing['chapter-10']
        self.assertEqual((await studio.load('chapter-10'))['status'], 'draft')

    async def test_opening_an_existing_story_clears_obsolete_review_flags(self):
        await studio.change('chapter', content='已完成正文', status='ready', needs_review=True)
        data=await studio.get_book('book')
        self.assertFalse(data['chapters'][0].get('needs_review'))
        self.assertFalse((await studio.load('chapter')).get('needs_review'))
        self.assertEqual(data['chapters'][0]['status'], 'ready')

    async def test_writing_stream_saves_draft_without_starting_audio(self):
        first=asyncio.Event(); proceed=asyncio.Event()
        async def generate(cid,prompt,on_chunk=None):
            if on_chunk:
                await on_chunk('故事的开头。');first.set();await proceed.wait();await on_chunk('故事的结尾。')
            return '剧情摘要'
        with patch.object(studio,'generate_text',generate):
            await studio.start_writing('chapter',studio.WriteRequest())
            task=studio._writing['chapter'];await first.wait()
            self.assertEqual((await studio.load('chapter'))['content'],'故事的开头。')
            self.assertNotIn('chapter',studio._speech)
            proceed.set();await task
        self.assertEqual((await studio.load('chapter'))['status'],'draft')
        self.assertEqual((await studio.load('chapter'))['content'],'故事的开头。故事的结尾。')

    async def test_user_controls_completion_and_can_resume_old_completed_chapter(self):
        with self.assertRaises(studio.HTTPException):
            await studio.set_chapter_completion('chapter', studio.ChapterCompletion(completed=True))
        await studio.change('chapter', content='旧版误判完成的半句', status='ready', summary='旧摘要')
        reopened = await studio.set_chapter_completion('chapter', studio.ChapterCompletion(completed=False))
        self.assertEqual(reopened['status'], 'draft')
        self.assertEqual(reopened['content'], '旧版误判完成的半句')
        await studio.save(dict(id='later',kind='chapter',conv_id='book',number=2,title='次章',plan='接着相处',content='',status='planned',revision=1))
        with self.assertRaises(studio.HTTPException):
            await studio.start_writing('later', studio.WriteRequest())
        started, proceed = asyncio.Event(), asyncio.Event()
        async def generate(cid, prompt, on_chunk=None):
            if on_chunk:
                self.assertTrue(prompt.endswith('旧版误判完成的半句'))
                started.set()
                await proceed.wait()
                await on_chunk('，现在接着写完。')
            return '更新后的摘要'
        with patch.object(studio, 'generate_text', generate), patch.object(studio, 'illustrate', AsyncMock()):
            await studio.start_writing('chapter', studio.WriteRequest())
            task = studio._writing['chapter']
            await asyncio.wait_for(started.wait(), 5)
            with self.assertRaises(studio.HTTPException):
                await studio.set_chapter_completion('chapter', studio.ChapterCompletion(completed=True))
            proceed.set()
            await task
        draft = await studio.load('chapter')
        self.assertEqual(draft['status'], 'draft')
        self.assertEqual(draft['content'], '旧版误判完成的半句，现在接着写完。')
        self.assertEqual(draft['summary'], '更新后的摘要')
        self.assertEqual(draft['revision'], 2)
        finished = await studio.set_chapter_completion('chapter', studio.ChapterCompletion(completed=True))
        self.assertEqual(finished['status'], 'ready')
        with patch.object(studio, 'write_chapter', AsyncMock()):
            await studio.start_writing('later', studio.WriteRequest())
            await studio._writing['later']

    async def test_restart_marks_draft_interrupted_and_blocks_skipping_chapters(self):
        await studio.change('chapter',status='writing',content='部分正文')
        await studio.get_book('book')
        self.assertEqual((await studio.load('chapter'))['status'],'interrupted')
        await studio.save(dict(id='later',kind='chapter',conv_id='book',number=2,content='',status='planned'))
        with self.assertRaises(studio.HTTPException) as error:
            await studio.start_writing('later',studio.WriteRequest())
        self.assertEqual(error.exception.status_code,409)

    async def test_named_reference_photos_reach_custom_provider_in_order(self):
        import image_gen
        import base64
        refs=[]
        for i, name in enumerate(['角色甲','角色乙','角色丙']):
            path=self.root/f'{i}.png';path.write_bytes(name.encode())
            refs.append({'name':name,'path':str(path)})
        response=Mock()
        response.json.return_value={'choices':[{'message':{'content':'data:image/png;base64,eA=='}}]}
        client=AsyncMock();client.__aenter__.return_value=client;client.post.return_value=response
        with patch.object(image_gen.httpx,'AsyncClient',return_value=client), patch.object(image_gen,'save_generated_image',new=AsyncMock(return_value='album/test.png')):
            result=await image_gen._generate_openai_image('三人同框',False,'','https://example.invalid/v1','fake','test',references=refs)
        self.assertEqual(result,'album/test.png')
        content=client.post.call_args.kwargs['json']['messages'][0]['content']
        for i, ref in enumerate(refs):
            self.assertIn(ref['name'],content[1+i*2]['text'])
            self.assertEqual(content[2+i*2]['image_url']['url'],'data:image/png;base64,'+base64.b64encode(ref['name'].encode()).decode())

    async def test_deleting_story_cancels_speech_for_a_historical_dialogue_message(self):
        async with studio.get_db() as db:
            await db.execute('INSERT INTO theater_messages VALUES(?,?,?,?)',('tm_old','往日的故事。'*100,'book','assistant'))
            await db.commit()
        started=asyncio.Event()
        async def synth(*args,**kwargs):
            started.set();await asyncio.Event().wait()
        with patch.object(studio,'_request_tts_audio',synth):
            await studio.start_speech('tm_old',studio.SpeechRequest(voice='test', allow_generation=True, prefer_cached=False))
            await started.wait()
            await studio.cleanup_conversation('book')
        self.assertNotIn('tm_old',studio._speech)
        self.assertEqual(await studio.rows('book'),[])


if __name__=='__main__': unittest.main()
