"""
AI 生图模块：可配置 OpenAI 兼容接口，未配置时沿用 Gemini
支持 SELFIE（带参考图）和 DRAW（纯文本）两种模式
"""

import base64, re
from pathlib import Path

import httpx

from config import SETTINGS, get_key, PUBLIC_DIR
from album import save_generated_image

# 参考图位置（用于 SELFIE 模式）
REFERENCE_IMAGE_PATH = PUBLIC_DIR / "生图锚点.jpg"
SECONDARY_REFERENCE_IMAGE_PATH = PUBLIC_DIR / "2号机生图锚点.jpg"
IMAGE_GEN_MODEL = "gemini-3.1-flash-lite-image"
IMAGE_GEN_TIMEOUT = 120  # 生图超时秒数


def _selfie_reference_path(source_identity: str = "") -> Path:
    """Return the SELFIE anchor by stable internal actor identity, not display name."""
    if str(source_identity or "").strip().lower() == "connor":
        return SECONDARY_REFERENCE_IMAGE_PATH
    return REFERENCE_IMAGE_PATH


def _openai_image_source(data: dict) -> str | None:
    """兼容中转站 image_urls、结构化图片内容及 Markdown 图片链接。"""
    def extract(value):
        if isinstance(value, list):
            return next((source for item in value if (source := extract(item))), None)
        if isinstance(value, dict):
            for key in ("image_urls", "images", "image_url", "url", "content", "text"):
                source = extract(value.get(key))
                if source:
                    return source
        if isinstance(value, str):
            value = value.strip()
            if value.startswith(("https://", "http://", "data:image/")) and not re.search(r"\s", value):
                return value
            match = re.search(r"!\[[^\]]*\]\(\s*<?([^\s>]+?)>?(?:\s+\"[^\"]*\")?\s*\)", value)
            if match:
                return match.group(1)
        return None

    choices = data.get("choices") or []
    return extract(choices[0].get("message", {}) if choices else {}) or extract(data.get("image_urls"))


async def _generate_openai_image(prompt: str, is_selfie: bool, source_identity: str,
                                 base_url: str, api_key: str, model: str) -> str | None:
    try:
        content = [{"type": "text", "text": prompt}]
        reference_bytes = None
        if is_selfie:
            reference = _selfie_reference_path(source_identity)
            if reference.exists():
                reference_bytes = reference.read_bytes()
                encoded = base64.b64encode(reference_bytes).decode("ascii")
                content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{encoded}"}})
            else:
                print(f"[image_gen] 参考图不存在: {reference}，降级为 DRAW 模式")

        url = base_url.rstrip("/")
        if not url.endswith("/chat/completions"):
            url += "/chat/completions" if url.endswith("/v1") else "/v1/chat/completions"
        async with httpx.AsyncClient(timeout=IMAGE_GEN_TIMEOUT) as client:
            print(f"[image_gen] 使用自定义生图模型: {model}")
            response = await client.post(
                url,
                headers={"Authorization": f"Bearer {api_key}"},
                json={"model": model, "stream": False, "messages": [{"role": "user", "content": content}]},
            )
            response.raise_for_status()
            source = _openai_image_source(response.json())
            if not source:
                print("[image_gen] 自定义接口响应中未找到图片")
                return None
            if source.startswith("data:image/"):
                header, encoded = source.split(",", 1)
                if ";base64" not in header:
                    raise ValueError("图片数据不是 Base64 格式")
                image_bytes = base64.b64decode(encoded, validate=True)
            elif source.startswith(("https://", "http://")):
                # 图片通常位于独立 CDN，不向下载地址传递中转站的 Key。
                image_response = await client.get(source, follow_redirects=True)
                image_response.raise_for_status()
                image_bytes = image_response.content
            else:
                raise ValueError("不支持的图片地址格式")

        return await save_generated_image(image_bytes, prompt=prompt, model=model,
                                          actor=source_identity, kind="selfie" if is_selfie else "draw",
                                          reference_bytes=reference_bytes)
    except httpx.HTTPStatusError as exc:
        print(f"[image_gen] 自定义生图请求失败（HTTP {exc.response.status_code}），未切换至 Gemini")
    except Exception as exc:
        # 不打印请求 URL、响应正文等可能包含凭据的内容。
        print(f"[image_gen] 自定义生图失败（{type(exc).__name__}），未切换至 Gemini")
    return None


async def generate_image(prompt: str, is_selfie: bool = False, source_identity: str = "") -> str | None:
    """
    调用设置中的生图模型，未配置时使用 Gemini；保存到相册并返回 uploads 相对路径。
    is_selfie=True 时自动附带参考图。
    失败返回 None。
    """
    custom = [(SETTINGS.get(key) or "").strip() for key in
              ("image_gen_base_url", "image_gen_api_key", "image_gen_model")]
    if any(custom):
        if not all(custom):
            print("[image_gen] 请完整填写生图 API 地址、Key 和模型名，或全部清空以使用 Gemini")
            return None
        return await _generate_openai_image(prompt, is_selfie, source_identity, *custom)

    api_key = get_key("gemini")
    if not api_key:
        print("[image_gen] 没有 Gemini API Key，无法生图")
        return None

    # 构建请求内容
    parts = [{"text": prompt}]

    # SELFIE 模式：附带参考图
    ref_bytes = None
    if is_selfie:
        reference_image_path = _selfie_reference_path(source_identity)
        if reference_image_path.exists():
            ref_bytes = reference_image_path.read_bytes()
            ref_b64 = base64.b64encode(ref_bytes).decode("utf-8")
            parts.append({
                "inlineData": {
                    "mimeType": "image/jpeg",
                    "data": ref_b64
                }
            })
            print(f"[image_gen] SELFIE 模式，已附带参考图: {reference_image_path}")
        else:
            print(f"[image_gen] 参考图不存在: {reference_image_path}，降级为 DRAW 模式")

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{IMAGE_GEN_MODEL}:generateContent?key={api_key}"

    payload = {
        "contents": [{"parts": parts}],
        "generationConfig": {
            "responseModalities": ["IMAGE", "TEXT"],
        },
        "safetySettings": [
            {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
        ]
    }

    try:
        async with httpx.AsyncClient(timeout=IMAGE_GEN_TIMEOUT) as client:
            print(f"[image_gen] 开始生图... prompt: {prompt[:80]}")
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()

            # 解析响应，提取图片
            candidates = data.get("candidates", [])
            if not candidates:
                error_msg = data.get("error", {}).get("message", "未知错误")
                print(f"[image_gen] API 返回空 candidates: {error_msg}")
                return None

            content_parts = candidates[0].get("content", {}).get("parts", [])
            image_data = None

            for part in content_parts:
                inline = part.get("inlineData")
                if inline and inline.get("mimeType", "").startswith("image/"):
                    image_data = inline["data"]
                    break

            if not image_data:
                print("[image_gen] 响应中未找到图片数据")
                return None

            return await save_generated_image(base64.b64decode(image_data), prompt=prompt,
                                              model=IMAGE_GEN_MODEL, actor=source_identity,
                                              kind="selfie" if is_selfie else "draw", reference_bytes=ref_bytes)

    except httpx.HTTPStatusError as e:
        error_body = e.response.text[:500] if e.response else ""
        print(f"[image_gen] API 请求失败 ({e.response.status_code}): {error_body}")
        return None
    except Exception as e:
        print(f"[image_gen] 生图异常: {e}")
        return None
