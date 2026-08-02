from __future__ import annotations

import os
import re
from typing import Any

import httpx

from config import SETTINGS


TAVILY_SEARCH_URL = "https://api.tavily.com/search"
TAVILY_EXTRACT_URL = "https://api.tavily.com/extract"
WEB_SEARCH_CMD_PATTERN = re.compile(r"\[WEB_SEARCH\s*[：:]\s*([^\]]+)\]", re.IGNORECASE)
WEB_EXTRACT_CMD_PATTERN = re.compile(r"\[WEB_EXTRACT\s*[：:]\s*([^\]]+)\]", re.IGNORECASE)
_WEB_CMD_START_RE = re.compile(r"\[(?:WEB_SEARCH|WEB_EXTRACT)\s*[：:]", re.IGNORECASE)
_MAX_QUERIES = 3
_MAX_EXTRACT_URLS = 3


class TavilyError(RuntimeError):
    pass


def tavily_api_key() -> str:
    return (SETTINGS.get("tavily_api_key") or os.environ.get("TAVILY_API_KEY") or "").strip()


def is_web_search_available() -> bool:
    return bool(tavily_api_key())


def clean_web_command_text(text: str) -> str:
    cleaned = WEB_SEARCH_CMD_PATTERN.sub("", text or "")
    cleaned = WEB_EXTRACT_CMD_PATTERN.sub("", cleaned)
    return cleaned.strip()


def format_tavily_context(payload: dict, *, kind: str = "search", max_chars_per_item: int = 700) -> str:
    if not isinstance(payload, dict):
        return "【联网搜索结果】\n系统没有收到有效结果。"

    title = "联网搜索结果" if kind == "search" else "网页读取结果"
    lines = [f"【{title}】"]
    query = _clean_text(payload.get("query"))
    if query:
        lines.append(f"查询：{query}")

    answer = _clean_text(payload.get("answer"))
    if answer:
        lines.append(f"简短答案：{_truncate(answer, max_chars_per_item)}")

    results = payload.get("results")
    if not isinstance(results, list):
        results = []

    for idx, item in enumerate(results[:5], start=1):
        if not isinstance(item, dict):
            continue
        item_title = _clean_text(item.get("title")) or _clean_text(item.get("url")) or f"结果 {idx}"
        url = _clean_text(item.get("url"))
        date = _clean_text(item.get("published_date"))
        if kind == "extract":
            snippet = _clean_text(item.get("raw_content") or item.get("content"))
        else:
            snippet = _clean_text(item.get("content") or item.get("snippet") or item.get("description"))
        lines.append("")
        lines.append(f"{idx}. {item_title}")
        if url:
            lines.append(f"URL：{url}")
        if date:
            lines.append(f"发布时间：{date}")
        if snippet:
            lines.append(f"内容：{_truncate(snippet, max_chars_per_item)}")

    failed = payload.get("failed_results")
    if isinstance(failed, list) and failed:
        lines.append("")
        lines.append(f"未成功读取：{len(failed)} 个来源")

    usage = payload.get("usage")
    if isinstance(usage, dict) and usage.get("credits") is not None:
        lines.append("")
        lines.append(f"本次 Tavily credits：{usage.get('credits')}")

    return "\n".join(lines).strip()


def format_web_system_message(actor_name: str, searches: list[str], extracts: list[str]) -> str:
    parts: list[str] = []
    for query in searches[:_MAX_QUERIES]:
        if query.strip():
            parts.append(f"搜索「{query.strip()}」")
    for url in extracts[:_MAX_EXTRACT_URLS]:
        if url.strip():
            parts.append(f"读取网页「{url.strip()}」")
    action = "、".join(parts) if parts else "联网查询"
    return f"{actor_name}发起了联网搜索：{action}"


async def tavily_search(query: str, *, max_results: int = 5, timeout: float = 30.0) -> dict[str, Any]:
    query = query.strip()
    if not query:
        raise TavilyError("搜索内容为空")
    key = tavily_api_key()
    if not key:
        raise TavilyError("未配置 Tavily API key")
    payload = {
        "query": query,
        "search_depth": "basic",
        "max_results": max(1, min(8, int(max_results or 5))),
        "include_answer": False,
        "include_raw_content": False,
        "include_images": False,
        "include_usage": True,
    }
    return await _post_tavily(TAVILY_SEARCH_URL, key, payload, timeout=timeout)


async def tavily_extract(url: str, *, query: str = "", timeout: float = 30.0) -> dict[str, Any]:
    url = url.strip()
    if not url:
        raise TavilyError("网页地址为空")
    key = tavily_api_key()
    if not key:
        raise TavilyError("未配置 Tavily API key")
    payload: dict[str, Any] = {
        "urls": [url],
        "extract_depth": "basic",
        "format": "markdown",
        "include_images": False,
        "include_usage": True,
        "timeout": min(max(timeout, 1.0), 60.0),
    }
    if query.strip():
        payload["query"] = query.strip()
        payload["chunks_per_source"] = 3
    return await _post_tavily(TAVILY_EXTRACT_URL, key, payload, timeout=timeout + 5)


async def run_web_commands(searches: list[str], extracts: list[str]) -> list[str]:
    contexts: list[str] = []
    for query in [item.strip() for item in searches if item.strip()][:_MAX_QUERIES]:
        payload = await tavily_search(query)
        contexts.append(format_tavily_context(payload, kind="search"))
    for url in [item.strip() for item in extracts if item.strip()][:_MAX_EXTRACT_URLS]:
        payload = await tavily_extract(url)
        contexts.append(format_tavily_context(payload, kind="extract", max_chars_per_item=1200))
    return contexts


class WebCommandStreamFilter:
    """Suppress WEB_SEARCH/WEB_EXTRACT tags from streamed UI and TTS chunks."""

    def __init__(self):
        self._pending = ""
        self._in_command = False

    def feed(self, chunk: str) -> str:
        if not chunk:
            return ""
        buf = self._pending + chunk
        self._pending = ""
        out: list[str] = []

        while buf:
            if self._in_command:
                end = buf.find("]")
                if end < 0:
                    return "".join(out)
                buf = buf[end + 1:]
                self._in_command = False
                continue

            match = _WEB_CMD_START_RE.search(buf)
            if match:
                out.append(buf[:match.start()])
                buf = buf[match.end():]
                self._in_command = True
                continue

            keep = _possible_command_prefix_len(buf)
            if keep:
                out.append(buf[:-keep])
                self._pending = buf[-keep:]
            else:
                out.append(buf)
            break

        return "".join(out)

    def flush(self) -> str:
        pending = self._pending
        self._pending = ""
        if self._in_command:
            self._in_command = False
            return ""
        return pending


async def _post_tavily(url: str, key: str, payload: dict[str, Any], *, timeout: float) -> dict[str, Any]:
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(url, json=payload, headers=headers)
    if resp.status_code >= 400:
        detail = resp.text[:300]
        raise TavilyError(f"Tavily 请求失败 [{resp.status_code}]: {detail}")
    data = resp.json()
    if not isinstance(data, dict):
        raise TavilyError("Tavily 返回了非 JSON 对象")
    return data


def _clean_text(value: Any) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _truncate(text: str, max_chars: int) -> str:
    max_chars = max(80, int(max_chars or 700))
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "..."


def _possible_command_prefix_len(text: str) -> int:
    probes = ("[WEB_SEARCH:", "[WEB_SEARCH：", "[WEB_EXTRACT:", "[WEB_EXTRACT：")
    max_len = min(len(text), max(len(item) for item in probes) - 1)
    upper = text.upper()
    for size in range(max_len, 0, -1):
        suffix = upper[-size:]
        if any(probe.upper().startswith(suffix) for probe in probes):
            return size
    return 0
