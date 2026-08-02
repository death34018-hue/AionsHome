import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import ai_providers
import config
from routes import date_theater as date_theater_routes
from routes import settings


async def fake_siliconflow(*args, **kwargs):
    yield "safe-model"


async def fake_gemini_cli(*args, **kwargs):
    yield "deprecated-cli"


class DeprecatedCliModelTests(unittest.IsolatedAsyncioTestCase):
    async def test_model_list_hides_gemini_and_antigravity_cli_routes(self):
        with patch.dict(
            config.MODELS,
            {
                "AGY-3.1pro": {
                    "provider": "antigravity_cli",
                    "model": "gemini-3.1-pro-preview",
                    "vision": True,
                }
            },
            clear=False,
        ):
            rows = await settings.list_models()

        providers = {row["provider"] for row in rows}
        keys = {row["key"] for row in rows}
        self.assertNotIn("gemini_cli", providers)
        self.assertNotIn("antigravity_cli", providers)
        self.assertNotIn("CLI-3.1pro", keys)
        self.assertNotIn("AGY-3.1pro", keys)

    async def test_date_theater_model_rows_hide_deprecated_cli_routes(self):
        with patch.dict(
            date_theater_routes.MODELS,
            {
                "AGY-3.1pro": {
                    "provider": "antigravity_cli",
                    "model": "gemini-3.1-pro-preview",
                    "vision": True,
                }
            },
            clear=False,
        ):
            rows = date_theater_routes._model_rows()

        providers = {row["provider"] for row in rows}
        keys = {row["key"] for row in rows}
        self.assertNotIn("gemini_cli", providers)
        self.assertNotIn("antigravity_cli", providers)
        self.assertNotIn("CLI-3.1pro", keys)
        self.assertNotIn("AGY-3.1pro", keys)

    async def test_date_theater_resolves_deprecated_locked_model_to_visible_model(self):
        with patch.dict(
            date_theater_routes.MODELS,
            {
                "Visible": {"provider": "siliconflow", "model": "safe", "vision": False},
                "CLI-3.1pro": {"provider": "gemini_cli", "model": "old", "vision": True},
            },
            clear=True,
        ), patch("routes.date_theater.load_chatroom_config", return_value={"aion_model": ""}):
            resolved = date_theater_routes._resolve_model("", {"model_locked": True, "model": "CLI-3.1pro"})

        self.assertEqual(resolved, "Visible")

    async def test_stream_ai_falls_back_instead_of_calling_gemini_cli(self):
        with (
            patch("ai_providers.call_siliconflow", new=fake_siliconflow),
            patch("ai_providers.call_gemini_cli", new=fake_gemini_cli),
        ):
            chunks = [
                chunk
                async for chunk in ai_providers.stream_ai(
                    [{"role": "user", "content": "hello"}],
                    "CLI-3.1pro",
                )
            ]

        self.assertEqual(chunks, ["safe-model"])


class ModelResolutionTests(unittest.TestCase):
    def test_deprecated_cli_model_resolves_to_visible_model(self):
        resolved = config.resolve_model_key("CLI-3.1pro")
        self.assertNotEqual(resolved, "CLI-3.1pro")
        self.assertFalse(config.is_model_deprecated(resolved))


if __name__ == "__main__":
    unittest.main()
