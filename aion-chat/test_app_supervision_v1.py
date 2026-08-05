import asyncio
import json
import pathlib
import subprocess
import sys
import unittest
from unittest.mock import patch


BASE_DIR = pathlib.Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from app_supervision import role_catalog
from routes import app_supervision as app_supervision_routes


class AppSupervisionV1BackendTest(unittest.TestCase):
    def test_role_catalog_uses_configured_names(self):
        with patch(
            "app_supervision.get_chatroom_names",
            return_value=("User X", "Main X", "Second X"),
        ):
            self.assertEqual(
                role_catalog(),
                [
                    {"id": "aion", "label": "Main X"},
                    {"id": "connor", "label": "Second X"},
                ],
            )

    def test_roles_route_returns_catalog(self):
        expected = [{"id": "aion", "label": "Configured"}]
        with patch.object(app_supervision_routes, "role_catalog", return_value=expected):
            self.assertEqual(
                {"roles": expected},
                asyncio.run(app_supervision_routes.get_roles()),
            )

    def test_main_registers_router_and_mobile_page(self):
        source = (BASE_DIR / "main.py").read_text(encoding="utf-8")
        self.assertIn("app_supervision_routes.router", source)
        self.assertIn('@app.get("/app-supervision")', source)
        self.assertIn('"app-supervision.html"', source)

    def test_v1_backend_has_no_ai_integration(self):
        source = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (
                BASE_DIR / "app_supervision.py",
                BASE_DIR / "routes" / "app_supervision.py",
            )
        )
        for forbidden in (
            "wake_ai",
            "build_capability_prompt_items",
            "simple_ai_call",
            "stream_ai",
            "parse_ai_command",
        ):
            self.assertNotIn(forbidden, source)


class AppSupervisionV1FrontendTest(unittest.TestCase):
    def page(self):
        return (BASE_DIR / "static" / "app-supervision.html").read_text(
            encoding="utf-8"
        )

    def run_ui_helper(self, expression):
        helper = BASE_DIR / "static" / "app-supervision-ui.js"
        script = (
            f"require({json.dumps(str(helper))});"
            f"const result=({expression});"
            "process.stdout.write(JSON.stringify(result));"
        )
        completed = subprocess.run(
            ["node", "-e", script],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        return json.loads(completed.stdout)

    def test_ui_helpers_filter_apps_by_label_and_package(self):
        apps = json.dumps(
            [
                {"label": "小红书", "packageName": "com.xingin.xhs"},
                {"label": "抖音", "packageName": "com.ss.android.ugc.aweme"},
                {"label": "Alpha", "packageName": "com.example.alpha"},
            ],
            ensure_ascii=False,
        )
        self.assertEqual(
            ["com.ss.android.ugc.aweme"],
            self.run_ui_helper(
                f"AppSupervisionUi.filterApps({apps}, '抖').map(x=>x.packageName)"
            ),
        )
        self.assertEqual(
            ["com.xingin.xhs"],
            self.run_ui_helper(
                f"AppSupervisionUi.filterApps({apps}, 'XINGIN').map(x=>x.packageName)"
            ),
        )

    def test_ui_helpers_sort_merge_and_preserve_unknown_aliases(self):
        apps = json.dumps(
            [
                {"label": "Zulu", "packageName": "com.example.z"},
                {"label": "Alpha", "packageName": "com.example.a"},
            ]
        )
        self.assertEqual(
            ["Alpha", "Zulu"],
            self.run_ui_helper(
                f"AppSupervisionUi.filterApps({apps}, '').map(x=>x.label)"
            ),
        )
        self.assertEqual(
            {
                "selected": ["com.example.a"],
                "aliases": ["clone.vendor.alias"],
            },
            self.run_ui_helper(
                f"AppSupervisionUi.splitKnownPackages(['com.example.a','clone.vendor.alias'], {apps})"
            ),
        )
        self.assertEqual(
            ["com.example.a", "clone.vendor.alias", "com.example.z"],
            self.run_ui_helper(
                "AppSupervisionUi.mergePackages(['com.example.a'], "
                "'clone.vendor.alias\\ncom.example.a\\ncom.example.z')"
            ),
        )

    def test_ui_helper_generates_uuid_without_random_uuid_support(self):
        generated = self.run_ui_helper(
            "AppSupervisionUi.createGroupId({getRandomValues(bytes){"
            "for(let i=0;i<bytes.length;i++)bytes[i]=i;return bytes;}})"
        )
        self.assertRegex(
            generated,
            r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
        )
        self.assertEqual(
            "native-id",
            self.run_ui_helper(
                "AppSupervisionUi.createGroupId({randomUUID(){return 'native-id';}})"
            ),
        )

    def test_emergency_hold_controller_handles_release_boundary_and_cancellation(self):
        result = self.run_ui_helper(
            "(()=>{"
            "function scenario(finalTruePhase,finish){"
            "const samples=[],active=[],renders=[];let tick=null;"
            "const controller=AppSupervisionUi.createEmergencyHoldController({"
            "begin(){return {phase:'HOLDING'};},"
            "sample(holding){samples.push(holding);return {phase:holding?finalTruePhase:'CANCELLED'};},"
            "schedule(callback){tick=callback;return 7;},cancelSchedule(){tick=null;},"
            "render(gate){renders.push(gate.phase);},setActive(value){active.push(value);}});"
            "controller.start(11);finish(controller);"
            "return {samples,active,renders,isActive:controller.isActive(),hasTick:tick!==null};}"
            "return {"
            "early:scenario('HOLDING',c=>c.release(11)),"
            "boundary:scenario('REASON_REQUIRED',c=>c.release(11)),"
            "cancelled:scenario('HOLDING',c=>c.cancel(11)),"
            "wrongPointer:scenario('HOLDING',c=>c.release(12))};"
            "})()"
        )
        self.assertEqual([True, False], result["early"]["samples"])
        self.assertFalse(result["early"]["isActive"])
        self.assertEqual([True], result["boundary"]["samples"])
        self.assertIn("REASON_REQUIRED", result["boundary"]["renders"])
        self.assertFalse(result["boundary"]["isActive"])
        self.assertEqual([False], result["cancelled"]["samples"])
        self.assertFalse(result["cancelled"]["isActive"])
        self.assertEqual([], result["wrongPointer"]["samples"])
        self.assertTrue(result["wrongPointer"]["isActive"])
        self.assertTrue(result["wrongPointer"]["hasTick"])

    def test_mobile_page_has_bridge_detection_and_required_sections(self):
        page = self.page()
        self.assertIn("viewport-fit=cover", page)
        self.assertIn("window.AionAppSupervision", page)
        self.assertIn("function nativeCall", page)
        self.assertIn("aion-client-update-ready", page)
        self.assertIn("JSON.parse", page)
        self.assertIn("@media (max-width:", page)
        for element_id in (
            "activeLocks",
            "managedGroups",
            "groupEditor",
            "debugPanel",
            "emergencyPanel",
        ):
            self.assertIn(f'id="{element_id}"', page)

    def test_page_has_home_back_link_and_folded_debug_panel_last(self):
        page = self.page()
        self.assertRegex(
            page,
            r'<a[^>]+class="back-link"[^>]+href="/"[^>]*>',
        )
        self.assertRegex(
            page,
            r'<details[^>]+id="debugPanel"(?![^>]*\bopen\b)[^>]*>',
        )
        self.assertGreater(
            page.rfind('id="debugPanel"'),
            page.rfind('id="diagnostics"'),
        )
        for element_id in (
            "debugGroup",
            "debugMinutes",
            "debugRole",
            "debugMessage",
            "setLock",
            "setTempUnlock",
            "removeLock",
        ):
            self.assertIn(f'id="{element_id}"', page)

    def test_page_uses_role_catalog_and_all_v1_bridge_controls(self):
        page = self.page()
        self.assertIn("/api/app-supervision/roles", page)
        self.assertIn("nativeCall('setRoleCatalog'", page)
        for method in (
            "listLaunchableApps",
            "upsertGroup",
            "setFeatureEnabled",
            "debugSetLock",
            "debugSetTemporaryUnlock",
            "debugRemoveLock",
            "emergencyAction",
        ):
            self.assertIn(method, page)

    def test_managed_groups_do_not_expose_manual_round_clear(self):
        page = self.page()
        self.assertNotIn("data-clear", page)
        self.assertNotIn("nativeCall('clearRound'", page)
        self.assertNotIn("清空本轮", page)

    def test_page_shows_device_status_and_only_emergency_device_unlock(self):
        page = self.page()
        self.assertIn('id="deviceLockStatus"', page)
        self.assertIn("整机专注", page)
        self.assertIn("__device__", page)
        self.assertIn("deviceLock", page)
        self.assertNotIn('id="setDeviceLock"', page)
        self.assertNotIn("debugSetDeviceLock", page)

    def test_emergency_hold_uses_real_pointer_completion_events(self):
        page = self.page()
        self.assertIn("AppSupervisionUi.createEmergencyHoldController", page)
        self.assertIn("window.addEventListener('pointerup'", page)
        self.assertIn("window.addEventListener('pointercancel'", page)
        self.assertNotIn("setPointerCapture", page)
        self.assertNotIn("lostpointercapture", page)
        self.assertIn("if (!emergencyHold.isActive()) refresh();", page)

    def test_group_editor_uses_searchable_automatic_package_selection(self):
        page = self.page()
        self.assertIn('src="/static/app-supervision-ui.js?v=20260719-4"', page)
        for element_id in (
            "appSearch",
            "appResults",
            "selectedApps",
            "advancedPackages",
        ):
            self.assertIn(f'id="{element_id}"', page)
        self.assertNotIn('id="appPicker"', page)
        self.assertNotIn('id="packageNames" required', page)
        self.assertIn("data-select-package", page)
        self.assertIn("data-remove-package", page)
        self.assertIn("AppSupervisionUi.filterApps", page)
        self.assertIn("AppSupervisionUi.splitKnownPackages", page)
        self.assertIn("AppSupervisionUi.mergePackages", page)
        self.assertIn("AppSupervisionUi.createGroupId", page)
        self.assertNotIn("crypto.randomUUID()", page)

    def test_group_cards_expose_confirmed_lock_safe_deletion(self):
        page = self.page()
        self.assertIn("data-delete", page)
        self.assertIn("async function deleteGroup", page)
        self.assertIn("group.lock && group.lock.deadlineWallMs > Date.now()", page)
        self.assertIn("window.confirm", page)
        self.assertIn("nativeCall('removeGroup', group.groupId)", page)
        self.assertIn("showToast('应用组已删除')", page)
        self.assertIn("catch (error) { showToast(error.message); }", page)

    def test_page_has_no_hardcoded_personal_names(self):
        page = self.page().replace("AionAppSupervision", "BRIDGE_PROTOCOL")
        for name in ("Aion", "Ithil", "Connor"):
            self.assertNotIn(name, page)

    def test_home_registers_supplied_supervision_icon_exactly_once(self):
        home = (BASE_DIR / "static" / "home.html").read_text(encoding="utf-8")
        self.assertEqual(1, home.count("id: 'app-supervision'"))
        self.assertIn("icon: '/public/funIcon_0027_监管.png'", home)
        self.assertIn("url: '/app-supervision'", home)
        self.assertTrue((BASE_DIR.parent / "public" / "funIcon_0027_监管.png").is_file())


if __name__ == "__main__":
    unittest.main()
