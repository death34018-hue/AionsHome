"""Dynamic Android widget artwork, state, prompt, and reply commands."""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from pathlib import Path
from typing import Callable

from config import DATA_DIR, PUBLIC_DIR


ACTOR_IDS = ("aion", "connor")
COMMAND_PATTERN = re.compile(r"[【\[](小组件|横幅)[:：]([^】\]]*)[】\]]")


def _configured_actor_names() -> dict[str, str]:
    from chatroom import get_chatroom_names

    _, main_name, second_name = get_chatroom_names()
    return {"aion": main_name, "connor": second_name}


class WidgetAssetCatalog:
    def __init__(self, widget_root: Path, name_provider: Callable[[], dict[str, str]]):
        self.root = Path(widget_root)
        self.name_provider = name_provider

    def snapshot(self) -> dict[str, dict]:
        names = self.name_provider()
        result = {
            actor_id: {"name": str(names.get(actor_id) or "AI"), "states": [], "assets": {}}
            for actor_id in ACTOR_IDS
        }
        directories = [self.root / "状态", self.root]
        for directory in directories:
            if not directory.is_dir():
                continue
            for path in directory.iterdir():
                if not path.is_file() or path.suffix.lower() != ".png":
                    continue
                stem = path.stem
                for actor_id in ACTOR_IDS:
                    prefix = result[actor_id]["name"] + "-"
                    if not stem.startswith(prefix):
                        continue
                    state = stem[len(prefix):].strip()
                    if not state or state in result[actor_id]["assets"]:
                        break
                    stat = path.stat()
                    result[actor_id]["assets"][state] = {
                        "path": str(path.resolve()),
                        "version": f"{stat.st_mtime_ns:x}-{stat.st_size:x}",
                    }
                    break
        for actor in result.values():
            actor["states"] = sorted(actor["assets"])
        return result

    def asset(self, actor_id: str, state: str) -> dict | None:
        return self.snapshot().get(actor_id, {}).get("assets", {}).get(state)

    def banner_asset(self) -> dict | None:
        path = self.root / "横幅.png"
        if not path.is_file():
            return None
        stat = path.stat()
        return {
            "path": str(path.resolve()),
            "version": f"{stat.st_mtime_ns:x}-{stat.st_size:x}",
        }


def build_widget_control_prompt(
    actor_id: str, catalog: WidgetAssetCatalog | None = None
) -> str:
    selected = (catalog or widget_asset_catalog).snapshot().get(actor_id, {})
    states = selected.get("states") or []
    if not states:
        return ""
    return "\n".join([
        "[手机桌面小组件]",
        f"可用状态：{'、'.join(states)}。",
        "需要改变自己状态时，在回复末尾输出【小组件:状态】；无需每次输出，状态只能从上面选择。",
        "需要留下醒目短句时，输出【横幅:内容】；内容保持一句话、最多两行。",
    ])


def extract_widget_command(
    text: str, actor_id: str, catalog: WidgetAssetCatalog | None = None
) -> tuple[str, dict | None]:
    source = str(text or "")
    selected = (catalog or widget_asset_catalog).snapshot().get(actor_id, {})
    available = set(selected.get("states") or [])
    last_state = None
    last_banner = None
    for kind, raw_value in COMMAND_PATTERN.findall(source):
        value = raw_value.strip()
        if kind == "横幅" and value:
            last_banner = value
        elif kind == "小组件" and value in available:
            last_state = value
    cleaned = COMMAND_PATTERN.sub("", source).strip()
    if last_banner is not None:
        return cleaned, {"type": "banner", "content": last_banner}
    if last_state is not None:
        return cleaned, {"type": "state", "state": last_state}
    return cleaned, None


class WidgetControlStore:
    def __init__(self, path: Path, catalog: WidgetAssetCatalog):
        self.path = Path(path)
        self.catalog = catalog
        self._lock = asyncio.Lock()

    def _default_state(self) -> dict:
        actor_states = {}
        snapshot = self.catalog.snapshot()
        for actor_id in ACTOR_IDS:
            states = snapshot.get(actor_id, {}).get("states") or []
            actor_states[actor_id] = "平静" if "平静" in states else (states[0] if states else "")
        return {
            "actor_states": actor_states,
            "banner": {"content": "", "owner_actor_id": ""},
            "revision": 0,
            "updated_at": 0.0,
        }

    def _read(self) -> dict:
        state = self._default_state()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return state
        if isinstance(raw.get("actor_states"), dict):
            for actor_id in ACTOR_IDS:
                value = str(raw["actor_states"].get(actor_id) or "").strip()
                if value:
                    state["actor_states"][actor_id] = value
        banner = raw.get("banner") if isinstance(raw.get("banner"), dict) else {}
        state["banner"] = {
            "content": str(banner.get("content") or "").strip(),
            "owner_actor_id": str(banner.get("owner_actor_id") or "").strip(),
        }
        state["revision"] = max(0, int(raw.get("revision") or 0))
        state["updated_at"] = float(raw.get("updated_at") or 0.0)
        return state

    def _write(self, state: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, self.path)

    async def get_state(self) -> dict:
        async with self._lock:
            return json.loads(json.dumps(self._read(), ensure_ascii=False))

    def _touch(self, state: dict) -> dict:
        state["revision"] = int(state.get("revision") or 0) + 1
        state["updated_at"] = time.time()
        self._write(state)
        return state

    async def set_actor_state(self, actor_id: str, state_name: str) -> dict:
        if actor_id not in ACTOR_IDS or not self.catalog.asset(actor_id, state_name):
            raise ValueError("unknown widget state")
        async with self._lock:
            state = self._read()
            state["actor_states"][actor_id] = state_name
            if state["banner"].get("owner_actor_id") == actor_id:
                state["banner"] = {"content": "", "owner_actor_id": ""}
            return json.loads(json.dumps(self._touch(state), ensure_ascii=False))

    async def show_banner(self, actor_id: str, content: str) -> dict:
        value = str(content or "").strip()
        if actor_id not in ACTOR_IDS or not value:
            raise ValueError("invalid widget banner")
        async with self._lock:
            state = self._read()
            state["banner"] = {"content": value, "owner_actor_id": actor_id}
            return json.loads(json.dumps(self._touch(state), ensure_ascii=False))

    async def clear_banner(self) -> dict:
        async with self._lock:
            state = self._read()
            if not state["banner"].get("content"):
                return json.loads(json.dumps(state, ensure_ascii=False))
            state["banner"] = {"content": "", "owner_actor_id": ""}
            return json.loads(json.dumps(self._touch(state), ensure_ascii=False))

    async def process_reply(self, text: str, actor_id: str) -> str:
        cleaned, action = extract_widget_command(text, actor_id, self.catalog)
        if not action:
            return cleaned
        if action["type"] == "banner":
            await self.show_banner(actor_id, action["content"])
        else:
            await self.set_actor_state(actor_id, action["state"])
        return cleaned


widget_asset_catalog = WidgetAssetCatalog(PUBLIC_DIR / "小组件", _configured_actor_names)
widget_control_store = WidgetControlStore(
    DATA_DIR / "widget_control_state.json", widget_asset_catalog
)


async def broadcast_widget_state_changed(state: dict) -> None:
    from ws import manager

    await manager.broadcast({
        "type": "widget_state_changed",
        "data": {"revision": int(state.get("revision") or 0)},
    })


async def process_widget_control_commands(text: str, actor_id: str) -> str:
    before = await widget_control_store.get_state()
    cleaned = await widget_control_store.process_reply(text, actor_id)
    after = await widget_control_store.get_state()
    if after["revision"] != before["revision"]:
        await broadcast_widget_state_changed(after)
    return cleaned


async def get_widget_state() -> dict:
    return await widget_control_store.get_state()


async def set_actor_state(actor_id: str, state: str) -> dict:
    result = await widget_control_store.set_actor_state(actor_id, state)
    await broadcast_widget_state_changed(result)
    return result


async def show_banner(actor_id: str, content: str) -> dict:
    result = await widget_control_store.show_banner(actor_id, content)
    await broadcast_widget_state_changed(result)
    return result


async def clear_banner() -> dict:
    before = await widget_control_store.get_state()
    result = await widget_control_store.clear_banner()
    if result["revision"] != before["revision"]:
        await broadcast_widget_state_changed(result)
    return result
