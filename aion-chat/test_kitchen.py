import json
import tempfile
import time
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import aiosqlite


class KitchenFixture(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import kitchen
        self.k = kitchen
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'kitchen.db'
        self.patches = [patch.object(kitchen, 'get_db', self.connect),
                        patch.object(kitchen, 'SETTINGS', {}),
                        patch.object(kitchen, 'save_settings', lambda _: None)]
        for p in self.patches:
            p.start()
        await kitchen.ensure_schema()
        await kitchen.set_enabled(True)

    async def asyncTearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def connect(self):
        return aiosqlite.connect(self.path)

    async def apply(self, source, changes, **kwargs):
        state = await self.k.state()
        return await self.k.apply_changes(source, changes, epoch=state['epoch'],
                                          source_ts=kwargs.get('source_ts', time.time()))


class KitchenTests(KitchenFixture):
    async def test_two_first_options_can_be_served_at_different_meals(self):
        egg = {'name':'蒸蛋','minutes':10,'ingredients':[{'name':'鸡蛋','amount':1,'unit':'颗'}],'steps':['蒸熟']}
        soup = {**egg,'name':'蛋花汤','ingredients':[{'name':'鸡蛋','amount':2,'unit':'颗'}]}
        epoch = (await self.k.state())['epoch']
        a = await self.k.save_menu({'options':[egg,soup]},actor='connor',epoch=epoch)
        b = await self.k.save_menu({'options':[soup,egg]},actor='aion',epoch=epoch)
        with self.assertRaises(ValueError):
            await self.k.confirm_menu(b['id'])
        await self.k.upsert_item({'name':'鸡蛋','amount':6,'unit':'颗'})
        await self.k.confirm_menu(a['id'],meal='lunch',serve_as='lunch')
        with patch('kitchen_notices.record_notice',new_callable=AsyncMock,return_value=None) as notice:
            table = await self.k.confirm_menu(b['id'],meal='lunch',serve_as='dinner',expected_selection_at=0)
            await self.k.confirm_menu(b['id'],meal='lunch',serve_as='dinner',expected_selection_at=0)
            self.assertEqual(notice.await_count,1)
            self.assertEqual(notice.await_args.kwargs['meal']['meal'],'dinner')
        self.assertEqual([(m['meal'],m['source_meal'],m['name'],m['actor']) for m in table['meals']],
                         [('lunch','lunch','蒸蛋','connor'),('dinner','lunch','蛋花汤','aion')])
        self.assertEqual((await self.k.get_menu(b['id'],as_card=True))['meal_status'],
                         {'lunch':'confirmed','dinner':'proposed'})
        with self.assertRaises(self.k.MenuChanged):
            await self.k.confirm_menu(a['id'],meal='dinner',serve_as='dinner',expected_selection_at=0)
        await self.apply('served-dinner',[{'op':'meal','meal':'dinner'}])
        self.assertEqual((await self.k.snapshot())['items'][0]['quantity'],6)
        await self.k.remove_menu(table['id'],meal='dinner')
        self.assertEqual((await self.k.get_menu(b['id']))['meal_status']['lunch'],'removed')
        # The first confirmed dish remains usable even after a new proposal was generated.
        await self.k.save_menu({'options':[soup,egg]},actor='connor',epoch=epoch)
        active = await self.k.get_menu(table['id'])
        again = await self.k.confirm_menu(active['id'],meal='lunch',serve_as='dinner',reselect=True,
                                          expected_updated_at=active['updated_at'],expected_selection_at=0)
        self.assertEqual([(m['meal'],m['name']) for m in again['meals']],[('lunch','蒸蛋'),('dinner','蒸蛋')])

    async def test_breakfast_ingredients_are_independent_of_menu_references(self):
        await self.k.bulk_items('鸡蛋 30 个\n冻玉米 7 节')
        changes = [{'op':'consume','name':'鸡蛋','amount':2,'unit':'颗'},
                   {'op':'consume','name':'冻玉米','amount':None,'unit':'节'}]
        await self.apply('breakfast', changes)
        data = await self.k.snapshot()
        self.assertEqual([p['name'] for p in data['pending']], ['冻玉米'])
        self.assertEqual([(i['quantity'], i['unit']) for i in data['items'] if i['name']=='鸡蛋'], [(28, '个')])

    async def test_confirm_usage_only_deducts_that_entry_once(self):
        corn = await self.k.upsert_item({'name':'冻玉米','amount':7,'unit':'节'})
        for source in ('breakfast','dinner'):
            await self.apply(source, [{'op':'consume','name':'冻玉米','unit':'节'}])
        pending = (await self.k.snapshot())['pending']
        delta = {'action':'consume','item_id':corn['id'],'amount':.5,'unit':'节'}
        await self.k.resolve_pending(pending[0]['id'], delta)
        with self.assertRaises(ValueError):
            await self.k.resolve_pending(pending[0]['id'], delta)
        data = await self.k.snapshot()
        self.assertEqual((data['items'][0]['quantity'],len(data['pending'])), (6.5,1))
        self.assertTrue(data['items'][0]['uncertain'])
        await self.k.resolve_pending(pending[1]['id'], {**delta,'amount':1})
        data = await self.k.snapshot()
        self.assertEqual(data['items'][0]['quantity'], 5.5)
        self.assertFalse(data['items'][0]['uncertain'])

    async def test_mixed_table_swap_keeps_other_meal_author_and_original_chat(self):
        epoch=(await self.k.state())['epoch']
        meal={'meal':'lunch','name':'午餐','minutes':15,'ingredients':[{'name':'鸡蛋','amount':2,'unit':'个'}],'steps':['蒸熟。']}
        first=await self.k.save_menu({'meals':[meal],'message':'午餐留言'},actor='connor',epoch=epoch,scope='chatroom',parent_id='group-a')
        second=await self.k.save_menu({'meals':[{**meal,'meal':'dinner','name':'另一位的晚餐'}],'message':'晚餐留言'},actor='aion',epoch=epoch)
        await self.k.confirm_menu(first['id'],meal='lunch')
        table=await self.k.confirm_menu(second['id'],meal='dinner')
        dinner=dict(table['meals'][1])
        replacement=await self.k.save_menu({'meals':[{**meal,'name':'新午餐'}],'message':'新留言'},actor='connor',epoch=epoch,
                                         target_meal='lunch',proposal_id=table['id'],scope='kitchen')
        self.assertEqual([m['meal'] for m in replacement['meals']],['lunch'])
        self.assertEqual(replacement['origin'],{'scope':'chatroom','parent_id':'group-a'})
        updated=await self.k.confirm_menu(replacement['id'],meal='lunch')
        self.assertEqual(updated['meals'][1],dinner)

    async def test_swaps_keep_same_authors_other_proposal_visible_and_actionable(self):
        epoch = (await self.k.state())['epoch']
        lunch = {'meal':'lunch','name':'虾饼饭','minutes':15,'ingredients':[{'name':'鸡蛋','amount':1,'unit':'颗'}],'steps':['蒸熟。']}
        dinner = {**lunch,'meal':'dinner','name':'肥牛汤'}
        original = await self.k.save_menu({'meals':[lunch,dinner]},actor='aion',epoch=epoch)
        other = await self.k.save_menu({'meals':[{**dinner,'name':'烧鹅'}]},actor='connor',epoch=epoch)
        await self.k.confirm_menu(original['id'],meal='lunch')
        table = await self.k.confirm_menu(other['id'],meal='dinner')
        new_lunch = await self.k.save_menu({'meals':[{**lunch,'name':'双丸汤'}]},actor='aion',epoch=epoch,
                                          target_meal='lunch',proposal_id=table['id'])
        async def card():
            return await self.k.get_menu(original['id'], as_card=True)
        shown = await card()
        self.assertEqual([(m['name'],m['source_id']) for m in shown['meals']],
                         [('双丸汤',new_lunch['id']),('肥牛汤',original['id'])])
        self.assertEqual(shown['meal_status'], {'lunch':'proposed','dinner':'proposed'})
        with self.assertRaises(self.k.MenuChanged):
            await self.k.confirm_menu(original['id'],meal='lunch',expected_updated_at=original['updated_at'])
        self.assertEqual((await self.k.snapshot(as_cards=True))['proposals'][0],shown)
        await self.k.confirm_menu(shown['meals'][1]['source_id'],meal='dinner',
                                  expected_updated_at=shown['meals'][1]['source_revision'],
                                  expected_selection_at=shown['selection_at']['dinner'])
        self.assertEqual((await card())['meal_status']['dinner'],'confirmed')
        await self.k.save_menu({'meals':[{**dinner,'name':'蒸鱼'}]},actor='aion',epoch=epoch,
                               target_meal='dinner',proposal_id=original['id'])
        self.assertEqual([m['name'] for m in (await card())['meals']],['双丸汤','蒸鱼'])
        official = (await self.k.snapshot())['menus'][0]
        self.assertEqual([m['name'] for m in official['meals']],['虾饼饭','肥牛汤'])
        await self.k.save_menu({'meals':[lunch,dinner]},actor='aion',epoch=epoch)
        self.assertEqual([m['name'] for m in (await card())['meals']],['双丸汤','蒸鱼'])

    async def test_stale_displayed_choices_do_not_select_or_remove_a_new_recipe(self):
        epoch=(await self.k.state())['epoch']
        meal={'meal':'lunch','name':'旧午餐','minutes':15,'ingredients':[{'name':'鸡蛋','amount':2,'unit':'个'}],'steps':['蒸熟。']}
        first=await self.k.save_menu({'meals':[meal]},actor='connor',epoch=epoch)
        changed=await self.k.save_menu({'meals':[{**meal,'name':'换过的午餐'}]},actor='connor',epoch=epoch,proposal_id=first['id'])
        with self.assertRaises(ValueError):
            await self.k.confirm_menu(first['id'],meal='lunch',expected_updated_at=first['updated_at'])
        table=await self.k.confirm_menu(changed['id'],meal='lunch')
        other=await self.k.save_menu({'meals':[{**meal,'name':'另一位的午餐'}]},actor='aion',epoch=epoch)
        new_table=await self.k.confirm_menu(other['id'],meal='lunch')
        with self.assertRaises(ValueError):
            await self.k.remove_menu(table['id'],meal='lunch',expected_updated_at=table['updated_at'])
        with self.assertRaises(ValueError):
            await self.k.confirm_menu(first['id'],meal='lunch',reselect=True,expected_selection_at=0)
        self.assertEqual((await self.k.snapshot())['menus'][0]['meals'][0]['name'],'另一位的午餐')

    async def test_meal_decisions_are_saved_once_as_model_visible_system_messages(self):
        import kitchen_notices
        from context_builder import _is_model_visible_timeline_message, render_merged_timeline
        async with self.connect() as db:
            await db.execute('CREATE TABLE chatroom_rooms (id TEXT PRIMARY KEY,type TEXT,updated_at REAL)')
            await db.execute('CREATE TABLE chatroom_messages (id TEXT PRIMARY KEY,room_id TEXT,sender TEXT,content TEXT,attachments TEXT,created_at REAL)')
            await db.execute("INSERT INTO chatroom_rooms VALUES ('family','group',0)")
            await db.commit()
        epoch=(await self.k.state())['epoch']
        lunch={'meal':'lunch','name':'西兰花炒虾仁','minutes':15,'ingredients':[{'name':'虾仁','amount':.5,'unit':'袋'}],'steps':['炒熟。']}
        proposal=await self.k.save_menu({'meals':[lunch]},actor='connor',epoch=epoch,scope='chatroom',parent_id='family')
        with patch.object(kitchen_notices,'load_worldbook',return_value={'user_name':'小月'}), patch.object(kitchen_notices,'publish',AsyncMock()):
            table=await self.k.confirm_menu(proposal['id'],meal='lunch')
            await self.k.confirm_menu(proposal['id'],meal='lunch')
            async with self.connect() as db:
                db.row_factory=aiosqlite.Row
                rows=[dict(r) for r in await (await db.execute('SELECT * FROM chatroom_messages')).fetchall()]
            self.assertEqual(len(rows),1)
            self.assertEqual(rows[0]['content'],'小月确认了午餐菜单：西兰花炒虾仁')
            self.assertTrue(_is_model_visible_timeline_message(rows[0]))
            history=render_merged_timeline([{**rows[0],'source':'chatroom'}],'connor',include_image_attachments=False)
            self.assertTrue(any('确认了午餐菜单：西兰花炒虾仁' in m.get('content','') for m in history))
            await self.k.remove_menu(table['id'],meal='lunch')
            async with self.connect() as db:
                removed=await (await db.execute('SELECT content FROM chatroom_messages ORDER BY created_at DESC LIMIT 1')).fetchone()
            self.assertIn('午餐尚未确定',removed[0])

    async def test_cross_actor_swap_creates_own_proposal_without_overwriting_original(self):
        epoch = (await self.k.state())['epoch']
        lunch = {'meal':'lunch','name':'原午餐','minutes':15,'ingredients':[{'name':'鸡蛋','amount':2,'unit':'个'}],'steps':['蒸熟。']}
        dinner = {**lunch,'meal':'dinner','name':'原晚餐'}
        first = await self.k.save_menu({'meals':[lunch,dinner],'message':'第一位的留言'},actor='aion',epoch=epoch)
        second = await self.k.save_menu({'meals':[{**lunch,'name':'另一位的午餐'}],'message':'第二位的留言'},
                        actor='connor',epoch=epoch,target_meal='lunch',proposal_id=first['id'])
        self.assertNotEqual(first['id'],second['id'])
        original = await self.k.get_menu(first['id'])
        self.assertEqual(original['actor'],'aion')
        self.assertEqual(original['meals'][0]['name'],'原午餐')
        self.assertEqual(original['message'],'第一位的留言')
        self.assertEqual(second['actor'],'connor')

    async def test_mix_actors_by_meal_and_remove_without_touching_other_meal_or_stock(self):
        await self.k.upsert_item({'name':'鸡蛋','amount':6,'unit':'个'})
        epoch = (await self.k.state())['epoch']
        lunch = {'meal':'lunch','name':'甲的午餐','minutes':15,'ingredients':[{'name':'鸡蛋','amount':2,'unit':'个'}],'steps':['蒸熟。']}
        dinner = {**lunch,'meal':'dinner','name':'甲的晚餐'}
        first = await self.k.save_menu({'meals':[lunch,dinner],'message':'甲的留言'},actor='aion',epoch=epoch)
        second = await self.k.save_menu({'meals':[{**lunch,'name':'乙的午餐'},{**dinner,'name':'乙的晚餐'}],'message':'乙的留言'},actor='connor',epoch=epoch)
        await self.k.confirm_menu(first['id'],meal='lunch')
        self.assertEqual(len((await self.k.snapshot())['menus'][0]['meals']),1)
        reported_at = time.time()
        table = await self.k.confirm_menu(second['id'],meal='dinner')
        self.assertEqual([m['name'] for m in table['meals']],['甲的午餐','乙的晚餐'])
        self.assertEqual([m['actor'] for m in table['meals']],['aion','connor'])
        self.assertEqual([m['message'] for m in table['meals']],['甲的留言','乙的留言'])
        self.assertEqual((await self.k.get_menu(first['id']))['meal_status'],{'lunch':'confirmed','dinner':'proposed'})
        await self.apply('private:mixed-table', [{'op':'meal','meal':'lunch'}],source_ts=reported_at)
        self.assertEqual((await self.k.snapshot())['items'][0]['quantity'],6)
        await self.k.remove_menu(table['id'],meal='lunch')
        self.assertEqual([m['meal'] for m in (await self.k.snapshot())['menus'][0]['meals']],['dinner'])
        await self.k.confirm_menu(first['id'],meal='lunch')
        self.assertEqual([m['meal'] for m in (await self.k.snapshot())['menus'][0]['meals']],['dinner'])
        await self.k.confirm_menu(first['id'],meal='lunch',reselect=True)
        await self.k.remove_menu(table['id'])
        self.assertEqual((await self.k.snapshot())['menus'],[])
        self.assertEqual((await self.k.snapshot())['items'][0]['quantity'],6)

    async def test_confirm_retry_does_not_replace_newer_choice_or_delay_meal_record(self):
        await self.k.upsert_item({'name':'鸡蛋','amount':6,'unit':'个'})
        epoch = (await self.k.state())['epoch']
        meal = {'meal':'lunch','name':'蒸蛋','minutes':15,'ingredients':[{'name':'鸡蛋','amount':2,'unit':'个'}],'steps':['蒸熟。']}
        first = await self.k.save_menu({'meals':[meal]},actor='aion',epoch=epoch)
        confirmed = await self.k.confirm_menu(first['id'])
        reported_at = time.time()
        retried = await self.k.confirm_menu(first['id'])
        self.assertEqual(retried['updated_at'], confirmed['updated_at'])
        await self.apply('private:after-confirm', [{'op':'meal','meal':'lunch'}], source_ts=reported_at)
        self.assertEqual((await self.k.snapshot())['items'][0]['quantity'],6)
        second = await self.k.save_menu({'meals':[{**meal,'name':'另一份午餐'}]},actor='connor',epoch=epoch)
        latest = await self.k.confirm_menu(second['id'])
        self.assertEqual((await self.k.get_menu(first['id']))['status'],'superseded')
        await self.k.confirm_menu(first['id'])
        self.assertEqual((await self.k.snapshot())['menus'][0]['updated_at'],latest['updated_at'])
        self.assertEqual((await self.k.snapshot())['menus'][0]['meals'][0]['name'],'另一份午餐')

    async def test_generated_menu_requires_confirmation_and_swap_keeps_table(self):
        epoch = (await self.k.state())['epoch']
        lunch = {'meal':'lunch','name':'午餐原计划','minutes':15,'ingredients':[{'name':'鸡蛋','amount':1,'unit':'个'}],'steps':['蒸熟。']}
        dinner = {**lunch, 'meal':'dinner', 'name':'晚餐原计划'}
        menu = await self.k.save_menu({'meals':[lunch,dinner], 'message':'今天辛苦啦，陪你好好吃饭。'}, actor='aion', epoch=epoch)
        data = await self.k.snapshot()
        self.assertEqual(data['menus'], [])
        self.assertEqual(data['proposals'][0]['message'], '今天辛苦啦，陪你好好吃饭。')
        await self.k.confirm_menu(menu['id'])
        replacement = await self.k.save_menu({'meals':[{**lunch,'name':'新午餐'},{**dinner,'name':'不该替换的晚餐'}]},
                         actor='aion', epoch=epoch, target_meal='lunch', proposal_id=menu['id'])
        data = await self.k.snapshot()
        self.assertEqual([m['name'] for m in data['menus'][0]['meals']], ['午餐原计划','晚餐原计划'])
        # An already chosen version forks only the changed meal; dinner stays on the table.
        self.assertEqual([m['name'] for m in replacement['meals']], ['新午餐'])
        await self.k.set_enabled(False)
        await self.k.confirm_menu(replacement['id'])
        self.assertEqual([m['name'] for m in (await self.k.snapshot())['menus'][0]['meals']], ['新午餐','晚餐原计划'])

    async def test_legacy_unapproved_menu_becomes_proposal_without_breaking_reference(self):
        async with self.connect() as db:
            await db.execute('DROP TABLE kitchen_proposals')
            await db.execute('DROP TABLE kitchen_menus')
            await db.execute('CREATE TABLE kitchen_menus (id TEXT PRIMARY KEY,date TEXT UNIQUE,actor TEXT,payload TEXT,updated_at REAL)')
            meal = {'meal':'lunch','name':'旧提案','minutes':15,'ingredients':[{'name':'鸡蛋','amount':1,'unit':'个'}],'steps':['蒸熟。']}
            await db.execute('INSERT INTO kitchen_menus VALUES (?,?,?,?,?)', ('legacy',date.today().isoformat(),'aion',json.dumps({'meals':[meal]}),time.time()))
            await db.commit()
        await self.k.ensure_schema()
        self.assertEqual((await self.k.snapshot())['menus'], [])
        self.assertEqual((await self.k.get_menu('legacy'))['status'], 'proposed')
        await self.k.confirm_menu('legacy')
        await self.k.ensure_schema()
        self.assertEqual(len((await self.k.snapshot())['menus']), 1)

    async def test_bulk_inventory_accepts_counts_and_half_packages_with_ai_off(self):
        await self.k.set_enabled(False)
        await self.k.bulk_items('黄瓜2根，西兰花一颗\n肥牛半盒')
        data = await self.k.snapshot()
        self.assertEqual({i['name']: i['quantity'] for i in data['items']}, {'黄瓜': 2, '西兰花': 1, '肥牛': .5})
        self.assertEqual(next(i for i in data['items'] if i['name'] == '西兰花')['category'], 'vegetable')
        with self.assertRaises(ValueError):
            await self.k.bulk_items('玉米2节\n这一行无法判断份量')
        self.assertEqual(len((await self.k.snapshot())['items']), 3)

    async def test_stock_changes_are_applied_once_per_user_message(self):
        item = await self.k.upsert_item({'name': '黄瓜', 'amount': '2', 'unit': '根'})
        changes = [{'op': 'consume', 'name': '黄瓜', 'amount': 1, 'unit': '根'}]
        await self.apply('private:one', changes)
        await self.apply('private:one', changes)
        data = await self.k.snapshot()
        self.assertEqual(data['items'][0]['quantity'], 1)
        self.assertEqual(data['items'][0]['id'], item['id'])

    async def test_unclear_amount_waits_for_confirmation_without_guessing(self):
        await self.k.upsert_item({'name': '肥牛', 'amount': '半', 'unit': '盒'})
        await self.apply('private:two', [{'op': 'consume', 'name': '肥牛'}])
        data = await self.k.snapshot()
        self.assertEqual(data['items'][0]['quantity'], .5)
        self.assertTrue(data['items'][0]['uncertain'])
        self.assertEqual(len(data['pending']), 1)
        await self.k.resolve_pending(data['pending'][0]['id'], {'action': 'apply', 'amount': '少量', 'unit': '盒'})
        data = await self.k.snapshot()
        self.assertEqual(data['pending'], [])
        self.assertEqual(data['items'][0]['amount'], '少量')
        self.assertFalse(data['items'][0]['uncertain'])

    async def test_old_result_cannot_overwrite_manual_correction_or_cross_toggle(self):
        before = time.time() - 20
        item = await self.k.upsert_item({'name': '黄瓜', 'amount': '3', 'unit': '根'})
        await self.apply('private:old', [{'op': 'set', 'name': '黄瓜', 'amount': 1, 'unit': '根'}], source_ts=before)
        old_epoch = (await self.k.state())['epoch']
        await self.k.set_enabled(False)
        await self.k.set_enabled(True)
        result = await self.k.apply_changes('private:late', [{'op': 'empty', 'name': '黄瓜'}],
                                             epoch=old_epoch, source_ts=time.time())
        self.assertFalse(result['updated'])
        data = await self.k.snapshot()
        self.assertEqual(data['items'][0]['quantity'], 3)
        self.assertEqual(data['items'][0]['id'], item['id'])

    async def test_unit_mismatch_needs_confirmation(self):
        await self.k.upsert_item({'name': '鱼丸', 'amount': '半', 'unit': '袋'})
        await self.apply('private:fish', [{'op': 'consume', 'name': '鱼丸', 'amount': 3, 'unit': '颗'}])
        data = await self.k.snapshot()
        self.assertEqual(data['items'][0]['quantity'], .5)
        self.assertTrue(data['items'][0]['uncertain'])

        self.assertEqual(len(data['pending']), 1)
        await self.apply('private:fish-more', [{'op':'add','name':'鱼丸','amount':1,'unit':'袋'}])
        data = await self.k.snapshot()
        self.assertEqual(data['items'][0]['quantity'], .5)
        self.assertTrue(data['items'][0]['uncertain'])
        # The user can acknowledge three pieces even if stock was entered in bags.
        fish = data['items'][0]
        await self.k.resolve_pending(data['pending'][0]['id'],
                                     {'action':'consume','item_id':fish['id'],'amount':3,'unit':'颗'})
        after = await self.k.snapshot()
        self.assertIsNone(after['items'][0]['quantity'])
        self.assertEqual(after['items'][0]['amount'], '待估')
        self.assertEqual(after['items'][0]['unit'], '袋')
        self.assertEqual(len(after['pending']), 1)

    async def test_delayed_changes_cannot_resurrect_deleted_or_renamed_items(self):
        item = await self.k.upsert_item({'name':'鱼丸','amount':1,'unit':'袋'})
        sent = time.time()
        await self.k.delete_item(item['id'])
        await self.apply('private:deleted', [{'op':'add','name':'鱼丸','amount':1,'unit':'袋'}], source_ts=sent)
        self.assertEqual((await self.k.snapshot())['items'], [])
        item = await self.k.upsert_item({'name':'虾滑','amount':1,'unit':'盒'})
        sent = time.time()
        await self.k.upsert_item({**item, 'name':'鲜虾滑','amount':2})
        await self.apply('private:renamed', [{'op':'set','name':'虾滑','amount':1,'unit':'盒'}], source_ts=sent)
        self.assertEqual([(i['name'],i['quantity']) for i in (await self.k.snapshot())['items']], [('鲜虾滑',2)])

    async def test_changed_menu_does_not_consume_or_create_pending_usage(self):
        await self.k.upsert_item({'name':'鸡蛋','amount':6,'unit':'个'})
        epoch = (await self.k.state())['epoch']
        meal = {'meal':'lunch','name':'蒸蛋','minutes':15,'ingredients':[{'name':'鸡蛋','amount':2,'unit':'个'}],'steps':['蒸熟。']}
        first = await self.k.save_menu({'meals':[meal]}, actor='aion', epoch=epoch)
        await self.k.confirm_menu(first['id'])
        sent = time.time()
        second = await self.k.save_menu({'meals':[{**meal,'name':'另一份蒸蛋','ingredients':[{'name':'鸡蛋','amount':4,'unit':'个'}]}]}, actor='connor', epoch=epoch)
        await self.k.confirm_menu(second['id'])
        await self.apply('private:late-meal', [{'op':'meal','meal':'lunch'}], source_ts=sent)
        data = await self.k.snapshot()
        self.assertEqual(data['items'][0]['quantity'],6)
        self.assertEqual(data['pending'], [])

    async def test_single_meal_save_preserves_other_meal_even_if_model_returns_both(self):
        epoch = (await self.k.state())['epoch']
        lunch = {'meal':'lunch','name':'午餐原计划','minutes':15,'ingredients':[{'name':'鸡蛋','amount':1,'unit':'个'}],'steps':['蒸熟。']}
        dinner = {**lunch,'meal':'dinner','name':'晚餐原计划'}
        first = await self.k.save_menu({'meals':[lunch,dinner]},actor='aion',epoch=epoch)
        await self.k.confirm_menu(first['id'])
        second = await self.k.save_menu({'meals':[{**lunch,'name':'新午餐'},{**dinner,'name':'不该替换的晚餐'}]},
                               actor='connor',epoch=epoch,target_meal='lunch')
        await self.k.confirm_menu(second['id'])
        meals = (await self.k.snapshot())['menus'][0]['meals']
        self.assertEqual([m['name'] for m in meals],['新午餐','晚餐原计划'])

    async def test_menu_is_shared_and_expires_after_seven_days_without_losing_stock(self):
        await self.k.upsert_item({'name': '鸡蛋', 'amount': '6', 'unit': '个'})
        epoch = (await self.k.state())['epoch']
        meal = {'meal': 'lunch', 'name': '青椒肥牛', 'minutes': 15,
                'ingredients': [{'name': '肥牛', 'amount': .5, 'unit': '盒'}], 'steps': ['少油炒熟。']}
        menu = await self.k.save_menu({'meals': [meal]}, actor='aion', epoch=epoch)
        meal['name'] = '青椒肥牛汤'
        again = await self.k.save_menu({'meals': [meal]}, actor='connor', epoch=epoch, proposal_id=menu['id'])
        self.assertNotEqual(menu['id'], again['id'])
        today = date.today()
        async with self.connect() as db:
            for days in (6, 7):
                await db.execute('INSERT INTO kitchen_proposals (id,date,actor,payload,updated_at) VALUES (?,?,?,?,?)',
                                 (f'old-{days}', (today-timedelta(days=days)).isoformat(), 'aion',
                                  json.dumps({'meals': [meal]}), time.time()))
            await db.commit()
        data = await self.k.snapshot()
        self.assertEqual({m['id'] for m in data['proposals']}, {menu['id'], again['id'], 'old-6'})
        self.assertEqual(data['items'][0]['quantity'], 6)


class KitchenSentinelTests(KitchenFixture):
    async def test_only_current_reported_ingredients_change_stock(self):
        import kitchen_sentinel as sentinel
        await self.k.bulk_items('冷冻黑虎虾 18 只\n鸡蛋 6 颗')
        epoch = (await self.k.state())['epoch']
        menu = await self.k.save_menu({'meals': [{'meal': 'lunch', 'name': '清蒸虾',
            'ingredients': [{'name': '冷冻黑虎虾', 'amount': 8, 'unit': '只'}], 'steps': ['蒸熟']}]},
            actor='connor', epoch=epoch)
        await self.k.confirm_menu(menu['id'])
        meal = {'op': 'meal', 'meal': 'lunch'}
        shrimp = {'op': 'consume', 'name': '冷冻黑虎虾', 'ingredient': '黑虎虾', 'amount': 6, 'unit': '只'}
        egg = {'op': 'consume', 'name': '鸡蛋', 'amount': 2, 'unit': '颗', 'evidence': '吃了两颗鸡蛋'}
        cases = [
            ('刚用了六只黑虎虾', [{**shrimp, 'evidence': '刚用了六只黑虎虾'}], 12),
            ('好！那今天就吃这些，等会一点我做', [meal], 12),
            ('午餐准备蒸6只黑虎虾', [{**shrimp, 'evidence': '蒸6只黑虎虾'}], 12),
            ('午饭做好了，用了6只黑虎虾，鸡蛋打算晚饭用', [
                {**shrimp, 'evidence': '用了6只黑虎虾'}, egg], 6),
            ('开饭啦，午餐按菜单做完了', [meal, egg], 6),
            ('物业每户发了12瓶饮用水，我等会去领', [
                {'op': 'add', 'name': '饮用水', 'amount': 12, 'evidence': '物业每户发了12瓶饮用水'}], 6),
        ]
        for index, (message, changes, remaining) in enumerate(cases):
            with self.subTest(message=message):
                await sentinel.enqueue_event(await self.event(message, str(index)))
                with patch.object(sentinel, '_call_model', AsyncMock(return_value={'changes': changes})), \
                     patch.object(sentinel, '_broadcast', AsyncMock()), patch.object(self.k, 'notify', AsyncMock()):
                    await sentinel.run_pending_once()
                data = await self.k.snapshot()
                self.assertEqual({i['name']: i['quantity'] for i in data['items']},
                                 {'冷冻黑虎虾': remaining, '鸡蛋': 6})
                self.assertEqual(data['pending'], [])

        # Even when the model repeats the recipe quantity, only a number in the report is supported.
        actual = '我又下锅了两只黑虎虾'
        self.assertEqual(sentinel.reported_changes(actual, [{**shrimp, 'evidence': actual}])[0]['amount'], None)

    async def event(self, text, msg_id='user-one'):
        async with self.connect() as db:
            await db.execute('CREATE TABLE IF NOT EXISTS messages (id TEXT PRIMARY KEY,conv_id TEXT,role TEXT,content TEXT,created_at REAL,attachments TEXT)')
            now = time.time()
            await db.execute('INSERT INTO messages VALUES (?,?,?,?,?,?)', (msg_id, 'room', 'user', text, now, '[]'))
            await db.commit()
        return {'type': 'msg_created', 'data': {'id': msg_id, 'conv_id': 'room', 'role': 'user',
                'content': text, 'created_at': now, 'attachments': []}}

    async def test_background_record_updates_inventory_and_original_message(self):
        import kitchen_sentinel as sentinel
        event = await self.event('刚买了两根黄瓜')
        await sentinel.enqueue_event(event)
        await sentinel.enqueue_event(event)
        with patch.object(sentinel, '_call_model', AsyncMock(return_value={'changes': [
                {'op': 'add', 'name': '黄瓜', 'amount': 2, 'unit': '根', 'evidence':'买了两根黄瓜'}]})), \
             patch.object(sentinel, '_broadcast', AsyncMock()), patch.object(self.k, 'notify', AsyncMock()):
            await sentinel.run_pending_once()
        data = await self.k.snapshot()
        self.assertEqual(data['items'][0]['quantity'], 2)
        async with self.connect() as db:
            row = await (await db.execute('SELECT attachments FROM messages')).fetchone()
            self.assertEqual(json.loads(row[0]), [{'type': 'kitchen_updated'}])
            count = (await (await db.execute('SELECT count(*) FROM kitchen_jobs')).fetchone())[0]
            self.assertEqual(count, 1)

    async def test_switch_during_model_call_discards_old_result_and_marker(self):
        import kitchen_sentinel as sentinel
        await sentinel.enqueue_event(await self.event('买了两根黄瓜'))
        async def delayed(_):
            await self.k.set_enabled(False)
            await self.k.set_enabled(True)
            return {'changes': [{'op': 'add', 'name': '黄瓜', 'amount': 2, 'unit': '根'}]}
        with patch.object(sentinel, '_call_model', delayed), patch.object(sentinel, '_broadcast', AsyncMock()), \
             patch.object(self.k, 'notify', AsyncMock()):
            await sentinel.run_pending_once()
        data = await self.k.snapshot()
        self.assertEqual(data['items'], [])
        async with self.connect() as db:
            self.assertEqual((await (await db.execute('SELECT attachments FROM messages')).fetchone())[0], '[]')

    async def test_unrelated_chat_and_disabled_kitchen_do_not_queue_jobs(self):
        import kitchen_sentinel as sentinel
        await sentinel.enqueue_event(await self.event('今天代码写得好开心', 'ordinary'))
        await sentinel.enqueue_event(await self.event('今天吃什么？', 'meal-question'))
        await self.k.set_enabled(False)
        await sentinel.enqueue_event(await self.event('刚买了两根黄瓜', 'disabled'))
        async with self.connect() as db:
            self.assertEqual((await (await db.execute('SELECT count(*) FROM kitchen_jobs')).fetchone())[0], 0)

    async def test_new_ingredients_enter_queue_but_old_messages_do_not_cross_reenable(self):
        import kitchen_sentinel as sentinel
        old = await self.event('刚买了三斤土豆','old-potato')
        await self.k.set_enabled(False)
        await self.k.set_enabled(True)
        await sentinel.enqueue_event(old)
        for index,text in enumerate(['刚买了三斤土豆','买了500克面粉','收到一瓶酱油']):
            await sentinel.enqueue_event(await self.event(text, f'new-{index}'))
        async with self.connect() as db:
            sources = [r[0] for r in await (await db.execute('SELECT message_id FROM kitchen_jobs ORDER BY message_id')).fetchall()]
        self.assertEqual(sources,['new-0','new-1','new-2'])

    async def test_closing_during_snapshot_never_dispatches_recording_prompt(self):
        import kitchen_sentinel as sentinel
        await sentinel.enqueue_event(await self.event('买了两根黄瓜'))
        real_snapshot = self.k.snapshot
        calls=[]
        async def snapshot_then_close():
            data=await real_snapshot()
            await self.k.set_enabled(False)
            return data
        async def model(_):
            calls.append(True)
            return {'changes':[]}
        with patch.object(self.k,'snapshot',snapshot_then_close), patch.object(sentinel,'_call_model',model):
            await sentinel.run_pending_once()
        self.assertEqual(calls,[])

    async def test_committed_record_recovers_missing_source_marker_without_recalling_model(self):
        import asyncio
        import kitchen_sentinel as sentinel
        await sentinel.enqueue_event(await self.event('买了两根黄瓜'))
        calls=[]
        async def model(_):
            calls.append(True)
            return {'changes':[{'op':'add','name':'黄瓜','amount':2,'unit':'根','evidence':'买了两根黄瓜'}]}
        with patch.object(sentinel,'_call_model',model), patch.object(sentinel,'_broadcast',AsyncMock()), patch.object(self.k,'notify',AsyncMock()):
            with patch.object(sentinel,'_mark_message',AsyncMock(side_effect=asyncio.CancelledError)):
                with self.assertRaises(asyncio.CancelledError):
                    await sentinel.run_pending_once()
            await sentinel.run_pending_once()
        self.assertEqual(len(calls),1)
        self.assertEqual((await self.k.snapshot())['items'][0]['quantity'],2)
        async with self.connect() as db:
            self.assertEqual(json.loads((await (await db.execute('SELECT attachments FROM messages')).fetchone())[0]),[{'type':'kitchen_updated'}])

    async def test_creative_messages_are_not_recorded_as_real_food(self):
        import kitchen_sentinel as sentinel
        event=await self.event('我吃了两个鸡蛋，接下来怎么办')
        event['data']['attachments']=[{'type':'kitchen_excluded'}]
        await sentinel.enqueue_event(event)
        async with self.connect() as db:
            self.assertEqual((await (await db.execute('SELECT count(*) FROM kitchen_jobs')).fetchone())[0],0)


class JointSentinelTests(KitchenFixture):
    event = KitchenSentinelTests.event

    async def asyncSetUp(self):
        import asyncio
        import kitchen_sentinel
        import post_sentinel
        import sentinel_dispatch
        await super().asyncSetUp()
        self.profile, self.sentinel, self.dispatch = post_sentinel, kitchen_sentinel, sentinel_dispatch
        patches = [
            patch.object(post_sentinel,'get_db',self.connect),
            patch.object(post_sentinel,'enabled',return_value=True),
            patch.object(post_sentinel,'_notify_changed',AsyncMock()),
            patch.object(post_sentinel,'_processing',asyncio.Lock()),
            patch.object(kitchen_sentinel,'_processing',asyncio.Lock()),
            patch.object(kitchen_sentinel,'_broadcast',AsyncMock()),
            patch.object(sentinel_dispatch,'queue_lock',asyncio.Lock()),
            patch.object(self.k,'notify',AsyncMock()),
        ]
        for p in patches:
            p.start()
        self.patches.extend(patches)
        await post_sentinel.ensure_schema()
        await self.k.upsert_item({'name':'鸡蛋','amount':6,'unit':'颗'})

    def answer(self):
        return {'life':{'updates':[],'state':{'text':'早饭吃完了','phase':'finished','ttl_minutes':30,'sources':['U1']}},
                'kitchen':{'changes':[{'op':'consume','name':'鸡蛋','amount':2,'unit':'颗','evidence':'吃了两颗鸡蛋'}]}}

    async def test_shared_call_handles_concurrent_workers_lookup_and_duplicate_delivery(self):
        import asyncio
        event = await self.event('早饭吃了两颗鸡蛋')
        with patch.object(self.profile,'_call_model',AsyncMock(return_value=self.answer())) as profile_model, \
             patch.object(self.sentinel,'_call_model',AsyncMock()) as kitchen_model:
            await asyncio.gather(self.dispatch.enqueue_event(event),
                                 self.profile.run_pending_once(),self.sentinel.flush_pending())
            await self.dispatch.enqueue_event(event)
            await self.sentinel.flush_pending()
        self.assertEqual(profile_model.await_count,1)
        kitchen_model.assert_not_awaited()
        self.assertIn('<kitchen模块>',profile_model.await_args.args[0])
        self.assertEqual((await self.k.snapshot())['items'][0]['quantity'],4)
        self.assertEqual((await self.profile.snapshot())['current_state']['text'],'早饭吃完了')
        async with self.connect() as db:
            attachments = json.loads((await (await db.execute('SELECT attachments FROM messages')).fetchone())[0])
        self.assertEqual(attachments,[{'type':'kitchen_updated'}])

    async def test_disabled_life_keeps_kitchen_only_and_disabled_kitchen_keeps_life_only(self):
        with patch.object(self.profile,'enabled',return_value=False), \
             patch.object(self.profile,'_call_model',AsyncMock()) as profile_model, \
             patch.object(self.sentinel,'_call_model',AsyncMock(return_value=self.answer()['kitchen'])) as kitchen_model:
            await self.dispatch.enqueue_event(await self.event('吃了两颗鸡蛋','kitchen-only'))
            await self.sentinel.flush_pending()
            profile_model.assert_not_awaited()
            self.assertEqual(kitchen_model.await_count,1)
            self.assertNotIn('近期生活',kitchen_model.await_args.args[0])
        await self.k.set_enabled(False)
        with patch.object(self.profile,'_call_model',AsyncMock(return_value={'updates':[],'state':None})) as profile_model, \
             patch.object(self.sentinel,'_call_model',AsyncMock()) as kitchen_model:
            await self.dispatch.enqueue_event(await self.event('买了两颗鸡蛋','life-only'))
            await self.profile.run_pending_once()
            self.assertEqual(profile_model.await_count,1)
            self.assertNotIn('kitchen模块',profile_model.await_args.args[0])
            self.assertNotIn('食材名称',profile_model.await_args.args[0])
            kitchen_model.assert_not_awaited()
        self.assertEqual((await self.k.snapshot())['items'][0]['quantity'],4)

    async def test_unrelated_chat_does_not_add_kitchen_prompt(self):
        await self.dispatch.enqueue_event(await self.event('今天代码写得好开心'))
        with patch.object(self.profile,'_call_model',AsyncMock(return_value={'updates':[],'state':None})) as profile_model, \
             patch.object(self.sentinel,'_call_model',AsyncMock()) as kitchen_model:
            await self.profile.run_pending_once()
            await self.sentinel.flush_pending()
        self.assertNotIn('kitchen模块',profile_model.await_args.args[0])
        self.assertNotIn('食材名称',profile_model.await_args.args[0])
        kitchen_model.assert_not_awaited()

    async def test_life_retry_does_not_reanalyze_or_consume_kitchen_twice(self):
        await self.dispatch.enqueue_event(await self.event('吃了两颗鸡蛋'))
        broken = {**self.answer(),'life':'invalid'}
        with patch.object(self.profile,'_call_model',AsyncMock(return_value=broken)) as first, \
             patch.object(self.sentinel,'_call_model',AsyncMock()) as kitchen_model:
            await self.profile.run_pending_once()
            await self.sentinel.run_pending_once()
            self.assertEqual(first.await_count,1)
            async with self.connect() as db:
                await db.execute('UPDATE post_sentinel_jobs SET next_attempt_at=0')
                await db.commit()
            with patch.object(self.profile,'_call_model',AsyncMock(return_value=self.answer()['life'])) as retry:
                await self.profile.run_pending_once()
                self.assertNotIn('kitchen模块',retry.await_args.args[0])
            await self.sentinel.flush_pending()
            kitchen_model.assert_not_awaited()
        self.assertEqual((await self.k.snapshot())['items'][0]['quantity'],4)
        self.assertEqual((await self.profile.snapshot())['last_error'],'')

    async def test_missing_kitchen_output_retries_only_kitchen(self):
        await self.dispatch.enqueue_event(await self.event('吃了两颗鸡蛋'))
        with patch.object(self.profile,'_call_model',AsyncMock(return_value={'life':self.answer()['life']})) as profile_model, \
             patch.object(self.sentinel,'_call_model',AsyncMock(return_value=self.answer()['kitchen'])) as kitchen_model:
            await self.profile.run_pending_once()
            await self.sentinel.flush_pending()
            self.assertEqual(profile_model.await_count,1)
            self.assertEqual(kitchen_model.await_count,1)
        self.assertEqual((await self.k.snapshot())['items'][0]['quantity'],4)

    async def test_closing_either_feature_during_joint_request_preserves_the_other(self):
        for feature in ('kitchen','life'):
            with self.subTest(feature=feature):
                await self.k.set_enabled(True)
                await self.dispatch.enqueue_event(await self.event('吃了两颗鸡蛋',feature))
                active = True
                async def model(_):
                    nonlocal active
                    if feature=='kitchen':
                        await self.k.set_enabled(False)
                        await self.k.set_enabled(True)
                    else:
                        active = False
                        await self.profile.invalidate_pending()
                    return self.answer()
                async with self.connect() as db:
                    await db.execute("UPDATE post_sentinel_document SET payload=json_set(payload,'$.current_state',null)")
                    await db.commit()
                with patch.object(self.profile,'enabled',side_effect=lambda:active), \
                     patch.object(self.profile,'_call_model',model), \
                     patch.object(self.sentinel,'_call_model',AsyncMock()) as kitchen_model:
                    await self.profile.run_pending_once()
                    await self.sentinel.flush_pending()
                    kitchen_model.assert_not_awaited()
                state = (await self.profile.snapshot())['current_state']
                self.assertEqual(bool(state),feature=='kitchen')
        self.assertEqual((await self.k.snapshot())['items'][0]['quantity'],4)


class KitchenChatTests(KitchenFixture):
    async def test_menu_words_do_not_query_inventory_without_a_model_request(self):
        import kitchen_chat as chat
        for actor in ('connor','aion'):
            for query in ('今天吃啥？','重新出两份菜单','帮我核对一下冰箱库存'):
                with self.subTest(actor=actor,query=query):
                    seen, statuses = [], []
                    async def generate(messages):
                        seen.extend(messages)
                        yield '先聊聊你今天想怎么吃。'
                    with patch.object(chat,'read_context',AsyncMock(side_effect=AssertionError('unrequested lookup'))):
                        _,state = await self.collect(generate,query,statuses,actor=actor)
                    self.assertFalse(state['queried'])
                    self.assertEqual(statuses,[])
                    self.assertFalse(any('[小厨房本次查询]' in m.get('content','') for m in seen))

    async def test_menu_repair_does_not_query_inventory_without_a_model_request(self):
        import kitchen_chat as chat
        calls = []
        async def generate(messages):
            calls.append(messages)
            yield '[KITCHEN_MENU]{"options":[]}[/KITCHEN_MENU]' if len(calls)==1 else self.menu_protocol()
        with patch.object(chat,'read_context',AsyncMock(side_effect=AssertionError('repair lookup'))):
            _,state = await self.collect(generate,'重新出两份菜单')
        self.assertEqual(len(state['payloads']),1)
        self.assertFalse(state['queried'])

    async def test_requesting_new_lunch_options_repairs_old_card_and_keeps_both_options(self):
        import kitchen_chat as chat
        payload = json.loads(self.menu_protocol().split('[KITCHEN_MENU]')[1].split('[/KITCHEN_MENU]')[0])
        old = await self.k.save_menu(payload,actor='aion',epoch=(await self.k.state())['epoch'])
        calls = []
        async def generate(messages):
            calls.append(messages)
            yield '[KITCHEN_CARD:'+old['id']+']' if len(calls)==1 else self.menu_protocol()
        with patch.object(self.k,'notify',AsyncMock()):
            visible, state = await self.collect(generate,'午餐的菜单我想重新出两份')
            _, attachments = await chat.finish_reply(visible,actor='aion',state=state)
        self.assertEqual(len(calls),2)
        self.assertNotEqual(attachments[0]['menu_id'],old['id'])
        self.assertEqual(len((await self.k.get_menu(attachments[0]['menu_id']))['meals']),2)
    async def test_swap_button_returns_new_dish_with_authors_other_meal(self):
        import ai_providers, context_builder, kitchen_chat as chat
        epoch = (await self.k.state())['epoch']
        await self.k.upsert_item({'name':'鸡蛋','amount':8,'unit':'颗'})
        lunch = {'meal':'lunch','name':'原午餐','ingredients':[{'name':'鸡蛋','amount':1,'unit':'颗'}],'steps':['蒸熟。']}
        dinner = {**lunch,'meal':'dinner','name':'原晚餐'}
        p = await self.k.save_menu({'meals':[lunch,dinner]},actor='aion',epoch=epoch)
        await self.k.confirm_menu(p['id'], meal='lunch')
        official = await self.k.save_menu({'meals':[{**dinner,'name':'另一份晚餐','ingredients':[
            {'name':'鸡蛋','amount':2,'unit':'颗'}]}]},actor='connor',epoch=epoch)
        await self.k.confirm_menu(official['id'], meal='dinner')
        async def generate(messages, *args):
            if not any(m['content'].startswith('[小厨房本次查询]') for m in messages):
                yield '[KITCHEN:查看]'
                return
            receipt = next(m['content'] for m in messages if m['content'].startswith('[小厨房本次查询]'))
            context = json.loads(receipt.splitlines()[1])
            self.assertEqual(context['replacing'], '原午餐')
            self.assertEqual(context['reserved'][0]['ingredients'][0]['amount'],2)
            yield '[KITCHEN_CARD:'+p['id']+']' + self.menu_protocol()
        with patch.object(context_builder,'fetch_merged_timeline',AsyncMock(return_value=[])), \
             patch.object(ai_providers,'stream_ai',generate), patch.object(self.k,'notify',AsyncMock()):
            result = await chat.generate_plan(actor='aion',meal='lunch',proposal_id=p['id'])
        self.assertEqual([m['name'] for m in result['menu']['meals']], ['青椒肥牛','原晚餐'])
        self.assertNotEqual(result['menu']['id'], p['id'])

    async def test_casual_followup_does_not_resend_the_confirmed_card(self):
        import kitchen_chat as chat
        menu = json.loads(self.menu_protocol().split('[KITCHEN_MENU]')[1].split('[/KITCHEN_MENU]')[0])
        p = await self.k.save_menu(menu, actor='aion', epoch=(await self.k.state())['epoch'])
        selected = await self.k.confirm_menu(p['id'], meal='lunch',serve_as='lunch')
        async def generate(messages):
            yield '我也喜欢。' + '[KITCHEN_CARD:'+selected['id']+']'
        with patch.object(self.k, 'notify', AsyncMock()):
            for query, expected in [('老公的脑子能把冰箱里的菜做成不错的一餐', []),
                                    ('重新出两道数学题', []),
                                    ('把刚选的菜单再发我看看', [selected['id']])]:
                visible, state = await self.collect(generate, query)
                reply, cards = await chat.finish_reply(visible,actor='aion',state=state)
                self.assertEqual(reply, '我也喜欢。')
                self.assertEqual([c['menu_id'] for c in cards if c['type']=='kitchen_menu'], expected)

    async def test_other_actor_proposal_reference_is_repaired_into_own_suggestion(self):
        import kitchen_chat as chat
        payload=json.loads(self.menu_protocol().split('[KITCHEN_MENU]')[1].split('[/KITCHEN_MENU]')[0])
        first=await self.k.save_menu(payload,actor='aion',epoch=(await self.k.state())['epoch'])
        calls=[]
        async def generate(messages):
            calls.append(messages)
            if len(calls)==1:
                yield '[KITCHEN_CARD:'+first['id']+']'
            elif len(calls)==2:
                yield '[KITCHEN:查看]'
            else:
                yield self.menu_protocol()
        with patch.object(chat,'actor_name',return_value='另一位'), patch.object(self.k,'notify',AsyncMock()):
            visible,state=await self.collect(generate,'今天吃啥？',actor='connor')
            _,attachments=await chat.finish_reply(visible,actor='connor',state=state)
        self.assertEqual(len(calls),3)
        self.assertEqual([a['type'] for a in attachments],['kitchen_menu','kitchen_lookup'])
        self.assertNotEqual(attachments[0]['menu_id'],first['id'])
        self.assertEqual((await self.k.get_menu(attachments[0]['menu_id']))['actor'],'connor')

    async def test_unrelated_reporting_does_not_read_fridge(self):
        seen=[]
        async def generate(messages):
            seen.extend(messages)
            yield '收到。'
        for query in ('我买了新电脑','项目只剩下测试了','刚把资料录入完了','能把冰箱里的菜做成一餐，真好'):
            seen.clear()
            statuses=[]
            await self.collect(generate, query, statuses)
            self.assertEqual(statuses,[],query)
            self.assertFalse(any('[小厨房本次查询]' in m.get('content','') for m in seen),query)

    async def test_food_report_does_not_prefetch_stock_for_either_actor(self):
        import kitchen_chat as chat
        query = '现在才5点吧，咱们6点半吃饭，一会儿我6:10再去做晚上这一顿，我现在先吃一颗香梨垫一垫~还有早上没喝完的半瓶酸奶'
        for actor in ('connor','aion'):
            with self.subTest(actor=actor):
                calls, statuses = [], []
                async def generate(messages):
                    calls.append(messages)
                    yield '六点十分再去做饭，先歇一会儿。'
                visible,state = await self.collect(generate,query,statuses,actor=actor)
                _,attachments = await chat.finish_reply(visible,actor=actor,state=state)
                self.assertEqual(len(calls),1)
                self.assertEqual(statuses,[])
                self.assertFalse(state['queried'])
                self.assertEqual(attachments,[])
                self.assertFalse(any('[小厨房本次查询]' in m.get('content','') for m in calls[0]))

    async def test_lookup_continuation_sees_own_preamble_and_does_not_repeat_it(self):
        import kitchen_chat as chat
        calls=[]
        opening='这份清单收到啦，我看看。'
        async def generate(messages):
            calls.append(messages)
            if len(calls)==1:
                yield opening + '[KITCHEN:查看]'
            else:
                previous = any(m.get('role')=='assistant' and opening in m.get('content','') for m in messages)
                yield ('' if previous else opening) + '已经核对好库存了。'
        with patch.object(chat, 'actor_name', return_value='AI'):
            visible, _ = await self.collect(generate, '帮我看一下吧')
        self.assertEqual(len(calls),2)
        self.assertEqual(visible.count(opening),1)

    async def test_lookup_boundary_survives_streaming_cleanup_and_is_saved_with_card(self):
        import asyncio
        import kitchen_chat as chat
        from context_builder import strip_tool_commands
        queue, state, calls = asyncio.Queue(), {}, []
        opening = '先吃饭，宝宝😊。'
        async def generate(messages):
            calls.append(messages)
            yield opening+'[MUSIC:轻音乐][KITCHEN:查看]' if len(calls)==1 else '库存看好啦。'+self.menu_protocol()
        with patch.object(chat,'actor_name',return_value='家里的厨师'), patch.object(self.k,'notify',AsyncMock()):
            chunks = [c async for c in chat.chat_stream([{'role':'user','content':'帮我看一下吧'}],generate,
                      actor='connor',scope='chatroom',parent_id='room',query='帮我看一下吧',queue=queue,state=state)]
            text, attachments = await chat.finish_reply(strip_tool_commands(''.join(chunks)),actor='connor',state=state)
        pending = queue.get_nowait()
        self.assertEqual(pending['type'],'connor_status')
        self.assertEqual(pending['kitchen_lookup']['before'],opening)
        self.assertEqual(pending['kitchen_lookup']['offset'],len(opening.encode('utf-16-le'))//2)
        self.assertEqual(pending['kitchen_lookup']['status'],'pending')
        self.assertEqual(pending['kitchen_lookup']['actor_name'],'家里的厨师')
        self.assertEqual(attachments[-1],{**pending['kitchen_lookup'],'status':'done'})
        self.assertEqual(text.count(opening),1)
        self.assertNotIn('查看了冰箱',text)

    async def test_failed_lookup_does_not_claim_inventory_was_viewed(self):
        import kitchen_chat as chat
        async def generate(messages):
            yield '库存暂时没读到。' if any(m['content'].startswith('[小厨房本次查询]') for m in messages) else '[KITCHEN:查看]'
        with patch.object(self.k,'snapshot',AsyncMock(side_effect=RuntimeError('offline'))):
            visible,state = await self.collect(generate,'今天吃啥？')
            _,attachments = await chat.finish_reply(visible,actor='aion',state=state)
        self.assertEqual([a['type'] for a in attachments],['kitchen_lookup'])
        self.assertEqual(attachments[0]['status'],'failed')

    async def test_kitchen_button_uses_recent_chat_and_saves_personal_message(self):
        import kitchen_chat as chat
        import context_builder
        import ai_providers
        await self.k.upsert_item({'name':'鸡蛋','amount':3,'unit':'个'})
        seen=[]
        async def generate(messages, *args, **kwargs):
            seen.append([m['content'] for m in messages])
            if not any(m['content'].startswith('[小厨房本次查询]') for m in messages):
                yield '[KITCHEN:查看]'
                return
            yield '先看看，喜欢再定。' + self.menu_protocol().replace('"options":', '"message":"忙了一天，给你一顿省心的饭。","options":')
        recent=[{'role':'user','content':'今天忙得有点累，想早点休息。'}]
        with patch.object(context_builder, 'fetch_merged_timeline', AsyncMock(return_value=[])), \
             patch.object(context_builder, 'render_merged_timeline', return_value=recent), \
             patch.object(ai_providers, 'stream_ai', generate), patch.object(self.k,'notify',AsyncMock()):
            result = await chat.generate_plan(actor='aion')
        first = '\n'.join(seen[0])
        self.assertEqual(first.count(chat.ABILITY), 1)
        self.assertLess(first.index('【可用指令】'), first.index(chat.ABILITY))
        self.assertLess(first.index(chat.ABILITY), first.index('想早点休息'))
        self.assertEqual(result['menu']['message'],'忙了一天，给你一顿省心的饭。')
        self.assertEqual((await self.k.snapshot())['menus'],[])
        async def no_plan(*args, **kwargs):
            yield '这次没能整理好菜单。'
        with patch.object(context_builder,'fetch_merged_timeline',AsyncMock(return_value=[])), \
             patch.object(ai_providers,'stream_ai',no_plan), \
             patch.object(chat,'read_context',AsyncMock(side_effect=AssertionError('button lookup without model request'))):
            with self.assertRaisesRegex(ValueError,'这次没能整理好菜单'):
                await chat.generate_plan(actor='aion')

    def menu_protocol(self):
        dish = {'name':'青椒肥牛','minutes':15,'ingredients':[{'name':'肥牛','amount':.5,'unit':'盒'}],'steps':['少油炒熟。']}
        return '[KITCHEN_MENU]' + json.dumps({'options':[dish,{**dish,'name':'肥牛汤饭'}]},ensure_ascii=False) + '[/KITCHEN_MENU]'

    async def collect(self, generate, query, status=None, actor='aion'):
        import kitchen_chat as chat
        state = {}
        chunks = []
        async def status_callback(text):
            if status is not None:
                status.append(text)
        async for chunk in chat.stream_with_kitchen([{'role': 'user', 'content': query}], generate,
                actor=actor, scope='private', parent_id='room', query=query,
                status=status_callback, state=state):
            chunks.append(chunk)
        return ''.join(chunks), state

    async def test_meal_query_reads_stock_and_returns_card_without_protocol_leaking(self):
        import kitchen_chat as chat
        from ai_providers import CLI_STATUS_PREFIX
        await self.k.upsert_item({'name': '肥牛', 'amount': '1', 'unit': '盒'})
        seen, statuses = [], []
        async def generate(messages):
            seen.extend(messages)
            if not any(m['content'].startswith('[小厨房本次查询]') for m in messages):
                yield '[KITCHEN:查看]'
                return
            yield CLI_STATUS_PREFIX + '正在思考…'
            text = '宝宝，午餐给你安排好了。' + self.menu_protocol()
            for i in range(0, len(text), 7):
                yield text[i:i+7]
        with patch.object(chat, 'actor_name', return_value='小森'), patch.object(self.k, 'notify', AsyncMock()):
            visible, state = await self.collect(generate, '今天吃啥？', statuses)
            clean, attachments = await chat.finish_reply(visible, actor='aion', state=state)
        self.assertEqual(clean, '宝宝，午餐给你安排好了。')
        self.assertEqual([a['type'] for a in attachments],['kitchen_menu','kitchen_lookup'])
        saved = await self.k.get_menu(attachments[0]['menu_id'])
        self.assertEqual(saved['meals'][0]['name'], '青椒肥牛')
        self.assertTrue(any('肥牛' in m['content'] for m in seen))
        self.assertEqual(statuses, ['小森正在查看冰箱…'])

    async def test_disabled_chat_has_no_kitchen_injection_or_saved_menu(self):
        import kitchen_chat as chat
        await self.k.set_enabled(False)
        seen = []
        async def generate(messages):
            seen.extend(messages)
            yield '今天一起好好吃饭。' + self.menu_protocol()
        visible, state = await self.collect(generate, '今天吃啥？')
        clean, attachments = await chat.finish_reply(visible, actor='aion', state=state)
        self.assertEqual(seen, [{'role': 'user', 'content': '今天吃啥？'}])
        self.assertEqual(clean, '今天一起好好吃饭。')
        self.assertEqual(attachments, [])
        self.assertEqual((await self.k.snapshot())['menus'], [])

    async def test_proactive_lookup_can_continue_once_and_preserves_other_commands(self):
        import kitchen_chat as chat
        calls = []
        async def generate(messages):
            calls.append(messages)
            yield '[KITCHEN:查看]' if len(calls) == 1 else '安排好了。[MUSIC:轻音乐]' + self.menu_protocol()
        with patch.object(chat, 'actor_name', return_value='AI'), patch.object(self.k, 'notify', AsyncMock()):
            visible, state = await self.collect(generate, '帮我安排一下吧')
            clean, attachments = await chat.finish_reply(visible, actor='aion', state=state)
        self.assertEqual(len(calls), 2)
        self.assertIn('[MUSIC:轻音乐]', clean)
        self.assertNotIn('KITCHEN', visible)
        self.assertEqual([a['type'] for a in attachments],['kitchen_menu','kitchen_lookup'])

    async def test_switch_in_read_status_does_not_dispatch_stale_continuation(self):
        import kitchen_chat as chat
        calls=[]
        async def generate(messages):
            calls.append(messages)
            yield '[KITCHEN:查看]' if len(calls)==1 else self.menu_protocol()
        async def status(text):
            if '正在查看冰箱' in text:
                await self.k.set_enabled(False)
                await self.k.set_enabled(True)
        state={}
        with patch.object(chat,'actor_name',return_value='AI'):
            result=[c async for c in chat.stream_with_kitchen([{'role':'user','content':'帮我安排一下'}],generate,
                    actor='aion',scope='private',parent_id='room',query='帮我安排一下',status=status,state=state)]
        self.assertEqual(len(calls),1)
        self.assertNotIn('KITCHEN',''.join(result))

    async def test_creative_reply_has_no_kitchen_capability_or_lookup(self):
        import kitchen_chat as chat
        from capabilities import format_ability_block
        seen=[]
        async def generate(messages):
            seen.extend(messages)
            yield '故事继续。'
        async def status(_):
            raise AssertionError('创作场景不应读取冰箱')
        tools = format_ability_block(['另一个工具', chat.ABILITY])
        messages = [{'role':'user', 'content':tools},
                    {'role':'assistant', 'content':'好的，需要时我会使用这些指令。'},
                    {'role':'user','content':'今天吃什么','attachments':[{'type':'kitchen_excluded'}]}]
        state={}
        result=[c async for c in chat.stream_with_kitchen(messages,generate,actor='aion',scope='private',
                parent_id='room',query='今天吃什么',status=status,state=state)]
        self.assertEqual(seen[0]['content'], format_ability_block(['另一个工具']))
        self.assertEqual(seen[1:], messages[1:])
        self.assertEqual(messages[0]['content'], tools)
        self.assertEqual(''.join(result),'故事继续。')


if __name__ == '__main__':
    unittest.main()
