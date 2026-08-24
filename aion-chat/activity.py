"""
设备活动日志：上报接收、JSONL 存储、自动清理（保留最近 N 小时）、PC 活动窗口采集、10 分钟摘要
"""

import json, time, threading, logging, sqlite3
from pathlib import Path
from collections import defaultdict
from datetime import datetime, timedelta

from config import DATA_DIR, DB_PATH, load_worldbook
from device_context import DeviceContextStore
from app_names import KNOWN_APPS, resolve_app_name
from ws import manager

log = logging.getLogger("activity")

# ── 文件操作锁（保护 JSONL 读写不被 PC 线程和 API 协程并发冲突）──
_file_lock = threading.Lock()
_last_cleanup_ts = 0.0
_CLEANUP_INTERVAL = 300  # 每 5 分钟清理一次

# ── 路径 ──────────────────────────────────────────
ACTIVITY_LOGS_DIR = DATA_DIR / "activity_logs"
ACTIVITY_LOGS_DIR.mkdir(exist_ok=True)

# 保留最近 N 小时的活动日志和摘要
KEEP_HOURS = 8

device_context_store = DeviceContextStore()

# ── JSONL 读写 ────────────────────────────────────

def _today_log_path() -> Path:
    return ACTIVITY_LOGS_DIR / f"{time.strftime('%Y-%m-%d')}.jsonl"


def append_activity_log(entry: dict):
    """追加一条活动日志"""
    path = _today_log_path()
    with _file_lock:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def read_activity_logs(date_str: str = None) -> list:
    """读取指定日期的全部活动日志"""
    if not date_str:
        date_str = time.strftime("%Y-%m-%d")
    path = ACTIVITY_LOGS_DIR / f"{date_str}.jsonl"
    if not path.exists():
        return []
    entries = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except Exception:
                    pass
    return entries


def read_recent_activity(hours: int = KEEP_HOURS) -> list:
    """读取最近 N 小时内的活动日志（跨天也支持）"""
    import datetime as _dt
    cutoff_ts = time.time() - hours * 3600
    cutoff_date = _dt.date.fromtimestamp(cutoff_ts)
    result = []
    for logfile in sorted(ACTIVITY_LOGS_DIR.glob("*.jsonl")):
        try:
            if _dt.date.fromisoformat(logfile.stem) < cutoff_date:
                continue
        except ValueError:
            continue
        with open(logfile, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    if entry.get("timestamp", 0) >= cutoff_ts:
                        result.append(entry)
                except Exception:
                    pass
    return result


def get_available_dates() -> list[str]:
    """返回所有有日志的日期列表（降序）"""
    dates = []
    for logfile in ACTIVITY_LOGS_DIR.glob("*.jsonl"):
        dates.append(logfile.stem)
    dates.sort(reverse=True)
    return dates


def cleanup_old_activity_logs():
    """清理过期条目：只保留最近 KEEP_HOURS 小时的数据（每 5 分钟最多执行一次）"""
    global _last_cleanup_ts
    now = time.time()
    if now - _last_cleanup_ts < _CLEANUP_INTERVAL:
        return
    _last_cleanup_ts = now

    import datetime as _dt
    cutoff_ts = now - KEEP_HOURS * 3600
    cutoff_date = _dt.date.fromtimestamp(cutoff_ts)
    today = _dt.date.today()

    for logfile in list(ACTIVITY_LOGS_DIR.glob("*.jsonl")):
        try:
            file_date = _dt.date.fromisoformat(logfile.stem)
        except ValueError:
            continue

        # 比截止日期还早的文件整个删除
        if file_date < cutoff_date:
            logfile.unlink(missing_ok=True)
            continue

        # 截止日期当天的文件需要过滤条目
        if file_date == cutoff_date and file_date != today:
            # 加锁读取、过滤、回写
            with _file_lock:
                kept = []
                with open(logfile, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            entry = json.loads(line)
                            if entry.get("timestamp", 0) >= cutoff_ts:
                                kept.append(line)
                        except Exception:
                            pass
                if kept:
                    with open(logfile, "w", encoding="utf-8") as f:
                        f.write("\n".join(kept) + "\n")
                else:
                    logfile.unlink(missing_ok=True)


# ── PC 活动窗口采集 ─────────────────────────────────

class PCActivityTracker:
    """后台线程：定期采集 PC 当前活动窗口标题"""

    def __init__(self, interval: int = 60):
        self.interval = interval  # 采集间隔（秒）
        self._thread: threading.Thread | None = None
        self._running = False
        self._last_title = ""
        self._event_loop = None

    def set_event_loop(self, loop):
        self._event_loop = loop

    def start(self):
        import sys
        print("[PCActivity] start() 被调用", flush=True)
        if self._thread and self._thread.is_alive():
            print("[PCActivity] 线程已在运行，跳过", flush=True)
            return
        try:
            import win32gui  # noqa: F401
            print("[PCActivity] win32gui 导入成功", flush=True)
        except ImportError:
            print("[PCActivity] pywin32 未安装，PC 活动采集已禁用", flush=True)
            return
        except Exception as e:
            print(f"[PCActivity] win32gui 导入失败: {e}", flush=True)
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True, name="PCActivity")
        self._thread.start()
        print(f"[PCActivity] PC 活动采集已启动（间隔 {self.interval}s）", flush=True)

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None

    def _loop(self):
        try:
            import win32gui
            import win32process
        except Exception as e:
            print(f"[PCActivity] ❌ 线程内导入失败: {e}")
            self._running = False
            return
        import time as _time

        print(f"[PCActivity] 采集线程已进入循环 (pid={__import__('os').getpid()})", flush=True)

        while self._running:
            try:
                hwnd = win32gui.GetForegroundWindow()
                title = win32gui.GetWindowText(hwnd)

                # 忽略空标题和桌面（Program Manager 是 Windows 桌面，无意义）
                if title and title != "Program Manager":
                    # 尝试获取进程名
                    app_name = self._get_process_name(hwnd, win32process)
                    title_changed = title != self._last_title

                    now = _time.time()
                    entry = {
                        "timestamp": now,
                        "time": _time.strftime("%H:%M:%S"),
                        "date": _time.strftime("%Y-%m-%d"),
                        "device": "pc",
                        "app": app_name,
                        "title": title,
                    }
                    try:
                        display_status = pc_display_tracker.get_status()
                        entry["idle_seconds"] = display_status.get("idle_seconds")
                        entry["display"] = (
                            display_status.get("physical_state")
                            if display_status.get("physical_state") in {"on", "off"}
                            else display_status.get("state", "unknown")
                        )
                    except Exception:
                        entry["idle_seconds"] = None
                        entry["display"] = "unknown"
                    append_activity_log(entry)

                    if title_changed:
                        self._last_title = title
                        print(f"[PCActivity] {entry['time']} {app_name} - {title[:60]}", flush=True)

                    # 清理过期日志
                    try:
                        cleanup_old_activity_logs()
                    except Exception:
                        pass

                    # 广播给前端（仅窗口变化时广播，避免刷屏）
                    if title_changed and self._event_loop:
                        import asyncio
                        try:
                            asyncio.run_coroutine_threadsafe(
                                manager.broadcast({
                                    "type": "activity_log",
                                    "data": entry
                                }),
                                self._event_loop
                            )
                        except Exception as be:
                            print(f"[PCActivity] ⚠ 广播失败: {be}")

            except Exception as e:
                print(f"[PCActivity] ❌ 错误: {e}")
                import traceback
                traceback.print_exc()

            # 等待间隔（每秒检查一次是否需要停止）
            for _ in range(self.interval):
                if not self._running:
                    break
                _time.sleep(1)

        print("[PCActivity] 线程退出")

    @staticmethod
    def _get_process_name(hwnd, win32process) -> str:
        """根据窗口句柄获取进程名"""
        try:
            import psutil
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            proc = psutil.Process(pid)
            return proc.name()
        except Exception:
            pass
        # fallback: 无 psutil 时返回通用名称
        return "Unknown"


# ── PC 显示器状态采集 ───────────────────────────────

class PCDisplayTracker:
    """后台线程：监听 Windows 显示器电源状态，并提供键鼠空闲时间兜底。"""

    STATE_LABELS = {
        0: "off",
        1: "on",
        2: "dimmed",
    }

    def __init__(self):
        self._thread: threading.Thread | None = None
        self._running = False
        self._state = "unknown"
        self._last_change_ts = 0.0
        self._physical_state = "unknown"
        self._physical_last_probe_ts = 0.0
        self._physical_probe_supported = False
        self._physical_unreachable_count = 0
        self._hwnd = None
        self._notify_handle = None
        self._wnd_proc_ref = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        import os
        if os.name != "nt":
            print("[PCDisplay] 非 Windows 环境，显示器状态采集已禁用", flush=True)
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True, name="PCDisplay")
        self._thread.start()
        print("[PCDisplay] 显示器状态采集已启动", flush=True)

    def stop(self):
        self._running = False
        if self._hwnd:
            try:
                import win32gui
                win32gui.PostMessage(self._hwnd, 0x0010, 0, 0)  # WM_CLOSE
            except Exception:
                pass
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None

    def get_state(self) -> str:
        return self._state

    def get_status(self) -> dict:
        return {
            "state": self._state,
            "physical_state": self._physical_state,
            "physical_probe_supported": self._physical_probe_supported,
            "physical_unreachable_count": self._physical_unreachable_count,
            "physical_last_probe_ts": self._physical_last_probe_ts,
            "last_change_ts": self._last_change_ts,
            "idle_seconds": self.get_idle_seconds(),
            "running": self._running,
            "thread_alive": self._thread is not None and self._thread.is_alive(),
        }

    def should_capture_screen(self, idle_skip_seconds: int = 10 * 60, probe_physical: bool = True) -> bool:
        """是否应该截取 PC 屏幕。显示器关闭时跳过；状态未知时用空闲时间兜底。"""
        if probe_physical:
            self.refresh_physical_state()
            if self._physical_state == "off":
                return False
            if self._physical_state == "on":
                return True
        if self._state == "off":
            return False
        if self._state == "unknown":
            idle = self.get_idle_seconds()
            if idle is not None and idle >= idle_skip_seconds:
                return False
        return True

    def refresh_physical_state(self, min_interval_seconds: int = 5) -> str:
        """Best-effort 查询物理显示器电源模式，补手动按显示器电源键的场景。"""
        now = time.time()
        if now - self._physical_last_probe_ts < min_interval_seconds:
            return self._physical_state
        self._physical_last_probe_ts = now
        try:
            state = self._probe_physical_monitor_power_state()
        except Exception as e:
            print(f"[PCDisplay] 物理显示器探测失败: {e}", flush=True)
            state = "unknown"

        if state == "on":
            self._physical_state = "on"
            self._physical_probe_supported = True
            self._physical_unreachable_count = 0
        elif state == "off":
            self._physical_state = "off"
            self._physical_probe_supported = True
            self._physical_unreachable_count = 0
        elif state == "unreachable":
            if self._physical_probe_supported:
                self._physical_unreachable_count += 1
                if self._physical_unreachable_count >= 2:
                    self._physical_state = "off"
            else:
                self._physical_state = "unknown"
        else:
            self._physical_state = "unknown"
        return self._physical_state

    def get_idle_seconds(self) -> float | None:
        """返回当前用户会话的键鼠空闲秒数，失败返回 None。"""
        try:
            import ctypes
            from ctypes import wintypes

            class LASTINPUTINFO(ctypes.Structure):
                _fields_ = [
                    ("cbSize", wintypes.UINT),
                    ("dwTime", wintypes.DWORD),
                ]

            lii = LASTINPUTINFO()
            lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
            if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii)):
                return None
            tick = ctypes.windll.kernel32.GetTickCount()
            return max(0, (tick - lii.dwTime) / 1000.0)
        except Exception:
            return None

    def _probe_physical_monitor_power_state(self) -> str:
        """通过 DDC/CI VCP 0xD6 查询物理显示器电源模式。"""
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        dxva2 = ctypes.windll.dxva2

        class RECT(ctypes.Structure):
            _fields_ = [
                ("left", wintypes.LONG),
                ("top", wintypes.LONG),
                ("right", wintypes.LONG),
                ("bottom", wintypes.LONG),
            ]

        class PHYSICAL_MONITOR(ctypes.Structure):
            _fields_ = [
                ("hPhysicalMonitor", wintypes.HANDLE),
                ("szPhysicalMonitorDescription", wintypes.WCHAR * 128),
            ]

        monitors = []
        monitor_enum_proc = ctypes.WINFUNCTYPE(
            wintypes.BOOL,
            wintypes.HMONITOR,
            wintypes.HDC,
            ctypes.POINTER(RECT),
            wintypes.LPARAM,
        )

        def enum_proc(hmonitor, hdc, rect, data):
            monitors.append(hmonitor)
            return True

        user32.EnumDisplayMonitors(0, 0, monitor_enum_proc(enum_proc), 0)
        if not monitors:
            return "unknown"

        dxva2.GetNumberOfPhysicalMonitorsFromHMONITOR.argtypes = [
            wintypes.HMONITOR,
            ctypes.POINTER(wintypes.DWORD),
        ]
        dxva2.GetPhysicalMonitorsFromHMONITOR.argtypes = [
            wintypes.HMONITOR,
            wintypes.DWORD,
            ctypes.POINTER(PHYSICAL_MONITOR),
        ]
        dxva2.DestroyPhysicalMonitors.argtypes = [
            wintypes.DWORD,
            ctypes.POINTER(PHYSICAL_MONITOR),
        ]
        dxva2.GetVCPFeatureAndVCPFeatureReply.argtypes = [
            wintypes.HANDLE,
            wintypes.BYTE,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(wintypes.DWORD),
        ]

        success_count = 0
        on_count = 0
        off_count = 0

        for hmonitor in monitors:
            count = wintypes.DWORD()
            if not dxva2.GetNumberOfPhysicalMonitorsFromHMONITOR(hmonitor, ctypes.byref(count)) or count.value <= 0:
                continue
            arr = (PHYSICAL_MONITOR * count.value)()
            if not dxva2.GetPhysicalMonitorsFromHMONITOR(hmonitor, count, arr):
                continue
            try:
                for item in arr:
                    code_type = wintypes.DWORD()
                    current = wintypes.DWORD()
                    maximum = wintypes.DWORD()
                    ok = dxva2.GetVCPFeatureAndVCPFeatureReply(
                        item.hPhysicalMonitor,
                        0xD6,
                        ctypes.byref(code_type),
                        ctypes.byref(current),
                        ctypes.byref(maximum),
                    )
                    if not ok:
                        continue
                    success_count += 1
                    # MCCS VCP D6: 1=on, 2=standby, 3=suspend, 4=off, 5=hard off
                    if current.value == 1:
                        on_count += 1
                    elif current.value in (2, 3, 4, 5):
                        off_count += 1
            finally:
                try:
                    dxva2.DestroyPhysicalMonitors(count, arr)
                except Exception:
                    pass

        if on_count > 0:
            return "on"
        if success_count > 0 and off_count == success_count:
            return "off"
        if success_count == 0:
            return "unreachable"
        return "unknown"

    def _loop(self):
        try:
            import ctypes
            import time as _time
            import win32con
            import win32gui
            from ctypes import wintypes
        except Exception as e:
            print(f"[PCDisplay] 初始化失败: {e}", flush=True)
            self._running = False
            return

        WM_POWERBROADCAST = 0x0218
        PBT_POWERSETTINGCHANGE = 0x8013
        DEVICE_NOTIFY_WINDOW_HANDLE = 0x00000000

        class GUID(ctypes.Structure):
            _fields_ = [
                ("Data1", wintypes.DWORD),
                ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD),
                ("Data4", ctypes.c_ubyte * 8),
            ]

        guid_console_display_state = GUID(
            0x6FE69556,
            0x704A,
            0x47A0,
            (ctypes.c_ubyte * 8)(0x8F, 0x24, 0xC2, 0x8D, 0x93, 0x6F, 0xDA, 0x47),
        )

        def wnd_proc(hwnd, msg, wparam, lparam):
            if msg == WM_POWERBROADCAST and wparam == PBT_POWERSETTINGCHANGE and lparam:
                try:
                    # POWERBROADCAST_SETTING = GUID + DWORD DataLength + BYTE Data[1]
                    data_offset = ctypes.sizeof(GUID) + ctypes.sizeof(wintypes.DWORD)
                    value = wintypes.DWORD.from_address(int(lparam) + data_offset).value
                    state = self.STATE_LABELS.get(value, "unknown")
                    if state != self._state:
                        self._state = state
                        self._last_change_ts = time.time()
                        print(f"[PCDisplay] display_state={state}", flush=True)
                except Exception as e:
                    print(f"[PCDisplay] 状态解析失败: {e}", flush=True)
            return win32gui.DefWindowProc(hwnd, msg, wparam, lparam)

        try:
            class_name = "AionPcDisplayTracker"
            wc = win32gui.WNDCLASS()
            wc.lpfnWndProc = wnd_proc
            wc.lpszClassName = class_name
            try:
                win32gui.RegisterClass(wc)
            except Exception:
                pass
            self._wnd_proc_ref = wnd_proc
            self._hwnd = win32gui.CreateWindow(
                class_name,
                class_name,
                0,
                0, 0, 0, 0,
                0,
                0,
                0,
                None,
            )
            ctypes.windll.user32.RegisterPowerSettingNotification.restype = wintypes.HANDLE
            ctypes.windll.user32.RegisterPowerSettingNotification.argtypes = [
                wintypes.HANDLE,
                ctypes.POINTER(GUID),
                wintypes.DWORD,
            ]
            self._notify_handle = ctypes.windll.user32.RegisterPowerSettingNotification(
                wintypes.HANDLE(self._hwnd),
                ctypes.byref(guid_console_display_state),
                DEVICE_NOTIFY_WINDOW_HANDLE,
            )
            if not self._notify_handle:
                print("[PCDisplay] RegisterPowerSettingNotification 失败，启用空闲时间兜底", flush=True)
            while self._running:
                win32gui.PumpWaitingMessages()
                _time.sleep(0.25)
        except Exception as e:
            print(f"[PCDisplay] 监听线程异常: {e}", flush=True)
        finally:
            try:
                if self._notify_handle:
                    ctypes.windll.user32.UnregisterPowerSettingNotification(self._notify_handle)
            except Exception:
                pass
            try:
                if self._hwnd:
                    win32gui.DestroyWindow(self._hwnd)
            except Exception:
                pass
            self._hwnd = None
            self._notify_handle = None
            self._running = False
            print("[PCDisplay] 线程退出", flush=True)


# ── 10 分钟活动摘要 ──────────────────────────────────

# App 名称美化映射（进程名 → 简称）
_APP_DISPLAY_MAP = {
    "explorer.exe": "文件管理", "msedge.exe": "Edge", "chrome.exe": "Chrome",
    "Code.exe": "VS Code", "Photoshop.exe": "PS", "WindowsTerminal.exe": "终端",
    "notepad++.exe": "Notepad++", "Notepad.exe": "记事本",
    "ApplicationFrameHost.exe": None,  # 由标题决定，_extract_hints 会提取
    "screen_off": "锁屏", "screen_on": "亮屏",
}


def _beautify_app(app: str, titles: set[str]) -> str:
    """将进程名/app名美化为简短可读名"""
    # 精确匹配优先
    if app in _APP_DISPLAY_MAP:
        name = _APP_DISPLAY_MAP[app]
        if name:
            return name
        # None 表示由标题决定，从 titles 取第一个有意义的词
        for t in titles:
            short = t.split(" - ")[0].strip()[:15]
            if short and "." not in short:
                return short
        return ""
    # 手机包名查 KNOWN_APPS
    if "." in app and app in KNOWN_APPS:
        resolved = KNOWN_APPS[app]
        if resolved:
            return resolved
    # Tortoise 系列 → SVN
    if "Tortoise" in app:
        return "SVN"
    # Claude
    if "claude" in app.lower() or any("claude" in t.lower() for t in titles):
        return "Claude"
    # 去掉 .exe 后缀
    if app.endswith(".exe"):
        return app[:-4]
    return app


def _extract_hints(app: str, titles: set[str]) -> list[str]:
    """从窗口标题提取关键上下文信息"""
    hints = []
    for t in titles:
        if "bilibili" in t.lower() or "哔哩" in t:
            name = t.split("_哔哩")[0].split("_bilibili")[0]
            if "和另外" in name:
                name = name.split(" 和另外")[0]
            hints.append(f"B站:{name[:25]}")
        elif "Visual Studio Code" in t:
            hints.append(t.split(" - ")[0].strip())
        elif "便笺" in t:
            hints.append("便笺")
        elif "文件资源管理器" in t:
            folder = t.replace(" - 文件资源管理器", "")
            if folder and folder != "Program Manager":
                hints.append(folder)
        elif "Commit" in t and ("SVN" in t or "Tortoise" in t):
            hints.append("提交代码")
        elif "TortoiseMerge" in t:
            fname = t.split(" - ")[0].strip()
            hints.append(f"对比{fname}")
        elif "Aion Chat" in t:
            hints.append("Aion Chat")
        elif t and t != app and "screen_" not in t and "Program Manager" not in t:
            short = t.split(" - ")[0].strip()[:20]
            if short and "." not in short:
                hints.append(short)
    # 去重保序
    seen = []
    for h in hints:
        if h not in seen:
            seen.append(h)
    return seen[:3]


def _format_duration(seconds: float) -> str:
    """格式化持续时间为可读字符串"""
    minutes = round(seconds / 60)
    if minutes < 1:
        return ""
    if minutes >= 60:
        h, m = divmod(minutes, 60)
        return f"{h}小时{m}分钟" if m else f"{h}小时"
    return f"{minutes}分钟"


def _summarize_window(entries: list[dict], window_start_ts: float, window_end_ts: float,
                      carry_forward: dict[str, dict] = None) -> str:
    """
    对一个 10 分钟窗口内的条目生成带时长权重的摘要。
    carry_forward: {device: entry} 每个设备在窗口开始前的最后一条记录，用于填补窗口开头的空白。
    """
    by_device = defaultdict(list)
    for e in entries:
        by_device[e["device"]].append(e)

    # 注入 carry_forward：如果某设备在窗口内有数据但第一条不在窗口起始，
    # 或者窗口内没数据但 carry_forward 有记录，补一条起始状态
    if carry_forward:
        for device, prev_entry in carry_forward.items():
            dev_list = by_device.get(device, [])
            if dev_list:
                earliest = min(e["timestamp"] for e in dev_list)
                if earliest > window_start_ts + 5:  # 有>5秒空白
                    synthetic = dict(prev_entry)
                    synthetic["timestamp"] = window_start_ts
                    by_device[device] = [synthetic] + dev_list
            else:
                # 窗口内没数据，用 carry_forward 填满整个窗口
                synthetic = dict(prev_entry)
                synthetic["timestamp"] = window_start_ts
                by_device[device] = [synthetic]

    parts = []
    for device in ["pc", "phone"]:
        if device not in by_device:
            continue
        dev_entries = sorted(by_device[device], key=lambda x: x["timestamp"])
        device_label = "手机" if device == "phone" else "PC"

        # 过滤"亮屏"（仅是过渡事件，锁屏时长由 screen_off→下一条 自动涵盖）
        dev_entries = [e for e in dev_entries if e["app"] != "screen_on"]
        if device == "pc":
            dev_entries = [
                e for e in dev_entries
                if e.get("display") != "off"
                and (
                    e.get("idle_seconds") is None
                    or float(e.get("idle_seconds") or 0) <= 300
                )
            ]
        if not dev_entries:
            continue

        # ① 构建连续段：(app, titles, duration_seconds)
        segments = []
        i = 0
        while i < len(dev_entries):
            app = dev_entries[i]["app"]
            start_ts = dev_entries[i]["timestamp"]
            titles = set()
            t = dev_entries[i].get("title", "")
            if t:
                titles.add(t)

            j = i + 1
            while j < len(dev_entries) and dev_entries[j]["app"] == app:
                t2 = dev_entries[j].get("title", "")
                if t2:
                    titles.add(t2)
                j += 1

            # 持续时间 = 到下一段起始；最后一段取窗口结束（不超过当前时间）
            if j < len(dev_entries):
                end_ts = dev_entries[j]["timestamp"]
            else:
                end_ts = min(window_end_ts, time.time())
            duration = max(0, end_ts - start_ts)
            segments.append((app, titles, duration))
            i = j

        # ② 按 display_name 合并同名段，累加时长和标题
        merged_order = []
        merged_dur: dict[str, float] = {}
        merged_titles: dict[str, set[str]] = {}
        merged_raw: dict[str, set[str]] = {}

        for app, titles, dur in segments:
            display = _beautify_app(app, titles)
            if not display:
                display = app
            dkey = display
            if dkey not in merged_dur:
                merged_dur[dkey] = 0
                merged_titles[dkey] = set()
                merged_raw[dkey] = set()
                merged_order.append(dkey)
            merged_dur[dkey] += dur
            merged_titles[dkey] |= titles
            merged_raw[dkey].add(app)

        # ③ 按时长降序排列（主要活动排前面）
        sorted_apps = sorted(merged_order, key=lambda k: merged_dur[k], reverse=True)

        app_descs = []
        for dkey in sorted_apps:
            dur_str = _format_duration(merged_dur[dkey])
            titles = merged_titles[dkey]
            raw_apps = merged_raw[dkey]
            hints = _extract_hints(next(iter(raw_apps)), titles)
            hints = [h for h in hints if h != dkey]

            if hints and dur_str:
                desc = f"{dkey}({', '.join(hints)}) {dur_str}"
            elif dur_str:
                desc = f"{dkey} {dur_str}"
            elif hints:
                desc = f"{dkey}({', '.join(hints)})"
            else:
                desc = dkey
            app_descs.append(desc)

        if app_descs:
            parts.append(f"{device_label}: {', '.join(app_descs)}")

    return " | ".join(parts)


def generate_activity_summary(hours: int = KEEP_HOURS) -> list[dict]:
    """
    对最近 N 小时的原始活动日志生成 10 分钟窗口摘要。
    空窗口标记为"没有活动"，连续空窗口自动合并。
    时间范围：第一条记录所在窗口 ~ 上一个已结束的完整窗口。
    """
    now = time.time()
    entries = read_recent_activity(hours)
    if not entries:
        return []

    # 过滤系统应用（不做 resolve，保留原始 app 名给 _beautify_app 用）
    filtered = []
    for e in entries:
        if e.get("device") == "home" and e.get("kind") == "home_sensor":
            continue
        app = e.get("app", "")
        if "." in app and app in KNOWN_APPS and KNOWN_APPS[app] is None:
            continue
        if app == "explorer.exe" and e.get("title", "") == "Program Manager":
            continue
        filtered.append(e)

    if not filtered:
        return []

    # 按 10 分钟窗口分组
    windows = defaultdict(list)
    for e in filtered:
        dt = datetime.fromtimestamp(e["timestamp"])
        block = (dt.minute // 10) * 10
        key = f"{dt.hour:02d}:{block:02d}"
        windows[key].append(e)

    # 时间范围：第一条记录所在窗口起始 ~ 当前时间所在窗口起始（不含当前未完成窗口）
    first_ts = min(e["timestamp"] for e in filtered)
    dt_first = datetime.fromtimestamp(first_ts)
    dt_start = dt_first.replace(minute=(dt_first.minute // 10) * 10, second=0, microsecond=0)

    dt_now = datetime.fromtimestamp(now)
    # 当前所在窗口的起始时间（这个窗口还在进行中，不生成摘要）
    dt_current_window = dt_now.replace(minute=(dt_now.minute // 10) * 10, second=0, microsecond=0)

    all_keys = []
    cursor = dt_start
    while cursor < dt_current_window:
        all_keys.append((cursor, f"{cursor.hour:02d}:{cursor.minute:02d}"))
        cursor += timedelta(minutes=10)

    if not all_keys:
        return []

    # 按时间排序所有条目（用于 carry_forward 追溯）
    filtered.sort(key=lambda e: e["timestamp"])

    # 生成每个窗口的摘要（含空窗口）
    raw_items = []
    for block_dt, key in all_keys:
        window_start_ts = block_dt.timestamp()
        window_end_ts = (block_dt + timedelta(minutes=10)).timestamp()
        start_str = f"{block_dt.hour:02d}:{block_dt.minute:02d}"
        end_dt = block_dt + timedelta(minutes=10)
        end_str = f"{end_dt.hour:02d}:{end_dt.minute:02d}"

        # 计算 carry_forward：每个设备在本窗口之前的最后一条记录
        carry_forward: dict[str, dict] = {}
        for e in filtered:
            if e["timestamp"] >= window_start_ts:
                break
            carry_forward[e["device"]] = e

        if key in windows:
            w_entries = windows[key]
            summary = _summarize_window(w_entries, window_start_ts, window_end_ts, carry_forward)
            raw_items.append({
                "start": start_str,
                "end": end_str,
                "summary": summary or "没有活动",
                "count": len(w_entries),
                "empty": not summary,
            })
        else:
            # 窗口内无数据，但 carry_forward 可能有状态
            summary = _summarize_window([], window_start_ts, window_end_ts, carry_forward)
            raw_items.append({
                "start": start_str,
                "end": end_str,
                "summary": summary or "没有活动",
                "count": 0,
                "empty": not summary,
            })

    # 合并连续的"没有活动"窗口
    result = []
    i = 0
    while i < len(raw_items):
        item = raw_items[i]
        if item["empty"]:
            # 向后找连续空窗口
            j = i + 1
            while j < len(raw_items) and raw_items[j]["empty"]:
                j += 1
            merged_start = raw_items[i]["start"]
            merged_end = raw_items[j - 1]["end"]
            result.append({
                "start": merged_start,
                "end": merged_end,
                "summary": "没有活动",
                "count": 0,
            })
            i = j
        else:
            result.append({
                "start": item["start"],
                "end": item["end"],
                "summary": item["summary"],
                "count": item["count"],
            })
            i += 1

    return result


# ── 活动追踪总开关 ─────────────────────────────────

def is_activity_tracking_enabled() -> bool:
    """检查活动追踪总开关是否开启"""
    from config import SETTINGS
    return SETTINGS.get("activity_tracking_enabled", True)


def set_activity_tracking_enabled(enabled: bool):
    """设置活动追踪总开关"""
    from config import SETTINGS, save_settings
    SETTINGS["activity_tracking_enabled"] = enabled
    save_settings(SETTINGS)


def get_activity_summary_for_prompt(n: int = 6) -> str:
    """
    获取最新 n 条 10 分钟摘要，格式化为可注入 prompt 的文本。
    n 会被 clamp 到 1-12（对应 10-120 分钟）。
    总开关关闭时返回空字符串。
    """
    if not is_activity_tracking_enabled():
        return ""
    n = max(1, min(12, n))
    summaries = generate_activity_summary(hours=KEEP_HOURS)
    if not summaries:
        return ""
    # 取最新 n 条（列表末尾是最新的）
    recent = summaries[-n:]
    lines = []
    for s in recent:
        lines.append(f"[{s['start']}~{s['end']}] {s['summary']}")
    return "\n".join(lines)


def _context_log_entry(event: dict) -> dict:
    timestamp = float(event.get("timestamp") or time.time())
    entry = dict(event)
    entry.update({
        "timestamp": timestamp,
        "time": time.strftime("%H:%M:%S", time.localtime(timestamp)),
        "date": time.strftime("%Y-%m-%d", time.localtime(timestamp)),
        "device": "phone",
        "app": "device_context",
        "title": event.get("kind", "device_context"),
    })
    return entry


def record_phone_context(payload: dict, received_at: float | None = None) -> list[dict]:
    received_at = float(received_at or time.time())
    changes = device_context_store.update_phone(payload, received_at)
    for change in changes:
        append_activity_log(_context_log_entry(change))
    return changes


def record_notification(payload: dict, received_at: float | None = None) -> dict:
    received_at = float(received_at or time.time())
    event = device_context_store.upsert_notification(payload, received_at)
    if event:
        append_activity_log(_context_log_entry(event))
    return event


def remove_notification_context(key: str, received_at: float | None = None) -> dict | None:
    received_at = float(received_at or time.time())
    event = device_context_store.remove_notification(key, received_at)
    if event:
        append_activity_log(_context_log_entry(event))
    return event


def get_current_pc_context(now: float | None = None) -> dict:
    now = float(now or time.time())
    result = {"display": "unknown", "idle_seconds": None}
    try:
        status = pc_display_tracker.get_status()
        physical = status.get("physical_state")
        result["display"] = physical if physical in {"on", "off"} else status.get("state", "unknown")
        result["idle_seconds"] = status.get("idle_seconds")
    except Exception:
        pass

    recent_pc = [
        entry for entry in read_recent_activity(1)
        if entry.get("device") == "pc" and entry.get("app") not in {"screen_on", "screen_off"}
    ]
    if recent_pc:
        latest = max(recent_pc, key=lambda entry: entry.get("timestamp", 0))
        if now - float(latest.get("timestamp") or 0) <= 180:
            result["app"] = _beautify_app(latest.get("app", ""), {latest.get("title", "")})
            result["title"] = latest.get("title", "")
            result["window_observed_at"] = latest.get("timestamp")
    return result


def get_device_context_snapshot(
        now: float | None = None, pc: dict | None = None) -> dict:
    now = float(now or time.time())
    return device_context_store.snapshot(pc if pc is not None else get_current_pc_context(now), now)


def get_device_context_for_prompt(max_chars: int = 800) -> str:
    if not is_activity_tracking_enabled():
        return ""
    try:
        now = time.time()
        return device_context_store.prompt(get_current_pc_context(now), now, max_chars)
    except Exception:
        return ""


# 全局单例
def _user_dynamic_names() -> dict:
    wb = load_worldbook()
    user_name = wb.get("user_name", "用户")
    ai_name = wb.get("ai_name", "AI")
    connor_name = "Connor"
    try:
        cfg_path = DATA_DIR / "chatroom_config.json"
        if cfg_path.exists():
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            connor_name = cfg.get("connor_name", connor_name) or connor_name
    except Exception:
        pass
    return {
        "user": user_name,
        "assistant": ai_name,
        "aion": ai_name,
        "connor": connor_name,
        "system": "系统",
    }


def _user_dynamic_actor(actor: str, names: dict) -> str:
    return names.get(actor, actor or "未知")


def _user_dynamic_clip(text: str, max_len: int = 80) -> str:
    text = (text or "").strip()
    if len(text) <= max_len:
        return text
    return text[:max_len].rstrip() + "..."


def get_user_dynamics_for_prompt(hours: int = 1, limit: int = 12) -> str:
    """
    获取最近用户关键动态，格式化为可注入主动观察/监督 prompt 的短文本。
    只包含：朋友圈、日记、礼物、位置状态变化；没有动态时返回空字符串。
    """
    hours = max(1, min(24, hours))
    limit = max(1, min(30, limit))
    cutoff = time.time() - hours * 3600
    names = _user_dynamic_names()
    user_name = _user_dynamic_actor("user", names)
    items: list[tuple[float, str]] = []

    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row

            rows = conn.execute(
                "SELECT id, author, content, attachments, created_at FROM moments "
                "WHERE created_at >= ? ORDER BY created_at DESC LIMIT ?",
                (cutoff, limit),
            ).fetchall()
            for row in rows:
                actor = _user_dynamic_actor(row["author"], names)
                try:
                    atts = json.loads(row["attachments"] or "[]")
                except Exception:
                    atts = []
                suffix = f"（{len(atts)}张图）" if atts else ""
                detail = _user_dynamic_clip(row["content"])
                text = f"{actor} 发布了朋友圈{suffix}"
                if detail:
                    text += f"：{detail}"
                items.append((float(row["created_at"]), text))

            rows = conn.execute(
                "SELECT id, author, title, content, created_at FROM diary_entries "
                "WHERE created_at >= ? ORDER BY created_at DESC LIMIT ?",
                (cutoff, limit),
            ).fetchall()
            for row in rows:
                actor = _user_dynamic_actor(row["author"], names)
                detail = _user_dynamic_clip(row["title"] or row["content"])
                text = f"{actor} 发布了日记"
                if detail:
                    text += f"：{detail}"
                items.append((float(row["created_at"]), text))

            rows = conn.execute(
                "SELECT id, message, created_at, sender FROM gifts "
                "WHERE created_at >= ? ORDER BY created_at DESC LIMIT ?",
                (cutoff, limit),
            ).fetchall()
            for row in rows:
                actor = _user_dynamic_actor(row["sender"] or "aion", names)
                detail = _user_dynamic_clip(row["message"])
                text = f"{actor} 给 {user_name} 送了礼物"
                if detail:
                    text += f"：{detail}"
                items.append((float(row["created_at"]), text))
    except Exception:
        pass

    try:
        status_path = DATA_DIR / "location_status.json"
        if status_path.exists():
            status = json.loads(status_path.read_text(encoding="utf-8"))
            changed_at = float(status.get("state_changed_at") or 0)
            if changed_at >= cutoff:
                state = status.get("state", "unknown")
                state_label = {"at_home": "在家", "outside": "外出", "unknown": "未知"}.get(state, state)
                detail = _user_dynamic_clip(status.get("address", ""), 60)
                text = f"{user_name} 的位置状态变为{state_label}"
                if detail:
                    text += f"：{detail}"
                items.append((changed_at, text))
    except Exception:
        pass

    if not items:
        return ""

    items.sort(key=lambda x: x[0])
    return "\n".join(
        f"[{time.strftime('%H:%M', time.localtime(ts))}] {text}"
        for ts, text in items[-limit:]
    )


pc_tracker = PCActivityTracker()
pc_display_tracker = PCDisplayTracker()

try:
    device_context_store.hydrate(
        [
            entry
            for entry in read_recent_activity(KEEP_HOURS)
            if entry.get("kind") in {"phone_context", "notification", "notification_removed"}
        ],
        time.time(),
    )
except Exception:
    pass
