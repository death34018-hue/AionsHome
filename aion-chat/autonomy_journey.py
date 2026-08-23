"""Bounded autonomous exploration sessions.

The scheduler stays in ``autonomy.py``. This module only owns the small
explore/observe/finish loop so future destinations can reuse it.
"""

import re
from dataclasses import dataclass, field
from typing import Awaitable, Callable


MAX_ACTION_ROUNDS = 5


@dataclass(frozen=True)
class ActionCapability:
    key: str
    label: str
    exploratory: bool = False


CAPABILITIES = {
    "web_roam": ActionCapability("web_roam", "上网冲浪", exploratory=True),
}


@dataclass
class JourneyResult:
    completed: bool
    outcome: str
    action_trace: list[str] = field(default_factory=list)
    card: dict | None = None
    message: dict | None = None
    rounds: int = 0


def _clip(value, limit: int) -> str:
    text = str(value or "").strip()
    return text if len(text) <= limit else text[:limit].rstrip()


def _sources(observations: list[dict]) -> list[dict]:
    result = []
    seen = set()
    for observation in observations:
        for url in re.findall(r"https?://[^\s<>()\]\[\"']+", observation["text"]):
            url = url.rstrip(".,，。；;）)")
            if url in seen:
                continue
            seen.add(url)
            result.append({"title": _clip(observation["query"], 120), "url": url})
            if len(result) >= 3:
                return result
    return result


def _observation_text(observations: list[dict]) -> str:
    if not observations:
        return "（还没有搜索结果）"
    return "\n\n".join(
        f"方向：{item['query']}\n{_clip(item['text'], 5000)}"
        for item in observations[-3:]
    )


async def run_web_journey(
    *,
    actor: str,
    session_id: str,
    ask_json: Callable[[str], Awaitable[dict]],
    search: Callable[[str], Awaitable[list[str]]],
    create_card: Callable[..., Awaitable[dict]],
    save_message: Callable[..., Awaitable[dict | None]],
    generate_image: Callable[[str], Awaitable[str | None]],
    actor_name: str,
    user_name: str,
    max_rounds: int = MAX_ACTION_ROUNDS,
) -> JourneyResult:
    """Explore until the actor finishes or consumes the model-call budget."""
    max_rounds = max(1, min(int(max_rounds or MAX_ACTION_ROUNDS), MAX_ACTION_ROUNDS))
    observations: list[dict] = []
    trace: list[str] = []
    tool_failures = 0

    for round_number in range(1, max_rounds + 1):
        remaining = max_rounds - round_number
        prompt = (
            "[自主旅行：网络探索]\n"
            f"你是{actor_name}。这是行动阶段第 {round_number} 轮，之后最多还可调用模型 {remaining} 次。"
            "工具搜索不计轮数。你可以选择一个新方向继续探索，或者根据已经看到的内容自然收尾。\n\n"
            f"已经看到的内容：\n{_observation_text(observations)}\n\n"
            "只返回 JSON。继续时："
            '{"decision":"CONTINUE","query":"下一次要搜索的明确方向"}。'
            "收尾时："
            '{"decision":"FINISH","title":"卡片标题","reflection":"感想，可为空",'
            '"tags":["最多3个"],"image_prompt":"可选照片提示词",'
            '"share":false,"share_message":"若分享，只对用户说的一句话"}。'
            "照片和感想会私下收入你的壁龛；即使分享，也不要把照片发给用户。"
        )
        data = await ask_json(prompt)
        decision = str((data or {}).get("decision") or "").strip().upper()

        if decision == "FINISH":
            if not observations:
                return JourneyResult(False, "no_direction", trace, rounds=round_number)
            reflection = _clip(data.get("reflection"), 6000)
            image_prompt = _clip(data.get("image_prompt"), 2000)
            photo_path = ""
            if image_prompt:
                try:
                    filename = await generate_image(image_prompt)
                    if filename:
                        photo_path = filename if str(filename).startswith("/") else f"/uploads/{filename}"
                except Exception:
                    photo_path = ""

            card = None
            if reflection or photo_path:
                card = await create_card(
                    actor=actor,
                    session_id=session_id,
                    title=_clip(data.get("title"), 120) or "一次小旅行",
                    reflection=reflection,
                    tags=list(data.get("tags") or [])[:3],
                    photo_path=photo_path,
                    image_prompt=image_prompt,
                    action_trace=trace[-3:],
                    shared=bool(data.get("share")),
                    sources=_sources(observations),
                )

            message = None
            share_message = _clip(data.get("share_message"), 220)
            if bool(data.get("share")) and share_message:
                message = await save_message(share_message, attachments=[])
            return JourneyResult(
                completed=True,
                outcome="finished",
                action_trace=trace,
                card=card,
                message=message,
                rounds=round_number,
            )

        query = _clip(data.get("query"), 180)
        if not query:
            return JourneyResult(False, "no_direction", trace, rounds=round_number)
        try:
            chunks = await search(query)
            text = "\n\n".join(str(chunk) for chunk in (chunks or []) if str(chunk).strip())
        except Exception as exc:
            text = f"搜索失败：{type(exc).__name__}"
            tool_failures += 1
        else:
            if not text:
                tool_failures += 1
        trace.append(f"搜索了「{query}」")
        observations.append({"query": query, "text": text or "没有拿到内容"})
        if tool_failures >= 2:
            return JourneyResult(
                completed=False,
                outcome="tool_failed",
                action_trace=trace,
                rounds=round_number,
            )

    return JourneyResult(
        completed=False,
        outcome="round_limit",
        action_trace=trace,
        rounds=max_rounds,
    )
