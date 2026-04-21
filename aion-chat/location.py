"""
高德地图定位模块：坐标转换、距离计算、高德 API 调用、状态机管理、哨兵联动
"""

import json, time, math, asyncio, re
from pathlib import Path

import httpx, aiosqlite

from config import (
    DATA_DIR, SETTINGS, get_key,
    load_worldbook, load_chat_status, save_chat_status,
)
from database import get_db
from ws import manager

# ── 文件路径 ──────────────────────────────────────
LOCATION_CONFIG_PATH = DATA_DIR / "location_config.json"
LOCATION_STATUS_PATH = DATA_DIR / "location_status.json"

# ── 默认配置 ──────────────────────────────────────
DEFAULT_LOCATION_CONFIG = {
    "amap_key": "",                   # 高德 Web 服务 API Key
    "home_lng": 0.0,                  # 家的经度 (GCJ-02)
    "home_lat": 0.0,                  # 家的纬度 (GCJ-02)
    "home_threshold": 500,            # 离家阈值（米）
    "heartbeat_outdoor_min": 10,      # 外出时心跳间隔（分钟）
    "heartbeat_home_min": 10,         # 在家时心跳间隔（分钟）
    "poi_types": {                    # POI 搜索类型
        "餐饮美食": "050000",
        "风景名胜": "110000",
        "休闲娱乐": "100000",
        "购物": "060000",
    },
    "poi_radius": 2000,               # POI 搜索半径（米）
    "movement_threshold": 500,        # 外出时"显著移动"判定距离（米）
    "enabled": False,                 # 定位功能总开关
    "quiet_hours_enabled": False,     # 静默时段开关
    "quiet_hours_start": "00:00",     # 静默开始
    "quiet_hours_end": "08:00",       # 静默结束
}


def load_location_config() -> dict:
    if LOCATION_CONFIG_PATH.exists():
        try:
            cfg = json.loads(LOCATION_CONFIG_PATH.read_text(encoding="utf-8"))
            for k, v in DEFAULT_LOCATION_CONFIG.items():
                cfg.setdefault(k, v)
            return cfg
        except Exception:
            pass
    return dict(DEFAULT_LOCATION_CONFIG)


def save_location_config(cfg: dict):
    LOCATION_CONFIG_PATH.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# ── 位置状态 ──────────────────────────────────────
DEFAULT_LOCATION_STATUS = {
    "state": "unknown",        # unknown / at_home / outside
    "lng": 0.0,
    "lat": 0.0,
    "accuracy": 0.0,
    "address": "",
    "adcode": "",
    "weather": {},             # 实况天气
    "forecast": [],            # 天气预报
    "nearby_pois": {},         # {类型名: [poi...]}
    "updated_at": 0,
    "state_changed_at": 0,
    "distance_from_home": 0,
}


def load_location_status() -> dict:
    if LOCATION_STATUS_PATH.exists():
        try:
            data = json.loads(LOCATION_STATUS_PATH.read_text(encoding="utf-8"))
            for k, v in DEFAULT_LOCATION_STATUS.items():
                data.setdefault(k, v)
            return data
        except Exception:
            pass
    return dict(DEFAULT_LOCATION_STATUS)


def save_location_status(data: dict):
    LOCATION_STATUS_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# ── WGS84 → GCJ-02 坐标转换 ─────────────────────
PI = math.pi
_a = 6378245.0
_ee = 0.00669342162296594323


def _out_of_china(lng: float, lat: float) -> bool:
    return lng < 72.004 or lng > 137.8347 or lat < 0.8293 or lat > 55.8271


def _transform_lat(x: float, y: float) -> float:
    ret = -100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * math.sqrt(abs(x))
    ret += (20.0 * math.sin(6.0 * x * PI) + 20.0 * math.sin(2.0 * x * PI)) * 2.0 / 3.0
    ret += (20.0 * math.sin(y * PI) + 40.0 * math.sin(y / 3.0 * PI)) * 2.0 / 3.0
    ret += (160.0 * math.sin(y / 12.0 * PI) + 320 * math.sin(y * PI / 30.0)) * 2.0 / 3.0
    return ret


def _transform_lng(x: float, y: float) -> float:
    ret = 300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * math.sqrt(abs(x))
    ret += (20.0 * math.sin(6.0 * x * PI) + 20.0 * math.sin(2.0 * x * PI)) * 2.0 / 3.0
    ret += (20.0 * math.sin(x * PI) + 40.0 * math.sin(x / 3.0 * PI)) * 2.0 / 3.0
    ret += (150.0 * math.sin(x / 12.0 * PI) + 300.0 * math.sin(x / 30.0 * PI)) * 2.0 / 3.0
    return ret


def wgs84_to_gcj02(wgs_lng: float, wgs_lat: float) -> tuple[float, float]:
    if _out_of_china(wgs_lng, wgs_lat):
        return wgs_lng, wgs_lat
    dlat = _transform_lat(wgs_lng - 105.0, wgs_lat - 35.0)
    dlng = _transform_lng(wgs_lng - 105.0, wgs_lat - 35.0)
    radlat = wgs_lat / 180.0 * PI
    magic = math.sin(radlat)
    magic = 1 - _ee * magic * magic
    sqrtmagic = math.sqrt(magic)
    dlat = (dlat * 180.0) / ((_a * (1 - _ee)) / (magic * sqrtmagic) * PI)
    dlng = (dlng * 180.0) / (_a / sqrtmagic * math.cos(radlat) * PI)
    return wgs_lng + dlng, wgs_lat + dlat


# ── Haversine 距离计算（米） ──────────────────────
def haversine(lng1: float, lat1: float, lng2: float, lat2: float) -> float:
    R = 6371000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


# ── 高德 API 调用 ────────────────────────────────
async def amap_regeo(lng: float, lat: float, key: str) -> dict | None:
    """逆地理编码：坐标 → 地址 + adcode"""
    url = "https://restapi.amap.com/v3/geocode/regeo"
    params = {"key": key, "location": f"{lng},{lat}", "extensions": "base"}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, params=params)
            data = resp.json()
            if data.get("status") == "1" and data.get("regeocode"):
                rc = data["regeocode"]
                return {
                    "address": rc.get("formatted_address", ""),
                    "adcode": rc.get("addressComponent", {}).get("adcode", ""),
                    "province": rc.get("addressComponent", {}).get("province", ""),
                    "city": rc.get("addressComponent", {}).get("city", ""),
                    "district": rc.get("addressComponent", {}).get("district", ""),
                }
    except Exception as e:
        print(f"[Location] 逆地理编码失败: {e}")
    return None


async def amap_weather(adcode: str, key: str) -> dict:
    """天气查询：实况 + 预报"""
    result = {"live": {}, "forecast": []}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            live_resp = await client.get(
                "https://restapi.amap.com/v3/weather/weatherInfo",
                params={"key": key, "city": adcode, "extensions": "base"},
            )
            live_data = live_resp.json()
            if live_data.get("status") == "1" and live_data.get("lives"):
                result["live"] = live_data["lives"][0]

            fc_resp = await client.get(
                "https://restapi.amap.com/v3/weather/weatherInfo",
                params={"key": key, "city": adcode, "extensions": "all"},
            )
            fc_data = fc_resp.json()
            if fc_data.get("status") == "1" and fc_data.get("forecasts"):
                result["forecast"] = fc_data["forecasts"][0].get("casts", [])
    except Exception as e:
        print(f"[Location] 天气查询失败: {e}")
    return result


async def amap_poi_search(lng: float, lat: float, types: str, key: str, radius: int = 2000) -> list:
    """周边 POI 搜索"""
    url = "https://restapi.amap.com/v3/place/around"
    params = {
        "key": key,
        "location": f"{lng},{lat}",
        "types": types,
        "radius": radius,
        "offset": 10,
        "page": 1,
        "extensions": "all",
        "sortrule": "distance",
    }
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, params=params)
            data = resp.json()
            if data.get("status") == "1":
                pois = data.get("pois", [])
                # 只保留关键字段，减少存储体积
                return [
                    {
                        "name": p.get("name", ""),
                        "type": p.get("type", ""),
                        "address": p.get("address", ""),
                        "distance": p.get("distance", ""),
                        "tel": p.get("tel", "") if p.get("tel") != "[]" else "",
                        "rating": (p.get("biz_ext") or {}).get("rating", ""),
                        "cost": (p.get("biz_ext") or {}).get("cost", ""),
                        "location": p.get("location", ""),
                        "photos": [ph.get("url", "") for ph in (p.get("photos") or []) if ph.get("url")][:1],
                    }
                    for p in pois
                ]
    except Exception as e:
        print(f"[Location] POI搜索失败: {e}")
    return []


# ── 位置信息格式化（供 prompt 注入） ───────────────
def format_location_for_prompt() -> str:
    """格式化当前位置状态，供哨兵/Core prompt 使用"""
    status = load_location_status()
    if status.get("state") == "unknown" or status.get("updated_at", 0) == 0:
        return ""

    cfg = load_location_config()
    if not cfg.get("enabled"):
        return ""

    lines = []
    state_label = {"at_home": "在家", "outside": "外出中"}.get(status["state"], "未知")
    lines.append(f"当前位置状态：{state_label}")

    if status.get("address"):
        lines.append(f"当前位置：{status['address']}")

    if status.get("distance_from_home", 0) > 0:
        d = status["distance_from_home"]
        d_str = f"{d / 1000:.1f}km" if d >= 1000 else f"{int(d)}m"
        lines.append(f"距离家：{d_str}")

    w = status.get("weather", {})
    if w:
        weather_text = f"天气：{w.get('weather', '')} {w.get('temperature', '')}°C"
        if w.get("humidity"):
            weather_text += f" 湿度{w['humidity']}%"
        if w.get("winddirection"):
            weather_text += f" {w['winddirection']}风{w.get('windpower', '')}级"
        lines.append(weather_text)

    if status.get("updated_at"):
        lines.append(f"位置更新时间：{time.strftime('%H:%M:%S', time.localtime(status['updated_at']))}")

    return "\n".join(lines)


def format_nearby_pois_for_prompt() -> str:
    """格式化周边 POI 数据，供 Core 回答用户提问时使用"""
    status = load_location_status()
    pois = status.get("nearby_pois", {})
    if not pois:
        return ""

    lines = ["以下是用户当前位置周边的信息："]
    for category, items in pois.items():
        if not items:
            continue
        lines.append(f"\n【{category}】")
        for p in items[:8]:
            entry = f"  - {p['name']}"
            if p.get("distance"):
                d = int(p["distance"])
                entry += f"（{d}m）"
            if p.get("rating") and p["rating"] != "[]":
                entry += f" ⭐{p['rating']}"
            if p.get("cost") and p["cost"] != "[]":
                entry += f" 人均¥{p['cost']}"
            if p.get("address") and p["address"] != "[]":
                entry += f" | {p['address']}"
            lines.append(entry)

    return "\n".join(lines)


# ── 静默时段检查 ─────────────────────────────────
def is_location_quiet_hours() -> bool:
    """检查当前是否处于定位静默时段"""
    cfg = load_location_config()
    if not cfg.get("quiet_hours_enabled", False):
        return False
    start_str = cfg.get("quiet_hours_start", "00:00")
    end_str = cfg.get("quiet_hours_end", "08:00")
    try:
        sh, sm = map(int, start_str.split(":"))
        eh, em = map(int, end_str.split(":"))
    except (ValueError, AttributeError):
        return False
    now = time.localtime()
    cur = now.tm_hour * 60 + now.tm_min
    start = sh * 60 + sm
    end = eh * 60 + em
    if start <= end:
        return start <= cur < end
    else:  # 跨午夜
        return cur >= start or cur < end


# ── 心跳处理核心逻辑 ─────────────────────────────
async def process_heartbeat(lng: float, lat: float, accuracy: float = 0.0, is_gcj02: bool = False, skip_sentinel: bool = False, force_full: bool = False) -> dict:
    """
    处理一次定位心跳。
    lng/lat: 坐标（默认 WGS84，is_gcj02=True 时为 GCJ-02）
    force_full: 强制全量刷新（地理编码+天气+POI）
    返回处理结果摘要。
    """
    cfg = load_location_config()
    amap_key = cfg.get("amap_key", "")

    # 坐标转换（即使没有 API Key 也先做，以便保存坐标）
    if not is_gcj02:
        gcj_lng, gcj_lat = wgs84_to_gcj02(lng, lat)
    else:
        gcj_lng, gcj_lat = lng, lat

    if not amap_key:
        # 没有高德 Key 时，只保存坐标（set-home 用）
        old_status = load_location_status()
        old_status["lng"] = round(gcj_lng, 6)
        old_status["lat"] = round(gcj_lat, 6)
        old_status["accuracy"] = accuracy
        old_status["updated_at"] = time.time()
        save_location_status(old_status)
        return {"state": old_status.get("state", "unknown"), "error": "高德 API Key 未配置，仅保存坐标"}

    # 静默时段：只更新坐标，不调 API、不触发哨兵
    if is_location_quiet_hours():
        old_status = load_location_status()
        old_status["lng"] = round(gcj_lng, 6)
        old_status["lat"] = round(gcj_lat, 6)
        old_status["accuracy"] = accuracy
        old_status["updated_at"] = time.time()
        save_location_status(old_status)
        print("[Location] 当前处于静默时段，仅保存坐标")
        return {"skipped": True, "reason": "quiet_hours", "state": old_status.get("state", "unknown")}

    old_status = load_location_status()
    old_state = old_status.get("state", "unknown")

    # ── 1. 计算与家的距离 → 判断状态 ──
    home_lng = cfg.get("home_lng", 0)
    home_lat = cfg.get("home_lat", 0)
    home_not_set = (home_lng == 0 and home_lat == 0)
    if home_not_set:
        distance_home = -1
    else:
        distance_home = haversine(gcj_lng, gcj_lat, home_lng, home_lat)

    threshold = cfg.get("home_threshold", 500)
    if home_not_set:
        new_state = old_state if old_state != "unknown" else "unknown"
    elif distance_home <= threshold:
        new_state = "at_home"
    else:
        new_state = "outside"

    state_changed = (old_state != new_state
                     and old_state != "unknown"
                     and new_state != "unknown")

    # ── 2. 计算与上次 API 调用位置的距离 → 判断是否显著移动 ──
    last_api_lng = old_status.get("last_api_lng", 0)
    last_api_lat = old_status.get("last_api_lat", 0)
    movement_threshold = cfg.get("movement_threshold", 500)
    if last_api_lng and last_api_lat:
        moved_distance = haversine(gcj_lng, gcj_lat, last_api_lng, last_api_lat)
    else:
        moved_distance = float('inf')  # 首次一定做全量

    significant_move = moved_distance >= movement_threshold

    # ── 3. 天气过期检查（30分钟刷新） ──
    WEATHER_STALE_SECONDS = 30 * 60  # 30 分钟
    last_weather = old_status.get("weather", {})
    last_weather_time = last_weather.get("reporttime", "")
    weather_stale = True
    if last_weather_time:
        try:
            wt = time.mktime(time.strptime(last_weather_time, "%Y-%m-%d %H:%M:%S"))
            weather_stale = (time.time() - wt) > WEATHER_STALE_SECONDS
        except Exception:
            weather_stale = True
    else:
        weather_stale = bool(old_status.get("adcode"))

    # ── 4. 三级处理逻辑 ──
    #   级别1（轻量）: 在家没动 / 外出没动  → 只存坐标，零 API（天气过期时仅刷新天气）
    #   级别2（刷新）: 外出且显著移动        → 地理编码+天气+POI
    #   级别3（全量）: 状态变化(出门/回家)   → 刷新 + 哨兵通知
    need_full_api = state_changed or significant_move or force_full

    now = time.time()

    if need_full_api:
        # ── 刷新级/全量级：调用高德 API ──
        geo_info = await amap_regeo(gcj_lng, gcj_lat, amap_key)
        address = geo_info["address"] if geo_info else ""
        adcode = geo_info["adcode"] if geo_info else old_status.get("adcode", "")

        weather_data = {"live": {}, "forecast": []}
        if adcode:
            weather_data = await amap_weather(adcode, amap_key)

        nearby_pois = old_status.get("nearby_pois", {})
        if new_state == "outside":
            poi_types = cfg.get("poi_types", DEFAULT_LOCATION_CONFIG["poi_types"])
            poi_radius = cfg.get("poi_radius", 2000)
            nearby_pois = {}
            for label, type_code in poi_types.items():
                pois = await amap_poi_search(gcj_lng, gcj_lat, type_code, amap_key, poi_radius)
                nearby_pois[label] = pois
        # 回家时保留上次外出的 POI 缓存，方便用户查看；下次外出会覆盖刷新

        api_lng = round(gcj_lng, 6)
        api_lat = round(gcj_lat, 6)
        print(f"[Location] 全量/刷新处理: moved={moved_distance:.0f}m, state_changed={state_changed}")
    else:
        # ── 轻量级：复用上次数据 ──
        address = old_status.get("address", "")
        adcode = old_status.get("adcode", "")
        weather_data = {"live": old_status.get("weather", {}), "forecast": old_status.get("forecast", [])}
        nearby_pois = old_status.get("nearby_pois", {})
        api_lng = last_api_lng
        api_lat = last_api_lat

        # 天气过期时单独刷新天气（不触发地理编码/POI）
        if weather_stale and adcode and amap_key:
            fresh_weather = await amap_weather(adcode, amap_key)
            if fresh_weather.get("live"):
                weather_data = fresh_weather
                print(f"[Location] 轻量处理: 天气已过期，刷新天气数据")
            else:
                print(f"[Location] 轻量处理: 天气刷新失败，继续使用缓存")
        else:
            print(f"[Location] 轻量处理: moved={moved_distance:.0f}m, 复用缓存数据")

    new_status = {
        "state": new_state,
        "lng": round(gcj_lng, 6),
        "lat": round(gcj_lat, 6),
        "accuracy": accuracy,
        "address": address,
        "adcode": adcode,
        "weather": weather_data.get("live", {}),
        "forecast": weather_data.get("forecast", []),
        "nearby_pois": nearby_pois,
        "updated_at": now,
        "state_changed_at": old_status.get("state_changed_at", now) if not state_changed else now,
        "distance_from_home": round(distance_home, 1) if distance_home >= 0 else -1,
        "last_api_lng": api_lng,
        "last_api_lat": api_lat,
    }
    save_location_status(new_status)

    # WebSocket 广播（轻量级也广播坐标更新）
    await manager.broadcast({
        "type": "location_update",
        "data": {
            "state": new_state,
            "lng": gcj_lng,
            "lat": gcj_lat,
            "accuracy": accuracy,
            "address": address,
            "distance_from_home": new_status["distance_from_home"],
            "weather": weather_data.get("live", {}),
            "nearby_pois": nearby_pois if nearby_pois else None,
            "updated_at": now,
            "state_changed": state_changed,
        }
    })

    result = {
        "state": new_state,
        "old_state": old_state,
        "state_changed": state_changed,
        "address": address,
        "distance_from_home": new_status["distance_from_home"],
        "home_not_set": home_not_set,
        "full_api": need_full_api,
        "moved_distance": round(moved_distance, 1) if moved_distance != float('inf') else -1,
    }

    # 全量级：状态变化 → 通知哨兵 + 更新 chat_status
    if state_changed and not skip_sentinel:
        await _on_state_change(old_state, new_state, new_status, cfg)
    # 刷新级：显著移动但未变状态 → 只更新 chat_status（不叫哨兵）
    elif need_full_api and new_state == "outside":
        await _update_chat_status_location(new_status)

    print(f"[Location] 心跳完成: state={new_state}, full_api={need_full_api}, dist_home={new_status['distance_from_home']}m, moved={moved_distance:.0f}m")
    return result


async def _update_chat_status_location(status: dict):
    """更新 chat_status 中的位置信息（非状态变化时也调用，保持位置实时）"""
    old_cs = load_chat_status()
    old_text = old_cs.get("status", "")

    # 构建位置行
    state_label = {"at_home": "在家", "outside": "外出中"}.get(status["state"], "")
    loc_line = f"[位置] {state_label}"
    if status.get("address"):
        loc_line += f"，当前在：{status['address']}"
    if status.get("distance_from_home", 0) > 0:
        d = status["distance_from_home"]
        d_str = f"{d / 1000:.1f}km" if d >= 1000 else f"{int(d)}m"
        loc_line += f"，距离家{d_str}"

    # 天气行
    w = status.get("weather", {})
    weather_line = ""
    if w:
        weather_line = f"[天气] {w.get('weather', '')} {w.get('temperature', '')}°C"
        if w.get("humidity"):
            weather_line += f" 湿度{w['humidity']}%"

    # 替换或追加位置/天气信息
    lines = old_text.split("\n") if old_text else []
    new_lines = []
    loc_found = False
    weather_found = False
    for line in lines:
        if line.startswith("[位置]"):
            new_lines.append(loc_line)
            loc_found = True
        elif line.startswith("[天气]"):
            if weather_line:
                new_lines.append(weather_line)
            weather_found = True
        else:
            new_lines.append(line)
    if not loc_found:
        new_lines.append(loc_line)
    if not weather_found and weather_line:
        new_lines.append(weather_line)

    save_chat_status("\n".join(new_lines))


async def _on_state_change(old_state: str, new_state: str, status: dict, cfg: dict):
    """位置状态发生变化时：更新 chat_status + 通知哨兵"""
    wb = load_worldbook()
    user_name = wb.get("user_name", "你")
    ai_name = wb.get("ai_name", "AI")
    now_str = time.strftime("%Y年%m月%d日 %H:%M:%S")

    # 1. 更新 chat_status
    old_cs = load_chat_status()
    old_text = old_cs.get("status", "")

    if new_state == "outside":
        event_desc = f"{user_name}离开家外出了"
        if status.get("address"):
            event_desc += f"，当前位置：{status['address']}"
    else:
        event_desc = f"{user_name}回到家了"

    state_label = {"at_home": "在家", "outside": "外出中"}.get(new_state, "")
    loc_line = f"[位置] {state_label}"
    if status.get("address"):
        loc_line += f"，当前在：{status['address']}"
    if status.get("distance_from_home", 0) > 0:
        d = status["distance_from_home"]
        d_str = f"{d / 1000:.1f}km" if d >= 1000 else f"{int(d)}m"
        loc_line += f"，距离家{d_str}"

    w = status.get("weather", {})
    weather_line = ""
    if w:
        weather_line = f"[天气] {w.get('weather', '')} {w.get('temperature', '')}°C"
        if w.get("humidity"):
            weather_line += f" 湿度{w['humidity']}%"

    lines = old_text.split("\n") if old_text else []
    new_lines = []
    loc_found = False
    weather_found = False
    for line in lines:
        if line.startswith("[位置]"):
            new_lines.append(loc_line)
            loc_found = True
        elif line.startswith("[天气]"):
            if weather_line:
                new_lines.append(weather_line)
            weather_found = True
        else:
            new_lines.append(line)
    if not loc_found:
        new_lines.append(loc_line)
    if not weather_found and weather_line:
        new_lines.append(weather_line)

    save_chat_status("\n".join(new_lines))

    # 2. 通知哨兵模型
    await _notify_sentinel(old_state, new_state, status, event_desc)


async def _notify_sentinel(old_state: str, new_state: str, status: dict, event_desc: str):
    """通知哨兵分析位置变化，决定要不要唤醒 Core"""
    from camera import read_logs_since, append_monitor_log, async_get_last_user_msg_time

    wb = load_worldbook()
    user_name = wb.get("user_name", "你")
    ai_name = wb.get("ai_name", "AI")
    now_str = time.strftime("%Y年%m月%d日 %H:%M:%S")

    gemini_key = get_key("gemini_free")
    if not gemini_key:
        print("[Location] Gemini API Key 未配置，跳过哨兵通知")
        return

    last_user_ts = await async_get_last_user_msg_time()
    last_user_time_str = (
        time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(last_user_ts))
        if last_user_ts > 0 else "未知"
    )

    chat_status_data = load_chat_status()
    chat_status_text = chat_status_data.get("status", "")

    recent_logs = read_logs_since(time.time() - 3600 * 6)
    log_history = ""
    if recent_logs:
        log_lines = [f"[{e.get('time', '')}] {e.get('monitoringlog', '')}" for e in recent_logs[-10:]]
        log_history = "\n".join(log_lines)

    # 最近聊天记录
    recent_chat_text = ""
    try:
        async with get_db() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT id FROM conversations ORDER BY updated_at DESC LIMIT 1")
            conv = await cur.fetchone()
            if conv:
                cur2 = await db.execute(
                    "SELECT role, content FROM messages WHERE conv_id=? AND role IN ('user','assistant') ORDER BY created_at DESC LIMIT 10",
                    (conv["id"],)
                )
                rows = await cur2.fetchall()
                if rows:
                    lines = []
                    for r in reversed(rows):
                        name = user_name if r["role"] == "user" else ai_name
                        text = r["content"][:200] + "..." if len(r["content"]) > 200 else r["content"]
                        lines.append(f"{name}: {text}")
                    recent_chat_text = "\n".join(lines)
    except Exception:
        pass

    # 位置和天气信息
    loc_info = format_location_for_prompt()
    state_desc = "从家里出门了" if new_state == "outside" else "从外面回到家了"

    prompt = f"""你是{user_name}的位置监控哨兵。也是{user_name}的爱人伴侣。检测到{user_name}的位置状态发生了变化：{state_desc}

当前时间：{now_str}
事件描述：{event_desc}
{loc_info}

{user_name}最后一次聊天时间：{last_user_time_str}
{user_name}当前状态：{chat_status_text if chat_status_text else "（暂无）"}

最近的聊天记录：
{recent_chat_text if recent_chat_text else "（暂无）"}

最近的监控日志：
{log_history if log_history else "（暂无）"}

请判断是否需要唤醒Core核心模型主动联系{user_name}。
判断依据：
- 如果聊天上下文或状态中已经提到了出门/回家的事，{ai_name}已经知道了，则不需要唤醒
- 如果{user_name}出门了但对话中没有提到这件事（{ai_name}还不知道），则需要唤醒
- 如果{user_name}回家了而之前状态显示在外面，且{ai_name}还不知道回家的事，则需要唤醒
- 其他你认为需要主动联系的情况

请严格按照以下JSON格式回复，不要包含其他内容：
{{"monitoringlog":"位置变化事件记录，例如：检测到{user_name}离开了家，当前位于XX。","call_core":false,"core_reason":""}}"""

    sentinel_model = "gemini-3.1-flash-lite-preview"
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{sentinel_model}:generateContent?key={gemini_key}"
    contents = [{"role": "user", "parts": [{"text": prompt}]}]
    payload = {"contents": contents}

    monitoring_log = event_desc
    call_core = False
    core_reason = ""

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()
            raw_text = data["candidates"][0]["content"]["parts"][0]["text"]

        cleaned = raw_text.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```\w*\n?", "", cleaned)
            cleaned = re.sub(r"\n?```$", "", cleaned)
            cleaned = cleaned.strip()
        parsed = json.loads(cleaned)
        monitoring_log = parsed.get("monitoringlog", event_desc)
        call_core = bool(parsed.get("call_core", False))
        core_reason = parsed.get("core_reason", "")
    except Exception as e:
        print(f"[Location] 哨兵分析失败: {e}")
        # 分析失败时默认通知 Core（保守策略）
        call_core = True
        core_reason = f"位置变化哨兵分析失败，默认唤醒Core通知：{event_desc}"

    print(f"[Location] 哨兵判断: call_core={call_core}, reason={core_reason}")

    # 写入监控日志
    now = time.time()
    log_entry = {
        "timestamp": now,
        "time": time.strftime("%H:%M:%S", time.localtime(now)),
        "date": time.strftime("%Y-%m-%d", time.localtime(now)),
        "monitoringlog": f"📍 {monitoring_log}",
        "summary": "",
        "call_core": call_core,
        "core_reason": core_reason,
        "screenshot": "",
        "source": "location",
    }
    append_monitor_log(log_entry)
    await manager.broadcast({"type": "monitor_log", "data": log_entry})

    if call_core:
        await _call_core_location(event_desc, status, core_reason, recent_logs)


async def _call_core_location(event_desc: str, status: dict, core_reason: str, cached_logs: list = None):
    """唤醒 Core 通知位置变化"""
    from camera import read_logs_since, append_monitor_log, async_get_last_user_msg_time
    from ai_providers import stream_ai
    from tts import TTSStreamer
    from memory import recall_memories

    wb = load_worldbook()
    user_name = wb.get("user_name", "你")
    ai_name = wb.get("ai_name", "AI")

    last_user_ts = await async_get_last_user_msg_time()
    if last_user_ts > 0:
        elapsed = time.time() - last_user_ts
        hours = int(elapsed // 3600)
        minutes = int((elapsed % 3600) // 60)
        time_ago = f"{hours}小时{minutes}分钟" if hours > 0 else f"{minutes}分钟"
    else:
        time_ago = "很长时间"

    if cached_logs is not None:
        recent_logs = cached_logs[-10:]
    else:
        recent_logs = read_logs_since(last_user_ts if last_user_ts > 0 else time.time() - 3600 * 6)
        recent_logs = recent_logs[-10:]
    recent_detail = "\n".join([f"[{e.get('time', '')}] {e.get('monitoringlog', '')}" for e in recent_logs[-5:]])

    loc_info = format_location_for_prompt()

    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM conversations ORDER BY updated_at DESC LIMIT 1")
        conv = await cur.fetchone()
        if not conv:
            return
        conv_id = conv["id"]
        model_key = conv["model"] or "gemini-3-flash"

        cur = await db.execute(
            "SELECT role, content, attachments FROM messages WHERE conv_id=? AND role IN ('user','assistant') ORDER BY created_at DESC LIMIT 20",
            (conv_id,)
        )
        rows = await cur.fetchall()
        history = []
        for r in reversed(rows):
            d = dict(r)
            d["attachments"] = []
            history.append(d)

    prefix = []
    if wb.get("ai_persona"):
        prefix.append({"role": "user", "content": f"[系统设定 - AI人设]\n{wb['ai_persona']}"})
        prefix.append({"role": "assistant", "content": "收到，我会按照设定扮演角色。"})
    if wb.get("user_persona"):
        prefix.append({"role": "user", "content": f"[系统设定 - 用户信息]\n{wb['user_persona']}"})
        prefix.append({"role": "assistant", "content": "收到，我会记住你的信息。"})

    core_parts = [f"【位置变化通知】{event_desc}"]
    if core_reason:
        core_parts.append(f"哨兵唤醒原因：{core_reason}")
    core_parts.append(f"\n{loc_info}")
    if recent_detail:
        core_parts.append(f"\n最近的监控记录：\n{recent_detail}")
    core_parts.append(f"\n请自然地根据位置变化和{user_name}互动。{user_name}已经{time_ago}没有和你说话了。")

    core_prompt = "\n".join(core_parts)

    recall_query = core_prompt[:300]
    recalled, _ = await recall_memories(recall_query)
    mem_inject = []
    if recalled:
        mem_lines = "\n".join([f"- {m['content']}" for m in recalled])
        mem_inject = [
            {"role": "user", "content": f"[相关记忆]\n你脑海中与当前话题相关的记忆：\n{mem_lines}"},
            {"role": "assistant", "content": "收到，我会自然地参考这些记忆。"},
        ]

    messages = prefix + mem_inject + history + [{"role": "user", "content": core_prompt}]

    # 预生成 msg_id + TTS
    core_msg_id = f"msg_{int(time.time() * 1000)}_lr"
    loc_tts = None
    if manager.any_tts_enabled():
        tts_voice = manager.get_tts_voice()
        if tts_voice:
            loc_tts = TTSStreamer(core_msg_id, tts_voice, manager)

    full_text = ""
    try:
        _temp = SETTINGS.get("temperature")
        async for chunk in stream_ai(messages, model_key, temperature=_temp):
            full_text += chunk
            if loc_tts:
                loc_tts.feed(chunk)
    except Exception as e:
        full_text = f"[Core 回复失败] {e}"

    if not full_text.strip():
        return

    now = time.time()
    trigger_msg_id = f"msg_{int(now * 1000)}_lt"
    async with get_db() as db:
        await db.execute(
            "INSERT INTO messages (id, conv_id, role, content, created_at, attachments) VALUES (?,?,?,?,?,?)",
            (trigger_msg_id, conv_id, "cam_trigger", core_prompt, now, "[]"),
        )
        sys_now = time.time()
        sys_msg_id = f"msg_{int(sys_now * 1000)}_ls"
        sys_content = f"检测到{user_name}的位置发生变化，拉响警报！"
        await db.execute(
            "INSERT INTO messages (id, conv_id, role, content, created_at, attachments) VALUES (?,?,?,?,?,?)",
            (sys_msg_id, conv_id, "system", sys_content, sys_now, "[]"),
        )
        await db.commit()
    sys_msg = {
        "id": sys_msg_id, "conv_id": conv_id, "role": "system",
        "content": sys_content, "created_at": sys_now, "attachments": [],
    }
    await manager.broadcast({"type": "msg_created", "data": sys_msg})

    async with get_db() as db:
        now2 = time.time()
        await db.execute(
            "INSERT INTO messages (id, conv_id, role, content, created_at, attachments) VALUES (?,?,?,?,?,?)",
            (core_msg_id, conv_id, "assistant", full_text, now2, "[]"),
        )
        await db.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now2, conv_id))
        await db.commit()

    core_msg = {
        "id": core_msg_id, "conv_id": conv_id, "role": "assistant",
        "content": full_text, "created_at": now2, "attachments": [],
    }
    await manager.broadcast({"type": "msg_created", "data": core_msg})

    if loc_tts:
        try:
            await loc_tts.flush()
        except Exception:
            pass

    from routes.files import export_conversation
    await export_conversation(conv_id)

    core_log = {
        "timestamp": now2,
        "time": time.strftime("%H:%M:%S", time.localtime(now2)),
        "date": time.strftime("%Y-%m-%d", time.localtime(now2)),
        "monitoringlog": f"🧠 Core因位置变化被唤醒并回复：{full_text[:80]}...",
        "call_core": False,
        "screenshot": "",
        "source": "location",
    }
    append_monitor_log(core_log)
    await manager.broadcast({"type": "monitor_log", "data": core_log})
