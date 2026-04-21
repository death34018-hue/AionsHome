"""
定位 API 路由：心跳上报、状态查询、配置管理、POI搜索
"""

import time
from fastapi import APIRouter
from pydantic import BaseModel
from typing import Optional

from location import (
    load_location_config, save_location_config,
    load_location_status, save_location_status,
    process_heartbeat,
    amap_poi_search, format_nearby_pois_for_prompt,
    is_location_quiet_hours,
)

router = APIRouter()


# ── 心跳上报 ──────────────────────────────────────
class HeartbeatBody(BaseModel):
    lng: float
    lat: float
    accuracy: float = 0.0
    is_gcj02: bool = False   # 默认 WGS84，Android GPS 原始数据
    force: bool = False      # 强制处理（即使未启用，如浏览器设家）

@router.post("/api/location/heartbeat")
async def location_heartbeat(body: HeartbeatBody):
    """接收手机端定位心跳"""
    cfg = load_location_config()
    if not cfg.get("enabled") and not body.force:
        return {"ok": False, "error": "定位功能未启用"}

    result = await process_heartbeat(body.lng, body.lat, body.accuracy, body.is_gcj02)
    return {"ok": True, **result}


# ── 立即同步（强制全量刷新）────────────────────────
@router.post("/api/location/force-sync")
async def location_force_sync():
    """用当前已有坐标强制做一次全量刷新（地理编码+天气+POI）"""
    cfg = load_location_config()
    if not cfg.get("enabled"):
        return {"ok": False, "error": "定位功能未启用"}
    status = load_location_status()
    if status.get("lng", 0) == 0 or status.get("lat", 0) == 0:
        return {"ok": False, "error": "当前位置未知，请先上报一次定位"}
    result = await process_heartbeat(
        status["lng"], status["lat"], status.get("accuracy", 0),
        is_gcj02=True, force_full=True
    )
    return {"ok": True, **result}


# ── 状态查询 ──────────────────────────────────────
@router.get("/api/location/status")
async def get_location_status():
    """查看当前位置状态"""
    status = load_location_status()
    cfg = load_location_config()
    return {
        "enabled": cfg.get("enabled", False),
        **status,
    }


# ── POI 查询 ─────────────────────────────────────
class PoiSearchBody(BaseModel):
    category: str = "餐饮美食"      # 类型名称
    radius: Optional[int] = None    # 覆盖默认半径

@router.post("/api/location/poi-search")
async def poi_search(body: PoiSearchBody):
    """手动触发 POI 搜索（刷新某个类型）"""
    cfg = load_location_config()
    amap_key = cfg.get("amap_key", "")
    if not amap_key:
        return {"ok": False, "error": "高德 API Key 未配置"}

    status = load_location_status()
    if status.get("state") == "unknown" or status.get("lng", 0) == 0:
        return {"ok": False, "error": "当前位置未知"}

    poi_types = cfg.get("poi_types", {})
    type_code = poi_types.get(body.category)
    if not type_code:
        return {"ok": False, "error": f"未知的 POI 类型: {body.category}", "available": list(poi_types.keys())}

    radius = body.radius or cfg.get("poi_radius", 2000)
    pois = await amap_poi_search(status["lng"], status["lat"], type_code, amap_key, radius)

    # 更新缓存
    status["nearby_pois"][body.category] = pois
    from location import save_location_status
    save_location_status(status)

    return {"ok": True, "category": body.category, "count": len(pois), "pois": pois}


# ── 获取缓存的 POI（供 Core 读取）────────────────
@router.get("/api/location/pois")
async def get_cached_pois():
    """获取缓存的周边 POI 数据"""
    status = load_location_status()
    return {
        "state": status.get("state", "unknown"),
        "address": status.get("address", ""),
        "nearby_pois": status.get("nearby_pois", {}),
        "prompt_text": format_nearby_pois_for_prompt(),
    }


# ── 配置管理 ──────────────────────────────────────
class LocationConfigUpdate(BaseModel):
    amap_key: Optional[str] = None
    home_lng: Optional[float] = None
    home_lat: Optional[float] = None
    home_threshold: Optional[int] = None
    heartbeat_outdoor_min: Optional[int] = None
    heartbeat_home_min: Optional[int] = None
    poi_radius: Optional[int] = None
    enabled: Optional[bool] = None
    quiet_hours_enabled: Optional[bool] = None
    quiet_hours_start: Optional[str] = None
    quiet_hours_end: Optional[str] = None

@router.get("/api/location/config")
async def get_location_config():
    cfg = load_location_config()
    # 脱敏 Key
    masked_key = ""
    k = cfg.get("amap_key", "")
    if k and len(k) >= 8:
        masked_key = k[:4] + "*" * (len(k) - 8) + k[-4:]
    return {
        **cfg,
        "amap_key": k,
        "amap_key_masked": masked_key,
        "active": cfg.get("enabled", False) and not is_location_quiet_hours(),
    }

@router.put("/api/location/config")
async def update_location_config(body: LocationConfigUpdate):
    cfg = load_location_config()
    if body.amap_key is not None:
        cfg["amap_key"] = body.amap_key
    if body.home_lng is not None:
        cfg["home_lng"] = body.home_lng
    if body.home_lat is not None:
        cfg["home_lat"] = body.home_lat
    if body.home_threshold is not None:
        cfg["home_threshold"] = max(50, body.home_threshold)
    if body.heartbeat_outdoor_min is not None:
        cfg["heartbeat_outdoor_min"] = max(1, body.heartbeat_outdoor_min)
    if body.heartbeat_home_min is not None:
        cfg["heartbeat_home_min"] = max(5, body.heartbeat_home_min)
    if body.poi_radius is not None:
        cfg["poi_radius"] = max(500, min(10000, body.poi_radius))
    if body.enabled is not None:
        cfg["enabled"] = body.enabled
    if body.quiet_hours_enabled is not None:
        cfg["quiet_hours_enabled"] = body.quiet_hours_enabled
    if body.quiet_hours_start is not None:
        cfg["quiet_hours_start"] = body.quiet_hours_start
    if body.quiet_hours_end is not None:
        cfg["quiet_hours_end"] = body.quiet_hours_end
    save_location_config(cfg)
    return {"ok": True}


# ── 设置家的位置（快捷接口：用当前位置设为家）─────
@router.post("/api/location/set-home")
async def set_home_location():
    """将当前位置设为家的位置"""
    status = load_location_status()
    if status.get("lng", 0) == 0 or status.get("lat", 0) == 0:
        return {"ok": False, "error": "当前位置未知，请先上报一次定位"}
    cfg = load_location_config()
    cfg["home_lng"] = status["lng"]
    cfg["home_lat"] = status["lat"]
    save_location_config(cfg)
    return {
        "ok": True,
        "home_lng": status["lng"],
        "home_lat": status["lat"],
        "address": status.get("address", ""),
    }
