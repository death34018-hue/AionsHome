import tempfile
import json
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from taobao_shopping import TaobaoStore, normalize_product, search_and_record, roam


def product(item_id="805862215859", **overrides):
    return {
        "itemId": item_id, "title": "机械手桌面摆件", "price": "19.7",
        "productUrl": f"https://item.taobao.com/item.htm?id={item_id}&spm=test",
        "image": "https://img.alicdn.com/test.jpg", "shopName": "测试商店",
        **overrides,
    }


class ProductTests(unittest.TestCase):
    def test_real_item_link_is_canonical_and_image_is_safe(self):
        actual = normalize_product(product(image="javascript:alert(1)"))
        self.assertEqual(actual["url"], "https://item.taobao.com/item.htm?id=805862215859")
        self.assertEqual(actual["image"], "")
        self.assertEqual(actual["price"], "19.7")

    def test_untrusted_or_mismatched_links_are_rejected(self):
        for url in ["https://taobao.com.evil.test/?id=805862215859",
                    "https://item.taobao.com/item.htm?id=123", "javascript:alert(1)"]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                normalize_product(product(productUrl=url))


class StoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = TaobaoStore(Path(self.tmp.name) / "taobao.sqlite3")
        await self.store.init()
        # Keep real chat databases and sockets outside isolated shopping tests.
        self.notices = AsyncMock(return_value={"id": "notice"})
        delivery = patch("taobao_notifications.notify_shopping_trip", new=self.notices)
        delivery.start()
        self.addCleanup(delivery.stop)

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def test_persistent_actor_wishlist_and_local_deletion(self):
        search = await self.store.record_search("机械手", [product()])
        candidate_id = search["products"][0]["id"]
        first = await self.store.save_item("aion", candidate_id, reflection="放在书桌上", purpose="桌面装饰", recipient="自己")
        again = await self.store.save_item("aion", candidate_id, reflection="仍然喜欢")
        other = await self.store.save_item("connor", candidate_id, reflection="想送给朋友")
        self.assertEqual(first["id"], again["id"])
        self.assertTrue(first["newly_saved"])
        self.assertFalse(again["newly_saved"])
        self.assertNotEqual(first["id"], other["id"])
        reopened = TaobaoStore(self.store.path)
        self.assertEqual(len(await reopened.list_items()), 2)
        await reopened.delete_item(first["id"])
        remaining = await reopened.list_items()
        self.assertEqual([row["actor"] for row in remaining], ["connor"])
        self.assertEqual(remaining[0]["reflection"], "想送给朋友")

    async def test_invented_candidate_cannot_be_saved(self):
        with self.assertRaises(KeyError):
            await self.store.save_item("aion", "invented", reflection="想要")
        self.assertEqual(await self.store.list_items(), [])

    async def test_deleting_trip_keeps_saved_wishlist_item(self):
        search = await self.store.record_search("机械手", [product()])
        saved = await self.store.save_item("aion", search["products"][0]["id"], reflection="想留下")
        trip_id = await self.store.start_trip("aion")
        await self.store.update_trip(trip_id, status="finished", selected=[saved])

        delete_trip = getattr(self.store, "delete_trip", None)
        self.assertIsNotNone(delete_trip, "shopping trips need an independent delete operation")
        await delete_trip(trip_id)

        with self.assertRaises(KeyError):
            await self.store.get_trip(trip_id)
        self.assertEqual([item["id"] for item in await self.store.list_items()], [saved["id"]])
        with self.assertRaises(KeyError):
            await delete_trip("missing")

    async def test_mcp_failure_does_not_create_fake_products(self):
        with patch("taobao_shopping.mcp_search", new=AsyncMock(side_effect=RuntimeError("未连接"))):
            with self.assertRaises(RuntimeError):
                await search_and_record(self.store, "机械手")
        self.assertEqual(await self.store.list_items(), [])

    async def test_search_records_only_valid_mcp_results(self):
        result = {"products": [product(), product("123", productUrl="https://evil.test/")]}
        with patch("taobao_shopping.mcp_search", new=AsyncMock(return_value=result)) as search:
            actual = await search_and_record(self.store, "机械手")
        search.assert_awaited_once()
        self.assertEqual(len(actual["products"]), 1)
        self.assertEqual(actual["products"][0]["item_id"], "805862215859")
        self.assertEqual(actual["skipped"], 1)

    async def test_empty_native_result_recovers_on_one_retry(self):
        with patch("taobao_shopping.mcp_search", new=AsyncMock(side_effect=[
            {"products": []}, {"products": [product()]},
        ])) as search, patch("taobao_shopping.asyncio.sleep", new=AsyncMock()):
            actual = await search_and_record(self.store, "机械手")
        self.assertEqual(len(actual["products"]), 1)
        self.assertEqual(search.await_count, 2)
        self.assertEqual(search.await_args_list[0], search.await_args_list[1])

    async def test_empty_results_stop_after_retry_and_keep_clear_trip_summary(self):
        with patch("autonomy._actor_context", new=AsyncMock(return_value=[])), \
             patch("autonomy._call_actor", new=AsyncMock(return_value='{"keyword":"机械手"}')) as model, \
             patch("taobao_shopping.mcp_search", new=AsyncMock(return_value={"products": []})) as search, \
             patch("taobao_shopping.asyncio.sleep", new=AsyncMock()):
            result = await roam("aion", self.store)
        self.assertEqual(search.await_count, 2)
        self.assertEqual(model.await_count, 1)
        self.assertIn("空商品列表", result["message"])
        self.assertEqual((await self.store.get_trip(result["trip_id"]))["summary"], result["message"])

    async def test_invalid_links_are_not_retried_or_reported_as_empty_response(self):
        with patch("autonomy._actor_context", new=AsyncMock(return_value=[])), \
             patch("autonomy._call_actor", new=AsyncMock(return_value='{"keyword":"机械手"}')), \
             patch("taobao_shopping.mcp_search", new=AsyncMock(return_value={
                 "products": [product(productUrl="https://evil.test/")],
             })) as search:
            result = await roam("aion", self.store)
        search.assert_awaited_once()
        self.assertIn("1 件", result["message"])
        self.assertIn("链接校验", result["message"])
        self.assertEqual(result["items"], [])

    async def test_invalid_model_direction_is_error_not_fake_rest(self):
        with patch("autonomy._actor_context", new=AsyncMock(return_value=[])), \
             patch("autonomy._call_actor", new=AsyncMock(return_value="模型调用失败")):
            with self.assertRaises(ValueError):
                await roam("aion", self.store)

    async def test_autonomy_summary_does_not_claim_search_when_actor_declines(self):
        import autonomy
        outcome = {"keyword": "", "items": [], "message": "这次没有搜索或新增收藏。"}
        with patch("autonomy._select_action", new=AsyncMock(return_value={"action": "taobao_roam", "reason": "想逛逛"})), \
             patch("autonomy._actor_label", return_value="测试角色"), \
             patch("taobao_shopping.autonomous_roam", new=AsyncMock(return_value=outcome)), \
             patch("autonomy.append_idle_event", new=AsyncMock(return_value={"id": "event"})) as append:
            await autonomy._run_actor_once("aion")
        summary = append.await_args_list[-1].args
        self.assertIn("没有搜索", summary[2])
        self.assertEqual(summary[3], "这次没有搜索或新增收藏。")

    async def test_ai_can_only_save_candidates_from_this_search(self):
        async def choose(actor, messages):
            if "独立逛淘宝" in messages[-1]["content"]:
                return '{"keyword":"机械手"}'
            async with self.store.connect() as db:
                candidate = await (await db.execute("SELECT id FROM candidates")).fetchone()
            return json.dumps({"picks": [{"candidate_id": candidate[0], "reflection": "喜欢可动的关节", "purpose": "桌面摆件"}]})
        with patch("autonomy._actor_context", new=AsyncMock(return_value=[])), \
             patch("autonomy._call_actor", new=AsyncMock(side_effect=choose)), \
             patch("taobao_shopping.mcp_search", new=AsyncMock(return_value={"products": [product()]})):
            result = await roam("aion", self.store)
        self.assertEqual(result["items"][0]["url"], "https://item.taobao.com/item.htm?id=805862215859")
        self.assertEqual(result["items"][0]["reflection"], "喜欢可动的关节")
        with patch("autonomy._actor_context", new=AsyncMock(return_value=[])), \
             patch("autonomy._call_actor", new=AsyncMock(side_effect=['{"keyword":"机械手"}', '{"picks":[{"candidate_id":"invented"}]}'])), \
             patch("taobao_shopping.mcp_search", new=AsyncMock(return_value={"products": [product()]})):
            with self.assertRaises(ValueError):
                await roam("connor", self.store)
        self.assertEqual(await self.store.list_items("connor"), [])

    async def test_trip_keeps_notes_with_exactly_two_model_calls(self):
        async def answer(actor, messages):
            if "独立逛淘宝" in messages[-1]["content"]:
                return '{"keyword":"机械手","motive":"想给书桌添点机械感"}'
            async with self.store.connect() as db:
                rows = await (await db.execute("SELECT id FROM candidates ORDER BY rowid")).fetchall()
            return json.dumps({"picks": [{"candidate_id": rows[0][0], "reflection": "喜欢关节造型"}],
                "notes": [{"candidate_id": rows[1][0], "verdict": "maybe", "comment": "价格让我冷静了，先想想"},
                          {"candidate_id": "invented", "comment": "不能保存这个编造点评"}],
                "summary": "有一个想放在桌边，另一个下次再说。"})
        with patch("autonomy._actor_context", new=AsyncMock(return_value=[])), \
             patch("autonomy._call_actor", new=AsyncMock(side_effect=answer)) as model, \
             patch("taobao_shopping.mcp_search", new=AsyncMock(return_value={"products": [product(), product("805862215860")]})) as mcp:
            await roam("connor", self.store)
        self.assertEqual(model.await_count, 2)
        self.assertEqual(mcp.await_count, 1)
        trips = await TaobaoStore(self.store.path).list_trips()
        self.assertEqual(trips[0]["motive"], "想给书桌添点机械感")
        self.assertEqual(trips[0]["candidate_count"], 2)
        self.assertEqual(len(trips[0]["notes"]), 1)
        self.assertEqual(trips[0]["notes"][0]["comment"], "价格让我冷静了，先想想")
        self.assertEqual(trips[0]["notes"][0]["url"], "https://item.taobao.com/item.htm?id=805862215860")
        self.assertEqual(trips[0]["summary"], "有一个想放在桌边，另一个下次再说。")
        await self.store.start_trip("aion")
        self.assertNotEqual((await self.store.list_trips(limit=1))[0]["id"], trips[0]["id"])
        self.assertEqual((await self.store.get_trip(trips[0]["id"]))["summary"], trips[0]["summary"])
        with self.assertRaises(KeyError):
            await self.store.get_trip("missing")

    async def test_empty_handed_trip_and_failure_are_kept_without_extra_calls(self):
        with patch("autonomy._actor_context", new=AsyncMock(return_value=[])), \
             patch("autonomy._call_actor", new=AsyncMock(return_value='{"keyword":"","motive":"今天没有想找的"}')) as model:
            await roam("aion", self.store)
        self.assertEqual(model.await_count, 1)
        trip = (await self.store.list_trips())[0]
        self.assertEqual(trip["status"], "finished")
        self.assertEqual(trip["selected"], [])
        self.assertEqual(trip["candidate_count"], 0)
        with patch("autonomy._actor_context", new=AsyncMock(return_value=[])), \
             patch("autonomy._call_actor", new=AsyncMock(side_effect=RuntimeError("离线"))):
            with self.assertRaises(RuntimeError):
                await roam("connor", self.store)
        self.assertEqual((await self.store.list_trips())[0]["status"], "failed")
        self.notices.assert_not_awaited()

    async def test_notice_counts_only_new_items_and_delivery_failure_keeps_shopping(self):
        async def answer(actor, messages):
            if "独立逛淘宝" in messages[-1]["content"]:
                return '{"keyword":"机械手"}'
            async with self.store.connect() as db:
                row = await (await db.execute("SELECT id FROM candidates ORDER BY rowid DESC LIMIT 1")).fetchone()
            return json.dumps({"picks": [{"candidate_id": row[0]}]})
        with patch("autonomy._actor_context", new=AsyncMock(return_value=[])), \
             patch("autonomy._call_actor", new=AsyncMock(side_effect=answer)) as model, \
             patch("taobao_shopping.mcp_search", new=AsyncMock(return_value={"products": [product()]})):
            first = await roam("aion", self.store)
            self.notices.assert_awaited_once()
            actor, trip_id, items = self.notices.await_args.args
            self.assertEqual((actor, trip_id, len(items)), ("aion", first["trip_id"], 1))
            await roam("aion", self.store)
            self.assertEqual(self.notices.await_count, 1)
            self.notices.side_effect = RuntimeError("通知暂时离线")
            result = await roam("connor", self.store)
        self.assertEqual(model.await_count, 6)
        self.assertIn("notification_error", result)
        self.assertEqual((await self.store.list_trips())[0]["status"], "finished")
        self.assertEqual(len(await self.store.list_items()), 2)


if __name__ == "__main__":
    unittest.main()
