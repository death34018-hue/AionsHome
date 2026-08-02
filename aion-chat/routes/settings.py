"""
设置、世界书、模型列表、TTS 路由
"""

import json
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import Response, FileResponse
from pydantic import BaseModel, Field
from typing import Any, Dict, Optional

import httpx

from config import SETTINGS, save_settings, get_key, get_sentinel_config, load_worldbook, save_worldbook, load_chat_status, TTS_CACHE_DIR, TTS_CACHE_MAX_BYTES, THEATER_TTS_CACHE_DIR, normalize_custom_model_routes, refresh_custom_models, iter_visible_models
from tts import cleanup_tts_cache_dir
from ws import manager

router = APIRouter()

RELAY_MODEL_PROVIDERS = {"aipro", "custom_openai"}

# ── 模型列表 ──────────────────────────────────────
@router.get("/api/models")
async def list_models():
    rows = [
        {
            "key": k,
            "provider": v["provider"],
            "custom": v.get("provider") == "custom_openai",
            "route_name": v.get("route_name", ""),
        }
        for k, v in iter_visible_models()
    ]
    return sorted(rows, key=lambda item: 1 if item["provider"] in RELAY_MODEL_PROVIDERS else 0)

# ── 设置 ──────────────────────────────────────────
class SettingsUpdate(BaseModel):
    gemini_key: Optional[str] = None
    siliconflow_key: Optional[str] = None
    gemini_free_key: Optional[str] = None
    aipro_key: Optional[str] = None
    netease_music_u: Optional[str] = None
    sentinel_base_url: Optional[str] = None
    sentinel_api_key: Optional[str] = None
    sentinel_model: Optional[str] = None
    embedding_base_url: Optional[str] = None
    embedding_api_key: Optional[str] = None
    embedding_model: Optional[str] = None
    luckin_mcp_enabled: Optional[bool] = None
    luckin_mcp_token: Optional[str] = None
    luckin_default_longitude: Optional[str] = None
    luckin_default_latitude: Optional[str] = None
    luckin_default_shop_keyword: Optional[str] = None
    custom_model_routes: Optional[list[Dict[str, Any]]] = None

class HomeLayoutUpdate(BaseModel):
    version: Optional[int] = 2
    positions: Dict[str, Any] = Field(default_factory=dict)

def _normalize_home_layout(payload: Any) -> Dict[str, Any]:
    positions = payload.get("positions", {}) if isinstance(payload, dict) else {}
    normalized: Dict[str, int] = {}
    if isinstance(positions, dict):
        for app_id, cell in positions.items():
            if not isinstance(app_id, str):
                continue
            try:
                cell_index = int(cell)
            except (TypeError, ValueError):
                continue
            if 0 <= cell_index <= 4095:
                normalized[app_id] = cell_index
    return {"version": 2, "positions": normalized}

@router.get("/api/home/layout")
async def get_home_layout():
    return _normalize_home_layout(SETTINGS.get("home_layout", {}))

@router.put("/api/home/layout")
async def update_home_layout(body: HomeLayoutUpdate):
    payload = body.model_dump() if hasattr(body, "model_dump") else body.dict()
    layout = _normalize_home_layout(payload)
    SETTINGS["home_layout"] = layout
    save_settings(SETTINGS)
    return {"ok": True, "layout": layout}

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
        "sentinel_base_url": SETTINGS.get("sentinel_base_url", ""),
        "sentinel_api_key": SETTINGS.get("sentinel_api_key", ""),
        "sentinel_model": SETTINGS.get("sentinel_model", ""),
        "embedding_base_url": SETTINGS.get("embedding_base_url", ""),
        "embedding_api_key": SETTINGS.get("embedding_api_key", ""),
        "embedding_model": SETTINGS.get("embedding_model", ""),
        "luckin_mcp_enabled": SETTINGS.get("luckin_mcp_enabled", False),
        "luckin_mcp_token": SETTINGS.get("luckin_mcp_token", ""),
        "luckin_default_longitude": SETTINGS.get("luckin_default_longitude", ""),
        "luckin_default_latitude": SETTINGS.get("luckin_default_latitude", ""),
        "luckin_default_shop_keyword": SETTINGS.get("luckin_default_shop_keyword", ""),
        "custom_model_routes": normalize_custom_model_routes(SETTINGS.get("custom_model_routes")),
        "gemini_key_masked": mask(SETTINGS.get("gemini_key", "")),
        "siliconflow_key_masked": mask(SETTINGS.get("siliconflow_key", "")),
        "gemini_free_key_masked": mask(SETTINGS.get("gemini_free_key", "")),
        "aipro_key_masked": mask(SETTINGS.get("aipro_key", "")),
        "netease_music_u_masked": mask(SETTINGS.get("netease_music_u", "")),
        "sentinel_api_key_masked": mask(SETTINGS.get("sentinel_api_key", "")),
        "embedding_api_key_masked": mask(SETTINGS.get("embedding_api_key", "")),
    }

@router.put("/api/settings")
async def update_settings(body: SettingsUpdate):
    luckin_changed = False
    if body.gemini_key is not None:
        SETTINGS["gemini_key"] = body.gemini_key
    if body.siliconflow_key is not None:
        SETTINGS["siliconflow_key"] = body.siliconflow_key
    if body.gemini_free_key is not None:
        SETTINGS["gemini_free_key"] = body.gemini_free_key
    if body.aipro_key is not None:
        SETTINGS["aipro_key"] = body.aipro_key
    if body.sentinel_base_url is not None:
        SETTINGS["sentinel_base_url"] = body.sentinel_base_url
    if body.sentinel_api_key is not None:
        SETTINGS["sentinel_api_key"] = body.sentinel_api_key
    if body.sentinel_model is not None:
        SETTINGS["sentinel_model"] = body.sentinel_model
    if body.embedding_base_url is not None:
        SETTINGS["embedding_base_url"] = body.embedding_base_url
    if body.embedding_api_key is not None:
        SETTINGS["embedding_api_key"] = body.embedding_api_key
    if body.embedding_model is not None:
        SETTINGS["embedding_model"] = body.embedding_model
    if body.luckin_mcp_enabled is not None:
        luckin_changed = luckin_changed or SETTINGS.get("luckin_mcp_enabled") != body.luckin_mcp_enabled
        SETTINGS["luckin_mcp_enabled"] = body.luckin_mcp_enabled
    if body.luckin_mcp_token is not None:
        luckin_changed = luckin_changed or SETTINGS.get("luckin_mcp_token", "") != body.luckin_mcp_token
        SETTINGS["luckin_mcp_token"] = body.luckin_mcp_token
    if body.luckin_default_longitude is not None:
        SETTINGS["luckin_default_longitude"] = body.luckin_default_longitude
    if body.luckin_default_latitude is not None:
        SETTINGS["luckin_default_latitude"] = body.luckin_default_latitude
    if body.luckin_default_shop_keyword is not None:
        SETTINGS["luckin_default_shop_keyword"] = body.luckin_default_shop_keyword
    if body.custom_model_routes is not None:
        SETTINGS["custom_model_routes"] = normalize_custom_model_routes(body.custom_model_routes)
        refresh_custom_models()
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
    if luckin_changed:
        try:
            from luckin import LUCKIN_SERVER_NAME
            from mcp_client import mcp_manager
            await mcp_manager.disconnect(LUCKIN_SERVER_NAME)
        except Exception:
            pass
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

# ── AI 生图开关 ───────────────────────────────────
@router.get("/api/settings/image-gen")
async def get_image_gen_setting():
    return {"image_gen_enabled": SETTINGS.get("image_gen_enabled", False)}

class ImageGenToggle(BaseModel):
    enabled: bool

@router.put("/api/settings/image-gen")
async def update_image_gen_setting(body: ImageGenToggle):
    SETTINGS["image_gen_enabled"] = body.enabled
    save_settings(SETTINGS)
    return {"ok": True, "image_gen_enabled": body.enabled}

# ── CLI 工具调用开关（Gemini CLI / Antigravity CLI） ─────────────────
# ── AI song generation toggle ─────────────────────────────────
@router.get("/api/settings/song-gen")
async def get_song_gen_setting():
    return {"song_gen_enabled": SETTINGS.get("song_gen_enabled", False)}

class SongGenToggle(BaseModel):
    enabled: bool

@router.put("/api/settings/song-gen")
async def update_song_gen_setting(body: SongGenToggle):
    SETTINGS["song_gen_enabled"] = body.enabled
    save_settings(SETTINGS)
    return {"ok": True, "song_gen_enabled": body.enabled}

@router.get("/api/settings/gemini-cli-tools")
async def get_gemini_cli_tools_setting():
    return {"gemini_cli_tools_enabled": SETTINGS.get("gemini_cli_tools_enabled", False)}

class GeminiCliToolsToggle(BaseModel):
    enabled: bool

@router.put("/api/settings/gemini-cli-tools")
async def update_gemini_cli_tools_setting(body: GeminiCliToolsToggle):
    SETTINGS["gemini_cli_tools_enabled"] = body.enabled
    save_settings(SETTINGS)
    return {"ok": True, "gemini_cli_tools_enabled": body.enabled}

# ── 桌宠开关 ──────────────────────────────────────
@router.get("/api/settings/pet")
async def get_pet_setting():
    return {"pet_enabled": SETTINGS.get("pet_enabled", False)}

class PetToggle(BaseModel):
    enabled: bool

@router.put("/api/settings/pet")
async def update_pet_setting(body: PetToggle):
    SETTINGS["pet_enabled"] = body.enabled
    save_settings(SETTINGS)
    return {"ok": True, "pet_enabled": body.enabled}

# ── 健康数据分享开关 ──────────────────────────────
@router.get("/api/settings/health-share")
async def get_health_share_setting():
    return {"health_share_enabled": SETTINGS.get("health_share_enabled", False)}

class HealthShareToggle(BaseModel):
    enabled: bool

@router.put("/api/settings/health-share")
async def update_health_share_setting(body: HealthShareToggle):
    SETTINGS["health_share_enabled"] = body.enabled
    save_settings(SETTINGS)
    await manager.broadcast({
        "type": "health_share_changed",
        "data": {"health_share_enabled": body.enabled},
    })
    await manager.broadcast({
        "type": "capability_config_changed",
        "data": {"key": "health_context", "enabled": body.enabled},
    })
    return {"ok": True, "health_share_enabled": body.enabled}

# ── 世界书 ────────────────────────────────────────
class WorldBookUpdate(BaseModel):
    ai_persona: str = ""
    user_persona: str = ""
    system_prompt: str = ""
    system_prompt_enabled: bool = True
    ai_name: str = "AI"
    user_name: str = "你"
    persona_schema_version: int = 1
    ai_persona_sections: Dict[str, str] = Field(default_factory=dict)
    user_persona_sections: Dict[str, str] = Field(default_factory=dict)
    creative_rules: str = ""
    persona_section_locks: Dict[str, Any] = Field(default_factory=dict)
    persona_evolution_enabled: bool = False

@router.get("/api/worldbook")
async def get_worldbook():
    return load_worldbook()

@router.put("/api/worldbook")
async def update_worldbook(body: WorldBookUpdate):
    current = load_worldbook()
    payload = body.model_dump() if hasattr(body, "model_dump") else body.dict()
    current.update(payload)
    save_worldbook(current)
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
                cleanup_tts_cache_dir(TTS_CACHE_DIR, TTS_CACHE_MAX_BYTES, skip={cache_path})
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

@router.head("/api/theater/tts/audio/{msg_id}")
@router.get("/api/theater/tts/audio/{msg_id}")
async def theater_tts_audio(msg_id: str):
    import re
    safe_id = re.sub(r'[^a-zA-Z0-9_\-]', '', msg_id)
    if not safe_id:
        return Response(status_code=404)
    cache_path = THEATER_TTS_CACHE_DIR / f"{safe_id}.mp3"
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

# ── 微信桥接 ──────────────────────────────────────
@router.get("/api/settings/wechat-bridge")
async def get_wechat_bridge():
    openclaw_home = SETTINGS.get("openclaw_home", "")
    accounts_summary = []
    status_error = None
    try:
        from openclaw_weixin import load_accounts, resolve_openclaw_home
        home = resolve_openclaw_home(openclaw_home or None)
        accounts = load_accounts(home)
        for acc in accounts:
            accounts_summary.append({
                "account_id": acc.account_id,
                "user_id": acc.user_id,
                "has_token": bool(acc.token),
                "known_contacts": len(acc.raw.get("contacts", []) if isinstance(acc.raw, dict) else []),
            })
    except Exception as e:
        status_error = str(e)
    return {
        "wechat_bridge_enabled": SETTINGS.get("wechat_bridge_enabled", False),
        "wechat_bridge_transport": SETTINGS.get("wechat_bridge_transport", ""),
        "openclaw_home": openclaw_home or str(Path.home() / ".openclaw"),
        "openclaw_accounts": accounts_summary,
        "openclaw_status_error": status_error,
    }

class WeChatBridgeToggle(BaseModel):
    enabled: bool
    transport: str = "openclaw"
    openclaw_home: str = ""

@router.put("/api/settings/wechat-bridge")
async def update_wechat_bridge(body: WeChatBridgeToggle):
    SETTINGS["wechat_bridge_enabled"] = body.enabled
    SETTINGS["wechat_bridge_transport"] = body.transport
    if body.openclaw_home:
        SETTINGS["openclaw_home"] = body.openclaw_home
    save_settings(SETTINGS)
    return await get_wechat_bridge()

# ── 微信绑定码 ────────────────────────────────────
@router.post("/api/settings/wechat-bridge/bindings")
async def create_wechat_binding_code(source_type: str = "", source_id: str = ""):
    from wechat_bridge import create_wechat_binding
    code = create_wechat_binding(source_type=source_type or None, source_id=source_id or None)
    return {"code": code, "instruction": f"Send this in WeChat: bind {code}"}
