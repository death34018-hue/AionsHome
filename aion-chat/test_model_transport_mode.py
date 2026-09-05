import unittest
from unittest.mock import patch

import config
from config import (
    normalize_model_transport_modes,
    resolve_model_transport_mode,
)
from routes import settings as settings_routes


class ModelTransportModeTest(unittest.TestCase):
    def test_codex_astra_is_registered_as_a_safe_live_codex_pipeline(self):
        self.assertEqual(
            config.BUILTIN_MODELS["Codex-Astra"],
            {
                "provider": "codex_cli",
                "model": "gpt-6-astra",
                "vision": True,
                "transport_mode": "safe_live",
            },
        )
        self.assertIn("Codex-Sol", config.BUILTIN_MODELS)
        with patch("config.SETTINGS", {}):
            self.assertEqual(resolve_model_transport_mode("Codex-Astra"), "safe_live")

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

    def test_missing_model_and_unknown_override_use_legacy(self):
        with patch("config.SETTINGS", {"model_transport_modes": {"missing": "unsafe"}}):
            self.assertEqual(resolve_model_transport_mode("missing"), "legacy")

    def test_codex_sol_has_explicit_safe_live_default(self):
        with patch("config.SETTINGS", {}):
            self.assertEqual(resolve_model_transport_mode("Codex-Sol"), "safe_live")

    def test_saved_legacy_override_switches_codex_back_without_restart(self):
        with patch("config.SETTINGS", {"model_transport_modes": {"Codex-Sol": "legacy"}}):
            self.assertEqual(resolve_model_transport_mode("Codex-Sol"), "legacy")

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

    def test_saved_legacy_overrides_keep_aion_models_disabled_after_reload(self):
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
            self.assertEqual(resolve_model_transport_mode("3.8Vertex"), "legacy")
            self.assertEqual(resolve_model_transport_mode("官Gem3.8flash"), "legacy")

    def test_capability_mismatch_fails_closed_to_legacy(self):
        with (
            patch("config.SETTINGS", {"model_transport_modes": {"relay": "safe_live"}}),
            patch.dict("config.MODELS", {"relay": {"provider": "custom_openai"}}, clear=True),
        ):
            self.assertEqual(resolve_model_transport_mode("relay"), "legacy")


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
                ("Codex-Sol", {"provider": "codex_cli"}),
                ("relay", {"provider": "custom_openai"}),
            ]),
        ), patch.object(
            settings_routes,
            "resolve_model_transport_mode",
            side_effect=lambda key: "safe_live" if key == "Codex-Sol" else "legacy",
        ):
            rows = await settings_routes.list_models()

        self.assertEqual(
            {row["key"]: row["transport_mode"] for row in rows},
            {"Codex-Sol": "safe_live", "relay": "legacy"},
        )
        self.assertEqual(
            {row["key"]: row["supports_safe_live"] for row in rows},
            {"Codex-Sol": True, "relay": False},
        )


if __name__ == "__main__":
    unittest.main()
