"""Local-only MiniMax cloned-voice preview page.

The browser sends the API key to this process on 127.0.0.1.  The key is
forwarded to MiniMax for one request and is never written to disk or logged.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


ROOT = Path(__file__).resolve().parent
PAGE_PATH = ROOT / "minimax_tts_preview.html"
MINIMAX_TTS_URL = "https://api.minimax.cn/v1/t2a_v2"
ALLOWED_MODELS = {"speech-2.8-hd", "speech-2.8-turbo"}
ALLOWED_EMOTIONS = {
    "",
    "happy",
    "sad",
    "angry",
    "fearful",
    "disgusted",
    "surprised",
    "neutral",
}
VOICE_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{7,255}$")


class InputError(ValueError):
    pass


def _number(value, name: str, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise InputError(f"{name}不是有效数字") from exc
    if not minimum <= parsed <= maximum:
        raise InputError(f"{name}需要在 {minimum:g}～{maximum:g} 之间")
    return parsed


def build_minimax_payload(data: dict) -> tuple[str, dict]:
    api_key = str(data.get("api_key") or "").strip()
    voice_id = str(data.get("voice_id") or "").strip()
    text = str(data.get("text") or "").strip()
    model = str(data.get("model") or "speech-2.8-hd").strip()
    emotion = str(data.get("emotion") or "").strip()

    if not api_key:
        raise InputError("请填写 API Key")
    if not VOICE_ID_RE.fullmatch(voice_id):
        raise InputError("音色 ID 格式不正确，请粘贴完整的克隆音色 ID")
    if not text:
        raise InputError("请填写试听文字")
    if len(text) > 1000:
        raise InputError("试听文字请控制在 1000 字以内")
    if model not in ALLOWED_MODELS:
        raise InputError("不支持的语音模型")
    if emotion not in ALLOWED_EMOTIONS:
        raise InputError("不支持的情绪参数")

    speed = _number(data.get("speed", 1), "语速", 0.5, 2)
    volume = _number(data.get("volume", 1), "音量", 0.1, 10)
    pitch = _number(data.get("pitch", 0), "音调", -12, 12)
    voice_setting = {
        "voice_id": voice_id,
        "speed": speed,
        "vol": volume,
        "pitch": int(pitch),
    }
    if emotion:
        voice_setting["emotion"] = emotion

    payload = {
        "model": model,
        "text": text,
        "stream": False,
        "voice_setting": voice_setting,
        "audio_setting": {
            "sample_rate": 32000,
            "bitrate": 128000,
            "format": "mp3",
            "channel": 1,
        },
        "language_boost": "Chinese",
        "subtitle_enable": False,
        "output_format": "hex",
        "aigc_watermark": False,
    }
    return api_key, payload


def call_minimax(api_key: str, payload: dict) -> tuple[bytes, dict]:
    request = urllib.request.Request(
        MINIMAX_TTS_URL,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "AionsHome-MiniMax-Voice-Preview/1.0",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            message = json.loads(raw.decode("utf-8", errors="replace"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            message = raw.decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"MiniMax HTTP {exc.code}：{message}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"无法连接 MiniMax：{exc.reason}") from exc

    try:
        result = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("MiniMax 返回了无法识别的数据") from exc

    base_resp = result.get("base_resp") or {}
    status_code = base_resp.get("status_code")
    if status_code not in (None, 0):
        raise RuntimeError(base_resp.get("status_msg") or f"MiniMax 错误 {status_code}")
    audio_hex = (result.get("data") or {}).get("audio")
    if not audio_hex:
        raise RuntimeError("MiniMax 没有返回音频")
    try:
        audio = bytes.fromhex(audio_hex)
    except ValueError as exc:
        raise RuntimeError("MiniMax 返回的音频编码无效") from exc
    if not audio:
        raise RuntimeError("MiniMax 返回了空音频")
    return audio, result


class PreviewHandler(BaseHTTPRequestHandler):
    server_version = "MiniMaxPreview/1.0"

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path not in ("/", "/index.html"):
            self.send_error(404)
            return
        try:
            body = PAGE_PATH.read_bytes()
        except OSError as exc:
            self._send_json(500, {"error": f"无法读取试听页面：{exc}"})
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; media-src 'self' blob:")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/api/preview":
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            length = 0
        if length <= 0 or length > 64 * 1024:
            self._send_json(400, {"error": "请求内容大小不正确"})
            return
        try:
            data = json.loads(self.rfile.read(length))
            api_key, payload = build_minimax_payload(data)
            audio, result = call_minimax(api_key, payload)
        except InputError as exc:
            self._send_json(400, {"error": str(exc)})
            return
        except Exception as exc:  # Show the provider's useful message, never the key.
            self._send_json(502, {"error": str(exc)})
            return

        extra = result.get("extra_info") or {}
        self.send_response(200)
        self.send_header("Content-Type", "audio/mpeg")
        self.send_header("Content-Length", str(len(audio)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-MiniMax-Usage-Characters", str(extra.get("usage_characters") or ""))
        self.send_header("X-MiniMax-Audio-Length", str(extra.get("audio_length") or ""))
        self.send_header("X-MiniMax-Trace-Id", str(result.get("trace_id") or ""))
        self.end_headers()
        self.wfile.write(audio)

    def log_message(self, fmt: str, *args) -> None:
        # Default logging contains only the route/status, never request bodies.
        sys.stderr.write("[MiniMax 试听] " + (fmt % args) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="在本机试听 MiniMax 克隆音色")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    if not PAGE_PATH.is_file():
        raise SystemExit(f"找不到页面文件：{PAGE_PATH}")
    server = ThreadingHTTPServer(("127.0.0.1", args.port), PreviewHandler)
    url = f"http://127.0.0.1:{args.port}/"
    print(f"MiniMax 音色试听已启动：{url}")
    print("关闭这个窗口即可停止；API Key 不会保存到磁盘。")
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
