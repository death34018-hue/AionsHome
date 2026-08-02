"""
AI 生图模块：Gemini gemini-3.1-flash-image-preview 生成图片
支持 SELFIE（带参考图）和 DRAW（纯文本）两种模式
"""

import base64, time
from pathlib import Path

import httpx

from config import get_key, UPLOADS_DIR, PUBLIC_DIR

# 参考图位置（用于 SELFIE 模式）
REFERENCE_IMAGE_PATH = PUBLIC_DIR / "生图锚点.jpg"
IMAGE_GEN_MODEL = "gemini-3.1-flash-image-preview"
IMAGE_GEN_TIMEOUT = 120  # 生图超时秒数


async def generate_image(prompt: str, is_selfie: bool = False) -> str | None:
    """
    调用 Gemini 生图模型生成图片，保存到 uploads 目录，返回文件名。
    is_selfie=True 时自动附带参考图（生图锚点.jpg）。
    失败返回 None。
    """
    api_key = get_key("gemini")
    if not api_key:
        print("[image_gen] 没有 Gemini API Key，无法生图")
        return None

    # 构建请求内容
    parts = [{"text": prompt}]

    # SELFIE 模式：附带参考图
    if is_selfie:
        if REFERENCE_IMAGE_PATH.exists():
            ref_bytes = REFERENCE_IMAGE_PATH.read_bytes()
            ref_b64 = base64.b64encode(ref_bytes).decode("utf-8")
            parts.append({
                "inlineData": {
                    "mimeType": "image/jpeg",
                    "data": ref_b64
                }
            })
            print(f"[image_gen] SELFIE 模式，已附带参考图: {REFERENCE_IMAGE_PATH}")
        else:
            print(f"[image_gen] 参考图不存在: {REFERENCE_IMAGE_PATH}，降级为 DRAW 模式")

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
            mime_type = "image/png"

            for part in content_parts:
                inline = part.get("inlineData")
                if inline and inline.get("mimeType", "").startswith("image/"):
                    image_data = inline["data"]
                    mime_type = inline["mimeType"]
                    break

            if not image_data:
                print("[image_gen] 响应中未找到图片数据")
                return None

            # 确定文件扩展名
            ext = "png"
            if "jpeg" in mime_type or "jpg" in mime_type:
                ext = "jpg"
            elif "webp" in mime_type:
                ext = "webp"

            # 保存图片
            filename = f"img_gen_{int(time.time() * 1000)}.{ext}"
            filepath = UPLOADS_DIR / filename
            filepath.write_bytes(base64.b64decode(image_data))
            print(f"[image_gen] 图片已保存: {filepath}")
            return filename

    except httpx.HTTPStatusError as e:
        error_body = e.response.text[:500] if e.response else ""
        print(f"[image_gen] API 请求失败 ({e.response.status_code}): {error_body}")
        return None
    except Exception as e:
        print(f"[image_gen] 生图异常: {e}")
        return None
