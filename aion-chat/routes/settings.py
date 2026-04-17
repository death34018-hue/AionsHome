"""
设置、世界书、模型列表、TTS 路由
"""

import json

from fastapi import APIRouter
from fastapi.responses import Response, FileResponse
from pydantic import BaseModel
from typing import Optional

import httpx

from config import SETTINGS, MODELS, save_settings, get_key, load_worldbook, save_worldbook, load_chat_status, TTS_CACHE_DIR

router = APIRouter()

# ── 模型列表 ──────────────────────────────────────
@router.get("/api/models")
async def list_models():
    return [{"key": k, "provider": v["provider"]} for k, v in MODELS.items()]

# ── 设置 ──────────────────────────────────────────
class SettingsUpdate(BaseModel):
    gemini_key: Optional[str] = None
    siliconflow_key: Optional[str] = None
    gemini_free_key: Optional[str] = None
    aipro_key: Optional[str] = None
    netease_music_u: Optional[str] = None

@router.get("/api/settings")
async def get_settings():
    def mask(k):
        if not k or len(k) < 8:
            return k
        return k[:4] + "*" * (len(k) - 8) + k[-4:]
    return {
        "gemini_key": SETTINGS.get("gemini_key", ""),
        "siliconflow_key": SETTINGS.get("siliconflow_key", ""),
        "gemini_free_key": SETTINGS.get("gemini_free_key", ""),
        "aipro_key": SETTINGS.get("aipro_key", ""),
        "netease_music_u": SETTINGS.get("netease_music_u", ""),
        "gemini_key_masked": mask(SETTINGS.get("gemini_key", "")),
        "siliconflow_key_masked": mask(SETTINGS.get("siliconflow_key", "")),
        "gemini_free_key_masked": mask(SETTINGS.get("gemini_free_key", "")),
        "aipro_key_masked": mask(SETTINGS.get("aipro_key", "")),
        "netease_music_u_masked": mask(SETTINGS.get("netease_music_u", "")),
    }

@router.put("/api/settings")
async def update_settings(body: SettingsUpdate):
    if body.gemini_key is not None:
        SETTINGS["gemini_key"] = body.gemini_key
    if body.siliconflow_key is not None:
        SETTINGS["siliconflow_key"] = body.siliconflow_key
    if body.gemini_free_key is not None:
        SETTINGS["gemini_free_key"] = body.gemini_free_key
    if body.aipro_key is not None:
        SETTINGS["aipro_key"] = body.aipro_key
    if body.netease_music_u is not None:
        old_mu = SETTINGS.get("netease_music_u", "")
        SETTINGS["netease_music_u"] = body.netease_music_u
        if body.netease_music_u != old_mu:
            # MUSIC_U 变更，重新登录 pyncm
            try:
                from music import reload_login
                reload_login()
            except Exception:
                pass
    save_settings(SETTINGS)
    return {"ok": True}

# ── 温度设置 ──────────────────────────────────────
class TempUpdate(BaseModel):
    temperature: float

@router.put("/api/settings/temperature")
async def update_temperature(body: TempUpdate):
    SETTINGS["temperature"] = body.temperature
    save_settings(SETTINGS)
    return {"ok": True}

# ── 视频通话开关 ──────────────────────────────────
@router.get("/api/settings/video-call")
async def get_video_call_setting():
    return {"video_call_enabled": SETTINGS.get("video_call_enabled", True)}

class VideoCallToggle(BaseModel):
    enabled: bool

@router.put("/api/settings/video-call")
async def update_video_call_setting(body: VideoCallToggle):
    SETTINGS["video_call_enabled"] = body.enabled
    save_settings(SETTINGS)
    return {"ok": True, "video_call_enabled": body.enabled}

# ── 世界书 ────────────────────────────────────────
class WorldBookUpdate(BaseModel):
    ai_persona: str = ""
    user_persona: str = ""
    system_prompt: str = ""
    ai_name: str = "AI"
    user_name: str = "你"

@router.get("/api/worldbook")
async def get_worldbook():
    return load_worldbook()

@router.put("/api/worldbook")
async def update_worldbook(body: WorldBookUpdate):
    save_worldbook({"ai_persona": body.ai_persona, "user_persona": body.user_persona,
                    "system_prompt": body.system_prompt, "ai_name": body.ai_name, "user_name": body.user_name})
    return {"ok": True}

# ── 聊天状态 ──────────────────────────────────────
@router.get("/api/chat_status")
async def get_chat_status_api():
    return load_chat_status()

# ── TTS 语音合成 ──────────────────────────────────
class TTSRequest(BaseModel):
    text: str
    voice: str = ""
    msg_id: Optional[str] = None

@router.post("/api/tts")
async def tts_synthesize(body: TTSRequest):
    key = get_key("siliconflow")
    if not key:
        return Response(content=json.dumps({"error": "未配置硅基流动 API Key"}), status_code=400, media_type="application/json")
    if not body.text.strip():
        return Response(content=json.dumps({"error": "文本不能为空"}), status_code=400, media_type="application/json")
    if not body.voice:
        return Response(content=json.dumps({"error": "未选择语音"}), status_code=400, media_type="application/json")
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                "https://api.siliconflow.cn/v1/audio/speech",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={
                    "model": "FunAudioLLM/CosyVoice2-0.5B",
                    "input": body.text.strip(),
                    "voice": body.voice,
                    "response_format": "mp3",
                    "speed": 1.0,
                    "gain": 0
                }
            )
        if resp.status_code != 200:
            return Response(content=json.dumps({"error": f"TTS API 错误: {resp.status_code}"}), status_code=502, media_type="application/json")
        audio_data = resp.content
        # 如果提供了 msg_id，将音频缓存到服务器
        if body.msg_id:
            import re
            safe_id = re.sub(r'[^a-zA-Z0-9_\-]', '', body.msg_id)
            if safe_id:
                cache_path = TTS_CACHE_DIR / f"{safe_id}.mp3"
                cache_path.write_bytes(audio_data)
        return Response(content=audio_data, media_type="audio/mpeg")
    except Exception as e:
        return Response(content=json.dumps({"error": str(e)}), status_code=500, media_type="application/json")

@router.head("/api/tts/audio/{msg_id}")
@router.get("/api/tts/audio/{msg_id}")
async def tts_audio(msg_id: str):
    import re
    safe_id = re.sub(r'[^a-zA-Z0-9_\-]', '', msg_id)
    if not safe_id:
        return Response(status_code=404)
    cache_path = TTS_CACHE_DIR / f"{safe_id}.mp3"
    if not cache_path.exists():
        return Response(status_code=404)
    return FileResponse(cache_path, media_type="audio/mpeg", filename=f"{safe_id}.mp3")

@router.get("/api/tts/voices")
async def tts_voice_list():
    key = get_key("siliconflow")
    if not key:
        return {"voices": [], "error": "未配置硅基流动 API Key"}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                "https://api.siliconflow.cn/v1/audio/voice/list",
                headers={"Authorization": f"Bearer {key}"}
            )
        if resp.status_code != 200:
            return {"voices": [], "error": "获取音色列表失败"}
        data = resp.json()
        voices = data.get("result") or data.get("voices") or data.get("data") or []
        return {"voices": voices}
    except Exception as e:
        return {"voices": [], "error": str(e)}
