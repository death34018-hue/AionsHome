"""Model tool defaults for companion chat; repair sessions never load this catalog."""

import json
import os
import subprocess
from functools import lru_cache
from pathlib import Path


def build_chat_model_catalog(source: dict) -> dict:
    models = source.get("models")
    if not isinstance(models, list) or not models:
        raise ValueError("Codex 陪伴聊天模型目录为空")
    return {"models": [
        {
            **model,
            # Recent CLI versions let model defaults force code mode and agents
            # even when their feature flags are off. Keep only view_image.
            "tool_mode": "disabled",
            "multi_agent_version": None,
            "experimental_supported_tools": [],
            "apply_patch_tool_type": None,
            "supports_search_tool": False,
        }
        for model in models
    ]}


@lru_cache(maxsize=1)
def _chat_catalog_text(node: str, script: str, cache: Path | None, cache_mtime_ns: int) -> str:
    if cache is not None:
        source = json.loads(cache.read_text(encoding="utf-8"))
    else:
        # First launch: use the CLI's bundled catalog without a model/API call.
        result = subprocess.run(
            [node, script, "debug", "models", "--bundled"],
            capture_output=True, check=True, timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        source = json.loads(result.stdout.decode("utf-8"))
    return json.dumps(build_chat_model_catalog(source), ensure_ascii=False)


def prepare_chat_model_catalog(
    chat_home: Path, node: str, script: str, *, source_cache: Path | None = None,
) -> Path:
    caches = [path for path in (chat_home / "models_cache.json", source_cache)
              if path is not None and path.is_file()]
    cache = max(caches, key=lambda path: path.stat().st_mtime_ns) if caches else None
    # A desktop model refresh must also invalidate the server's in-memory catalog.
    text = _chat_catalog_text(node, script, cache, cache.stat().st_mtime_ns if cache else 0)
    chat_home.mkdir(parents=True, exist_ok=True)
    target = chat_home / "companion-models.json"
    if not target.is_file() or target.read_text(encoding="utf-8") != text:
        temporary = chat_home / f"companion-models-{os.getpid()}.tmp"
        temporary.write_text(text, encoding="utf-8")
        temporary.replace(target)
    return target
