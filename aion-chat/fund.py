"""
奥罗斯财团 — 基金持仓监控核心模块
- 数据拉取（akshare）
- 持仓计算
- 历史走势查询
- AI 分析 prompt 生成
- 每日定时任务（14:45 交易日自动触发）
"""

import asyncio, json, time, threading, logging
from datetime import datetime, date

import aiosqlite
import akshare as ak
from chinese_calendar import is_workday

from config import (
    DB_PATH, DATA_DIR, DEFAULT_MODEL, SETTINGS,
    load_worldbook,
)
from database import get_db
from ws import manager
from ai_providers import stream_ai, CLI_STATUS_PREFIX
from tts import TTSStreamer
from web_search import WebCommandStreamFilter

log = logging.getLogger("fund")

FUND_CONFIG_PATH = DATA_DIR / "fund_config.json"
FUND_CACHE_PATH = DATA_DIR / "fund_cache.json"


# ── 配置读写 ─────────────────────────────────────
def load_fund_config() -> dict:
    if FUND_CONFIG_PATH.exists():
        try:
            return json.loads(FUND_CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"enabled": True, "tendency": ""}


def save_fund_config(data: dict):
    FUND_CONFIG_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# ── 交易日判断 ────────────────────────────────────
def is_trading_day(d: date | None = None) -> bool:
    d = d or date.today()
    if d.weekday() >= 5:
        return False
    try:
        return is_workday(d)
    except Exception:
        return d.weekday() < 5


# ── 数据拉取 ─────────────────────────────────────
def fetch_fund_data(holdings: list[dict]) -> dict:
    """
    拉取所有持仓基金的最新净值、涨跌幅，并计算盈亏。
    返回结构化数据 dict。
    """
    if not holdings:
        return {"funds": [], "index": None, "fetch_time": datetime.now().isoformat()}

    # 1. 拉取开放式基金今日数据（全量表，按代码筛选）
    fund_daily = None
    try:
        fund_daily = ak.fund_open_fund_daily_em()
    except Exception as e:
        log.warning("拉取基金日数据失败: %s", e)

    # 2. 拉取上证指数（新浪财经接口，稳定可靠）
    index_info = None
    try:
        import requests as _req
        resp = _req.get(
            "https://hq.sinajs.cn/list=s_sh000001",
            headers={"Referer": "https://finance.sina.com.cn"},
            timeout=10,
        )
        # 返回格式: var hq_str_s_sh000001="上证指数,4079.90,-13.35,-0.33,6050317,114300020";
        text = resp.text.strip()
        if '"' in text:
            parts = text.split('"')[1].split(",")
            if len(parts) >= 4:
                index_info = {
                    "name": parts[0],
                    "date": datetime.now().strftime("%Y-%m-%d"),
                    "close": float(parts[1]),
                    "change_pct": float(parts[3]),
                }
    except Exception as e:
        log.warning("拉取上证指数失败: %s", e)

    # 3. 逐只匹配
    results = []
    for h in holdings:
        code = h["fund_code"]
        info = {
            "fund_code": code,
            "fund_name": h["fund_name"],
            "shares": h["shares"],
            "avg_cost": h["avg_cost"],
            "total_cost": h["total_cost"],
            "warn_down": h["warn_down"],
            "warn_up": h["warn_up"],
            "latest_nav": None,
            "prev_nav": None,
            "day_change_pct": None,
            "market_value": None,
            "profit": None,
            "profit_pct": None,
            "alert": None,
        }

        # 先从全量日数据表取（盘中/收盘后有值的基金）
        got_nav = False
        if fund_daily is not None:
            row = fund_daily[fund_daily["基金代码"] == code]
            if len(row) > 0:
                r = row.iloc[0]
                cols = fund_daily.columns.tolist()
                nav_cols = [c for c in cols if c.endswith("-单位净值")]
                nav_cols.sort()
                if nav_cols:
                    # 尝试最新列
                    latest_nav_str = r[nav_cols[-1]]
                    if latest_nav_str and str(latest_nav_str).strip():
                        try:
                            info["latest_nav"] = float(latest_nav_str)
                            got_nav = True
                        except (ValueError, TypeError):
                            pass
                    # 前一日列
                    if len(nav_cols) >= 2:
                        prev_nav_str = r[nav_cols[-2]]
                        if prev_nav_str and str(prev_nav_str).strip():
                            try:
                                info["prev_nav"] = float(prev_nav_str)
                            except (ValueError, TypeError):
                                pass
                    # 如果最新列为空，用前一日列作为 latest
                    if not got_nav and info["prev_nav"] is not None:
                        info["latest_nav"] = info["prev_nav"]
                        got_nav = True

                day_chg = r.get("日增长率")
                if day_chg and str(day_chg).strip():
                    try:
                        info["day_change_pct"] = float(day_chg)
                    except (ValueError, TypeError):
                        pass
                info["fund_name"] = r.get("基金简称", info["fund_name"])

        # 如果全量表没取到净值或日增长率，用历史净值接口兜底
        if not got_nav or info["day_change_pct"] is None:
            try:
                hist_df = ak.fund_open_fund_info_em(symbol=code, indicator="单位净值走势")
                if hist_df is not None and len(hist_df) > 0:
                    last_row = hist_df.iloc[-1]
                    if not got_nav:
                        info["latest_nav"] = float(last_row["单位净值"])
                        got_nav = True
                    if info["day_change_pct"] is None and last_row.get("日增长率") is not None:
                        info["day_change_pct"] = float(last_row["日增长率"])
                    if info["prev_nav"] is None and len(hist_df) >= 2:
                        info["prev_nav"] = float(hist_df.iloc[-2]["单位净值"])
            except Exception as e:
                log.warning("历史净值兜底失败 %s: %s", code, e)

        # 计算盈亏
        if info["latest_nav"] is not None and info["shares"] > 0:
            info["market_value"] = round(info["latest_nav"] * info["shares"], 2)
            info["profit"] = round(info["market_value"] - info["total_cost"], 2)
            if info["total_cost"] > 0:
                info["profit_pct"] = round(
                    (info["market_value"] - info["total_cost"]) / info["total_cost"] * 100, 2
                )
            # 检查预警
            if info["profit_pct"] is not None:
                if info["warn_down"] and info["profit_pct"] <= info["warn_down"]:
                    info["alert"] = "跌破预警，考虑加仓"
                elif info["warn_up"] and info["profit_pct"] >= info["warn_up"]:
                    info["alert"] = "涨幅达标，考虑止盈"

        results.append(info)

    result = {
        "funds": results,
        "index": index_info,
        "fetch_time": datetime.now().isoformat(),
    }
    # 缓存到文件，刷新页面后仍可显示
    try:
        FUND_CACHE_PATH.write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:
        pass
    return result


# ── 历史走势 ─────────────────────────────────────
def fetch_fund_history(fund_code: str, days: int = 30) -> list[dict]:
    """拉取最近 N 天的净值走势"""
    try:
        df = ak.fund_open_fund_info_em(symbol=fund_code, indicator="单位净值走势")
        if df is None or len(df) == 0:
            return []
        df = df.tail(days)
        result = []
        for _, row in df.iterrows():
            result.append({
                "date": str(row.get("净值日期", "")),
                "nav": float(row.get("单位净值", 0)),
                "change_pct": float(row.get("日增长率", 0)) if row.get("日增长率") else 0,
            })
        return result
    except Exception as e:
        log.warning("拉取基金 %s 历史走势失败: %s", fund_code, e)
        return []


# ── 生成 AI 分析 prompt ──────────────────────────
def build_analysis_prompt(data: dict, histories: dict[str, list], tendency: str = "") -> str:
    now_str = datetime.now().strftime("%Y年%m月%d日 %H:%M")
    lines = [f"我的基金持仓情况（{now_str}）：\n"]

    for f in data["funds"]:
        lines.append(f"【{f['fund_name']}】（代码：{f['fund_code']}）")
        lines.append(f"- 持仓份额：{f['shares']}份")
        lines.append(f"- 平均成本：{f['avg_cost']:.4f}元")
        if f["latest_nav"] is not None:
            lines.append(f"- 最新净值：{f['latest_nav']:.4f}元")
        if f["day_change_pct"] is not None:
            sign = "+" if f["day_change_pct"] >= 0 else ""
            lines.append(f"- 今日涨跌：{sign}{f['day_change_pct']}%")
        if f["market_value"] is not None:
            lines.append(f"- 当前市值：{f['market_value']:.2f}元")
        if f["profit"] is not None:
            sign = "+" if f["profit"] >= 0 else ""
            lines.append(f"- 浮盈亏：{sign}{f['profit']:.2f}元（{sign}{f['profit_pct']:.2f}%）")
        if f["alert"]:
            lines.append(f"- ⚠️ 预警：{f['alert']}")

        # 近期走势
        hist = histories.get(f["fund_code"], [])
        if hist:
            navs = [h["nav"] for h in hist]
            low, high = min(navs), max(navs)
            first, last = navs[0], navs[-1]
            if last > first * 1.02:
                trend = "上涨"
            elif last < first * 0.98:
                trend = "下跌"
            else:
                trend = "震荡"
            lines.append(f"- 近{len(hist)}日走势：最低{low:.4f} ~ 最高{high:.4f}，整体趋势：{trend}")
        lines.append("")

    if data["index"]:
        idx = data["index"]
        sign = "+" if idx["change_pct"] >= 0 else ""
        lines.append(f"大盘背景：{idx['name']}今日 {sign}{idx['change_pct']}%\n")

    if tendency:
        lines.append(f"我的投资倾向与计划：\n{tendency}\n")

    lines.append(
        "请结合以上信息，对每只基金给出当前是否适合加仓/持有/减仓的建议，并说明理由。"
        "如果有触发预警的基金请重点分析。给出的建议要简明具体，最终决策由我自己来做。"
    )

    return "\n".join(lines)


# ── 获取持仓列表 ─────────────────────────────────
async def get_holdings() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM fund_holdings ORDER BY fund_code")
        return [dict(r) for r in await cur.fetchall()]


# ── 完整分析流程（拉数据 + 生成 prompt + 调用 AI + 存消息 + TTS）──
async def run_fund_analysis(manual: bool = False):
    """执行一次完整的基金分析。manual=True 时跳过交易日检查。"""
    cfg = load_fund_config()
    if not cfg.get("enabled", True) and not manual:
        log.info("基金监控功能已关闭，跳过")
        return {"ok": False, "reason": "功能已关闭"}

    if not manual and not is_trading_day():
        log.info("非交易日，跳过基金分析")
        return {"ok": False, "reason": "非交易日"}

    holdings = await get_holdings()
    if not holdings:
        log.info("无持仓基金，跳过分析")
        return {"ok": False, "reason": "无持仓"}

    # 1. 拉取数据（同步 akshare 调用放到线程池）
    loop = asyncio.get_event_loop()
    data = await loop.run_in_executor(None, fetch_fund_data, holdings)

    # 2. 拉取历史走势
    histories = {}
    for h in holdings:
        hist = await loop.run_in_executor(None, fetch_fund_history, h["fund_code"], 30)
        histories[h["fund_code"]] = hist

    # 3. 生成 prompt
    tendency = cfg.get("tendency", "")
    prompt_text = build_analysis_prompt(data, histories, tendency)

    # 4. 调用 AI（复用现有聊天的上下文模式）
    wb = load_worldbook()
    user_name = wb.get("user_name", "你")
    ai_name = wb.get("ai_name", "AI")

    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM conversations ORDER BY updated_at DESC LIMIT 1")
        conv = await cur.fetchone()
        if not conv:
            return {"ok": False, "reason": "无对话"}
        conv_id = conv["id"]
        model_key = conv["model"] or DEFAULT_MODEL

        cur = await db.execute(
            "SELECT role, content, attachments FROM messages WHERE conv_id=? "
            "AND role IN ('user','assistant') ORDER BY created_at DESC LIMIT 20",
            (conv_id,),
        )
        rows = await cur.fetchall()
        history = []
        for r in reversed(rows):
            d = dict(r)
            try:
                d["attachments"] = json.loads(d.get("attachments") or "[]") if d.get("attachments") else []
            except Exception:
                d["attachments"] = []
            history.append(d)

    # 世界书前缀
    prefix = []
    if wb.get("ai_persona"):
        prefix.append({"role": "user", "content": f"[系统设定 - AI人设]\n{wb['ai_persona']}"})
        prefix.append({"role": "assistant", "content": "收到，我会按照设定扮演角色。"})
    if wb.get("user_persona"):
        prefix.append({"role": "user", "content": f"[系统设定 - 用户信息]\n{wb['user_persona']}"})
        prefix.append({"role": "assistant", "content": "收到，我会记住你的信息。"})

    now_str = datetime.now().strftime("%Y年%m月%d日 %H:%M:%S")
    if prefix:
        prefix[-1]["content"] += f"\n系统当前的准确时间是 {now_str}"

    messages = prefix + history + [{"role": "user", "content": prompt_text}]

    # 预生成 msg_id
    ai_msg_id = f"msg_{int(time.time()*1000)}_fund"

    # TTS
    fund_tts = None
    if manager.any_tts_enabled():
        tts_voice = manager.get_tts_voice()
        if tts_voice:
            fund_tts = TTSStreamer(ai_msg_id, tts_voice, manager)

    from schedule import _consume_background_stream

    fund_command_filter = WebCommandStreamFilter()
    _temp = SETTINGS.get("temperature")

    async def content_stream():
        async for chunk in stream_ai(messages, model_key, temperature=_temp):
            if chunk.startswith(CLI_STATUS_PREFIX):
                continue
            yield chunk

    stream_result = await _consume_background_stream(
        content_stream(),
        fund_command_filter,
        fund_tts,
    )
    full_text = stream_result.committed_text
    if stream_result.stop_reason:
        log.error(
            "基金分析 AI 流已停止 (%s): %s",
            stream_result.stop_reason,
            stream_result.diagnostic_error or "",
        )
    if stream_result.notice:
        full_text = f"{full_text}\n\n[{stream_result.notice}]".strip()

    if not full_text.strip():
        return {"ok": False, "reason": "AI 返回空"}

    # 插入系统提示 + AI 回复
    now = time.time()
    sys_msg_id = f"msg_{int(now*1000)}_fs"
    sys_content = "💰 奥罗斯财团 — 基金持仓分析"
    async with get_db() as db:
        await db.execute(
            "INSERT INTO messages (id, conv_id, role, content, created_at, attachments) VALUES (?,?,?,?,?,?)",
            (sys_msg_id, conv_id, "system", sys_content, now, "[]"),
        )
        await db.commit()
    sys_msg = {"id": sys_msg_id, "conv_id": conv_id, "role": "system",
               "content": sys_content, "created_at": now, "attachments": []}
    await manager.broadcast({"type": "msg_created", "data": sys_msg})

    now2 = time.time()
    async with get_db() as db:
        await db.execute(
            "INSERT INTO messages (id, conv_id, role, content, created_at, attachments) VALUES (?,?,?,?,?,?)",
            (ai_msg_id, conv_id, "assistant", full_text, now2, "[]"),
        )
        await db.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now2, conv_id))
        await db.commit()
    ai_msg = {"id": ai_msg_id, "conv_id": conv_id, "role": "assistant",
              "content": full_text, "created_at": now2, "attachments": []}
    await manager.broadcast({"type": "msg_created", "data": ai_msg})

    if fund_tts:
        try:
            await fund_tts.flush()
        except Exception:
            pass

    from routes.files import export_conversation
    await export_conversation(conv_id)

    return {"ok": True, "msg_id": ai_msg_id}


# ── 仅拉取数据（不调 AI）───────────────────────
async def fetch_only() -> dict:
    """仅拉取最新数据 + 计算盈亏，不调用 AI。"""
    holdings = await get_holdings()
    if not holdings:
        return {"funds": [], "index": None, "fetch_time": datetime.now().isoformat()}
    loop = asyncio.get_event_loop()
    data = await loop.run_in_executor(None, fetch_fund_data, holdings)
    return data


def load_fund_cache() -> dict | None:
    """读取上次拉取的缓存数据"""
    if FUND_CACHE_PATH.exists():
        try:
            return json.loads(FUND_CACHE_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return None


# ── 定时任务 ─────────────────────────────────────
class FundScheduler:
    def __init__(self):
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._running = False

    def set_event_loop(self, loop: asyncio.AbstractEventLoop):
        self._loop = loop

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        log.info("FundScheduler started")

    def stop(self):
        self._running = False

    def _run_loop(self):
        while self._running:
            try:
                now = datetime.now()
                # 每天 14:45 触发
                if now.hour == 14 and now.minute == 45:
                    cfg = load_fund_config()
                    if cfg.get("enabled", True) and is_trading_day():
                        log.info("定时基金分析触发")
                        future = asyncio.run_coroutine_threadsafe(
                            run_fund_analysis(manual=False), self._loop
                        )
                        try:
                            future.result(timeout=120)
                        except Exception as e:
                            log.error("定时基金分析异常: %s", e)
                    # 等待到 14:46 避免重复触发
                    for _ in range(120):
                        if not self._running:
                            return
                        time.sleep(0.5)
                    continue

            except Exception as e:
                log.error("FundScheduler tick error: %s", e)

            # 每 30 秒检查一次
            for _ in range(60):
                if not self._running:
                    return
                time.sleep(0.5)


fund_scheduler = FundScheduler()
