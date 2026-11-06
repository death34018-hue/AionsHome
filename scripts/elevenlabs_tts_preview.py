"""Standalone local ElevenLabs listening room. Python standard library only.

Keys are forwarded to ElevenLabs, never stored or logged by this server.
The page can explicitly remember a key in this browser's local storage.
"""
from __future__ import annotations

import argparse
import json
import re
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
API = "https://api.elevenlabs.io"
FILES = {
    "/": ("elevenlabs_tts_preview.html", "text/html; charset=utf-8"),
    "/app.js": ("elevenlabs_tts_preview.js", "text/javascript; charset=utf-8"),
    "/style.css": ("elevenlabs_tts_preview.css", "text/css; charset=utf-8"),
}


class InputError(ValueError):
    pass


def require_key(data: dict) -> str:
    key = str(data.get("api_key") or "").strip()
    if not key or len(key) > 1024 or any(ord(c) < 33 or ord(c) > 126 for c in key):
        raise InputError("请填写有效的 API Key（不要包含空格或换行）")
    return key


def build_payload(data: dict) -> tuple[str, str, dict]:
    key = require_key(data)
    voice = str(data.get("voice_id") or "").strip()
    model = str(data.get("model_id") or "eleven_v3").strip()
    text = str(data.get("text") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", voice):
        raise InputError("请先加载并选择一个音色")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", model):
        raise InputError("模型 ID 无效")
    if not text or len(text) > 5000:
        raise InputError("试听文本需在 1～5000 个字符之间")
    if re.search(r"\[[^\]]+\]", text) and not re.match(r"eleven_v[34](?:_|$)", model):
        raise InputError("这款模型未确认支持方括号语气标签，请选择 v3 / v4，或移除标签后试听")
    if text.count("[") != text.count("]"):
        raise InputError("语气标签的方括号没有配对，请检查 [ 和 ]")
    return key, voice, {"text": text, "model_id": model}


def request_api(path: str, key: str, payload: dict | None = None) -> tuple[bytes, str]:
    request = urllib.request.Request(
        API + path,
        data=None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"xi-api-key": key, "Content-Type": "application/json",
                 "User-Agent": "AionsHome-ElevenLabs-Preview/1.0"},
        method="GET" if payload is None else "POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=120 if payload else 30) as response:
            return response.read(), response.headers.get("Content-Type", "")
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read()).get("detail", {})
            message = detail.get("message", detail.get("status", "")) if isinstance(detail, dict) else str(detail)
        except (ValueError, AttributeError):
            message = "服务暂时不可用"
        message = str(message).replace(key, "[已隐藏]")[:600]
        hints = {401: "请检查 Key 是否有效", 403: "请检查 Key 权限、套餐或音色访问权限",
                 429: "额度或并发受到限制，请稍后再试"}
        raise RuntimeError(f"ElevenLabs HTTP {exc.code}：{hints.get(exc.code, '请求未成功')}。{message}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        if getattr(reason, "winerror", None) == 10013 or getattr(reason, "errno", None) == 13:
            message = "本机进程的网络权限被限制，请双击「启动ElevenLabs语音试听.bat」在正常本机环境中启动服务。"
        elif isinstance(reason, ssl.SSLCertVerificationError):
            message = "HTTPS 证书校验失败，请检查本机证书和代理配置。"
        elif isinstance(reason, socket.gaierror):
            message = "无法解析 api.elevenlabs.io，请检查本机网络或代理的 DNS 配置。"
        elif isinstance(reason, TimeoutError):
            message = "连接或读取 ElevenLabs 超时，请检查本机网络与代理后重试。"
        else:
            message = "无法连接 ElevenLabs，请检查本机网络与代理是否可用。"
        if payload is not None:
            message += " 本次生成未收到完整响应，请先检查官方历史与用量，避免重复扣费。"
        else:
            message = "读取列表失败：" + message + " 此操作没有发起语音生成。"
        raise RuntimeError(message) from None


def get_json(path: str, key: str):
    raw, _ = request_api(path, key)
    try:
        return json.loads(raw)
    except ValueError:
        raise RuntimeError("ElevenLabs 返回了无法识别的数据") from None


def list_voices(key: str) -> list[dict]:
    voices, tokens = {}, set()
    params = {"page_size": 100, "include_total_count": "false"}
    while True:
        page = get_json("/v2/voices?" + urllib.parse.urlencode(params), key)
        for voice in page.get("voices", []):
            identifier = voice.get("voice_id")
            if identifier and identifier not in voices:
                # Samples and training recordings are not needed by a listening UI.
                voices[identifier] = {k: voice[k] for k in (
                    "voice_id", "name", "category", "description", "labels", "fine_tuning", "is_owner"
                ) if k in voice}
        if not page.get("has_more"):
            return list(voices.values())
        token = page.get("next_page_token")
        if not token or token in tokens:
            raise RuntimeError("音色分页返回异常，未能读取完整列表，请重新加载")
        tokens.add(token)
        params["next_page_token"] = token


class PreviewHandler(BaseHTTPRequestHandler):
    server_version = "ElevenLabsPreview/1.0"

    def send_body(self, status: int, body: bytes, content_type: str, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; media-src 'self' blob:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        for name, value in (extra or {}).items():
            self.send_header(name, str(value))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def send_json(self, status: int, data):
        self.send_body(status, json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def local_request(self) -> bool:
        authority = f"127.0.0.1:{self.server.server_port}"
        return (self.headers.get("Host") == authority
                and self.headers.get("Origin") in (None, f"http://{authority}"))

    def do_GET(self):
        if not self.local_request():
            self.send_json(403, {"error": "请使用本机 127.0.0.1 地址打开页面"})
            return
        path = urllib.parse.urlsplit(self.path).path
        if path == "/health":
            self.send_json(200, {"app": "elevenlabs-preview"})
        elif path in FILES:
            filename, content_type = FILES[path]
            self.send_body(200, (ROOT / filename).read_bytes(), content_type)
        else:
            self.send_json(404, {"error": "页面不存在"})

    def do_POST(self):
        if not self.local_request():
            self.send_json(403, {"error": "仅允许此本机页面发起请求"})
            return
        if self.path not in ("/api/voices", "/api/models", "/api/preview"):
            self.send_json(404, {"error": "接口不存在"})
            return
        key = ""
        try:
            if self.headers.get_content_type() != "application/json":
                raise InputError("请求必须使用 JSON")
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 128 * 1024:
                raise InputError("请求内容大小不正确")
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise InputError("请求内容必须是一个对象")
            key = require_key(data)
            if self.path == "/api/voices":
                self.send_json(200, {"voices": list_voices(key)})
            elif self.path == "/api/models":
                models = get_json("/v1/models", key)
                self.send_json(200, {"models": [m for m in models if m.get("can_do_text_to_speech")]})
            else:
                key, voice, payload = build_payload(data)
                start = time.monotonic()
                audio, content_type = request_api(f"/v1/text-to-speech/{voice}?output_format=mp3_44100_128", key, payload)
                if not audio or not (content_type.startswith("audio/") or content_type.startswith("application/octet-stream")):
                    raise RuntimeError("ElevenLabs 没有返回可播放的音频")
                self.send_body(200, audio, "audio/mpeg", {"X-Generation-Seconds": f"{time.monotonic() - start:.2f}"})
        except ValueError as exc:
            # Do not echo JSON parsing errors: these may contain request material.
            self.send_json(400, {"error": str(exc) if isinstance(exc, InputError) else "请求内容格式不正确"})
        except RuntimeError as exc:
            self.send_json(502, {"error": str(exc).replace(key, "[已隐藏]") if key else str(exc)})
        except Exception:
            self.send_json(500, {"error": "本机试听服务遇到异常，请重新加载后再试"})

    def log_message(self, fmt, *args):
        # No headers, keys, request bodies, or upstream error bodies in logs.
        pass


class PreviewServer(ThreadingHTTPServer):
    allow_reuse_address = False

    def server_bind(self):
        # Windows otherwise permits two preview processes to share one port.
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def main():
    parser = argparse.ArgumentParser(description="ElevenLabs 本机语音试听")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    url = f"http://127.0.0.1:{args.port}/"
    try:
        server = PreviewServer(("127.0.0.1", args.port), PreviewHandler)
    except OSError:
        try:
            with urllib.request.urlopen(url + "health", timeout=2) as response:
                if json.load(response).get("app") != "elevenlabs-preview":
                    raise ValueError()
        except Exception:
            raise SystemExit(f"端口 {args.port} 已被占用，请使用 --port 指定其他端口")
        print(f"试听页已在运行：{url}")
        if not args.no_browser:
            webbrowser.open(url)
        return
    print(f"ElevenLabs 试听页：{url}", flush=True)
    print("只监听本机；关闭此进程即可停止。", flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
