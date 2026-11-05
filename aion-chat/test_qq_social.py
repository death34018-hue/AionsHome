import asyncio
import tempfile
import unittest
from pathlib import Path


class QQStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        from qq_social.store import Store
        self.store = Store(Path(self.tmp.name) / 'qq.db')

    def test_switch_epoch_prevents_old_reply_after_reopen(self):
        self.assertFalse(self.store.settings()['enabled'])
        self.store.configure({'enabled': True})
        old = self.store.settings()['epoch']
        self.store.configure({'enabled': False})
        self.store.configure({'enabled': True})
        self.assertNotEqual(old, self.store.settings()['epoch'])

    def test_group_identity_is_bot_scoped_and_new_groups_are_closed(self):
        a = self.store.discover('aion', 'g1')
        b = self.store.discover('connor', 'g1')
        self.assertNotEqual(a['id'], b['id'])
        self.assertFalse(a['enabled'])
        self.store.configure({'bind_first_group': True})
        self.assertTrue(self.store.discover('aion', 'g2')['enabled'])
        self.assertFalse(self.store.discover('aion', 'g3')['enabled'])

    def test_event_dedup_and_member_memories_never_cross_groups(self):
        self.store.discover('aion', 'g1')
        row = dict(actor='aion', group_id='g1', message_id='m1', member_id='u1', name='朋友', content='喜欢星空', is_bot=False, direction='in', created_at=100)
        self.assertTrue(self.store.record(row))
        self.assertFalse(self.store.record(row))
        self.store.save_memory('aion', 'g1', 'u1', '喜欢星空', ['m1'])
        self.assertEqual(len(self.store.memories('aion', 'g1')), 1)
        self.assertEqual(self.store.memories('connor', 'g1'), [])
        self.assertEqual(self.store.memories('aion', 'g2'), [])

    def test_digest_reads_oldest_unprocessed_messages(self):
        for n in range(75):
            self.store.record(dict(actor='aion',group_id='g1',message_id=str(n),member_id='u',name='朋友',content='话题',is_bot=False,direction='in',created_at=n))
        rows=self.store.history('aion','g1',60,after=0,oldest=True)
        self.assertEqual(rows[0]['message_id'],'0')
        self.assertEqual(rows[-1]['message_id'],'59')

    def test_repeated_memory_keeps_new_source_evidence(self):
        import json
        self.store.save_memory('aion','g1','u1','喜欢星空',['m1'])
        self.store.save_memory('aion','g1','u1','喜欢星空',['m2'])
        rows = self.store.memories('aion','g1')
        self.assertEqual(len(rows),1)
        self.assertEqual(json.loads(rows[0]['sources']),['m1','m2'])

    def test_merge_preserves_originals_and_rejects_foreign_members(self):
        import json
        self.store.save_memory('aion','g1','u1','生日六月四日',['m1'])
        old = self.store.memories('aion','g1')[0]
        self.store.save_memory('aion','g1','u2','生日六月四日',['other'])
        foreign = self.store.memories('aion','g1')[0]
        with self.assertRaises(ValueError):
            self.store.save_memory('aion','g1','u1','生日是六月四日，双子座',['m2'],merge_ids=[foreign['id']])
        self.store.save_memory('aion','g1','u1','生日是六月四日，双子座',['m2'],merge_ids=[old['id']])
        rows = self.store.memories('aion','g1')
        merged = next(r for r in rows if r['member_id']=='u1')
        self.assertEqual(len(rows),2)
        self.assertEqual(set(json.loads(merged['sources'])),{'m1','m2'})
        with self.store.db() as db:
            original = dict(db.execute('SELECT * FROM memories WHERE id=?',(old['id'],)).fetchone())
        self.assertEqual(original['content'],'生日六月四日')
        self.assertEqual(json.loads(original['sources']),['m1'])
        self.assertTrue(original['archive_reason'])

    def test_tunable_context_image_wait_and_interval_are_persisted(self):
        self.store.configure({'interval_seconds':15,'context_messages':70,'image_wait_seconds':90,'hourly_limit':45})
        cfg=self.store.settings()
        self.assertEqual((cfg['interval_seconds'],cfg['context_messages'],cfg['image_wait_seconds'],cfg['hourly_limit']),(15,70,90,45))
        for patch in ({'context_messages':101},{'image_wait_seconds':-1},{'interval_seconds':9}):
            with self.assertRaises(ValueError):
                self.store.configure(patch)

    def test_actor_settings_inherit_old_values_then_remain_independent(self):
        from qq_social.store import Store
        self.store.configure({'enabled':True,'mode':'natural','interval_seconds':180,'context_messages':40})
        self.assertEqual(self.store.settings('aion')['interval_seconds'],180)
        self.store.configure_actor('aion',{'enabled':False,'mode':'respond','interval_seconds':600,'context_messages':10})
        self.store.configure_actor('connor',{'mode':'natural','interval_seconds':30,'context_messages':50})
        reloaded = Store(self.store.path)
        self.assertFalse(reloaded.settings('aion')['enabled'])
        self.assertEqual(reloaded.settings('aion')['mode'],'respond')
        self.assertTrue(reloaded.settings('connor')['enabled'])
        self.assertEqual(reloaded.settings('connor')['context_messages'],50)
        self.assertEqual(reloaded.settings('aion')['interval_seconds'],600)
        before = reloaded.settings('connor')['epoch']
        reloaded.configure_actor('aion',{'enabled':True})
        self.assertEqual(reloaded.settings('connor')['epoch'],before)
        reloaded.configure({'enabled':False})
        self.assertFalse(reloaded.settings('connor')['enabled'])
        self.assertTrue(reloaded.actor_settings('connor')['enabled'])


class QQPolicyTests(unittest.TestCase):
    def test_queued_natural_calls_keep_interval_even_if_previous_call_was_silent(self):
        from qq_social.policy import ReplyPolicy
        p=ReplyPolicy()
        cfg={'mode':'natural','interval_seconds':120}
        p.started('a',direct=False,now=1000)
        self.assertFalse(p.allow('a',direct=False,is_bot=False,settings=cfg,now=1050,include_candidate=False))
        self.assertTrue(p.allow('a',direct=False,is_bot=False,settings=cfg,now=1120,include_candidate=False))
        self.assertTrue(p.allow('a',direct=True,is_bot=False,settings=cfg,now=1050))

    def test_rate_and_bot_loop_limits(self):
        from qq_social.policy import ReplyPolicy
        p = ReplyPolicy()
        cfg = {'mode': 'natural', 'interval_seconds': 120, 'hourly_limit': 6, 'total_hourly_limit': 60}
        self.assertTrue(p.allow('a', direct=False, is_bot=False, settings=cfg, now=1000))
        p.sent('a', direct=False, is_bot=False, now=1000)
        self.assertFalse(p.allow('a', direct=False, is_bot=False, settings=cfg, now=1010))
        self.assertTrue(p.allow('a', direct=True, is_bot=False, settings=cfg, now=1010))
        for n in range(3):
            p.sent('b', direct=True, is_bot=True, now=1000+n*10)
        self.assertFalse(p.allow('b', direct=True, is_bot=True, settings=cfg, now=1100))
        p.human('b')
        self.assertTrue(p.allow('b', direct=True, is_bot=True, settings=cfg, now=1100))

    def test_respond_mode_never_initiates(self):
        from qq_social.policy import ReplyPolicy
        self.assertFalse(ReplyPolicy().allow('a', direct=False, is_bot=False, settings={'mode':'respond'}, now=1000))

    def test_queued_natural_reply_rechecks_spontaneous_budget_without_own_candidate(self):
        from qq_social.policy import ReplyPolicy
        p=ReplyPolicy()
        cfg={'mode':'natural','interval_seconds':120,'hourly_limit':1,'total_hourly_limit':60}
        p.candidate('a',1000)
        self.assertTrue(p.allow('a',direct=False,is_bot=False,settings=cfg,now=1001,include_candidate=False))
        p.sent('a',direct=False,is_bot=False,now=1001)
        self.assertFalse(p.allow('a',direct=False,is_bot=False,settings=cfg,now=1300,include_candidate=False))


class QQContextTests(unittest.TestCase):
    def test_model_is_exact_current_chatroom_selection(self):
        from qq_social.context import select_model
        cfg = {'aion_model':'chosen-a', 'connor_model':'chosen-c'}
        models = {'chosen-a':{}, 'chosen-c':{}}
        self.assertEqual(select_model('aion', cfg, models), 'chosen-a')
        self.assertEqual(select_model('connor', cfg, models), 'chosen-c')
        with self.assertRaises(ValueError):
            select_model('connor', {'connor_model':'missing'}, models)

    def test_public_output_removes_hidden_content_and_rejects_error(self):
        from qq_social.context import clean_reply
        self.assertEqual(clean_reply('<think>secret</think>你好<meta>internal</meta>[HOME:turn_on]'), '你好')
        self.assertEqual(clean_reply('[保持安静]'), '')
        with self.assertRaises(ValueError):
            clean_reply('[CodexCLI错误] timeout')

    def test_private_qq_image_reference_cannot_escape_media_directory(self):
        from ai_providers import _resolve_attachment_path
        path=_resolve_attachment_path('/qq-media/test.jpg')
        self.assertEqual(path.parent.name,'media')
        self.assertEqual(path.parent.parent.name,'qq_social')
        self.assertIsNone(_resolve_attachment_path('/qq-media/../../secrets.jpg'))


class QQImageTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_qq_cdn_domain_is_accepted_and_http_upgraded(self):
        from qq_social.media import image_attachments
        for scheme in ('https','http'):
            refs=image_attachments([{'content_type':'image/jpeg','url':scheme+'://multimedia.nt.qq.com.cn/download?token=test'}])
            self.assertEqual(len(refs),1)
            self.assertEqual(refs[0]['url'],'https://multimedia.nt.qq.com.cn/download?token=test')
        self.assertEqual(image_attachments([{'content_type':'image/jpeg','url':'https://multimedia.nt.qq.com.cn.evil.example/a.jpg'}]),[])

    async def test_download_validate_attach_and_cleanup(self):
        import io, httpx
        from PIL import Image
        from qq_social.media import image_attachments, prepare_images
        raw=io.BytesIO()
        Image.new('RGB',(40,30),'red').save(raw,format='PNG')
        refs=image_attachments([{'content_type':'image/png','url':'https://gchat.qpic.cn/test.png'}])
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req:httpx.Response(200,content=raw.getvalue()))) as client:
            with tempfile.TemporaryDirectory() as tmp:
                async with prepare_images(refs,directory=Path(tmp),client=client) as (attachments,notes):
                    self.assertEqual(len(attachments),1)
                    self.assertEqual(notes,[])
                    self.assertTrue((Path(tmp)/attachments[0].split('/')[-1]).exists())
                self.assertEqual(list(Path(tmp).iterdir()),[])

    async def test_untrusted_url_and_nonimage_body_are_not_forwarded(self):
        import httpx
        from qq_social.media import image_attachments, prepare_images
        self.assertEqual(image_attachments([{'content_type':'image/png','url':'http://127.0.0.1/secret'}]),[])
        refs=image_attachments([{'content_type':'image/png','url':'https://gchat.qpic.cn/a'}])
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req:httpx.Response(200,content=b'not an image'))) as client:
            with tempfile.TemporaryDirectory() as tmp:
                async with prepare_images(refs,directory=Path(tmp),client=client) as (attachments,notes):
                    self.assertEqual(attachments,[])
                    self.assertTrue(notes)
                    self.assertNotIn('https:',str(notes))

    async def test_image_history_is_scoped_to_current_speaker(self):
        import time
        from qq_social.media import recent_images
        refs=[{'url':'https://gchat.qpic.cn/a','content_type':'image/png'}]
        event={'member_id':'u1','attachments':[],'created_at':time.time()}
        history=[{'member_id':'u2','direction':'in','created_at':event['created_at']-1,'attachments':refs}]
        self.assertEqual(recent_images(event,history),[])
        history[0]['member_id']='u1'
        self.assertEqual(len(recent_images(event,history)),1)


class QQServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import time
        from qq_social.store import Store
        from qq_social.service import Service
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / 'qq.db')
        self.sent = []
        class Transport:
            user_id = 'bot-id'
            async def send(inner, group_id, content, message_id, **kwargs):
                self.sent.append(content)
                return {'id':'out1'}
        self.calls = 0
        async def generate(*args):
            self.calls += 1
            return '你好'
        self.service = Service(self.store, {'aion':Transport()}, generator=generate)
        self.event = {'id':'m1','group_openid':'g1','content':'你好', 'author':{'member_openid':'u1','username':'姐妹'}, 'timestamp':time.time()}

    async def asyncTearDown(self):
        await self.service.close()

    async def test_disabled_actor_drops_mentions_while_other_actor_still_replies(self):
        import time
        self.service.transports['connor'] = self.service.transports['aion']
        self.service.configure({'enabled':True,'bind_first_group':True})
        self.service.configure({'enabled':False},actor='aion')
        self.event['timestamp']=time.time()
        await self.service.ingest('aion','GROUP_AT_MESSAGE_CREATE',self.event)
        await self.service.ingest('connor','GROUP_AT_MESSAGE_CREATE',self.event)
        await self.service.drain()
        self.assertEqual(self.calls,1)
        self.assertEqual(self.store.history('aion','g1'),[])
        self.assertEqual(self.sent,['你好'])

    async def test_actor_mode_and_interval_are_used_independently(self):
        import time
        self.service.transports['connor'] = self.service.transports['aion']
        self.service.configure({'enabled':True,'bind_first_group':True})
        self.service.configure({'mode':'respond','interval_seconds':600},actor='aion')
        self.service.configure({'mode':'natural','interval_seconds':10},actor='connor')
        now=time.time()
        for actor in ('aion','connor'):
            self.service.policy.started((actor,'g1'),direct=False,now=now-15)
            await self.service.ingest(actor,'GROUP_MESSAGE_CREATE',{**self.event,'timestamp':now})
        await self.service.drain()
        self.assertEqual(self.calls,1)
        self.assertEqual(self.sent,['你好'])

    async def test_disabling_one_actor_preserves_other_inflight_reply(self):
        import time
        self.service.transports['connor'] = self.service.transports['aion']
        self.service.configure({'enabled':True,'bind_first_group':True})
        entered,release=asyncio.Event(),asyncio.Event()
        async def slow(*args):
            entered.set()
            await release.wait()
            return '仍然回复'
        self.service.generator=slow
        await self.service.ingest('connor','GROUP_AT_MESSAGE_CREATE',{**self.event,'timestamp':time.time()})
        await asyncio.wait_for(entered.wait(),2)
        self.service.configure({'enabled':False},actor='aion')
        release.set()
        await self.service.drain()
        self.assertEqual(self.sent,['仍然回复'])

    async def test_actor_close_reopen_discards_its_own_inflight_reply(self):
        import time
        self.service.configure({'enabled':True,'bind_first_group':True})
        entered,release=asyncio.Event(),asyncio.Event()
        async def slow(*args):
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                pass
            return '过时回复'
        self.service.generator=slow
        await self.service.ingest('aion','GROUP_AT_MESSAGE_CREATE',{**self.event,'timestamp':time.time()})
        await asyncio.wait_for(entered.wait(),2)
        self.service.configure({'enabled':False},actor='aion')
        self.service.configure({'enabled':True},actor='aion')
        release.set()
        await self.service.drain()
        self.assertEqual(self.sent,[])

    async def test_disabled_actor_skips_background_model_work(self):
        from unittest.mock import patch, AsyncMock
        self.service.transports['connor'] = self.service.transports['aion']
        self.service.configure({'enabled':True,'bind_first_group':True})
        group=self.store.discover('aion','g1')
        self.service.configure({'enabled':False},actor='aion')
        with patch('qq_social.context.summarize_group',new=AsyncMock()) as summary:
            await self.service.digest_group(group,[{'id':1}],self.store.settings('aion')['epoch'])
            summary.assert_not_awaited()
        sleeps=0
        async def sleep(_):
            nonlocal sleeps
            sleeps+=1
            if sleeps>1:
                raise asyncio.CancelledError()
        with patch('qq_social.service.asyncio.sleep',sleep), patch('external_memories.retry_pending_index',new=AsyncMock()) as retry:
            with self.assertRaises(asyncio.CancelledError):
                await self.service.digest_groups()
            retry.assert_awaited_once_with('connor')

    async def test_off_drops_body_and_duplicate_generates_once(self):
        await self.service.ingest('aion', 'GROUP_AT_MESSAGE_CREATE', self.event)
        self.assertEqual(self.store.history('aion','g1'), [])
        self.service.configure({'enabled':True,'bind_first_group':True})
        self.event['timestamp']=__import__('time').time()
        await self.service.ingest('aion', 'GROUP_AT_MESSAGE_CREATE', self.event)
        await self.service.ingest('aion', 'GROUP_AT_MESSAGE_CREATE', self.event)
        await self.service.drain()
        self.assertEqual(self.calls, 1)
        self.assertEqual(self.sent, ['你好'])

    async def test_close_reopen_discards_inflight_reply(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def slow(*args):
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                pass  # even a provider that swallows cancellation cannot send
            return '旧回复'
        self.service.generator = slow
        self.service.configure({'enabled':True,'bind_first_group':True})
        self.event['timestamp']=__import__('time').time()
        await self.service.ingest('aion', 'GROUP_AT_MESSAGE_CREATE', self.event)
        await asyncio.wait_for(entered.wait(), 2)
        self.service.configure({'enabled':False})
        self.service.configure({'enabled':True})
        release.set()
        await self.service.drain()
        self.assertEqual(self.sent, [])

    async def test_stale_and_other_bot_echo_are_ignored(self):
        self.service.configure({'enabled':True,'bind_first_group':True})
        old = {**self.event, 'timestamp':1}
        await self.service.ingest('aion','GROUP_AT_MESSAGE_CREATE',old)
        own = {**self.event, 'author':{'id':'bot-id','bot':True}}
        await self.service.ingest('aion','GROUP_AT_MESSAGE_CREATE',own)
        await self.service.drain()
        self.assertEqual(self.calls, 0)

    async def test_delayed_event_from_disabled_period_is_not_stored(self):
        import time
        self.service.configure({'enabled':True,'bind_first_group':True})
        event={**self.event,'timestamp':time.time()-1}
        await self.service.ingest('aion','GROUP_AT_MESSAGE_CREATE',event)
        self.assertEqual(self.store.history('aion','g1'),[])
        self.assertEqual(self.calls,0)

    async def test_disabling_cancels_memory_model_and_releases_slot(self):
        from unittest.mock import patch
        entered,cancelled=asyncio.Event(),asyncio.Event()
        async def summary(*args):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        self.service.configure({'enabled':True,'bind_first_group':True})
        group=self.store.discover('aion','g1')
        with patch('qq_social.context.summarize_group',summary):
            task=asyncio.create_task(self.service.digest_group(group,[{'id':1}],self.store.settings()['epoch']))
            await asyncio.wait_for(entered.wait(),2)
            self.service.configure({'enabled':False})
            await asyncio.wait_for(cancelled.wait(),2)
            await asyncio.gather(task,return_exceptions=True)
        self.assertFalse(self.service.serial.locked())

    async def test_natural_reply_rechecks_total_budget_after_queue(self):
        import time
        self.service.configure({'enabled':True,'bind_first_group':True,'mode':'natural','total_hourly_limit':5})
        self.event['timestamp']=time.time()
        await self.service.serial.acquire()
        await self.service.ingest('aion','GROUP_MESSAGE_CREATE',self.event)
        for n in range(5):
            self.service.policy.sent(('aion','g1'),direct=True,is_bot=False,now=time.time()-10+n)
        self.service.serial.release()
        await self.service.drain()
        self.assertEqual(self.calls,0)
        self.assertEqual(self.sent,[])

    async def test_same_human_followup_does_not_bypass_natural_cooldown(self):
        import time
        self.service.configure({'enabled':True,'bind_first_group':True,'mode':'natural'})
        self.event['timestamp']=time.time()
        await self.service.ingest('aion','GROUP_AT_MESSAGE_CREATE',self.event)
        await self.service.drain()
        self.sent.clear()
        self.calls=0
        self.service.policy.history[('aion','g1')].clear()
        self.service.policy.sent(('aion','g1'),direct=False,is_bot=False,now=time.time()-5)
        self.event['timestamp']=time.time()
        self.event['id']='followup'
        await self.service.ingest('aion','GROUP_MESSAGE_CREATE',self.event)
        await self.service.drain()
        self.assertEqual(self.sent,[])
        self.assertEqual(self.calls,0)

    async def test_image_wait_uses_saved_setting(self):
        import time
        self.service.configure({'enabled':True,'bind_first_group':True,'image_wait_seconds':60})
        self.service.configure({'image_wait_seconds':90},actor='aion')
        picture={**self.event,'timestamp':time.time(),'content':'','attachments':[{'content_type':'image/jpeg','url':'https://multimedia.nt.qq.com.cn/a'}]}
        before=time.monotonic()
        await self.service.ingest('aion','GROUP_AT_MESSAGE_CREATE',picture)
        remaining=self.service.image_batches[('aion','g1','u1')]['deadline']-before
        self.assertGreaterEqual(remaining,90)
        self.assertLess(remaining,91)

    async def test_digest_filters_temporary_facts_and_only_merges_offered_member_memories(self):
        import json
        from unittest.mock import AsyncMock, patch
        from qq_social.context import summarize_group
        group = self.store.discover('aion','g1')
        self.store.save_memory('aion','g1','u1','生日六月四日',['old'])
        own = self.store.memories('aion','g1')[0]['id']
        self.store.save_memory('aion','g1','u2','喜欢星空',['other'])
        foreign = self.store.memories('aion','g1')[0]['id']
        rows = [dict(message_id='new',member_id='u1',name='朋友',content='六月四日生日，我先去吃饭',direction='in',is_bot=False)]
        result = {'summary':'聊了生日，随后去吃饭','memories':[
            {'member_id':'u1','content':'去吃饭了','sources':['new'],'lasting':False,'merge_ids':[]},
            {'member_id':'u1','content':'生日是六月四日','sources':['new'],'lasting':True,'merge_ids':[own]},
            {'member_id':'u1','content':'六月四日过生日','sources':['new'],'lasting':True,'merge_ids':[own]},
            {'member_id':'u1','content':'喜欢星空','sources':['new'],'lasting':True,'merge_ids':[foreign]},
            {'member_id':'u1','content':'未分类的信息','sources':['new']}]}
        with patch('qq_social.context.home_config',return_value=({'aion_model':'model'},{'model':{}},{},{})), patch('qq_social.context.call_model',new=AsyncMock(return_value=json.dumps(result))) as model:
            summary, memories = await summarize_group('aion',group,rows,self.store)
        self.assertEqual(summary,'聊了生日，随后去吃饭')
        self.assertEqual(memories,[('u1','生日是六月四日',['new'],[own])])
        prompt = model.call_args.args[0][1]['content']
        self.assertIn('生日六月四日',prompt)
        self.assertNotIn('喜欢星空',prompt)

    async def test_group_context_uses_saved_message_count(self):
        from unittest.mock import AsyncMock, patch
        import time, json
        from qq_social.context import generate_reply
        self.store.configure({'context_messages':35,'bind_first_group':True})
        self.store.configure_actor('aion',{'context_messages':5})
        group=self.store.discover('aion','g1')
        self.store.save_memory('aion','g1','u1','喜欢星空',['long-source-id-not-for-reply'])
        for n in range(9):
            self.store.record(dict(actor='aion',group_id='g1',message_id=str(n),member_id='u1',name='朋友',content='消息'+str(n),is_bot=False,direction='in',created_at=time.time()-10+n))
        event={'message_id':'original','member_id':'u2','content':'排队前的问题','created_at':time.time(),'attachments':[]}
        diag={}
        with patch('qq_social.context.private_context',new=AsyncMock(return_value=('model',{'aion':'AI','user':'用户'},'private',{}))), patch('qq_social.context.call_model',new=AsyncMock(return_value=json.dumps({'reply':'回复','memory':'一次有趣的讨论','activity_summary':''}))) as model:
            reply = await generate_reply('aion',group,event,self.store,True,diag)
        self.assertEqual((reply.text,reply.memory),('回复','一次有趣的讨论'))
        self.assertEqual(diag['group_messages'],5)
        social = json.loads(model.call_args.args[0][2]['content'].split('\n')[-1])
        self.assertEqual(social,[{'member_id':'u1','name':'朋友','content':'喜欢星空'}])
        content=model.call_args.args[0][-1]['content']
        transcript=json.loads(content.split('\n')[1])
        self.assertEqual([row['text'] for row in transcript],['消息4','消息5','消息6','消息7','消息8'])
        trigger=json.loads(content.split('[本次触发消息]\n')[1].split('\n')[0])
        self.assertEqual(trigger,{'id':'original','member':'u2','text':'排队前的问题'})

    async def test_reply_selects_recent_target_once_and_preserves_memory(self):
        import time
        from unittest.mock import AsyncMock,patch
        from qq_social.context import generate_reply
        self.service.configure({'enabled':True,'bind_first_group':True})
        group=self.store.discover('aion','g1')
        for mid,age,direction in [('old',600,'in'),('own',2,'out'),('topic',1,'in')]:
            self.store.record(dict(actor='aion',group_id='g1',message_id=mid,member_id='friend',
                name='朋友',content='兔子的话题',is_bot=False,direction=direction,
                created_at=time.time()-age,ref_id='REFIDX_'+mid))
        event=dict(message_id='trigger',member_id='u1',content='新话题',created_at=time.time(),ref_id='REFIDX_trigger')
        cases=[('［回复消息：q3］\n兔子好可爱。\n［带回小家记忆：朋友养了兔子。］',False,'兔子好可爱。','REFIDX_topic','朋友养了兔子。'),
               ('［面向全群］\n大家聊得好热闹。',False,'大家聊得好热闹。','',''),
               ('［回复消息：q1］\n过时的话。',False,'','',''),
               ('［回复消息：q2］\n回自己。',False,'','',''),
               ('［回复消息：foreign］\n串群了。',False,'','',''),
               ('突然插话。',False,'','',''),
               ('[保持安静]\n［带回小家记忆：一次见闻。］',False,'','','一次见闻。'),
               ('［回复消息：q3］\n收到点名。',True,'收到点名。','REFIDX_trigger','')]
        for raw,direct,text,reference,memory in cases:
            from external_memories import split_memory_command
            import json
            public,remember=split_memory_command(raw)
            raw=json.dumps({'reply':public,'memory':remember,'activity_summary':''})
            with self.subTest(raw=raw), patch('qq_social.context.private_context',new=AsyncMock(return_value=('model',{'aion':'AI','user':'用户'},'private',{}))), patch('qq_social.context.call_model',new=AsyncMock(return_value=raw)) as model:
                result=await generate_reply('aion',group,event,self.store,direct,{})
                self.assertEqual((result.text,result.reference_id,result.memory),(text,reference,memory))
                self.assertEqual(model.await_count,1)

    async def test_direct_send_uses_mention_reference_even_after_image_merge(self):
        import time
        from unittest.mock import AsyncMock
        self.service.configure({'enabled':True,'bind_first_group':True,'mode':'natural'})
        send=AsyncMock(return_value={'id':'quoted-out'})
        self.service.transports['aion'].send=send
        self.service.image_wait_seconds=.04
        picture={**self.event,'timestamp':time.time(),'content':'',
                 'attachments':[{'content_type':'image/png','url':'https://gchat.qpic.cn/a.png'}],
                 'message_scene':{'ext':['msg_idx=REFIDX_picture']}}
        await self.service.ingest('aion','GROUP_AT_MESSAGE_CREATE',picture)
        await self.service.ingest('aion','GROUP_MESSAGE_CREATE',
            {**self.event,'id':'followup','timestamp':time.time(),'content':'看看颜色',
             'message_scene':{'ext':['msg_idx=REFIDX_followup']}})
        await self.service.drain()
        self.assertEqual(send.await_count,1)
        self.assertEqual(send.call_args.kwargs,{'reference_id':'REFIDX_picture'})
        self.assertEqual(send.call_args.args[2],'followup')

    async def test_full_group_self_mention_bypasses_natural_cooldown(self):
        import time
        self.service.configure({'enabled':True,'bind_first_group':True,'mode':'natural','interval_seconds':300})
        self.service.policy.started(('aion','g1'),direct=False,now=time.time())
        direct_calls=[]
        async def capture(actor,group,event,store,direct,diag):
            direct_calls.append(direct)
            return '回应点名'
        self.service.generator=capture
        event={**self.event,'timestamp':time.time(),'content':'<@group-scoped-id> 在吗',
               'mentions':[{'id':'group-scoped-id','is_you':True}]}
        await self.service.ingest('aion','GROUP_MESSAGE_CREATE',event)
        await self.service.drain()
        self.assertEqual(direct_calls,[True])
        self.assertEqual(self.sent,['回应点名'])

    async def test_self_mention_in_full_event_also_works_in_mention_only_mode(self):
        import time
        self.service.configure({'enabled':True,'bind_first_group':True,'mode':'respond'})
        event={**self.event,'timestamp':time.time(),
               'mentions':[{'id':'group-scoped-id','is_you':True}]}
        await self.service.ingest('aion','GROUP_MESSAGE_CREATE',event)
        # Repeat delivery must not generate or send twice.
        await self.service.ingest('aion','GROUP_AT_MESSAGE_CREATE',event)
        await self.service.drain()
        self.assertEqual(self.calls,1)
        self.assertEqual(self.sent,['你好'])

    async def test_other_member_mentions_do_not_bypass_natural_cooldown(self):
        import time
        self.service.configure({'enabled':True,'bind_first_group':True,'mode':'natural'})
        self.service.policy.started(('aion','g1'),direct=False,now=time.time())
        for index,mentions in enumerate(([{'id':'friend','is_you':False}],
                                         [{'id':'friend','is_you':'false'}],None)):
            await self.service.ingest('aion','GROUP_MESSAGE_CREATE',
                {**self.event,'id':str(index),'timestamp':time.time(),'mentions':mentions})
        await self.service.drain()
        self.assertEqual(self.calls,0)

    async def test_mention_only_ignores_plain_and_quoted_messages(self):
        import time
        self.service.configure({'enabled':True,'bind_first_group':True,'mode':'respond'})
        self.store.record(dict(actor='aion',group_id='g1',message_id='previous',member_id='aion',name='AI',content='先前回复',is_bot=True,direction='out',created_at=time.time()-10,ref_id='ref-1'))
        for message_id, scene in [('plain',{}),('quoted',{'ext':['ref_msg_idx=ref-1']})]:
            event={**self.event,'id':message_id,'timestamp':time.time(),'message_scene':scene}
            await self.service.ingest('aion','GROUP_MESSAGE_CREATE',event)
            await self.service.drain()
        self.assertEqual(self.calls,0)
        self.event['timestamp']=time.time()
        await self.service.ingest('aion','GROUP_AT_MESSAGE_CREATE',self.event)
        await self.service.drain()
        self.assertEqual(self.sent,['你好'])

    async def test_image_only_event_reaches_generator_and_history(self):
        import time
        received=[]
        async def capture(actor,group,event,*args):
            received.extend(event.get('attachments',[]))
            return '看到图片了'
        self.service.generator=capture
        self.service.image_wait_seconds=0
        self.service.configure({'enabled':True,'bind_first_group':True})
        self.event.update(timestamp=time.time(),content='',attachments=[{'content_type':'image/png','url':'https://gchat.qpic.cn/a.png'}])
        await self.service.ingest('aion','GROUP_AT_MESSAGE_CREATE',self.event)
        await self.service.drain()
        self.assertEqual(len(received),1)
        import json
        self.assertEqual(len(json.loads(self.store.history('aion','g1')[0]['attachments'])),1)
        self.assertNotIn('只读取文字',self.store.history('aion','g1')[0]['content'])

    async def test_image_wait_combines_followup_once_without_resetting_deadline(self):
        import time
        received=[]
        async def capture(actor,group,event,*args):
            received.append(dict(event))
            return '合并回复'
        self.service.generator=capture
        self.service.image_wait_seconds=.08
        self.service.configure({'enabled':True,'bind_first_group':True,'mode':'natural'})
        picture={**self.event,'timestamp':time.time(),'content':'','attachments':[{'content_type':'image/png','url':'https://gchat.qpic.cn/one.png'}]}
        await self.service.ingest('aion','GROUP_MESSAGE_CREATE',picture)
        await asyncio.sleep(0)
        self.assertEqual(received,[])
        self.assertFalse(self.service.serial.locked())
        batch=self.service.image_batches[('aion','g1','u1')]
        deadline=batch['deadline']
        followup={**self.event,'id':'m2','timestamp':time.time(),'content':'帮我看看图里的颜色'}
        await self.service.ingest('aion','GROUP_AT_MESSAGE_CREATE',followup)
        second={**picture,'id':'m3','timestamp':time.time(),'attachments':[{'content_type':'image/png','url':'https://gchat.qpic.cn/two.png'}]}
        await self.service.ingest('aion','GROUP_MESSAGE_CREATE',second)
        self.assertEqual(batch['deadline'],deadline)
        self.assertEqual(received,[])
        await self.service.drain()
        self.assertEqual(len(received),1)
        self.assertIn('帮我看看图里的颜色',received[0]['content'])
        self.assertEqual(len(received[0]['attachments']),2)
        self.assertEqual(self.sent,['合并回复'])

    async def test_image_wait_cancelled_by_close_and_other_speaker_not_merged(self):
        import time
        self.service.configure({'enabled':True,'bind_first_group':True})
        picture={**self.event,'timestamp':time.time(),'content':'','attachments':[{'content_type':'image/png','url':'https://gchat.qpic.cn/a.png'}]}
        await self.service.ingest('aion','GROUP_MESSAGE_CREATE',picture)
        other={**self.event,'id':'other','timestamp':time.time(),'author':{'member_openid':'u2'},'content':'别人的话'}
        await self.service.ingest('aion','GROUP_MESSAGE_CREATE',other)
        self.assertEqual(len(self.service.image_batches),1)
        self.assertNotIn('别人的话',self.service.image_batches[('aion','g1','u1')]['event']['content'])
        self.service.configure({'enabled':False})
        self.service.configure({'enabled':True})
        await self.service.drain()
        self.assertEqual(self.calls,0)
        self.assertEqual(self.sent,[])
        self.assertEqual(self.service.image_batches,{})

    async def test_followup_text_releases_image_wait_immediately(self):
        import time
        self.service.configure({'enabled':True,'bind_first_group':True,'mode':'natural'})
        picture={**self.event,'timestamp':time.time(),'content':'','attachments':[{'content_type':'image/jpeg','url':'https://multimedia.nt.qq.com.cn/a'}]}
        await self.service.ingest('aion','GROUP_MESSAGE_CREATE',picture)
        await asyncio.sleep(0)
        self.assertEqual(self.calls,0)
        followup={**self.event,'id':'caption','timestamp':time.time(),'content':'看看我这碗面'}
        await self.service.ingest('aion','GROUP_AT_MESSAGE_CREATE',followup)
        await asyncio.wait_for(self.service.drain(),.5)
        self.assertEqual(self.calls,1)
        self.assertEqual(self.sent,['你好'])

    async def test_image_without_followup_keeps_timeout(self):
        import time
        self.service.image_wait_seconds=.04
        self.service.configure({'enabled':True,'bind_first_group':True,'mode':'natural'})
        picture={**self.event,'timestamp':time.time(),'content':'','attachments':[{'content_type':'image/jpeg','url':'https://multimedia.nt.qq.com.cn/a'}]}
        await self.service.ingest('aion','GROUP_AT_MESSAGE_CREATE',picture)
        await asyncio.sleep(0)
        self.assertEqual(self.calls,0)
        self.assertFalse(self.service.image_batches[('aion','g1','u1')]['ready'].is_set())
        await self.service.drain()
        self.assertEqual(self.calls,1)


class QQTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_connection_stops_after_three_failures_without_another_timer(self):
        import httpx
        from unittest.mock import patch, AsyncMock
        from qq_social.transport import BotTransport
        attempts = []
        def offline(request):
            attempts.append(request.url.path)
            raise httpx.ConnectError('offline', request=request)
        bot = BotTransport({'app_id':'123','secret':'test'}, None)
        await bot.client.aclose()
        bot.client = httpx.AsyncClient(transport=httpx.MockTransport(offline))
        sleep = AsyncMock()
        # Bound the old infinite loop so the regression fails promptly.
        sleep.side_effect = [None, None, asyncio.CancelledError()]
        try:
            with patch('qq_social.transport.asyncio.sleep', sleep):
                try:
                    await bot.run()
                except asyncio.CancelledError:
                    pass
            self.assertTrue(bot.status.get('retry_stopped'))
            self.assertEqual(len(attempts), 3)
            self.assertEqual(sleep.await_count, 2)
        finally:
            await bot.close()

    async def test_official_token_and_group_reply_never_expose_secret(self):
        import httpx
        from qq_social.transport import BotTransport
        requests=[]
        def handler(req):
            requests.append(req)
            if req.url.path=='/app/getAppAccessToken':
                return httpx.Response(200,json={'access_token':'private-token','expires_in':7200})
            return httpx.Response(200,json={'id':'reply-id'})
        bot=BotTransport({'app_id':'123','secret':'private-secret'},lambda *args:None)
        await bot.client.aclose()
        bot.client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            self.assertEqual(await bot.send('group1','你好','original'),{'id':'reply-id'})
            self.assertEqual(requests[-1].url.path,'/v2/groups/group1/messages')
            self.assertEqual(__import__('json').loads(requests[-1].content)['msg_id'],'original')
            self.assertNotIn('private',str(bot.status))
            await bot.send('group1','有明确对象','latest-trigger',reference_id='REFIDX_earlier')
            body=__import__('json').loads(requests[-1].content)
            self.assertEqual(body['msg_id'],'latest-trigger')
            self.assertEqual(body['message_reference'],{'message_id':'REFIDX_earlier','ignore_get_message_error':True})
            self.assertNotIn('private',str(body))
        finally:
            await bot.close()

    async def test_http_200_token_error_is_a_failure(self):
        import httpx
        from qq_social.transport import BotTransport,QQAPIError
        bot=BotTransport({'app_id':'123','secret':'private-secret'},lambda *args:None)
        await bot.client.aclose()
        bot.client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(200,json={'code':100016,'message':'private-secret'})))
        try:
            with self.assertRaises(QQAPIError) as caught:
                await bot.authorize()
            self.assertNotIn('private-secret',str(caught.exception))
        finally:
            await bot.close()


class QQManagementTests(unittest.TestCase):
    def test_stopped_connection_survives_worker_restart_until_manual_connect(self):
        import json
        from unittest.mock import patch
        from fastapi.testclient import TestClient
        from qq_social.worker import create_app
        created = []
        class FakeBot:
            def __init__(self, credentials, event):
                self.status = {'connected':False, 'state':'准备连接', 'retry_stopped':False}
                created.append(self)
            async def run(self):
                await asyncio.Future()
            async def close(self):
                pass
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory/'management.key').write_text('test-management')
            (directory/'credentials.json').write_text(json.dumps({'aion':{'app_id':'123','secret':'test'}}))
            (directory/'aion.paused').touch()
            headers = {'authorization':'Bearer test-management'}
            with patch('qq_social.worker.BotTransport', FakeBot), patch('qq_social.context.home_config', return_value=({}, {}, {}, {'aion':'甲','connor':'乙'})), TestClient(create_app(directory)) as client:
                self.assertTrue(client.get('/status', headers=headers).json()['actors']['aion']['retry_stopped'])
                self.assertEqual(client.post('/reconnect').status_code, 401)
                self.assertEqual(client.post('/reconnect', headers=headers).status_code, 200)
                self.assertFalse((directory/'aion.paused').exists())
                self.assertFalse(client.get('/status', headers=headers).json()['actors']['aion']['retry_stopped'])
                count = len(created)
                client.post('/reconnect', headers=headers)
                self.assertEqual(len(created), count, 'Do not duplicate an active connection')

    def test_actor_settings_api_changes_only_selected_actor(self):
        from unittest.mock import patch
        from fastapi.testclient import TestClient
        from qq_social.worker import create_app
        with tempfile.TemporaryDirectory() as tmp:
            directory=Path(tmp)
            (directory/'management.key').write_text('test-management')
            client=TestClient(create_app(directory))
            url='/actors/aion/settings'
            self.assertEqual(client.patch(url,json={'enabled':False}).status_code,401)
            headers={'authorization':'Bearer test-management'}
            self.assertEqual(client.patch(url,json={'enabled':False,'interval_seconds':600},headers=headers).status_code,200)
            self.assertEqual(client.patch(url,json={'interval_seconds':1},headers=headers).status_code,400)
            with patch('qq_social.context.home_config',return_value=({}, {}, {}, {'aion':'甲','connor':'乙'})):
                status=client.get('/status',headers=headers).json()
            self.assertFalse(status['actors']['aion']['settings']['enabled'])
            self.assertEqual(status['actors']['aion']['settings']['interval_seconds'],600)
            self.assertTrue(status['actors']['connor']['settings']['enabled'])
            self.assertEqual(status['actors']['connor']['settings']['interval_seconds'],120)

    def test_worker_rejects_unauthorized_requests(self):
        from fastapi.testclient import TestClient
        from qq_social.worker import create_app
        with tempfile.TemporaryDirectory() as tmp:
            directory=Path(tmp)
            (directory/'management.key').write_text('private-management-key')
            client=TestClient(create_app(directory))
            self.assertEqual(client.get('/status').status_code,401)
            self.assertEqual(client.patch('/settings',json={'enabled':True}).status_code,401)
            self.assertNotIn('private-management-key',client.get('/status').text)


class QQCredentialSetupTests(unittest.TestCase):
    def setUp(self):
        from unittest.mock import patch, AsyncMock
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from routes import qq_social
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        for target, value in (
            ('routes.qq_social.DIRECTORY', self.directory),
            ('qq_social.context.home_config', lambda: ({}, {}, {}, {'aion':'角色甲','connor':'角色乙'})),
            ('qq_social.transport.BotTransport.authorize', AsyncMock()),
        ):
            mocked = patch(target, value)
            mocked.start()
            self.addCleanup(mocked.stop)
        app = FastAPI()
        app.include_router(qq_social.router)
        self.client = TestClient(app)

    def save(self, **body):
        return self.client.patch('/api/qq-social/credentials', json=body,
                                 headers={'origin':'http://testserver'})

    def test_first_setup_supports_one_bot_and_never_returns_secrets(self):
        import json
        from unittest.mock import patch, AsyncMock
        from qq_social.store import Store
        with patch('routes.qq_social.connect_saved_credentials', AsyncMock(return_value='connecting'), create=True):
            before = self.client.get('/api/qq-social/credentials')
            self.assertEqual(before.status_code, 200)
            self.assertEqual(before.json()['actors']['aion']['name'], '角色甲')
            response = self.save(actor='aion', app_id='123456', secret='private-setup-secret')
            self.assertEqual(response.status_code, 200)
        saved = json.loads((self.directory/'credentials.json').read_text())
        self.assertEqual(saved, {'aion':{'app_id':'123456','secret':'private-setup-secret'}})
        self.assertTrue((self.directory/'management.key').is_file())
        self.assertFalse(Store(self.directory/'social.db').settings()['enabled'])
        self.assertTrue(Store(self.directory/'social.db').settings()['bind_first_group'])
        public = self.client.get('/api/qq-social/credentials')
        self.assertTrue(public.json()['actors']['aion']['configured'])
        self.assertFalse(public.json()['actors']['connor']['configured'])
        self.assertNotIn('private-setup-secret', public.text + response.text)

    def test_invalid_secret_or_cross_origin_does_not_replace_working_config(self):
        import json
        from unittest.mock import patch, AsyncMock
        existing = {'aion':{'app_id':'123456','secret':'working-secret'}}
        (self.directory/'credentials.json').write_text(json.dumps(existing))
        response = self.client.patch('/api/qq-social/credentials', json={'actor':'aion','app_id':'999','secret':'bad'},
                                     headers={'origin':'https://elsewhere.invalid'})
        self.assertEqual(response.status_code,403)
        with patch('qq_social.transport.BotTransport.authorize', AsyncMock(side_effect=ValueError('sensitive provider response'))):
            response = self.save(actor='aion', app_id='123456', secret='wrong-secret')
        self.assertEqual(response.status_code,400)
        self.assertNotIn('sensitive',response.text)
        self.assertEqual(json.loads((self.directory/'credentials.json').read_text()),existing)
        self.assertEqual(self.save(actor='aion', app_id='different-id', secret='').status_code,400)

    def test_blank_secret_keeps_existing_bot_and_settings(self):
        import json
        from unittest.mock import patch, AsyncMock
        from qq_social.store import Store
        existing = {'aion':{'app_id':'123456','secret':'working-secret'},
                    'connor':{'app_id':'654321','secret':'other-secret'}}
        (self.directory/'credentials.json').write_text(json.dumps(existing))
        Store(self.directory/'social.db').configure({'enabled':False,'interval_seconds':345})
        with patch('routes.qq_social.connect_saved_credentials', AsyncMock(return_value='connecting'), create=True):
            response = self.save(actor='aion', app_id='123456', secret='')
        self.assertEqual(response.status_code,200)
        self.assertEqual(json.loads((self.directory/'credentials.json').read_text()),existing)
        self.assertEqual(Store(self.directory/'social.db').settings()['interval_seconds'],345)
        self.assertFalse(Store(self.directory/'social.db').settings()['enabled'])

    def test_worker_reloads_changed_credentials_without_restarting_home(self):
        import json
        from unittest.mock import patch
        from fastapi.testclient import TestClient
        from qq_social.worker import create_app
        from qq_social.service import Service
        created = []
        services, pending, drained_at_close = [], [], []
        def capture_service(*args):
            service = Service(*args)
            services.append(service)
            return service
        async def pending_reply():
            await asyncio.Future()
        async def queue_reply():
            task = asyncio.create_task(pending_reply())
            pending.append(task)
            services[0].tasks.add(task)
            task.add_done_callback(services[0].tasks.discard)
            await asyncio.sleep(0)
        class FakeBot:
            def __init__(self, credentials, event):
                self.credentials = credentials
                self.closed = False
                created.append(self)
            async def run(self):
                await asyncio.Future()
            async def close(self):
                self.closed = True
                drained_at_close.append(all(task.done() for task in pending))
        (self.directory/'management.key').write_text('test-management')
        path = self.directory/'credentials.json'
        path.write_text(json.dumps({'aion':{'app_id':'123','secret':'first'}}))
        with patch('qq_social.worker.BotTransport', FakeBot), patch('qq_social.worker.Service',capture_service), TestClient(create_app(self.directory)) as client:
            self.assertEqual(len(created),1)
            client.portal.call(queue_reply)
            path.write_text(json.dumps({'aion':{'app_id':'123','secret':'changed'}}))
            self.assertEqual(client.post('/reload-credentials').status_code,401)
            response = client.post('/reload-credentials', headers={'authorization':'Bearer test-management'})
            self.assertEqual(response.status_code,200)
            self.assertEqual(len(created),2)
            self.assertTrue(created[0].closed)
            self.assertTrue(drained_at_close[0], 'Pending replies must stop before old connection closes')
            self.assertEqual(created[1].credentials['secret'],'changed')
            client.post('/reload-credentials', headers={'authorization':'Bearer test-management'})
            self.assertEqual(len(created),2)

    def test_adding_bot_during_memory_retry_keeps_digest_loop_alive(self):
        from unittest.mock import patch
        from qq_social.store import Store
        from qq_social.service import Service
        async def exercise():
            store = Store(self.directory/'digest.db')
            store.configure({'enabled':True})
            transports = {'aion':object()}
            service = Service(store,transports)
            sleeps = 0
            async def sleep(_):
                nonlocal sleeps
                sleeps += 1
                if sleeps > 1:
                    raise asyncio.CancelledError()
            async def retry(actor):
                transports['connor'] = object()
            with patch('qq_social.service.asyncio.sleep',sleep), patch('external_memories.retry_pending_index',retry):
                with self.assertRaises(asyncio.CancelledError):
                    await service.digest_groups()
        asyncio.run(exercise())


class QQSupervisorTests(unittest.TestCase):
    def test_restart_can_recover_when_only_home_is_running(self):
        result = self.run_restart_script(worker=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('STARTED_HOME', result.stdout)

    def test_restart_does_not_wait_for_busy_or_unresponsive_worker(self):
        for unavailable in (False, True):
            with self.subTest(unavailable=unavailable):
                result = self.run_restart_script(worker=True, unavailable=unavailable)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn('STOPPED_PROCESS', result.stdout)
                self.assertIn('STARTED_HOME', result.stdout)

    def test_restart_starts_home_when_both_services_are_stopped(self):
        result = self.run_restart_script(worker=False, home=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn('STOPPED_PROCESS', result.stdout)
        self.assertIn('STARTED_HOME', result.stdout)

    def run_restart_script(self, worker, home=True, unavailable=False):
        import os
        import subprocess
        if os.name != 'nt':
            self.skipTest('Windows restart helper')
        # Run the real script, replacing only OS/network effects. Never stop live services.
        root = str(Path(__file__).resolve().parents[1]).replace("'", "''")
        harness = r'''
$ErrorActionPreference = 'Stop'
function Get-NetTCPConnection {
    param($LocalPort, $State, $ErrorAction)
    if (($homeRunning -and $LocalPort -eq 8080) -or ($worker -and $LocalPort -eq 18788)) {
        [pscustomobject]@{ OwningProcess = $LocalPort }
    }
}
function Get-CimInstance {
    param($ClassName, $Filter)
    $id = [int]($Filter -replace 'ProcessId=', '')
    $command = if ($id -eq 8080) { 'main.py' } else { '-m qq_social.worker' }
    [pscustomobject]@{ ProcessId = $id; CommandLine = "$root $command" }
}
function Invoke-RestMethod {
    param($Uri, $TimeoutSec, $Headers)
    if (-not $script:started) {
        if (-not $worker -or $unavailable) { throw 'QQ worker unavailable (503)' }
        return @{ pending = 1 }
    }
    return @{ settings = @{ bring_home = $true } }
}
function Stop-Process { param($Id) Write-Output 'STOPPED_PROCESS' }
function Get-ScheduledTask { return $null }
function Start-Process { $script:started = $true; Write-Output 'STARTED_HOME' }
function Start-Sleep {}
function Get-Content { param($LiteralPath, [switch]$Raw) return 'test-management' }
function Test-Path { param($LiteralPath) return $false }
& "$root\aion-chat\restart-home-qq.ps1"
'''
        harness = (f"$root = '{root}'\n$worker = ${str(worker).lower()}\n"
                   f"$homeRunning = ${str(home).lower()}\n$unavailable = ${str(unavailable).lower()}\n" + harness)
        return subprocess.run(['powershell.exe', '-NoProfile', '-Command', harness],
                              capture_output=True, text=True, errors='replace', timeout=20)

    def test_failed_starts_are_bounded_and_manual_reset_releases_persistent_pause(self):
        from unittest.mock import patch
        import qq_social_process as process
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory/'credentials.json').write_text('{}')
            (directory/'management.key').write_text('test')
            with patch.object(process, 'DIRECTORY', directory), patch.object(process, '_last_start', 0), patch.object(process, '_start_attempts', 0, create=True), patch.object(process, 'reachable', return_value=False) as reachable, patch.object(process.time, 'monotonic', side_effect=[100,100,121,121,142,142,163,163,184,184,205,205,226,226]), patch.object(process.subprocess, 'Popen') as spawn:
                for _ in range(6):
                    process.ensure_started()
                self.assertEqual(spawn.call_count, 3)
                self.assertTrue((directory/'connection.paused').exists())
                checked = reachable.call_count
                process.ensure_started()
                self.assertEqual(reachable.call_count, checked, 'Stopped means no more connection probes')
                process.reset_retries()
                self.assertFalse((directory/'connection.paused').exists())
                process.ensure_started()
                self.assertEqual(spawn.call_count, 4)


if __name__ == '__main__':
    unittest.main()
