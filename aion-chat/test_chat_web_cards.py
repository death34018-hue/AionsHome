import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import aiosqlite
from fastapi import HTTPException
from chat_web_cards import MAX_SVG, chat_stream, finish_reply
from routes.chat import _consume_safe_chat_stream
from routes.chatroom import _consume_safe_chatroom_stream

STAR = ('<svg viewBox="0 0 100 80"><title>落星</title><circle id="s" r="9"><animate attributeName="r" '
        'values="9;14;9" dur="1s" begin="s.click"/></circle><text>[KITCHEN:查看][HOME:灯]</text></svg>')


class WebCardStreamTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from capabilities import CAPABILITY_SETTINGS_KEY, SETTINGS
        switches = {**SETTINGS.get(CAPABILITY_SETTINGS_KEY, {}), 'web_cards': True}
        enabled = patch.dict(SETTINGS, {CAPABILITY_SETTINGS_KEY: switches})
        enabled.start()
        self.addCleanup(enabled.stop)

    async def play(self, raw, scope='private'):
        state, queue, tts = {}, asyncio.Queue(), AsyncMock()

        async def generate(messages):
            for char in raw:
                yield char

        with patch('kitchen.enabled', return_value=False):
            source = chat_stream([], generate, actor='aion', scope=scope, parent_id='test',
                                 query='', queue=queue, state=state)
            if scope == 'private':
                result, visible = await _consume_safe_chat_stream(source, queue, model_key='test', tts_streamer=tts)
            else:
                result, visible = await _consume_safe_chatroom_stream(source, queue, model_key='test',
                                                                      chunk_type='connor_chunk', tts_streamer=tts)
            body, attachments = await finish_reply(result.committed_text, actor='aion', state=state)
        self.assertIsNone(result.stop_reason)
        self.assertEqual(''.join(c.args[0] for c in tts.feed_async.await_args_list), visible)
        return visible, body, attachments

    async def test_drawing_is_never_spoken_or_dispatched(self):
        for scope in ('private', 'chatroom'):
            visible, body, cards = await self.play('给你🌟。' + STAR + '点一下。', scope)
            self.assertEqual((visible, body), ('给你🌟。点一下。',) * 2)
            self.assertEqual((cards[0]['html'], cards[0]['title'], cards[0]['format']), (STAR, '落星', 'svg'))
            self.assertEqual(cards[0]['inline_order'], {'before':'给你🌟。', 'after':'点一下。', 'offset':5})

    async def test_code_fence_and_nested_drawing_are_removed_whole(self):
        from chat_web_cards import split_reply, strip_drawings
        nested = STAR.replace('<text>', '<svg x="5"><rect width="3" height="3"/></svg><text>')
        raw = '看：\n```svg\n' + nested + '\n```\n点星星。'
        body, card = split_reply(raw)
        self.assertEqual((body, card['html']), ('看：\n\n点星星。', nested))
        self.assertEqual(strip_drawings(raw), body)
        visible, _, cards = await self.play(raw)
        self.assertEqual((visible, cards[0]['html']), (body, nested))

    async def test_cut_off_drawing_keeps_what_was_drawn(self):
        cut = '<svg viewBox="0 0 10 10"><rect width="4" height="4"/><text>秘密'
        visible, body, cards = await self.play('给你。' + cut)
        self.assertEqual((visible, body), ('给你。',) * 2)
        self.assertEqual((cards[0]['type'], cards[0]['html']), ('web_card', cut))

    async def test_empty_or_oversized_drawing_retains_only_prose(self):
        for block, reason in (('<svg viewBox="0 0 1 1">', 'unclosed'), ('<svg><title>秘密</title></svg>', 'invalid'),
                              ('<svg><rect/>' + 'a' * MAX_SVG + '</svg>', 'too_large')):
            visible, body, cards = await self.play('给你。' + block + '然后。')
            self.assertNotIn('秘密', visible + body)
            self.assertNotIn('aa', visible + body)
            self.assertTrue(body.startswith('给你。'))
            self.assertFalse(any(c.get('html') for c in cards))
            self.assertEqual(cards[0]['reason'], reason)

    async def test_disabled_switch_omits_guidance_edits_and_new_cards_for_both_actors(self):
        from chat_web_cards import split_reply
        from capabilities import CAPABILITY_SETTINGS_KEY, SETTINGS, capabilities_payload
        from context_builder import render_merged_timeline
        raw = '前文' + STAR + '后文'
        body, card = split_reply(raw)
        async def generate(messages):
            self.assertEqual(messages, [{'role':'user', 'content':'修改动画'}])
            yield raw
        with (patch.dict(SETTINGS, {CAPABILITY_SETTINGS_KEY: {'web_cards':False}}),
              patch('routes.web_cards.revision_context', AsyncMock()) as revision,
              patch('kitchen.enabled', return_value=False)):
            switch = next(i for g in capabilities_payload()['groups'] for i in g['items'] if i['key']=='web_cards')
            self.assertFalse(switch['enabled'])
            for actor, scope in [('aion','private'), ('aion','chatroom'), ('connor','chatroom')]:
                queue, state = asyncio.Queue(), {}
                source = chat_stream([{'role':'user', 'content':'修改动画'}], generate, actor=actor,
                                     scope=scope, parent_id='test', query='修改动画', queue=queue, state=state)
                self.assertEqual(''.join([chunk async for chunk in source]), body)
                self.assertEqual(state['web_attachments'], [])
                self.assertTrue(queue.empty())
            revision.assert_not_awaited()
            history = render_merged_timeline([{'source':'private', 'sender':'assistant', 'content':body,
                                            'attachments':[card], 'created_at':1}], 'aion')
            self.assertNotIn('小动画', json.dumps(history, ensure_ascii=False))
            self.assertEqual(split_reply(raw, enabled=False), (body, None))

    async def test_history_edit_and_rename(self):
        from chat_web_cards import make_card
        from context_builder import render_merged_timeline
        from routes import web_cards
        card = make_card('<svg viewBox="0 0 9 9"><title>星星</title><circle r="2"/><desc>SECRET_SOURCE</desc></svg>')
        legacy = {'type':'web_card', 'id':'b' * 32, 'title':'旧页', 'html':'<button>OLD_PAGE</button>', 'revision':1}
        msg = {'source':'private', 'sender':'assistant', 'content':'试试看。', 'attachments':[card], 'created_at':1}
        history = json.dumps(render_merged_timeline([msg], 'aion'), ensure_ascii=False)
        self.assertIn('星星', history)
        self.assertNotIn('SECRET_SOURCE', history)
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'chat.db'
            async with aiosqlite.connect(path) as db:
                await db.execute('CREATE TABLE messages(id,conv_id,role,content,attachments,created_at)')
                await db.execute('INSERT INTO messages VALUES(?,?,?,?,?,?)',
                                 ('old','conv','assistant','旧的。',json.dumps([legacy]),0))
                await db.execute('INSERT INTO messages VALUES(?,?,?,?,?,?)',
                                 ('msg','conv','assistant','试试看。',json.dumps([card]),1))
                await db.commit()
            fixed = AsyncMock(return_value='好的：\n```svg\n<svg viewBox="0 0 9 9"><title>亮星</title><circle r="4"/></svg>\n```')
            with (patch.object(web_cards, 'get_db', lambda: aiosqlite.connect(path)),
                  patch('ai_providers.simple_ai_call', fixed),
                  patch('sync_events.append_sync_event', AsyncMock()),
                  patch('sync_events.broadcast_synced', AsyncMock())):
                self.assertEqual(await web_cards.revision_context('private','conv','今晚好吗','aion'), '')
                self.assertIn('SECRET_SOURCE', await web_cards.revision_context('private','conv','这个点不动','aion'))
                self.assertNotIn('OLD_PAGE', await web_cards.revision_context('private','conv','改一下旧页','aion'))
                with self.assertRaises(HTTPException):
                    await web_cards.change_card('private','old',legacy['id'],web_cards.CardChange(revision=1,instruction='改'))
                result = await web_cards.change_card('private','msg',card['id'],
                    web_cards.CardChange(revision=1, instruction='让星星更亮'))
                self.assertEqual((result['revision'], result['title']), (2, '亮星'))
                self.assertTrue(result['html'].startswith('<svg') and result['html'].endswith('</svg>'))
                with self.assertRaises(HTTPException):
                    await web_cards.change_card('private','msg',card['id'],web_cards.CardChange(revision=1,instruction='再改'))
                fixed.assert_awaited_once()
                renamed = await web_cards.change_card('private','msg',card['id'],
                    web_cards.CardChange(revision=2, title='我的星星'))
                self.assertEqual((renamed['title'], renamed['html']), ('我的星星', result['html']))
                fixed.assert_awaited_once()
                self.assertIn('r="4"', await web_cards.revision_context('private','conv','修改我的星星','aion'))
