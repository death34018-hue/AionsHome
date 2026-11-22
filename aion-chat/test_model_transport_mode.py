import unittest
from unittest.mock import AsyncMock, patch

import config
from config import (
    normalize_model_transport_modes,
    resolve_model_transport_mode,
)
from routes import settings as settings_routes


class ModelTransportModeTest(unittest.TestCase):
    def test_codex_6_1_sol_is_visible_with_safe_live_transport(self):
        self.assertEqual(
            config.BUILTIN_MODELS["Codex-6.1-Sol"],
            {
                "provider": "codex_cli",
                "model": "gpt-6.1-sol",
                "vision": True,
                "transport_mode": "safe_live",
            },
        )
        self.assertIn("Codex-6.1-Sol", dict(config.iter_visible_models()))
        with patch("config.SETTINGS", {}):
            self.assertEqual(resolve_model_transport_mode("Codex-6.1-Sol"), "safe_live")

    def test_renamed_codex_sol_is_registered_as_a_safe_live_codex_pipeline(self):
        self.assertEqual(
            config.BUILTIN_MODELS["Codex-5.6-Sol"],
            {
                "provider": "codex_cli",
                "model": "gpt-5.6-sol",
                "vision": True,
                "transport_mode": "safe_live",
            },
        )
        self.assertIn("Codex-5.6-Sol", dict(config.iter_visible_models()))
        with patch("config.SETTINGS", {}):
            self.assertEqual(resolve_model_transport_mode("Codex-5.6-Sol"), "safe_live")

    def test_normalization_keeps_only_named_supported_modes(self):
        self.assertEqual(
            normalize_model_transport_modes({
                "Codex-Sol": "safe_live",
                "relay": "legacy",
                "bad": "unsafe",
                "": "safe_live",
                "list": ["safe_live"],
            }),
            {"Codex-Sol": "safe_live", "relay": "legacy"},
        )

    def test_missing_model_cannot_bypass_live_guard(self):
        with patch("config.SETTINGS", {"model_transport_modes": {"missing": "unsafe"}}):
            self.assertEqual(resolve_model_transport_mode("missing"), "safe_live")

    def test_codex_sol_has_explicit_safe_live_default(self):
        with patch("config.SETTINGS", {}):
            self.assertEqual(resolve_model_transport_mode("Codex-5.6-Sol"), "safe_live")

    def test_saved_legacy_override_no_longer_disables_live_stream(self):
        with patch("config.SETTINGS", {"model_transport_modes": {"Codex-5.6-Sol": "legacy"}}):
            self.assertEqual(resolve_model_transport_mode("Codex-5.6-Sol"), "safe_live")

    def test_stable_aion_models_have_safe_live_defaults(self):
        with (
            patch("config.SETTINGS", {}),
            patch.dict("config.MODELS", {
                "3.8Vertex": {"provider": "custom_openai"},
                "官Gem3.8flash": {"provider": "gemini"},
            }),
        ):
            self.assertEqual(resolve_model_transport_mode("3.8Vertex"), "safe_live")
            self.assertEqual(resolve_model_transport_mode("官Gem3.8flash"), "safe_live")

    def test_saved_legacy_overrides_no_longer_disable_aion_live_stream(self):
        persisted = {
            "model_transport_modes": {
                "3.8Vertex": "legacy",
                "官Gem3.8flash": "legacy",
            }
        }
        with (
            patch("config.SETTINGS", persisted),
            patch.dict("config.MODELS", {
                "3.8Vertex": {"provider": "custom_openai"},
                "官Gem3.8flash": {"provider": "gemini"},
            }),
        ):
            self.assertEqual(resolve_model_transport_mode("3.8Vertex"), "safe_live")
            self.assertEqual(resolve_model_transport_mode("官Gem3.8flash"), "safe_live")

    def test_new_relay_model_uses_live_stream_without_allowlist(self):
        with (
            patch("config.SETTINGS", {"model_transport_modes": {"relay": "safe_live"}}),
            patch.dict("config.MODELS", {"relay": {"provider": "custom_openai"}}, clear=True),
        ):
            self.assertEqual(resolve_model_transport_mode("relay"), "safe_live")


class ModelTransportSettingsApiTest(unittest.IsolatedAsyncioTestCase):
    async def test_settings_update_persists_only_supported_modes(self):
        stored = {}
        body = settings_routes.SettingsUpdate(
            model_transport_modes={
                "Codex-Sol": "legacy",
                "relay": "safe_live",
                "bad": "unsafe",
            }
        )

        with (
            patch.object(settings_routes, "SETTINGS", stored),
            patch.object(settings_routes, "save_settings"),
        ):
            await settings_routes.update_settings(body)

        self.assertEqual(
            stored["model_transport_modes"],
            {"Codex-Sol": "legacy", "relay": "safe_live"},
        )

    async def test_model_list_exposes_resolved_transport_mode(self):
        with patch.object(
            settings_routes,
            "iter_visible_models",
            return_value=iter([
                ("Codex-5.6-Sol", {"provider": "codex_cli"}),
                ("relay", {"provider": "custom_openai"}),
            ]),
        ), patch.dict(config.MODELS, {"relay": {"provider": "custom_openai"}}        ), patch.object(settings_routes.antigravity_cli, "catalog_due", return_value=False):
            rows = await settings_routes.list_models()

        self.assertEqual(
            {row["key"]: row["transport_mode"] for row in rows},
            {"Codex-5.6-Sol": "safe_live", "relay": "safe_live"},
        )
        self.assertEqual(
            {row["key"]: row["supports_safe_live"] for row in rows},
            {"Codex-5.6-Sol": True, "relay": True},
        )


if __name__ == "__main__":
    unittest.main()
