"""
摄像头监控：CameraMonitor 类、Sentinel 分析、Core 唤醒、监控日志读写
"""

import json, time, re, base64, asyncio, threading, sqlite3, random, urllib.request
from dataclasses import dataclass
from pathlib import Path

import cv2, httpx, numpy as np, aiosqlite

from config import (
    DB_PATH, SCREENSHOTS_DIR, MONITOR_LOGS_DIR,
    get_key, get_sentinel_config, load_worldbook, load_chat_status, load_cam_config,
    normalize_camera_wake_mode, save_cam_config, DEFAULT_MODEL, SETTINGS,
    resolve_model_transport_mode,
)
from database import get_db
from ws import manager
from ai_providers import stream_ai, CLI_STATUS_PREFIX
from memory import recall_memories, format_recalled_memories_for_prompt
from tts import TTSStreamer
from web_search import WebCommandStreamFilter
from phone_camera import phone_camera


SENTINEL_WAKE_TARGETS = {"main_ai", "second_ai", "both"}
SENTINEL_LEGACY_WAKE_TARGETS = {
    "aion": "main_ai",
    "connor": "second_ai",
}


def _load_connor_model_resolver():
    """Load the route-owned Connor model resolver without a startup import cycle."""
    from routes.chatroom import _resolve_connor_model

    return _resolve_connor_model


def build_monitor_layout_guidance(include_pc_screen: bool) -> str:
    """Describe image regions by labels instead of assuming fixed coordinates."""
    camera_rule = (
        '标有 "CAMERA VIEW" 的区域才是摄像头事实，是判断身体位置、姿势和环境的唯一图像依据。'
    )
    phone_rule = (
        '标有 "PHONE SCREEN" 的区域是手机屏幕设备上下文，通常位于摄像头画面左侧；'
        '它只能说明设备或应用使用情况，不能用于判断身体位置。'
    )
    if not include_pc_screen:
        return f"{camera_rule}\n- {phone_rule}\n- 本次不会包含电脑桌面截图。"
    return (
        f"{camera_rule}\n- {phone_rule}\n- 标有 \"DEVICE CONTEXT\" 的区域可能包含电脑桌面和手机屏幕，"
        "同样只能作为设备上下文，不能用于判断身体位置。"
    )


def normalize_sentinel_wake_target(value: object) -> str:
    target = str(value or "").strip().lower()
    target = SENTINEL_LEGACY_WAKE_TARGETS.get(target, target)
    return target if target in SENTINEL_WAKE_TARGETS else "main_ai"


def resolve_sentinel_wake_targets(
    call_core: bool,
    wake_mode: str,
    sentinel_target: object,
) -> list[str]:
    if not call_core:
        return []
    mode = normalize_camera_wake_mode(wake_mode)
    if mode != "smart":
        return [mode]
    target = normalize_sentinel_wake_target(sentinel_target)
    if target == "both":
        return ["aion", "connor"]
    return ["aion"] if target == "main_ai" else ["connor"]


def parse_sentinel_dispatch(parsed: dict, wake_mode: str) -> dict:
    call_core = bool(parsed.get("call_core", False))
    sentinel_target = normalize_sentinel_wake_target(parsed.get("wake_target"))
    confidence = str(parsed.get("wake_confidence") or "").strip().lower()
    if confidence not in {"high", "medium", "low"}:
        confidence = "low"
    return {
        "sentinel_wake_target": sentinel_target,
        "final_wake_targets": resolve_sentinel_wake_targets(
            call_core,
            wake_mode,
            sentinel_target,
        ),
        "wake_reason": str(parsed.get("wake_reason") or "").strip(),
        "wake_confidence": confidence,
    }


def build_sentinel_dispatch_policy(
    *,
    user_name: str,
    ai_name: str,
    connor_name: str,
) -> str:
    return f"""派单规则（仅在 call_core=true 时使用）：
- wake_target 只能是 main_ai、second_ai、both。main_ai 代表{ai_name}，second_ai 代表{connor_name}，both 代表两位都唤醒。
- 明确点名优先：{user_name}明确呼唤其中一位时，优先派给被点名者。
- 强烈情绪、想发疯吐槽、受委屈需要立刻站队、玩闹撩骚，优先派给{ai_name}（main_ai）。
- 饮食出行、运动工作、直播前后、睡眠作息、身体不适，偷懒摸鱼，不去休息，不吃饭需要提醒时，优先派给{connor_name}（second_ai）。
- 撒娇、亲密互动、低落但嘴硬等不固定归属；结合点名、最近由谁处理同一事情、近期互动和谁更久没有被唤醒来选择。
- 当明显危险、健康异常、情绪崩溃、涉及三个人共同的家庭关系、或长时间没有交流，选择 both。
- 普通的不确定或低置信度不能成为选择 both 的理由；仍要先判断更匹配的一位。
- 最近处理同一事件的人优先保持上下文；同类事件连续发生时避免机械地总派同一位，但上下文连续性高于轮换。
- wake_reason 用一句话说明派单依据；wake_confidence 只能是 high、medium、low。"""


# ── 监控日志文件读写 ──────────────────────────────
def _today_log_path() -> Path:
    return MONITOR_LOGS_DIR / f"{time.strftime('%Y-%m-%d')}.jsonl"


def append_monitor_log(entry: dict):
    path = _today_log_path()
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def read_monitor_logs(date_str: str = None) -> list:
    if not date_str:
        date_str = time.strftime('%Y-%m-%d')
    path = MONITOR_LOGS_DIR / f"{date_str}.jsonl"
    if not path.exists():
        return []
    entries = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except:
                    pass
    return entries


def read_logs_since(since_ts: float) -> list:
    import datetime as _dt
    since_date = _dt.date.fromtimestamp(since_ts)
    result = []
    for logfile in sorted(MONITOR_LOGS_DIR.glob("*.jsonl")):
        # 按文件名日期跳过不可能包含目标时间戳的旧文件
        try:
            if _dt.date.fromisoformat(logfile.stem) < since_date:
                continue
        except ValueError:
            pass
        with open(logfile, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    if entry.get("timestamp", 0) >= since_ts:
                        result.append(entry)
                except:
                    pass
    return result


def cleanup_old_logs(keep_days: int = 3):
    import datetime
    cutoff = datetime.date.today() - datetime.timedelta(days=keep_days)
    for logfile in MONITOR_LOGS_DIR.glob("*.jsonl"):
        try:
            file_date = datetime.date.fromisoformat(logfile.stem)
            if file_date < cutoff:
                logfile.unlink()
        except:
            pass


def get_last_user_msg_time() -> float:
    """同步版本（仅供非 async 上下文使用）"""
    conn = sqlite3.connect(str(DB_PATH))
    try:
        cur = conn.execute("SELECT created_at FROM messages WHERE role='user' ORDER BY created_at DESC LIMIT 1")
        row = cur.fetchone()
        return row[0] if row else 0
    finally:
        conn.close()


async def async_get_last_user_msg_time() -> float:
    """异步版本，避免在事件循环中阻塞"""
    async with get_db() as db:
        cur = await db.execute("SELECT created_at FROM messages WHERE role='user' ORDER BY created_at DESC LIMIT 1")
        row = await cur.fetchone()
        return row[0] if row else 0


async def async_get_last_aion_timeline_user_msg_time(conv_id: str = None) -> float:
    """哨兵视角的最后用户发言时间：合并两位 AI 的活跃对话。"""
    latest_ts = 0.0
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        if not conv_id:
            cur = await db.execute(
                "SELECT id FROM conversations ORDER BY updated_at DESC LIMIT 1"
            )
            conv = await cur.fetchone()
            conv_id = conv["id"] if conv else None

        if conv_id:
            cur = await db.execute(
                "SELECT created_at FROM messages "
                "WHERE conv_id=? AND role='user' "
                "ORDER BY created_at DESC LIMIT 1",
                (conv_id,),
            )
            row = await cur.fetchone()
            if row and row["created_at"]:
                latest_ts = max(latest_ts, float(row["created_at"]))

        cur = await db.execute(
            "SELECT id FROM chatroom_rooms "
            "WHERE type='group' ORDER BY updated_at DESC LIMIT 1"
        )
        room = await cur.fetchone()
        if room:
            cur = await db.execute(
                "SELECT created_at FROM chatroom_messages "
                "WHERE room_id=? AND sender='user' "
                "ORDER BY created_at DESC LIMIT 1",
                (room["id"],),
            )
            row = await cur.fetchone()
            if row and row["created_at"]:
                latest_ts = max(latest_ts, float(row["created_at"]))

        cur = await db.execute(
            "SELECT id FROM chatroom_rooms "
            "WHERE type='connor_1v1' ORDER BY updated_at DESC LIMIT 1"
        )
        connor_room = await cur.fetchone()
        if connor_room:
            cur = await db.execute(
                "SELECT created_at FROM chatroom_messages "
                "WHERE room_id=? AND sender='user' "
                "ORDER BY created_at DESC LIMIT 1",
                (connor_room["id"],),
            )
            row = await cur.fetchone()
            if row and row["created_at"]:
                latest_ts = max(latest_ts, float(row["created_at"]))

    return latest_ts


async def async_get_recent_aion_timeline_text(
    conv_id: str = None,
    limit: int = 10,
    *,
    user_name: str = "用户",
    ai_name: str = "AI",
    connor_name: str = "AI",
) -> str:
    """给哨兵看的最近聊天记录，合并主私聊、群聊和第二 AI 私聊。"""
    rows = []
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        if not conv_id:
            cur = await db.execute(
                "SELECT id FROM conversations ORDER BY updated_at DESC LIMIT 1"
            )
            conv = await cur.fetchone()
            conv_id = conv["id"] if conv else None

        if conv_id:
            cur = await db.execute(
                "SELECT '私聊' AS source, role AS sender, content, created_at "
                "FROM messages "
                "WHERE conv_id=? AND role IN ('user','assistant') "
                "ORDER BY created_at DESC LIMIT ?",
                (conv_id, limit),
            )
            rows.extend(dict(r) for r in await cur.fetchall())

        cur = await db.execute(
            "SELECT id FROM chatroom_rooms "
            "WHERE type='connor_1v1' ORDER BY updated_at DESC LIMIT 1"
        )
        connor_room = await cur.fetchone()
        if connor_room:
            cur = await db.execute(
                "SELECT '第二AI私聊' AS source, sender, content, created_at "
                "FROM chatroom_messages "
                "WHERE room_id=? AND sender IN ('user','connor') "
                "ORDER BY created_at DESC LIMIT ?",
                (connor_room["id"], limit),
            )
            rows.extend(dict(r) for r in await cur.fetchall())

        cur = await db.execute(
            "SELECT id FROM chatroom_rooms "
            "WHERE type='group' ORDER BY updated_at DESC LIMIT 1"
        )
        room = await cur.fetchone()
        if room:
            cur = await db.execute(
                "SELECT '群聊' AS source, sender, content, created_at "
                "FROM chatroom_messages "
                "WHERE room_id=? AND sender IN ('user','aion','connor') "
                "ORDER BY created_at DESC LIMIT ?",
                (room["id"], limit),
            )
            rows.extend(dict(r) for r in await cur.fetchall())

    rows.sort(key=lambda r: r.get("created_at") or 0)
    rows = rows[-limit:]
    lines = []
    for r in rows:
        sender = r.get("sender")
        if sender == "user":
            name = user_name
        elif sender in ("assistant", "aion"):
            name = ai_name
        elif sender == "connor":
            name = connor_name
        else:
            name = str(sender or "unknown")
        content = (r.get("content") or "").strip()
        if sender in ("assistant", "aion"):
            content = _strip_leading_cli_role_header(content)
        content = re.sub(r"<meta>.*?</meta>", "", content, flags=re.S).strip()
        content = content[:200] + "..." if len(content) > 200 else content
        if content:
            lines.append(f"[{r.get('source')}] {name}: {content}")
    return "\n".join(lines)


def _strip_leading_cli_role_header(text: str) -> str:
    """去掉 CLI 偶尔回吐的续写角色标签，如开头的 [Assistant]。"""
    if not text:
        return text
    return re.sub(r"^\s*\[(?:Assistant|Model|AI|Aion)\]\s*", "", text, count=1).lstrip()


def _is_readable_camera_frame(frame) -> bool:
    if frame is None or not hasattr(frame, "shape") or frame.size == 0:
        return False
    h, w = frame.shape[:2]
    if h <= 0 or w <= 0:
        return False
    return True


def detect_cameras(max_test: int = 10, skip_index: int = -1) -> list:
    """扫描可用摄像头（DirectShow 后端 + 实际读帧验证）
    skip_index: 跳过正在使用的摄像头，避免抢占设备导致采集线程中断
    """
    available = []
    for i in range(max_test):
        if i == skip_index:
            available.append(i)  # 正在用的摄像头直接视为可用
            continue
        try:
            cap = cv2.VideoCapture(i, cv2.CAP_DSHOW)
            if cap.isOpened():
                available.append(i)
                cap.release()
                time.sleep(0.3)
            else:
                cap.release()
        except Exception:
            pass
    return available


# ── 摄像头监控类 ──────────────────────────────────
class CameraMonitor:
    def __init__(self):
        self.cfg = load_cam_config()
        self.cap = None
        self.running = False
        self.monitoring = False
        self._thread = None
        self._monitor_thread = None
        self._latest_frame = None
        self._lock = threading.Lock()
        self._cam_op_lock = threading.Lock()   # 防止 open/close 并发
        self._cancel_verify = False            # 允许取消验证
        self._loop = None
        self._next_capture_at = 0
        # 画面裁剪状态：zoom=放大倍数, cx/cy=裁剪中心(0~1)
        self.crop_zoom = 1.0
        self.crop_cx = 0.5
        self.crop_cy = 0.5
        # ESP32-CAM 状态
        self._esp32_thread = None
        self._esp32_running = False
        self._esp32_bridge_active = False  # App 桥接模式
        self._frame_ts = 0                 # 最新帧的时间戳

    def set_event_loop(self, loop):
        self._loop = loop

    def set_crop(self, zoom: float, cx: float, cy: float):
        self.crop_zoom = max(1.0, min(10.0, zoom))
        self.crop_cx = max(0.0, min(1.0, cx))
        self.crop_cy = max(0.0, min(1.0, cy))

    def get_crop(self) -> dict:
        return {"zoom": self.crop_zoom, "cx": self.crop_cx, "cy": self.crop_cy}

    def _apply_crop(self, frame):
        """根据 zoom/cx/cy 裁剪帧，zoom=1 时返回原图"""
        if self.crop_zoom <= 1.0:
            return frame
        h, w = frame.shape[:2]
        crop_w = int(w / self.crop_zoom)
        crop_h = int(h / self.crop_zoom)
        # 根据 cx/cy 计算裁剪区域中心，并 clamp 到合法范围
        center_x = int(self.crop_cx * w)
        center_y = int(self.crop_cy * h)
        x1 = max(0, min(center_x - crop_w // 2, w - crop_w))
        y1 = max(0, min(center_y - crop_h // 2, h - crop_h))
        return frame[y1:y1 + crop_h, x1:x1 + crop_w]

    def open_camera(self, index: int = None):
        if not self._cam_op_lock.acquire(blocking=False):
            print("[Camera] 操作进行中，忽略重复请求")
            return False
        try:
            if index is not None:
                self.cfg["camera_index"] = index
                save_cam_config(self.cfg)
            self._close_camera_internal()

            idx = self.cfg["camera_index"]
            self._cancel_verify = False
            self.cap = cv2.VideoCapture(idx, cv2.CAP_DSHOW)
            if not self._verify_camera(max_wait=10):
                if self.cap:
                    try: self.cap.release()
                    except: pass
                    self.cap = None
                print(f"[Camera] 摄像头 index={idx} 打开失败")
                return False

            self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
            self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
            self.running = True
            self._thread = threading.Thread(target=self._capture_loop, daemon=True)
            self._thread.start()
            print(f"[Camera] 摄像头已启动 index={idx}")
            return True
        finally:
            self._cam_op_lock.release()

    def _verify_camera(self, max_wait: int = 4) -> bool:
        """验证摄像头：最多等 max_wait 秒，读到非垃圾帧才算成功，可被 _cancel_verify 中断"""
        if not self.cap or not self.cap.isOpened():
            return False
        deadline = time.time() + max_wait
        while time.time() < deadline:
            if self._cancel_verify:
                print("[Camera] 验证被取消")
                return False
            try:
                ret, frame = self.cap.read()
            except Exception:
                ret = False
            if ret and frame is not None:
                avg = frame.mean()
                if _is_readable_camera_frame(frame):
                    print(f"[Camera] 验证通过 (avg_pixel={avg:.1f})")
                    with self._lock:
                        self._latest_frame = frame
                    return True
            time.sleep(0.15)
        print(f"[Camera] 验证失败：{max_wait}s 内未获取到有效帧")
        return False

    def close_camera(self):
        if not self._cam_op_lock.acquire(blocking=False):
            print("[Camera] 操作进行中，忽略重复请求")
            return
        try:
            self._close_camera_internal()
        finally:
            self._cam_op_lock.release()

    def _close_camera_internal(self):
        """内部关闭方法（调用者需持有 _cam_op_lock）"""
        self._cancel_verify = True  # 中断正在进行的验证
        self.running = False
        # 等待采集线程退出（它内部会检查 self.running）
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=10)
            if self._thread.is_alive():
                print("[Camera] 警告: 采集线程未在 10s 内退出")
        self._thread = None
        if self.cap:
            try:
                self.cap.release()
            except Exception as e:
                print(f"[Camera] 释放摄像头异常: {e}")
            self.cap = None
        with self._lock:
            self._latest_frame = None

    # ── ESP32-CAM 支持 ────────────────────────────────

    def open_esp32(self) -> bool:
        """启动 ESP32-CAM 抓帧（直连 + App 桥接自动切换）"""
        url = self.cfg.get("esp32_cam_url", "").strip()
        if not url:
            print("[ESP32-CAM] URL 未配置")
            return False
        self._stop_current_source()

        # 快速探测直连
        snapshot_url = url.rstrip("/") + "/capture"
        direct_ok = self._try_direct_fetch(snapshot_url) is not None
        if direct_ok:
            print(f"[ESP32-CAM] 直连成功: {url}")
        else:
            print(f"[ESP32-CAM] 直连失败，将依赖 App 桥接")

        self._esp32_running = True
        self._esp32_bridge_active = not direct_ok
        self.running = True
        self._esp32_thread = threading.Thread(target=self._esp32_capture_loop, daemon=True)
        self._esp32_thread.start()

        # 直连失败时，通知 App 启动桥接
        if not direct_ok and self._loop:
            asyncio.run_coroutine_threadsafe(
                manager.broadcast({"type": "esp32_bridge", "data": {"active": True, "url": snapshot_url}}),
                self._loop
            )
        print(f"[ESP32-CAM] 已启动, URL={url}, bridge={self._esp32_bridge_active}")
        return True

    def close_esp32(self):
        """停止 ESP32-CAM 抓帧线程"""
        self._esp32_running = False
        self.running = False
        self.monitoring = False
        # 通知 App 停止桥接
        if self._esp32_bridge_active and self._loop:
            asyncio.run_coroutine_threadsafe(
                manager.broadcast({"type": "esp32_bridge", "data": {"active": False}}),
                self._loop
            )
        self._esp32_bridge_active = False
        if self._esp32_thread and self._esp32_thread.is_alive():
            self._esp32_thread.join(timeout=5)
        self._esp32_thread = None
        if self._monitor_thread and self._monitor_thread.is_alive():
            self._monitor_thread.join(timeout=3)
        self._monitor_thread = None
        with self._lock:
            self._latest_frame = None
        self._frame_ts = 0
        print("[ESP32-CAM] 已关闭")

    def _try_direct_fetch(self, url: str):
        """尝试直连 ESP32 拉取一帧，成功返回 frame，失败返回 None"""
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=5) as resp:
                jpg_data = resp.read()
            if len(jpg_data) < 100:
                return None
            arr = np.frombuffer(jpg_data, dtype=np.uint8)
            frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            return frame if frame is not None and frame.size > 0 else None
        except Exception:
            return None

    def receive_esp32_frame(self, jpg_bytes: bytes) -> bool:
        """接收 App 桥接推来的 JPEG 帧"""
        try:
            arr = np.frombuffer(jpg_bytes, dtype=np.uint8)
            frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            if frame is not None and frame.size > 0:
                with self._lock:
                    self._latest_frame = frame
                self._frame_ts = time.time()
                return True
        except Exception as e:
            print(f"[ESP32-CAM] 接收桥接帧失败: {e}")
        return False

    def _esp32_capture_loop(self):
        """ESP32 抓帧循环：优先直连，失败时通过 App 桥接"""
        url = self.cfg.get("esp32_cam_url", "").rstrip("/") + "/capture"
        fail_count = 0
        bridge_notified = self._esp32_bridge_active
        while self._esp32_running:
            # 尝试直连
            frame = self._try_direct_fetch(url)
            if frame is not None:
                with self._lock:
                    self._latest_frame = frame
                self._frame_ts = time.time()
                fail_count = 0
                # 直连恢复时关闭桥接
                if bridge_notified:
                    bridge_notified = False
                    self._esp32_bridge_active = False
                    if self._loop:
                        asyncio.run_coroutine_threadsafe(
                            manager.broadcast({"type": "esp32_bridge", "data": {"active": False}}),
                            self._loop
                        )
                    print("[ESP32-CAM] 直连恢复，关闭桥接")
                time.sleep(0.5)
                continue

            # 直连失败
            fail_count += 1
            if fail_count >= 3 and not bridge_notified and self._loop:
                # 连续失败 3 次，激活 App 桥接
                bridge_notified = True
                self._esp32_bridge_active = True
                asyncio.run_coroutine_threadsafe(
                    manager.broadcast({"type": "esp32_bridge", "data": {"active": True, "url": url}}),
                    self._loop
                )
                print(f"[ESP32-CAM] 直连失败 {fail_count} 次，激活 App 桥接")
            elif fail_count % 20 == 0:
                print(f"[ESP32-CAM] 连续 {fail_count} 次拉取失败")

            # 桥接模式下不频繁重试直连（App 会推帧过来）
            if bridge_notified:
                time.sleep(5)
            else:
                time.sleep(min(5, 0.5 + fail_count * 0.3))
        # 退出时关闭桥接
        if bridge_notified and self._loop:
            asyncio.run_coroutine_threadsafe(
                manager.broadcast({"type": "esp32_bridge", "data": {"active": False}}),
                self._loop
            )
        print("[ESP32-CAM] 抓帧线程退出")

    def _stop_current_source(self):
        """关闭当前正在运行的画面源（本地或 ESP32），不改 cfg"""
        if self._esp32_running:
            self._esp32_running = False
            self.running = False
            if self._esp32_thread and self._esp32_thread.is_alive():
                self._esp32_thread.join(timeout=5)
            self._esp32_thread = None
        if self.cap or (self._thread and self._thread.is_alive()):
            # 需要持有锁来关闭本地摄像头
            if self._cam_op_lock.acquire(blocking=False):
                try:
                    self._close_camera_internal()
                finally:
                    self._cam_op_lock.release()
            else:
                self._cancel_verify = True
                self.running = False
        if self._monitor_thread and self._monitor_thread.is_alive():
            self._monitor_thread.join(timeout=3)
        self._monitor_thread = None
        with self._lock:
            self._latest_frame = None

    def switch_source(self, source: str, esp32_url: str = None) -> bool:
        """切换画面源: 'local' 或 'esp32'"""
        was_monitoring = self.monitoring
        self._stop_current_source()
        self.monitoring = False

        if esp32_url is not None:
            self.cfg["esp32_cam_url"] = esp32_url
        self.cfg["active_source"] = source
        save_cam_config(self.cfg)

        if source == "esp32":
            ok = self.open_esp32()
        elif source == "phone":
            # Phone capture is event-driven. Source selection does not arm it.
            self.running = False
            ok = True
        else:
            ok = self.open_camera(self.cfg["camera_index"])

        if ok and was_monitoring:
            self.start_monitoring()
        return ok

    def _capture_loop(self):
        """采集循环：读帧失败或绿屏超时后触发重连（只重连用户配置的摄像头）"""
        fail_count = 0
        max_fails = 100  # ~10 秒
        reconnect_attempts = 0
        while self.running:
            if not self.cap or not self.cap.isOpened():
                idx = self.cfg["camera_index"]
                reconnect_attempts += 1
                wait_time = min(30, 2 * reconnect_attempts)
                print(f"[Camera] 设备断开，{wait_time}s 后尝试重连 index={idx}（第{reconnect_attempts}次）...")
                if self.cap:
                    try: self.cap.release()
                    except: pass
                    self.cap = None
                for _ in range(wait_time * 2):
                    if not self.running:
                        return
                    time.sleep(0.5)
                if not self.running:
                    return
                self.cap = cv2.VideoCapture(idx, cv2.CAP_DSHOW)
                self._cancel_verify = False
                if self._verify_camera(max_wait=10):
                    self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
                    self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
                    fail_count = 0
                    reconnect_attempts = 0
                    print(f"[Camera] 重连成功 index={idx}")
                else:
                    if self.cap:
                        try: self.cap.release()
                        except: pass
                        self.cap = None
                    print(f"[Camera] 重连失败 index={idx}")
                continue
            try:
                ret, frame = self.cap.read()
            except Exception:
                ret = False
            # Darkness is a valid camera state; only require that a frame exists.
            valid = False
            if ret and frame is not None:
                valid = _is_readable_camera_frame(frame)
            if valid:
                fail_count = 0
                with self._lock:
                    self._latest_frame = frame
            else:
                fail_count += 1
                if fail_count % 100 == 0:
                    print(f"[Camera] 已连续 {fail_count} 帧无效，持续尝试...")
                if fail_count >= max_fails:
                    print(f"[Camera] 连续 {max_fails} 帧无效，触发重连")
                    if self.cap:
                        try: self.cap.release()
                        except: pass
                        self.cap = None
                    fail_count = 0
                time.sleep(0.1)
                continue
            # 监控活跃时 ~30fps，空闲时 ~10fps 减少 CPU
            time.sleep(0.033 if self.monitoring else 0.1)

    def _capture_screen(self, *, force: bool = False) -> np.ndarray | None:
        """截取主屏幕画面，缩放到与摄像头同宽，返回 BGR numpy 数组。"""
        try:
            if not self.cfg.get("include_pc_screen", False):
                return None
            if not force and not self._should_capture_pc_screen():
                return None
            from PIL import ImageGrab
            img = ImageGrab.grab()  # 仅主屏幕
            screen_np = np.array(img)
            # PIL 返回 RGB，转 BGR 供 OpenCV 使用
            screen_bgr = cv2.cvtColor(screen_np, cv2.COLOR_RGB2BGR)
            return screen_bgr
        except Exception as e:
            print(f"[Camera] 屏幕截图失败: {e}")
            return None

    @staticmethod
    def _add_layer_label(frame: np.ndarray, text: str) -> np.ndarray:
        """给拼接图层加英文标签，帮助视觉模型区分摄像头事实和设备上下文。"""
        if frame is None:
            return frame
        out = frame.copy()
        h, w = out.shape[:2]
        if h <= 0 or w <= 0:
            return out
        label_w = min(w, max(220, len(text) * 10 + 18))
        label_h = min(h, 30)
        overlay = out.copy()
        cv2.rectangle(overlay, (0, 0), (label_w, label_h), (0, 0, 0), -1)
        cv2.addWeighted(overlay, 0.55, out, 0.45, 0, out)
        cv2.putText(
            out,
            text,
            (8, min(22, label_h - 7)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.55,
            (245, 245, 245),
            1,
            cv2.LINE_AA,
        )
        return out

    def _combine_with_screen(
        self,
        cam_frame: np.ndarray,
        *,
        force_pc_screen: bool = False,
        phone_screen_after: float | None = None,
    ) -> np.ndarray:
        """将摄像头画面（上）和主屏幕截图（下）上下拼接"""
        if not self.cfg.get("include_pc_screen", False):
            return self._combine_phone_screen_beside_camera(
                cam_frame,
                phone_screen_after=phone_screen_after,
            )

        cam_layer = self._add_layer_label(cam_frame, "CAMERA VIEW - body/location source")
        screen = self._capture_screen(force=force_pc_screen)
        if screen is None:
            return self._combine_phone_screen_beside_camera(
                cam_frame,
                phone_screen_after=phone_screen_after,
            )
        cam_h, cam_w = cam_layer.shape[:2]
        # 将屏幕截图缩放到与摄像头同宽
        scr_h, scr_w = screen.shape[:2]
        new_scr_h = int(cam_w / scr_w * scr_h)
        screen_resized = cv2.resize(screen, (cam_w, new_scr_h), interpolation=cv2.INTER_AREA)
        screen_resized = self._overlay_phone_screen(
            screen_resized,
            phone_screen_after=phone_screen_after,
        )
        screen_resized = self._add_layer_label(screen_resized, "DEVICE CONTEXT - phone/PC only")
        # 上下拼接
        combined = np.vstack([cam_layer, screen_resized])
        return combined

    def _combine_phone_screen_beside_camera(
        self,
        cam_frame: np.ndarray,
        *,
        phone_screen_after: float | None = None,
    ) -> np.ndarray:
        """Place a fresh phone screen to the left of the camera without cropping."""
        cam_layer = self._add_layer_label(
            cam_frame,
            "CAMERA VIEW - body/location source",
        )
        try:
            from phone_screen import get_recent_phone_screen_path

            phone_path = get_recent_phone_screen_path(
                max_age_seconds=150,
                received_after=phone_screen_after,
            )
            if not phone_path:
                return cam_layer
            phone = cv2.imread(str(phone_path))
            if phone is None:
                return cam_layer

            cam_h = cam_layer.shape[0]
            ph_h, ph_w = phone.shape[:2]
            if cam_h <= 0 or ph_h <= 0 or ph_w <= 0:
                return cam_layer
            phone_w = max(1, round(cam_h * ph_w / ph_h))
            phone_layer = cv2.resize(
                phone,
                (phone_w, cam_h),
                interpolation=cv2.INTER_AREA,
            )
            phone_layer = self._add_layer_label(
                phone_layer,
                "PHONE SCREEN - device context only",
            )
            return np.hstack([phone_layer, cam_layer])
        except Exception as e:
            print(f"[Camera] 手机屏幕横向拼接失败: {e}")
            return cam_layer

    def _should_capture_pc_screen(self) -> bool:
        """根据 Windows 显示器电源状态/空闲时间判断是否截取 PC 屏幕。"""
        try:
            from activity import pc_display_tracker
            if pc_display_tracker.should_capture_screen():
                return True
            status = pc_display_tracker.get_status()
            idle = status.get("idle_seconds")
            idle_text = f"{idle:.0f}s" if isinstance(idle, (int, float)) else "unknown"
            print(
                "[Camera] PC 屏幕截图跳过: "
                f"display={status.get('state')} "
                f"physical={status.get('physical_state')} "
                f"idle={idle_text}"
            )
            return False
        except Exception as e:
            print(f"[Camera] PC 显示状态检查失败，继续截图: {e}")
            return True

    def _overlay_phone_screen(
        self,
        screen_frame: np.ndarray,
        *,
        phone_screen_after: float | None = None,
    ) -> np.ndarray:
        """把最近的手机屏幕截图缩到与电脑屏幕层同高，贴在左侧窄条。"""
        try:
            from phone_screen import get_recent_phone_screen_path
            phone_path = get_recent_phone_screen_path(
                max_age_seconds=150,
                received_after=phone_screen_after,
            )
            if not phone_path:
                return screen_frame
            phone = cv2.imread(str(phone_path))
            if phone is None:
                return screen_frame

            scr_h, scr_w = screen_frame.shape[:2]
            ph_h, ph_w = phone.shape[:2]
            if ph_h <= 0 or ph_w <= 0:
                return screen_frame

            new_w = max(1, int(scr_h / ph_h * ph_w))
            max_w = max(1, int(scr_w * 0.38))
            if new_w > max_w:
                new_w = max_w
            phone_resized = cv2.resize(phone, (new_w, scr_h), interpolation=cv2.INTER_AREA)

            combined = screen_frame.copy()
            combined[:, :new_w] = phone_resized
            return combined
        except Exception as e:
            print(f"[Camera] 手机屏幕拼接失败: {e}")
            return screen_frame

    def _build_phone_only_layer(
        self,
        cam_w: int,
        *,
        phone_screen_after: float | None = None,
    ) -> np.ndarray | None:
        """PC 屏幕跳过时，仍保留手机截图所在的下方窄层。"""
        try:
            from phone_screen import get_recent_phone_screen_path
            phone_path = get_recent_phone_screen_path(
                max_age_seconds=150,
                received_after=phone_screen_after,
            )
            if not phone_path:
                return None
            phone = cv2.imread(str(phone_path))
            if phone is None:
                return None

            ph_h, ph_w = phone.shape[:2]
            if ph_h <= 0 or ph_w <= 0:
                return None

            layer_h = max(120, int(cam_w * 9 / 16))
            layer = np.zeros((layer_h, cam_w, 3), dtype=np.uint8)
            layer[:] = (18, 18, 18)

            new_w = max(1, int(layer_h / ph_h * ph_w))
            max_w = max(1, int(cam_w * 0.38))
            if new_w > max_w:
                new_w = max_w
            phone_resized = cv2.resize(phone, (new_w, layer_h), interpolation=cv2.INTER_AREA)
            layer[:, :new_w] = phone_resized
            cv2.putText(
                layer,
                "DEVICE CONTEXT - phone/PC only",
                (new_w + 24, min(layer_h - 24, 54)),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.9,
                (180, 180, 180),
                2,
                cv2.LINE_AA,
            )
            cv2.putText(
                layer,
                "PC display off / idle",
                (new_w + 24, min(layer_h - 24, 92)),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.65,
                (150, 150, 150),
                2,
                cv2.LINE_AA,
            )
            return layer
        except Exception as e:
            print(f"[Camera] 手机单独图层构建失败: {e}")
            return None

    def get_capture_jpegs(
        self,
        *,
        force_pc_screen: bool = False,
        phone_screen_after: float | None = None,
    ) -> tuple[bytes | None, bytes | None]:
        with self._lock:
            if self._latest_frame is None:
                return None, None
            frame = self._apply_crop(self._latest_frame).copy()
        pure_ok, pure_buf = cv2.imencode(".jpg", frame)
        if not pure_ok:
            return None, None
        combined = self._combine_with_screen(
            frame,
            force_pc_screen=force_pc_screen,
            phone_screen_after=phone_screen_after,
        )
        combined_ok, combined_buf = cv2.imencode(".jpg", combined)
        if not combined_ok:
            return None, None
        return combined_buf.tobytes(), pure_buf.tobytes()

    def get_frame_jpeg(
        self,
        *,
        force_pc_screen: bool = False,
        phone_screen_after: float | None = None,
    ) -> bytes | None:
        with self._lock:
            if self._latest_frame is None:
                return None
            frame = self._apply_crop(self._latest_frame).copy()
        combined = self._combine_with_screen(
            frame,
            force_pc_screen=force_pc_screen,
            phone_screen_after=phone_screen_after,
        )
        ok, encoded = cv2.imencode(".jpg", combined)
        return encoded.tobytes() if ok else None

    def get_screen_only_jpeg(
        self,
        *,
        force_pc_screen: bool = False,
        phone_screen_after: float | None = None,
    ) -> bytes | None:
        """摄像头未开启时，仅截取 PC 屏幕 + 手机截屏，返回 JPEG bytes。"""
        screen = None
        if self.cfg.get("include_pc_screen", False):
            screen = self._capture_screen(force=force_pc_screen)
        if screen is not None:
            screen = self._overlay_phone_screen(
                screen,
                phone_screen_after=phone_screen_after,
            )
            _, buf = cv2.imencode(".jpg", screen)
            return buf.tobytes()
        # PC 屏幕不可用时尝试仅手机截屏
        try:
            from phone_screen import get_recent_phone_screen_path
            phone_path = get_recent_phone_screen_path(
                max_age_seconds=150,
                received_after=phone_screen_after,
            )
            if phone_path:
                phone = cv2.imread(str(phone_path))
                if phone is not None:
                    phone = self._add_layer_label(
                        phone,
                        "PHONE SCREEN - device context only",
                    )
                    _, buf = cv2.imencode(".jpg", phone)
                    return buf.tobytes()
        except Exception:
            pass
        return None

    def save_screenshot(self, *, phone_screen_after: float | None = None) -> str | None:
        frame = None
        with self._lock:
            if self._latest_frame is not None:
                frame = self._apply_crop(self._latest_frame).copy()
        if frame is not None:
            combined = self._combine_with_screen(
                frame,
                phone_screen_after=phone_screen_after,
            )
        else:
            # 摄像头未开启，尝试仅截屏
            screen = None
            if self.cfg.get("include_pc_screen", False):
                screen = self._capture_screen()
            if screen is not None:
                combined = self._overlay_phone_screen(
                    screen,
                    phone_screen_after=phone_screen_after,
                )
            else:
                try:
                    from phone_screen import get_recent_phone_screen_path
                    phone_path = get_recent_phone_screen_path(
                        max_age_seconds=150,
                        received_after=phone_screen_after,
                    )
                    if phone_path:
                        phone = cv2.imread(str(phone_path))
                        if phone is not None:
                            combined = self._add_layer_label(
                                phone,
                                "PHONE SCREEN - device context only",
                            )
                        else:
                            return None
                    else:
                        return None
                except Exception:
                    return None
        SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S")
        filename = f"cam_{ts}.jpg"
        filepath = SCREENSHOTS_DIR / filename
        cv2.imwrite(str(filepath), combined)
        self._cleanup()
        return filename

    def _cleanup(self):
        cleanup_monitor_screenshots()

    def _random_interval_seconds(self) -> int:
        """根据配置的分钟区间随机生成一个间隔（秒）"""
        lo = max(1, self.cfg.get("auto_interval_min", 10))
        hi = max(lo, self.cfg.get("auto_interval_max", 20))
        return random.randint(lo, hi) * 60

    def start_monitoring(self):
        if self.monitoring:
            return
        self.monitoring = True
        self.cfg["monitor_enabled"] = True
        save_cam_config(self.cfg)
        self._next_capture_at = time.time() + self._random_interval_seconds()
        self._monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._monitor_thread.start()

    def reset_patrol_timer(self):
        """用户发送新消息时重置巡逻计时器，避免聊天中触发哨兵"""
        if self.monitoring:
            self._next_capture_at = time.time() + self._random_interval_seconds()

    def stop_monitoring(self):
        self.monitoring = False
        self._next_capture_at = 0
        self.cfg["monitor_enabled"] = False
        save_cam_config(self.cfg)

    def _is_quiet_hours(self) -> bool:
        """检查当前是否处于静默时段"""
        if not self.cfg.get("quiet_hours_enabled", False):
            return False
        start_str = self.cfg.get("quiet_hours_start", "00:00")
        end_str = self.cfg.get("quiet_hours_end", "09:00")
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
        else:  # 跨午夜，例如 23:00 ~ 07:00
            return cur >= start or cur < end

    def _monitor_loop(self):
        print("[Monitor] 监控线程已启动")
        while self.monitoring:
            now = time.time()
            if now < self._next_capture_at:
                time.sleep(0.5)
                continue
            self._next_capture_at = time.time() + self._random_interval_seconds()
            if self._is_quiet_hours():
                print("[Monitor] 当前处于静默时段，跳过截图")
                continue
            phone_screen_requested_at = time.time()
            if self._loop:
                broadcast_future = asyncio.run_coroutine_threadsafe(
                    manager.broadcast({
                        "type": "monitor_alert",
                        "data": build_monitor_alert_data("哨兵即将查看监控"),
                    }),
                    self._loop
                )
                try:
                    broadcast_future.result(timeout=5)
                except Exception as error:
                    print(f"[Monitor] 手机截图请求广播失败: {error}")

            filename = None
            image_result = None
            camera_jpeg = None
            if self.cfg.get("active_source", "local") == "phone":
                image_result = acquire_monitor_image_sync(
                    "sentinel_patrol",
                    should_continue=lambda: self.monitoring,
                )
                camera_jpeg = image_result.camera_jpeg
                if image_result.jpeg:
                    ts = time.strftime("%Y%m%d_%H%M%S")
                    filename = f"monitor_{ts}_{time.time_ns()}.jpg"
                    save_monitor_screenshot(image_result.jpeg, filename)
            else:
                # 本地/ESP32 继续等待本轮新鲜的手机屏幕截图后再合成。
                if self._loop:
                    from phone_screen import wait_for_phone_screen_after_sync
                    wait_for_phone_screen_after_sync(
                        phone_screen_requested_at,
                        should_continue=lambda: self.monitoring,
                    )
                if not self.monitoring:
                    break
                composite_jpeg, camera_jpeg = self.get_capture_jpegs(
                    phone_screen_after=phone_screen_requested_at,
                )
                if composite_jpeg:
                    ts = time.strftime("%Y%m%d_%H%M%S")
                    filename = f"cam_{ts}.jpg"
                    save_monitor_screenshot(composite_jpeg, filename)
                else:
                    filename = self.save_screenshot(
                        phone_screen_after=phone_screen_requested_at,
                    )
            if filename and self._loop:
                print(f"[Monitor] 截图已保存: {filename}, 开始 Sentinel 分析")
                asyncio.run_coroutine_threadsafe(
                    self._analyze_and_log(filename, camera_jpeg), self._loop
                )
            elif not filename:
                print("[Monitor] 截图失败: 无可用画面")
                if (
                    self._loop
                    and self.cfg.get("active_source", "local") == "phone"
                    and image_result is not None
                ):
                    asyncio.run_coroutine_threadsafe(
                        self._handle_monitor_no_image(image_result),
                        self._loop,
                    )
        print(f"[Monitor] 监控线程退出 (monitoring={self.monitoring})")

    async def _handle_monitor_no_image(self, image_result):
        """Keep the patrol notification flow alive without inventing a picture."""
        last_user_ts = await async_get_last_aion_timeline_user_msg_time(None)
        recent_logs = read_logs_since(time.time() - 3600 * 6)
        error = image_result.error or image_result.status or "unknown"
        monitoring_log = (
            f"{image_result.no_image_context or PHONE_CAMERA_NO_IMAGE_CONTEXT}\n"
            f"本次哨兵巡逻取图结果：{error}。"
        )
        wake_targets = resolve_sentinel_wake_targets(
            True,
            self.cfg.get("wake_mode", "aion"),
            "main_ai",
        )
        now = time.time()
        log_entry = {
            "timestamp": now,
            "time": time.strftime("%H:%M:%S", time.localtime(now)),
            "date": time.strftime("%Y-%m-%d", time.localtime(now)),
            "monitoringlog": monitoring_log,
            "summary": "本次外出监控没有成功取得手机摄像头画面。",
            "call_core": True,
            "core_reason": "监控事件已触发，但手机画面获取失败，需要明确告知用户。",
            "camera_observation": "无图像附件，无法观察。",
            "device_activity": "",
            "inference": "不得根据缺失画面推断用户状态。",
            "confidence": "low",
            "screenshot": "",
            "sentinel_wake_target": "main_ai",
            "final_wake_targets": wake_targets,
            "wake_reason": "取图失败时沿用主 AI 安全回退。",
            "wake_confidence": "low",
        }
        append_monitor_log(log_entry)
        await manager.broadcast({"type": "monitor_log", "data": log_entry})
        await self._wake_core_targets(
            wake_targets,
            monitoring_log,
            last_user_ts,
            log_entry["summary"],
            log_entry["core_reason"],
            recent_logs,
            "",
        )

    async def _analyze_and_log(
        self,
        screenshot_filename: str,
        camera_jpeg: bytes | None = None,
    ):
        filepath = SCREENSHOTS_DIR / screenshot_filename
        if not filepath.exists():
            print(f"[Monitor] 截图文件不存在: {filepath}")
            return

        cleanup_old_logs(3)

        wb = load_worldbook()
        user_name = wb.get("user_name", "你")
        ai_name = wb.get("ai_name", "AI")
        connor_name = "AI"
        try:
            from chatroom import load_chatroom_config
            connor_name = load_chatroom_config().get("connor_name") or "AI"
        except Exception:
            pass
        now_str = time.strftime("%Y年%m月%d日  %H时:%M分:%S秒")

        conv_id = None
        try:
            async with get_db() as db:
                db.row_factory = aiosqlite.Row
                cur = await db.execute(
                    "SELECT id FROM conversations ORDER BY updated_at DESC LIMIT 1"
                )
                conv = await cur.fetchone()
                conv_id = conv["id"] if conv else None
        except Exception:
            conv_id = None

        last_user_ts = await async_get_last_aion_timeline_user_msg_time(conv_id)
        last_user_time_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(last_user_ts)) if last_user_ts > 0 else "未知"

        recent_logs = read_logs_since(time.time() - 3600 * 6)
        log_history = ""
        if recent_logs:
            log_lines = [f"[{e.get('time','')}] {e.get('monitoringlog','')}" for e in recent_logs[-20:]]
            log_history = "\n".join(log_lines)

        chat_status_data = load_chat_status()
        chat_status_text = chat_status_data.get("status", "")

        # 获取位置信息
        location_text = ""
        try:
            from location import format_location_for_prompt
            location_text = format_location_for_prompt()
        except Exception:
            pass

        # 获取最近 10 条聊天上下文，帮助哨兵更好地了解用户近况
        recent_chat_text = ""
        try:
            recent_chat_text = await async_get_recent_aion_timeline_text(
                conv_id,
                10,
                user_name=user_name,
                ai_name=ai_name,
                connor_name=connor_name,
            )
        except Exception:
            recent_chat_text = ""

        # 当前设备状态与近期使用历史并存，补齐本轮尚未生成摘要的变化。
        activity_summary_text = ""
        user_dynamics_text = ""
        try:
            from activity import get_monitor_activity_for_prompt, get_user_dynamics_for_prompt
            activity_summary_text = get_monitor_activity_for_prompt()
            user_dynamics_text = get_user_dynamics_for_prompt(hours=1)
        except Exception:
            pass
        user_dynamics_block = (
            f"\n{user_name}近一小时的用户关键动态：\n{user_dynamics_text}\n"
            if user_dynamics_text else ""
        )
        heart_rate_summary_text = ""
        try:
            from health_context import build_heart_rate_summary_for_prompt
            heart_rate_summary_text = await build_heart_rate_summary_for_prompt()
        except Exception:
            heart_rate_summary_text = ""
        heart_rate_block = (
            f"\n{user_name}最近心率摘要（穿戴设备数据，仅作辅助）：\n{heart_rate_summary_text}\n"
            if heart_rate_summary_text
            else f"\n{user_name}最近心率摘要（穿戴设备数据，仅作辅助）：\n（暂无可用心率数据）\n"
        )
        dispatch_policy = build_sentinel_dispatch_policy(
            user_name=user_name,
            ai_name=ai_name,
            connor_name=connor_name,
        )
        layout_guidance = build_monitor_layout_guidance(
            bool(self.cfg.get("include_pc_screen", False))
        )

        prompt = f"""你是一个监控画面分析师，同时也是{user_name}的恋人。分析当前画面，并根据历史日志和当前状况，决定是否调用伴侣职权。

当前时间：{now_str}
{user_name}最后一次和你聊天的时间：{last_user_time_str}
{user_name}最后的聊天状态：{chat_status_text if chat_status_text else "（暂无）"}
{(chr(10) + location_text) if location_text else ""}

最近的聊天记录：
{recent_chat_text if recent_chat_text else "（暂无聊天记录）"}

{user_name}的设备当前状态与近期使用历史（分别标注）：
{activity_summary_text if activity_summary_text else "（暂无设备活动记录）"}
{user_dynamics_block}
{heart_rate_block}

历史监控日志：
{log_history if log_history else "（暂无历史日志）"}

        画面分区规则：
        - {layout_guidance}
        - 最近聊天、设备动态、心率摘要、历史监控日志只能作为背景上下文，不能覆盖当前摄像头事实。手机活跃、QQ/小红书活跃、PC黑屏、心率升高或降低，都不能单独推断{user_name}已经起床、睡着、运动或到了电脑桌前。
        - 心率摘要不是医学诊断，只能作为“可能睡眠/可能醒来/可能运动/可能异常”的辅助信号；如果摘要标注数据已过期、可能未佩戴或没电，必须忽略这条心率信号。
        - 如果摄像头画面中身体、床、桌、被子边界不清楚，必须写“不确定/疑似”，不要把不清楚的被褥或枕头说成手臂、头部或桌前。
        - 如果最近几次监控都显示床上被褥/睡眠状态，当前画面除非清楚看到离床或坐到桌前，否则应延续为“仍可能在床上休息/睡觉”，不要改写成“趴在电脑桌前”。

        请严格按照以下JSON格式回复，不要包含其他任何内容：
        {{"camera_observation":"只描述CAMERA VIEW中的客观画面；不确定就明确说不确定。","device_activity":"只描述PHONE SCREEN、DEVICE CONTEXT和设备动态，不推断身体位置。","inference":"把画面事实和设备动态分开后的谨慎判断。","confidence":"high/medium/low","monitoringlog":"综合日志，必须优先基于camera_observation，设备动态只能作为补充。","summary":"根据历史日志，概括{user_name}这段时间以来的整体状况，去掉重复无用的信息，保留关键事件和状态变化，一两句话即可。注意力重点放在CAMERA VIEW；如有PHONE SCREEN，也应关注其中的设备活动内容。","call_core":false,"core_reason":"","wake_target":"main_ai","wake_reason":"","wake_confidence":"low"}}

        字段说明：
        - camera_observation: 只允许来自CAMERA VIEW；没有清楚看到人就说没有清楚看到人；看不清床/桌/身体边界就写不确定。
        - device_activity: 只允许来自DEVICE CONTEXT和设备使用动态；不能包含“人在桌前/在床上”等身体位置结论。
        - inference: 可以综合上下文，但必须标明不确定性，不能把设备活动当作身体位置证据。
        - confidence: high表示画面清楚；medium表示可见但有遮挡；low表示昏暗/遮挡/边界不清。
        - monitoringlog: 结合当前时间和画面的客观描述，禁止胡编猜测。没有看到人就说没看到，如果最后状态没有说去睡觉，则不能推测{user_name}可能去睡觉了。身体位置必须来自CAMERA VIEW，设备动态只能补充。
        - summary: 综合最后的聊天状态和上下文内容，概括{user_name}这段时间的整体状态变化和关键事件，禁止胡编猜测。{user_name}
        - call_core: 判断是否需要主动联系{user_name}
        - core_reason: 仅当call_core为true时填写，说明为什么要主动联系{user_name}，让核心模型了解情况

        call_core判断依据：
        - false: {user_name}一切正常复合聊天内容 /夜间在睡觉 /前不久才发过消息。
        - true: {user_name}和上下文聊天内容不符/ 故意引起注意 / 已经有一段时间没有聊天了（超过半小时） / 长时间同一姿势需提醒活动 / 长时间未看到{user_name} / 单纯想念她可以想主动联系 / 或当前摄像头画面显示状态不佳 / 重点关注PHONE SCREEN标记的手机画面内容，有异常或有趣的设备活动时可以主动询问。
        - 结合设备活动动态综合判断：设备动态可以提高“是否联系”的权重，但不能改变monitoringlog里的身体位置。若只是手机活跃而摄像头仍像床上休息，大概率在赖床，需要唤醒”。"""
        prompt += f"\n\n{dispatch_policy}"

        img_b64 = base64.b64encode(filepath.read_bytes()).decode()
        scfg = get_sentinel_config()
        if not scfg["api_key"]:
            print("[Monitor] 哨兵模型 API Key 未配置，跳过分析")
            return

        sentinel_model = scfg["model"]
        print(f"[Monitor] 正在调用 Sentinel 模型: {sentinel_model} ({'OpenAI兼容' if scfg['use_openai'] else 'Gemini'})")

        monitoring_log = ""
        call_core = False
        dispatch = parse_sentinel_dispatch({}, self.cfg.get("wake_mode", "aion"))

        try:
            from memory import _call_sentinel_vision
            raw_text = await _call_sentinel_vision(scfg, prompt, img_b64, timeout=60)

            cleaned = raw_text.strip() if raw_text else ""
            if cleaned.startswith("```"):
                cleaned = re.sub(r"^```\w*\n?", "", cleaned)
                cleaned = re.sub(r"\n?```$", "", cleaned)
                cleaned = cleaned.strip()
            parsed = json.loads(cleaned)
            monitoring_log = parsed.get("monitoringlog", raw_text)
            call_core = bool(parsed.get("call_core", False))
            dispatch = parse_sentinel_dispatch(
                parsed,
                self.cfg.get("wake_mode", "aion"),
            )
            summary = parsed.get("summary", "")
            core_reason = parsed.get("core_reason", "")
            camera_observation = parsed.get("camera_observation", "")
            device_activity = parsed.get("device_activity", "")
            inference = parsed.get("inference", "")
            confidence = parsed.get("confidence", "")
        except json.JSONDecodeError:
            monitoring_log = raw_text.strip() if raw_text else "[Sentinel 无响应]"
            summary = ""
            core_reason = ""
            camera_observation = ""
            device_activity = ""
            inference = ""
            confidence = ""
        except Exception as e:
            monitoring_log = f"[Sentinel 分析失败] {e}"
            print(f"[Monitor] Sentinel API 调用异常: {e}")
            summary = ""
            core_reason = ""
            camera_observation = ""
            device_activity = ""
            inference = ""
            confidence = ""

        print(f"[Monitor] 分析完成, call_core={call_core}, log长度={len(monitoring_log)}")
        now = time.time()
        log_entry = {
            "timestamp": now,
            "time": time.strftime("%H:%M:%S", time.localtime(now)),
            "date": time.strftime("%Y-%m-%d", time.localtime(now)),
            "monitoringlog": monitoring_log,
            "summary": summary,
            "call_core": call_core,
            "core_reason": core_reason,
            "camera_observation": camera_observation,
            "device_activity": device_activity,
            "inference": inference,
            "confidence": confidence,
            "screenshot": screenshot_filename,
            **dispatch,
        }
        append_monitor_log(log_entry)
        await manager.broadcast({"type": "monitor_log", "data": log_entry})

        if call_core:
            dispatch_reason = dispatch.get("wake_reason", "")
            actor_reason = core_reason
            if dispatch_reason:
                actor_reason = (
                    f"{core_reason}\n派单依据：{dispatch_reason}"
                    if core_reason else f"派单依据：{dispatch_reason}"
                )
            await self._wake_core_targets(
                dispatch["final_wake_targets"],
                monitoring_log,
                last_user_ts,
                summary,
                actor_reason,
                recent_logs,
                screenshot_filename,
                camera_jpeg,
            )

    async def _resolve_actor_target(self, actor: str) -> dict | None:
        actor = "connor" if actor == "connor" else "aion"
        async with get_db() as db:
            db.row_factory = aiosqlite.Row
            if actor == "aion":
                active = manager.get_aion_last_active()
                if active and active.startswith("chatroom:"):
                    room_id = active.split(":", 1)[1]
                    cur = await db.execute(
                        "SELECT id, type FROM chatroom_rooms WHERE id=? AND type='group'",
                        (room_id,),
                    )
                    room = await cur.fetchone()
                    if room:
                        return {
                            "type": "chatroom",
                            "room_id": room["id"],
                            "room_type": room["type"],
                        }
                cur = await db.execute(
                    "SELECT id FROM conversations ORDER BY updated_at DESC LIMIT 1"
                )
                conv = await cur.fetchone()
                return (
                    {"type": "private", "conv_id": conv["id"]}
                    if conv else None
                )

            active_room_id = manager.get_connor_last_active()
            if active_room_id:
                cur = await db.execute(
                    "SELECT id, type FROM chatroom_rooms "
                    "WHERE id=? AND type IN ('group','connor_1v1')",
                    (active_room_id,),
                )
                room = await cur.fetchone()
                if room:
                    return {
                        "type": "chatroom",
                        "room_id": room["id"],
                        "room_type": room["type"],
                    }
            cur = await db.execute(
                "SELECT id, type FROM chatroom_rooms "
                "WHERE type='connor_1v1' ORDER BY updated_at DESC LIMIT 1"
            )
            room = await cur.fetchone()
            return (
                {
                    "type": "chatroom",
                    "room_id": room["id"],
                    "room_type": room["type"],
                }
                if room else None
            )

    async def _call_core_for_actor(
        self,
        actor: str,
        target: dict,
        *args,
        **kwargs,
    ):
        if actor == "connor":
            return await self._call_connor_core(target, *args, **kwargs)
        return await self._call_core(*args, target_override=target, **kwargs)

    async def _wake_core_targets(self, actors: list[str], *args, **kwargs) -> dict:
        ordered_actors = [a for a in ("aion", "connor") if a in actors]
        targets = {
            actor: await self._resolve_actor_target(actor)
            for actor in ordered_actors
        }
        results: dict[str, dict] = {}

        async def wake_one(actor: str):
            target = targets.get(actor)
            if not target:
                results[actor] = {"ok": False, "error": "没有可用的回复窗口"}
                return
            try:
                await self._call_core_for_actor(actor, target, *args, **kwargs)
                results[actor] = {"ok": True, "target": target}
            except Exception as exc:
                print(f"[Monitor] 唤醒 {actor} 失败: {exc}")
                results[actor] = {
                    "ok": False,
                    "target": target,
                    "error": str(exc),
                }

        same_group = (
            len(ordered_actors) == 2
            and targets.get("aion")
            and targets.get("connor")
            and targets["aion"].get("type") == "chatroom"
            and targets["connor"].get("type") == "chatroom"
            and targets["aion"].get("room_type") == "group"
            and targets["connor"].get("room_type") == "group"
            and targets["aion"].get("room_id") == targets["connor"].get("room_id")
        )
        if same_group:
            for actor in ordered_actors:
                await wake_one(actor)
        else:
            await asyncio.gather(*(wake_one(actor) for actor in ordered_actors))
        return results

    async def _call_connor_core(
        self,
        target: dict,
        trigger_log: str,
        last_user_ts: float,
        summary: str = "",
        core_reason: str = "",
        cached_logs: list = None,
        screenshot_filename: str = "",
        camera_jpeg: bytes | None = None,
    ):
        if target.get("type") != "chatroom" or not target.get("room_id"):
            raise RuntimeError("第二 AI 没有可用的聊天室窗口")

        from app_supervision_ai import inject_app_supervision_context
        from chatroom import (
            build_connor_1v1_context,
            build_connor_group_context,
            get_chatroom_names,
            load_chatroom_config,
            stream_connor_cli,
        )
        from schedule import (
            _broadcast_trigger_debug,
            _consume_background_realtime_stream,
            _new_background_meta,
            _process_background_reply_commands,
            _tts_voice_for_target,
            schedule_mgr,
        )

        user_name, _, connor_name = get_chatroom_names()
        room_id = target["room_id"]
        room_type = target.get("room_type") or "group"
        if last_user_ts > 0:
            elapsed = time.time() - last_user_ts
            hours = int(elapsed // 3600)
            minutes = int((elapsed % 3600) // 60)
            time_ago = f"{hours}小时{minutes}分钟" if hours > 0 else f"{minutes}分钟"
        else:
            time_ago = "很长时间"

        all_logs = (
            cached_logs[-24:]
            if cached_logs is not None
            else read_logs_since(
                last_user_ts if last_user_ts > 0 else time.time() - 3600 * 6
            )[-24:]
        )
        recent_detail = "\n".join(
            f"[{entry.get('time', '')}] {entry.get('monitoringlog', '')}"
            for entry in all_logs[-5:]
        ) or trigger_log

        core_parts = [f"【{user_name}】已经{time_ago}没有和你说话了。"]
        if core_reason:
            core_parts.append(f"哨兵唤醒你的原因：{core_reason}")
        if summary:
            core_parts.append(f"这段时间{user_name}的整体状况：{summary}")
        core_parts.append(
            f"最新一条监控日志原文（哨兵看到的画面完整描述）：{trigger_log}"
        )
        core_parts.append(f"最近的监控记录：\n{recent_detail}")
        from activity import get_monitor_activity_for_prompt
        device_activity = get_monitor_activity_for_prompt()
        if device_activity:
            core_parts.append(device_activity)
        contact_scene = (
            "群聊里"
            if room_type == "group"
            else f"{connor_name} 与 {user_name} 的私聊里"
        )
        core_parts.append(
            f"你现在是在{contact_scene}主动联系她。"
            "只用你自己的口吻回复，不要续写、复述或模仿历史里的角色标签，"
            "也不要替其他角色发言。"
        )
        try:
            from location import format_location_for_prompt
            loc_info = format_location_for_prompt()
            if loc_info:
                core_parts.append(f"\n{loc_info}")
        except Exception:
            pass
        try:
            from health_context import build_heart_rate_prompt_block
            core_parts.append(await build_heart_rate_prompt_block(user_name))
        except Exception:
            pass
        core_prompt = "\n".join(core_parts)

        if room_type == "connor_1v1":
            messages, digest_result = await build_connor_1v1_context(
                room_id,
                [],
                query_text=core_prompt,
                include_image_attachments=False,
            )
        else:
            messages, digest_result = await build_connor_group_context(
                room_id,
                [],
                query_text=core_prompt,
                include_image_attachments=False,
            )

        fresh_fname = ""
        if screenshot_filename:
            src_path = SCREENSHOTS_DIR / screenshot_filename
            if src_path.exists():
                fresh_fname = screenshot_filename
        trigger_message = {"role": "user", "content": core_prompt}
        if fresh_fname:
            trigger_message["attachments"] = [f"/screenshots/{fresh_fname}"]
            trigger_message["content"] += (
                "\n\n（附带了最新的监控截图，请结合画面内容回应。）"
            )
        messages.append(trigger_message)
        from autonomy_state import inject_autonomy_ability
        await inject_autonomy_ability(messages, "connor")
        inject_app_supervision_context(messages)

        _resolve_connor_model = _load_connor_model_resolver()
        cfg = load_chatroom_config()
        model_key = _resolve_connor_model(cfg.get("connor_model"))
        core_msg_id = f"cm_{int(time.time() * 1000)}_cr"
        usage_meta = _new_background_meta()
        core_tts = None
        tts_voice = _tts_voice_for_target(True, "connor")
        if tts_voice:
            core_tts = TTSStreamer(
                core_msg_id,
                tts_voice,
                manager,
                low_latency_first_chunk=resolve_model_transport_mode(model_key) == "safe_live",
            )

        if model_key == "Codex":
            async def content_stream():
                async for chunk in stream_connor_cli(
                    messages=messages,
                    meta=usage_meta,
                ):
                    if chunk.startswith(CLI_STATUS_PREFIX):
                        continue
                    yield chunk
        else:
            temperature = SETTINGS.get("temperature")

            async def content_stream():
                async for chunk in stream_ai(
                    messages,
                    model_key,
                    meta=usage_meta,
                    temperature=temperature,
                ):
                    if chunk.startswith(CLI_STATUS_PREFIX):
                        continue
                    yield chunk

        transport_outcome = await _consume_background_realtime_stream(
            content_stream,
            model_key=model_key,
            tts_streamer=core_tts,
        )
        if transport_outcome.manual_retry_required:
            return
        stream_result = transport_outcome.result
        full_text = _strip_leading_cli_role_header(stream_result.committed_text)
        safety_notice = stream_result.notice
        if not full_text.strip() and not safety_notice:
            return
        full_text = await _process_background_reply_commands(
            full_text,
            target=target,
            conv_id=None,
            sender="connor",
            ai_msg_id=core_msg_id,
        )
        if safety_notice:
            full_text = f"{full_text}\n\n[{safety_notice}]".strip()
        reasoning_content = (usage_meta.get("reasoning_content") or "").strip()
        snapshot_attachment = save_monitor_camera_snapshot(camera_jpeg, "sentinel")
        system_atts = [snapshot_attachment] if snapshot_attachment else []
        await schedule_mgr._save_to_chatroom(
            room_id,
            "connor",
            f"{connor_name}偷偷查看了监控",
            full_text,
            core_msg_id,
            "[]",
            [],
            reasoning_content,
            system_atts=system_atts,
        )
        await _broadcast_trigger_debug(
            msg_id=core_msg_id,
            model_key=model_key,
            usage_meta=usage_meta,
            prompt_messages=messages,
            recalled_memories=[],
            has_error=stream_result.stop_reason is not None,
            error_text=(
                stream_result.diagnostic_error or stream_result.stop_reason
            ),
        )
        if core_tts:
            try:
                await core_tts.flush()
            except Exception:
                pass
        now = time.time()
        core_log = {
            "timestamp": now,
            "time": time.strftime("%H:%M:%S", time.localtime(now)),
            "date": time.strftime("%Y-%m-%d", time.localtime(now)),
            "monitoringlog": f"🧠 {connor_name}已唤醒并回复：{full_text[:80]}...",
            "call_core": False,
            "screenshot": "",
        }
        append_monitor_log(core_log)
        await manager.broadcast({"type": "monitor_log", "data": core_log})
        return full_text

    async def _call_core(
        self,
        trigger_log: str,
        last_user_ts: float,
        summary: str = "",
        core_reason: str = "",
        cached_logs: list = None,
        screenshot_filename: str = "",
        camera_jpeg: bytes | None = None,
        *,
        target_override: dict | None = None,
    ):
        wb = load_worldbook()
        user_name = wb.get("user_name", "你")
        ai_name = wb.get("ai_name", "AI")

        if last_user_ts > 0:
            elapsed = time.time() - last_user_ts
            hours = int(elapsed // 3600)
            minutes = int((elapsed % 3600) // 60)
            time_ago = f"{hours}小时{minutes}分钟" if hours > 0 else f"{minutes}分钟"
        else:
            time_ago = "很长时间"

        # 复用 _analyze_and_log 已加载的日志，避免重复读文件
        if cached_logs is not None:
            all_logs = cached_logs[-24:]
        else:
            all_logs = read_logs_since(last_user_ts if last_user_ts > 0 else time.time() - 3600 * 6)
            all_logs = all_logs[-24:]
        recent_detail = "\n".join([f"[{e.get('time','')}] {e.get('monitoringlog','')}" for e in all_logs[-5:]])
        if not recent_detail:
            recent_detail = trigger_log

        async with get_db() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM conversations ORDER BY updated_at DESC LIMIT 1")
            conv = await cur.fetchone()
            if not conv:
                return
            conv_id = conv["id"]
            model_key = conv["model"] or DEFAULT_MODEL

        from schedule import (
            _broadcast_trigger_debug,
            _consume_background_realtime_stream,
            _new_background_meta,
            _process_background_reply_commands,
            schedule_mgr,
        )
        target = target_override or schedule_mgr._resolve_target({"origin": "aion"})
        is_chatroom = target["type"] == "chatroom"
        if is_chatroom:
            try:
                from chatroom import load_chatroom_config
                chatroom_model = (load_chatroom_config().get("aion_model") or "").strip()
                if chatroom_model:
                    model_key = chatroom_model
            except Exception:
                pass

        # 统一时间线：合并私聊 + 群聊消息
        from context_builder import fetch_merged_timeline, render_merged_timeline
        merged = await fetch_merged_timeline("aion", 20, conv_id=conv_id)
        history = render_merged_timeline(
            merged,
            "aion",
            include_image_attachments=False,
        )

        prefix = []
        if wb.get("ai_persona"):
            prefix.append({"role": "user", "content": f"[系统设定 - {ai_name}人设]\n{wb['ai_persona']}"})
            prefix.append({"role": "assistant", "content": "收到，我会按照设定扮演角色。"})
        if wb.get("user_persona"):
            prefix.append({"role": "user", "content": f"[系统设定 - {user_name}信息]\n{wb['user_persona']}"})
            prefix.append({"role": "assistant", "content": "收到，我会记住你的信息。"})

        core_parts = [f"【{user_name}】已经{time_ago}没有和你说话了。"]
        if core_reason:
            core_parts.append(f"哨兵唤醒你的原因：{core_reason}")
        if summary:
            core_parts.append(f"这段时间{user_name}的整体状况：{summary}")
        core_parts.append(f"最新一条监控日志原文（哨兵看到的画面完整描述）：{trigger_log}")
        core_parts.append(f"最近的监控记录：\n{recent_detail}")
        from activity import get_monitor_activity_for_prompt
        device_activity = get_monitor_activity_for_prompt()
        if device_activity:
            core_parts.append(device_activity)
        contact_scene = "群聊里" if is_chatroom else f"{ai_name} 与 {user_name} 的私聊里"
        core_parts.append(
            f"你现在是在{contact_scene}主动联系她。"
            "只用你自己的口吻回复，不要续写、复述或模仿历史里的 [其他角色名]:、[Assistant]、[User] 等角色标签，"
            "也不要替 其他角色 发言。"
        )
        # 注入位置和天气信息
        try:
            from location import format_location_for_prompt
            loc_info = format_location_for_prompt()
            if loc_info:
                core_parts.append(f"\n{loc_info}")
        except Exception:
            pass
        try:
            from health_context import build_heart_rate_prompt_block
            core_parts.append(await build_heart_rate_prompt_block(user_name))
        except Exception:
            pass
        core_prompt = "\n".join(core_parts)

        recall_query = core_prompt[:300]
        recalled, _ = await recall_memories(recall_query)
        mem_inject = []
        if recalled:
            mem_lines = format_recalled_memories_for_prompt(recalled)
            mem_inject = [
                {"role": "user", "content": f"[相关记忆]\n你脑海中与当前话题相关的记忆：\n{mem_lines}"},
                {"role": "assistant", "content": "收到，我会自然地参考这些记忆。"}
            ]

        # 直接使用哨兵已截取的画面（提示音已在截图前播放）
        fresh_fname = ""
        if screenshot_filename:
            src_path = SCREENSHOTS_DIR / screenshot_filename
            if src_path.exists():
                fresh_fname = screenshot_filename

        last_msg = {"role": "user", "content": core_prompt}
        if fresh_fname:
            last_msg["attachments"] = [f"/screenshots/{fresh_fname}"]
            core_prompt += "\n\n（附带了最新的监控截图，请结合画面内容回应。）"
            last_msg["content"] = core_prompt

        from capabilities import build_band_note_ability_text
        passive_band_ability = build_band_note_ability_text(user_name, passive=True)
        passive_band_messages = []
        if passive_band_ability:
            passive_band_messages = [
                {"role": "user", "content": f"[系统能力]\n{passive_band_ability}"},
                {"role": "assistant", "content": "收到，需要时我会使用手环纸条提醒。"},
            ]
        messages = prefix + mem_inject + history + passive_band_messages + [last_msg]
        from proactive_companionship import inject_proactive_ability
        inject_proactive_ability(messages, "aion")
        from autonomy_state import inject_autonomy_ability
        await inject_autonomy_ability(messages, "aion")
        from app_supervision_ai import inject_app_supervision_context
        inject_app_supervision_context(messages)

        # 预生成 msg_id（TTS 分段文件命名需要）
        core_msg_id = f"msg_{int(time.time()*1000)}_cr"
        usage_meta = _new_background_meta()
        has_error = False
        error_text = None

        # TTS：检查是否有前端开了 TTS
        core_tts = None
        if manager.any_tts_enabled():
            tts_voice = manager.get_tts_voice()
            if tts_voice:
                core_tts = TTSStreamer(
                    core_msg_id,
                    tts_voice,
                    manager,
                    low_latency_first_chunk=resolve_model_transport_mode(model_key) == "safe_live",
                )

        _temp = SETTINGS.get("temperature")

        async def content_stream():
            async for chunk in stream_ai(messages, model_key, meta=usage_meta, temperature=_temp):
                if chunk.startswith(CLI_STATUS_PREFIX):
                    continue
                yield chunk

        transport_outcome = await _consume_background_realtime_stream(
            content_stream,
            model_key=model_key,
            tts_streamer=core_tts,
        )
        if transport_outcome.manual_retry_required:
            return
        stream_result = transport_outcome.result
        full_text = stream_result.committed_text
        has_error = stream_result.stop_reason is not None
        error_text = stream_result.diagnostic_error or stream_result.stop_reason
        safety_notice = stream_result.notice

        full_text = _strip_leading_cli_role_header(full_text)
        if not full_text.strip() and not safety_notice:
            return

        full_text = await _process_background_reply_commands(
            full_text,
            target=target,
            conv_id=conv_id,
            sender="aion",
            ai_msg_id=core_msg_id,
        )
        if safety_notice:
            full_text = f"{full_text}\n\n[{safety_notice}]".strip()
        reasoning_content = (usage_meta.get("reasoning_content") or "").strip()
        sys_content = f"{ai_name}偷偷查看了监控"
        snapshot_attachment = save_monitor_camera_snapshot(
            camera_jpeg,
            "sentinel",
        )
        system_atts = [snapshot_attachment] if snapshot_attachment else []
        system_atts.append({
            "type": "system_notice_order",
            "before_msg_id": core_msg_id,
        })
        if is_chatroom:
            await schedule_mgr._save_to_chatroom(
                target["room_id"],
                "aion",
                sys_content,
                full_text,
                core_msg_id,
                "[]",
                [],
                reasoning_content,
                system_atts=system_atts,
            )
        else:
            now = time.time()
            trigger_msg_id = f"msg_{int(now*1000)}_ct"
            async with get_db() as db:
                await db.execute(
                    "INSERT INTO messages (id, conv_id, role, content, created_at, attachments) VALUES (?,?,?,?,?,?)",
                    (trigger_msg_id, conv_id, "cam_trigger", core_prompt, now, "[]")
                )
                # 插入系统提示：哨兵唤醒了Core
                sys_now = time.time()
                sys_msg_id = f"msg_{int(sys_now*1000)}_sw"
                await db.execute(
                    "INSERT INTO messages (id, conv_id, role, content, created_at, attachments) VALUES (?,?,?,?,?,?)",
                    (
                        sys_msg_id,
                        conv_id,
                        "system",
                        sys_content,
                        sys_now,
                        json.dumps(system_atts, ensure_ascii=False)
                        if system_atts else "[]",
                    )
                )
                await db.commit()
            sys_msg = {"id": sys_msg_id, "conv_id": conv_id, "role": "system",
                       "content": sys_content, "created_at": sys_now,
                       "attachments": system_atts}
            await manager.broadcast({"type": "msg_created", "data": sys_msg})

            async with get_db() as db:
                now2 = time.time()
                await db.execute(
                    "INSERT INTO messages (id, conv_id, role, content, created_at, attachments, reasoning_content) VALUES (?,?,?,?,?,?,?)",
                    (core_msg_id, conv_id, "assistant", full_text, now2, "[]", reasoning_content)
                )
                await db.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now2, conv_id))
                await db.commit()

            core_msg = {"id": core_msg_id, "conv_id": conv_id, "role": "assistant",
                        "content": full_text, "created_at": now2, "attachments": [],
                        "reasoning_content": reasoning_content}
            await manager.broadcast({"type": "msg_created", "data": core_msg})

            # 延迟导入避免循环
            from routes.files import export_conversation
            await export_conversation(conv_id)
        await _broadcast_trigger_debug(
            msg_id=core_msg_id,
            model_key=model_key,
            usage_meta=usage_meta,
            prompt_messages=messages,
            recalled_memories=recalled,
            has_error=has_error,
            error_text=error_text,
        )

        now = time.time()
        # 刷新 TTS 剩余文本
        if core_tts:
            try:
                await core_tts.flush()
            except Exception:
                pass

        core_log = {
            "timestamp": now,
            "time": time.strftime("%H:%M:%S", time.localtime(now)),
            "date": time.strftime("%Y-%m-%d", time.localtime(now)),
            "monitoringlog": f"🧠 Core已唤醒并回复：{full_text[:80]}...",
            "call_core": False,
            "screenshot": "",
        }
        append_monitor_log(core_log)
        await manager.broadcast({"type": "monitor_log", "data": core_log})


cam = CameraMonitor()


def build_monitor_alert_data(content: str, **extra) -> dict:
    """Build one monitor alert and assign phone-camera audio to Android."""
    data = {"content": content, **extra}
    if cam.cfg.get("active_source", "local") == "phone":
        data["phone_camera_native_capture"] = True
    return data


# ── Core 主动查看监控 [CAM_CHECK] ─────────────────
CAM_CHECK_CMD = "[CAM_CHECK]"

PHONE_CAMERA_NO_IMAGE_CONTEXT = (
    "[手机摄像头采集状态]\n"
    "本次手机摄像头请求没有成功取得有效画面。本轮没有图像附件，"
    "不得描述或推断摄像头画面。请结合本次任务目的自然回应。"
)


@dataclass(frozen=True)
class MonitorImageResult:
    status: str
    source: str
    jpeg: bytes | None = None
    camera_jpeg: bytes | None = None
    request_id: str = ""
    no_image_context: str = ""
    error: str = ""


@dataclass(frozen=True)
class MonitorPromptContext:
    image_result: MonitorImageResult
    prompt_attachment: str = ""
    snapshot_attachment: dict | None = None


def _phone_result_to_monitor(
    result,
    *,
    force_pc_screen: bool = False,
    phone_screen_after: float | None = None,
) -> MonitorImageResult:
    if result.status == "ready" and result.path and result.path.is_file():
        try:
            payload = result.path.read_bytes()
        except OSError as error:
            return MonitorImageResult(
                status="unavailable",
                source="phone",
                request_id=result.request_id,
                no_image_context=PHONE_CAMERA_NO_IMAGE_CONTEXT,
                error=f"read_failed:{type(error).__name__}",
            )
        if payload:
            frame = cv2.imdecode(
                np.frombuffer(payload, dtype=np.uint8),
                cv2.IMREAD_COLOR,
            )
            if frame is None or frame.size == 0:
                return MonitorImageResult(
                    status="unavailable",
                    source="phone",
                    request_id=result.request_id,
                    no_image_context=PHONE_CAMERA_NO_IMAGE_CONTEXT,
                    error="jpeg_decode_failed",
                )
            combined = cam._combine_with_screen(
                frame,
                force_pc_screen=force_pc_screen,
                phone_screen_after=phone_screen_after,
            )
            ok, encoded = cv2.imencode(
                ".jpg",
                combined,
                [cv2.IMWRITE_JPEG_QUALITY, 82],
            )
            if not ok:
                return MonitorImageResult(
                    status="unavailable",
                    source="phone",
                    request_id=result.request_id,
                    no_image_context=PHONE_CAMERA_NO_IMAGE_CONTEXT,
                    error="combined_jpeg_encode_failed",
                )
            return MonitorImageResult(
                status="ready",
                source="phone",
                jpeg=encoded.tobytes(),
                camera_jpeg=payload,
                request_id=result.request_id,
            )
    return MonitorImageResult(
        status=result.status,
        source="phone",
        request_id=result.request_id,
        no_image_context=PHONE_CAMERA_NO_IMAGE_CONTEXT,
        error=result.error,
    )


async def acquire_monitor_image(
    reason: str,
    *,
    force_pc_screen: bool = False,
    timeout_seconds: float = 45.0,
    retry_after_seconds: float = 15.0,
) -> MonitorImageResult:
    if cam.cfg.get("active_source", "local") == "phone":
        phone_screen_requested_at = time.time()
        try:
            result = await phone_camera.request_capture(
                reason,
                manager.send_to_client,
                timeout_seconds=timeout_seconds,
                retry_after_seconds=retry_after_seconds,
            )
        except RuntimeError as error:
            return MonitorImageResult(
                status="unavailable",
                source="phone",
                no_image_context=PHONE_CAMERA_NO_IMAGE_CONTEXT,
                error=str(error),
            )
        if result.status == "ready":
            from phone_screen import wait_for_phone_screen_after
            await wait_for_phone_screen_after(
                phone_screen_requested_at,
                timeout_seconds=min(10.0, timeout_seconds),
            )
        return _phone_result_to_monitor(
            result,
            force_pc_screen=force_pc_screen,
            phone_screen_after=phone_screen_requested_at,
        )

    jpg, camera_jpg = cam.get_capture_jpegs(force_pc_screen=force_pc_screen)
    if jpg:
        return MonitorImageResult(
            status="ready",
            source="camera",
            jpeg=jpg,
            camera_jpeg=camera_jpg,
        )
    jpg = cam.get_screen_only_jpeg(force_pc_screen=force_pc_screen)
    if jpg:
        return MonitorImageResult(status="ready", source="device", jpeg=jpg)
    return MonitorImageResult(status="unavailable", source="device")


async def acquire_prompt_monitor_context(
    reason: str,
    *,
    alert_content: str,
    alert_extra: dict | None = None,
) -> MonitorPromptContext:
    """Play the standard monitor alert and capture one fresh model context."""
    await manager.broadcast({
        "type": "monitor_alert",
        "data": build_monitor_alert_data(alert_content, **(alert_extra or {})),
    })

    if cam.cfg.get("active_source", "local") == "phone":
        image_result = await acquire_monitor_image(reason)
    else:
        phone_screen_requested_at = time.time()
        from phone_screen import wait_for_phone_screen_after
        await wait_for_phone_screen_after(phone_screen_requested_at)
        jpg_bytes, camera_jpeg = cam.get_capture_jpegs(
            phone_screen_after=phone_screen_requested_at,
        )
        if jpg_bytes:
            image_result = MonitorImageResult(
                status="ready",
                source="camera",
                jpeg=jpg_bytes,
                camera_jpeg=camera_jpeg,
            )
        else:
            jpg_bytes = cam.get_screen_only_jpeg(
                phone_screen_after=phone_screen_requested_at,
            )
            image_result = (
                MonitorImageResult(status="ready", source="device", jpeg=jpg_bytes)
                if jpg_bytes else MonitorImageResult(status="unavailable", source="device")
            )

    prompt_attachment = ""
    payload = bytes(image_result.jpeg or b"")
    if payload:
        safe_reason = re.sub(r"[^A-Za-z0-9_-]+", "_", str(reason or "capture"))[:40]
        filename = f"monitor_{safe_reason}_{time.time_ns()}.jpg"
        save_monitor_screenshot(payload, filename)
        prompt_attachment = f"/screenshots/{filename}"

    return MonitorPromptContext(
        image_result=image_result,
        prompt_attachment=prompt_attachment,
        snapshot_attachment=save_monitor_camera_snapshot(
            image_result.camera_jpeg,
            reason,
        ),
    )


def acquire_monitor_image_sync(
    reason: str,
    *,
    send_command=None,
    should_continue=None,
    force_pc_screen: bool = False,
    timeout_seconds: float = 45.0,
    retry_after_seconds: float = 15.0,
) -> MonitorImageResult:
    if cam.cfg.get("active_source", "local") == "phone":
        phone_screen_requested_at = time.time()
        if send_command is None:
            if cam._loop is None:
                return MonitorImageResult(
                    status="unavailable",
                    source="phone",
                    no_image_context=PHONE_CAMERA_NO_IMAGE_CONTEXT,
                    error="event_loop_unavailable",
                )

            def send_command(client_id, event):
                future = asyncio.run_coroutine_threadsafe(
                    manager.send_to_client(client_id, event),
                    cam._loop,
                )
                return future.result(timeout=5)

        try:
            result = phone_camera.request_capture_sync(
                reason,
                send_command,
                timeout_seconds=timeout_seconds,
                retry_after_seconds=retry_after_seconds,
                should_continue=should_continue,
            )
        except RuntimeError as error:
            return MonitorImageResult(
                status="unavailable",
                source="phone",
                no_image_context=PHONE_CAMERA_NO_IMAGE_CONTEXT,
                error=str(error),
            )
        if result.status == "ready":
            from phone_screen import wait_for_phone_screen_after_sync
            wait_for_phone_screen_after_sync(
                phone_screen_requested_at,
                timeout_seconds=min(10.0, timeout_seconds),
                should_continue=should_continue,
            )
        return _phone_result_to_monitor(
            result,
            force_pc_screen=force_pc_screen,
            phone_screen_after=phone_screen_requested_at,
        )

    jpg, camera_jpg = cam.get_capture_jpegs(force_pc_screen=force_pc_screen)
    if jpg:
        return MonitorImageResult(
            status="ready",
            source="camera",
            jpeg=jpg,
            camera_jpeg=camera_jpg,
        )
    jpg = cam.get_screen_only_jpeg(force_pc_screen=force_pc_screen)
    if jpg:
        return MonitorImageResult(status="ready", source="device", jpeg=jpg)
    return MonitorImageResult(status="unavailable", source="device")


def cleanup_monitor_screenshots() -> None:
    """Keep every monitoring capture in one bounded screenshots pool."""
    max_keep = int(cam.cfg.get("max_screenshots", 200) or 0)
    if max_keep <= 0:
        return
    SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    files = sorted(
        (
            path for pattern in ("cam_*.jpg", "monitor_*.jpg", "phone_camera_*.jpg")
            for path in SCREENSHOTS_DIR.glob(pattern)
        ),
        key=lambda path: (path.stat().st_mtime_ns, path.name),
    )
    for path in files[:-max_keep]:
        path.unlink(missing_ok=True)


def save_monitor_screenshot(payload: bytes, filename: str) -> Path:
    SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    target = SCREENSHOTS_DIR / filename
    temp = target.with_suffix(".tmp")
    temp.write_bytes(bytes(payload))
    temp.replace(target)
    cleanup_monitor_screenshots()
    return target


def save_monitor_camera_snapshot(
    camera_jpeg: bytes | None,
    prefix: str,
    *,
    upload_dir: Path | None = None,
) -> dict | None:
    payload = bytes(camera_jpeg or b"")
    if not payload:
        return None
    safe_prefix = re.sub(r"[^A-Za-z0-9_-]+", "_", str(prefix or "capture"))[:40]
    filename = f"monitor_camera_{safe_prefix}_{time.time_ns()}.jpg"
    if upload_dir is None:
        save_monitor_screenshot(payload, filename)
    else:
        target_dir = Path(upload_dir)
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / filename
        temp = target.with_suffix(".tmp")
        temp.write_bytes(payload)
        temp.replace(target)
    return {
        "type": "monitor_camera_snapshot",
        "url": f"/screenshots/{filename}",
    }

async def perform_cam_check(conv_id: str, model_key: str):
    """Core 在聊天中主动请求查看监控画面：截图 → 发给 Core → 保存为新消息"""
    image_result = await acquire_monitor_image(
        "ai_cam_check",
        force_pc_screen=True,
    )
    jpg_bytes = image_result.jpeg
    frame_source = image_result.source
    fname = None

    if jpg_bytes:
        ts = time.strftime("%Y%m%d_%H%M%S")
        fname = f"cam_check_{ts}_{time.time_ns()}.jpg"
        save_monitor_screenshot(jpg_bytes, fname)

    wb = load_worldbook()
    user_name = wb.get("user_name") or "用户"
    ai_name = wb.get("ai_name") or "AI"

    # 构建人设前缀
    prefix = []
    if wb.get("ai_persona"):
        prefix.append({"role": "user", "content": f"[系统设定 - {ai_name}人设]\n{wb['ai_persona']}"})
        prefix.append({"role": "assistant", "content": "收到，我会按照设定扮演角色。"})
    if wb.get("user_persona"):
        prefix.append({"role": "user", "content": f"[系统设定 - {user_name}信息]\n{wb['user_persona']}"})
        prefix.append({"role": "assistant", "content": "收到，我会记住你的信息。"})

    # 获取最近对话上下文（统一时间线：合并私聊 + 群聊消息）
    from context_builder import fetch_merged_timeline, render_merged_timeline
    merged = await fetch_merged_timeline("aion", 6, conv_id=conv_id)
    recent = render_merged_timeline(
        merged,
        "aion",
        include_image_attachments=False,
    )

    layout_guidance = build_monitor_layout_guidance(
        bool(cam.cfg.get("include_pc_screen", False))
    )

    cam_prompt = (
        f"你刚才想看看{user_name}在干什么，这是系统抓取的实时监控画面。"
        f"{layout_guidance}如果没有CAMERA VIEW，就只根据设备上下文说明设备使用情况，不要推断身体位置。"
        f"请根据画面内容，自然地描述你看到的情况并和{user_name}互动。"
        f"不需要再说\"让我看看\"之类的话，直接说你看到了什么。"
    )
    if frame_source == "device":
        if cam.cfg.get("include_pc_screen", False):
            cam_prompt += "本次没有可用摄像头画面，系统改用电脑桌面和/或手机屏幕截图。"
        else:
            cam_prompt += "本次没有可用摄像头画面，仅附带了手机屏幕设备上下文。"
    if image_result.no_image_context:
        cam_prompt += image_result.no_image_context
    try:
        from health_context import build_heart_rate_prompt_block
        cam_prompt += await build_heart_rate_prompt_block(user_name)
    except Exception:
        pass
    from capabilities import build_band_note_ability_text
    passive_band_ability = build_band_note_ability_text(user_name, passive=True)
    passive_band_messages = []
    if passive_band_ability:
        passive_band_messages = [
            {"role": "user", "content": f"[系统能力]\n{passive_band_ability}"},
            {"role": "assistant", "content": "收到，需要时我会使用手环纸条提醒。"},
        ]
    cam_message = {"role": "user", "content": cam_prompt}
    if fname:
        cam_message["attachments"] = [f"/screenshots/{fname}"]
    messages = prefix + recent + passive_band_messages + [cam_message]
    from proactive_companionship import inject_proactive_ability
    inject_proactive_ability(messages, "aion")
    from autonomy_state import inject_autonomy_ability
    await inject_autonomy_ability(messages, "aion")
    from app_supervision_ai import inject_app_supervision_context
    inject_app_supervision_context(messages)

    # 预生成 msg_id（TTS 分段文件命名需要）
    msg_id = f"msg_{int(time.time()*1000)}_cc"
    from schedule import (
        _broadcast_trigger_debug,
        _consume_background_stream,
        _new_background_meta,
        _process_background_reply_commands,
    )
    usage_meta = _new_background_meta()
    has_error = False
    error_text = None

    # TTS：检查是否有前端开了 TTS
    cam_tts = None
    if manager.any_tts_enabled():
        tts_voice = manager.get_tts_voice()
        if tts_voice:
            cam_tts = TTSStreamer(msg_id, tts_voice, manager)

    cam_command_filter = WebCommandStreamFilter()
    _temp = SETTINGS.get("temperature")

    async def content_stream():
        async for chunk in stream_ai(messages, model_key, meta=usage_meta, temperature=_temp):
            if chunk.startswith(CLI_STATUS_PREFIX):
                continue
            yield chunk

    stream_result = await _consume_background_stream(
        content_stream(),
        cam_command_filter,
        cam_tts,
    )
    full_text = stream_result.committed_text
    has_error = stream_result.stop_reason is not None
    error_text = stream_result.diagnostic_error or stream_result.stop_reason
    safety_notice = stream_result.notice

    if not full_text.strip() and not safety_notice:
        return

    full_text = await _process_background_reply_commands(
        full_text,
        target={"type": "private"},
        conv_id=conv_id,
        sender="aion",
        ai_msg_id=msg_id,
    )
    if safety_notice:
        full_text = f"{full_text}\n\n[{safety_notice}]".strip()
    reasoning_content = (usage_meta.get("reasoning_content") or "").strip()
    snapshot_attachment = save_monitor_camera_snapshot(
        image_result.camera_jpeg,
        "cam_check",
    )
    system_atts = [snapshot_attachment] if snapshot_attachment else []
    system_atts.append({
        "type": "system_notice_order",
        "before_msg_id": msg_id,
    })
    # 插入系统提示：查看了监控画面
    sys_now = time.time()
    sys_msg_id = f"msg_{int(sys_now*1000)}_cc_sys"
    sys_content = f"{ai_name}查看了监控画面"
    async with get_db() as db:
        await db.execute(
            "INSERT INTO messages (id, conv_id, role, content, created_at, attachments) VALUES (?,?,?,?,?,?)",
            (
                sys_msg_id,
                conv_id,
                "system",
                sys_content,
                sys_now,
                json.dumps(system_atts, ensure_ascii=False)
                if system_atts else "[]",
            )
        )
        await db.commit()
    sys_msg = {"id": sys_msg_id, "conv_id": conv_id, "role": "system",
               "content": sys_content, "created_at": sys_now,
               "attachments": system_atts}
    await manager.broadcast({"type": "msg_created", "data": sys_msg})

    now = time.time()
    async with get_db() as db:
        await db.execute(
            "INSERT INTO messages (id, conv_id, role, content, created_at, attachments, reasoning_content) VALUES (?,?,?,?,?,?,?)",
            (msg_id, conv_id, "assistant", full_text, now, "[]", reasoning_content)
        )
        await db.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now, conv_id))
        await db.commit()

    ai_msg = {"id": msg_id, "conv_id": conv_id, "role": "assistant",
              "content": full_text, "created_at": now, "attachments": [],
              "reasoning_content": reasoning_content}
    await manager.broadcast({"type": "msg_created", "data": ai_msg})
    await _broadcast_trigger_debug(
        msg_id=msg_id,
        model_key=model_key,
        usage_meta=usage_meta,
        prompt_messages=messages,
        recalled_memories=[],
        has_error=has_error,
        error_text=error_text,
    )

    # 刷新 TTS 剩余文本
    if cam_tts:
        try:
            await cam_tts.flush()
        except Exception:
            pass

    from routes.files import export_conversation
    await export_conversation(conv_id)
