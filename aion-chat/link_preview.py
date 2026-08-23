import asyncio
import html as html_lib
import ipaddress
import re
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import httpx


URL_RE = re.compile(r"https?://[^\s<>'\"`，。；：！？、]+", re.IGNORECASE)
MARKDOWN_HTTP_LINK_RE = re.compile(
    r"(?<!!)\[([^\]\n]+)\]\(\s*(https?://[^\s<>]+)\s*\)",
    re.IGNORECASE,
)
TRAILING_PUNCTUATION = ".,;:!?，。；：！？、"
TRAILING_BRACKETS = ")]}）】》」』"
MAX_PREVIEW_ITEMS = 3
MAX_HTML_BYTES = 512 * 1024


def _clean_url(raw_url: str) -> str:
    url = (raw_url or "").strip()
    url = url.rstrip(TRAILING_PUNCTUATION)
    while url and url[-1] in TRAILING_BRACKETS:
        opener = {"）": "（", ")": "(", "]": "[", "}": "{", "】": "【", "》": "《", "」": "「", "』": "『"}.get(url[-1])
        if opener and url.count(opener) >= url.count(url[-1]):
            break
        url = url[:-1]
    return url


def extract_urls(text: str, limit: int = MAX_PREVIEW_ITEMS) -> list[str]:
    seen = set()
    urls: list[str] = []
    for match in URL_RE.finditer(text or ""):
        url = _clean_url(match.group(0))
        if not url or url in seen:
            continue
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            continue
        seen.add(url)
        urls.append(url)
        if len(urls) >= limit:
            break
    return urls


def strip_urls_for_message(text: str) -> str:
    """Keep readable Markdown labels while removing raw web addresses."""
    cleaned = MARKDOWN_HTTP_LINK_RE.sub(lambda match: match.group(1).strip(), text or "")

    def _remove_bare_url(match: re.Match) -> str:
        raw = match.group(0)
        url = _clean_url(raw)
        suffix = raw[len(url):]
        return "".join(char for char in suffix if char in TRAILING_PUNCTUATION)

    cleaned = URL_RE.sub(_remove_bare_url, cleaned)
    lines: list[str] = []
    empty_link_label = re.compile(
        r"^(?:原始资料|原文|来源|网址|链接|参考|参考资料|参考链接)\s*[：:]?\s*[。.]?$"
    )
    for line in cleaned.splitlines():
        line = re.sub(r"[ \t]+", " ", line).strip()
        line = re.sub(r"\s+([，。；：！？、,.])", r"\1", line)
        if empty_link_label.fullmatch(line):
            continue
        if not line:
            if lines and lines[-1]:
                lines.append("")
            continue
        lines.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _host_for_url(url: str) -> str:
    parsed = urlparse(url)
    host = parsed.hostname or parsed.netloc or url
    return host.lower().removeprefix("www.")


def _normalize_text(value: str, max_len: int) -> str:
    value = html_lib.unescape(value or "")
    value = re.sub(r"\s+", " ", value).strip()
    if len(value) <= max_len:
        return value
    return value[: max_len - 1].rstrip() + "…"


def _safe_abs_url(base_url: str, value: str) -> str:
    if not value:
        return ""
    resolved = urljoin(base_url, html_lib.unescape(value).strip())
    parsed = urlparse(resolved)
    return resolved if parsed.scheme in ("http", "https") and parsed.netloc else ""


class _PreviewParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}
        self.icon = ""
        self._in_title = False
        self._title_parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        attrs_dict = {str(k).lower(): (v or "") for k, v in attrs}
        tag = tag.lower()
        if tag == "title":
            self._in_title = True
            return
        if tag == "meta":
            key = (attrs_dict.get("property") or attrs_dict.get("name") or "").lower()
            content = attrs_dict.get("content") or ""
            if key and content:
                self.meta.setdefault(key, content)
            return
        if tag == "link":
            rel = (attrs_dict.get("rel") or "").lower()
            href = attrs_dict.get("href") or ""
            if href and ("icon" in rel) and not self.icon:
                self.icon = href

    def handle_endtag(self, tag):
        if tag.lower() == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title and data:
            self._title_parts.append(data)

    @property
    def title(self) -> str:
        return " ".join(self._title_parts)


def link_preview_fallback(url: str) -> dict:
    host = _host_for_url(url)
    return {
        "type": "link_preview",
        "url": url,
        "title": host,
        "description": "",
        "site_name": host,
        "image": "",
        "favicon": "",
    }


def link_preview_from_html(url: str, html: str, final_url: str | None = None) -> dict:
    base_url = final_url or url
    parser = _PreviewParser()
    try:
        parser.feed(html or "")
    except Exception:
        return link_preview_fallback(url)

    meta = parser.meta
    host = _host_for_url(base_url)
    title = (
        meta.get("og:title")
        or meta.get("twitter:title")
        or parser.title
        or host
    )
    description = (
        meta.get("og:description")
        or meta.get("twitter:description")
        or meta.get("description")
        or ""
    )
    image = meta.get("og:image") or meta.get("twitter:image") or ""
    site_name = meta.get("og:site_name") or meta.get("application-name") or host
    card = link_preview_fallback(url)
    card.update(
        {
            "title": _normalize_text(title, 120),
            "description": _normalize_text(description, 180),
            "site_name": _normalize_text(site_name, 80) or host,
            "image": _safe_abs_url(base_url, image),
            "favicon": _safe_abs_url(base_url, parser.icon),
        }
    )
    return card


def _is_private_host(hostname: str) -> bool:
    host = (hostname or "").strip().lower()
    if not host:
        return True
    if host in {"localhost", "127.0.0.1", "::1"} or host.endswith(".local"):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast


async def _fetch_preview(client: httpx.AsyncClient, url: str) -> dict:
    if _is_private_host(urlparse(url).hostname or ""):
        return link_preview_fallback(url)
    try:
        async with client.stream(
            "GET",
            url,
            headers={
                "Accept": "text/html,application/xhtml+xml",
                "User-Agent": "LinkPreviewBot/1.0",
            },
        ) as response:
            final_url = str(response.url)
            content_type = response.headers.get("content-type", "").lower()
            if response.status_code >= 400 or ("text/html" not in content_type and "application/xhtml+xml" not in content_type):
                return link_preview_fallback(url)
            chunks: list[bytes] = []
            total = 0
            async for chunk in response.aiter_bytes():
                if not chunk:
                    continue
                remaining = MAX_HTML_BYTES - total
                if remaining <= 0:
                    break
                chunks.append(chunk[:remaining])
                total += len(chunk[:remaining])
                if total >= MAX_HTML_BYTES:
                    break
            encoding = response.encoding or "utf-8"
            html = b"".join(chunks).decode(encoding, errors="replace")
            return link_preview_from_html(url, html, final_url=final_url)
    except Exception:
        return link_preview_fallback(url)


async def build_link_preview_attachments(
    text: str,
    existing_attachments: list | None = None,
    max_items: int = MAX_PREVIEW_ITEMS,
) -> list[dict]:
    urls = extract_urls(text, limit=max_items)
    if not urls:
        return []
    existing_urls = {
        item.get("url")
        for item in (existing_attachments or [])
        if isinstance(item, dict) and item.get("type") == "link_preview"
    }
    urls = [url for url in urls if url not in existing_urls]
    if not urls:
        return []

    timeout = httpx.Timeout(5.0, connect=2.0)
    limits = httpx.Limits(max_connections=max_items, max_keepalive_connections=0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, limits=limits) as client:
        return await asyncio.gather(*(_fetch_preview(client, url) for url in urls))
