import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import aiosqlite
from fastapi import HTTPException
from chat_web_cards import chat_stream, finish_reply
from routes.chat import _consume_safe_chat_stream
from routes.chatroom import _consume_safe_chatroom_stream


class WebCardStreamTests(unittest.IsolatedAsyncioTestCase):
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

    async def test_code_is_never_spoken_or_dispatched(self):
        html = '<title>落星</title><button>[KITCHEN:查看][HOME:灯]</button>'
        for scope in ('private', 'chatroom'):
            visible, body, cards = await self.play('给你🌟。[WEB]' + html + '[/WEB]点一下。', scope)
            self.assertEqual((visible, body), ('给你🌟。点一下。',) * 2)
            self.assertEqual((cards[0]['html'], cards[0]['title']), (html, '落星'))
            self.assertEqual(cards[0]['inline_order'], {'before':'给你🌟。', 'after':'点一下。', 'offset':5})

    def test_long_finished_page_round_trips_with_its_heading(self):
        from chat_web_cards import split_reply
        source = '<h1>深夜茶饮台</h1><!--' + '茶' * 18000 + '--><button>续杯</button>'
        body, card = split_reply('给你[WEB]' + source + '[/WEB]。')
        self.assertEqual(body, '给你。')
        self.assertEqual((card['type'], card.get('title'), card.get('html')), ('web_card', '深夜茶饮台', source))

    async def test_truncated_or_oversized_card_retains_only_prose(self):
        for block, reason in (('[WEB]<button>秘密', 'unclosed'), ('[WEB]文字[/WEB]', 'invalid_html'),
                              ('[WEB]<p>' + 'a' * 25000 + '[/WEB]', 'too_large')):
            visible, body, cards = await self.play('给你。' + block)
            self.assertNotIn('秘密', visible + body)
            self.assertNotIn('aa', visible + body)
            self.assertTrue(body.startswith('给你。'))
            self.assertFalse(any(c.get('html') for c in cards))
            self.assertEqual(cards[0]['reason'], reason)

    async def test_disabled_switch_omits_guidance_edits_and_new_cards_for_both_actors(self):
        from chat_web_cards import split_reply
        from capabilities import CAPABILITY_SETTINGS_KEY, SETTINGS, capabilities_payload
        from context_builder import render_merged_timeline
        raw = '前文[WEB]<button>SECRET</button>[/WEB]后文'
        body, card = split_reply(raw)
        async def generate(messages):
            self.assertEqual(messages, [{'role':'user', 'content':'修改网页'}])
            yield raw
        with (patch.dict(SETTINGS, {CAPABILITY_SETTINGS_KEY: {'web_cards':False}}),
              patch('routes.web_cards.revision_context', AsyncMock()) as revision,
              patch('kitchen.enabled', return_value=False)):
            switch = next(i for g in capabilities_payload()['groups'] for i in g['items'] if i['key']=='web_cards')
            self.assertFalse(switch['enabled'])
            for actor, scope in [('aion','private'), ('aion','chatroom'), ('connor','chatroom')]:
                queue, state = asyncio.Queue(), {}
                source = chat_stream([{'role':'user', 'content':'修改网页'}], generate, actor=actor,
                                     scope=scope, parent_id='test', query='修改网页', queue=queue, state=state)
                self.assertEqual(''.join([chunk async for chunk in source]), body)
                self.assertEqual(state['web_attachments'], [])
                self.assertTrue(queue.empty())
            revision.assert_not_awaited()
            history = render_merged_timeline([{'source':'private', 'sender':'assistant', 'content':body,
                                            'attachments':[card], 'created_at':1}], 'aion')
            self.assertNotIn('互动网页', json.dumps(history, ensure_ascii=False))
            self.assertEqual(split_reply(raw, enabled=False), (body, None))

    async def test_history_edit_and_one_automatic_repair(self):
        from chat_web_cards import make_card
        from context_builder import render_merged_timeline
        from routes import web_cards
        card = make_card('<title>星星</title><button>SECRET_SOURCE</button>')
        msg = {'source':'private', 'sender':'assistant', 'content':'试试看。', 'attachments':[card], 'created_at':1}
        history = json.dumps(render_merged_timeline([msg], 'aion'), ensure_ascii=False)
        self.assertIn('星星', history)
        self.assertNotIn('SECRET_SOURCE', history)
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'chat.db'
            async with aiosqlite.connect(path) as db:
                await db.execute('CREATE TABLE messages(id,conv_id,role,content,attachments,created_at)')
                await db.execute('INSERT INTO messages VALUES(?,?,?,?,?,?)',
                                 ('msg','conv','assistant','试试看。',json.dumps([card]),1))
                await db.commit()
            fixed = AsyncMock(return_value='[WEB]<title>星星</title><button>亮了</button>[/WEB]')
            with (patch.object(web_cards, 'get_db', lambda: aiosqlite.connect(path)),
                  patch('ai_providers.simple_ai_call', fixed),
                  patch('sync_events.append_sync_event', AsyncMock()),
                  patch('sync_events.broadcast_synced', AsyncMock())):
                self.assertEqual(await web_cards.revision_context('private','conv','今晚好吗','aion'), '')
                self.assertIn('SECRET_SOURCE', await web_cards.revision_context('private','conv','这张点不动','aion'))
                repair = web_cards.CardChange(revision=1, error='button not found')
                result = await web_cards.change_card('private','msg',card['id'],repair)
                self.assertEqual(result['revision'], 2)
                await web_cards.change_card('private','msg',card['id'],repair)
                with self.assertRaises(HTTPException):
                    await web_cards.change_card('private','msg',card['id'],web_cards.CardChange(revision=2,error='again'))
                fixed.assert_awaited_once()
                self.assertIn('亮了', await web_cards.revision_context('private','conv','修改星星','aion'))
                renamed = await web_cards.change_card('private','msg',card['id'],
                    web_cards.CardChange(revision=2, title='我的星星'))
                self.assertEqual((renamed['title'], renamed['html']), ('我的星星', result['html']))
                fixed.assert_awaited_once()
                self.assertIn('亮了', await web_cards.revision_context('private','conv','修改我的星星','aion'))
