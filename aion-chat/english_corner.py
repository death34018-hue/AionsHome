import asyncio
from datetime import datetime, timedelta
import json
import math
from pathlib import Path
import re
import time
from typing import Literal
import weakref

import aiosqlite


class EnglishCornerError(Exception):
    """Base exception for English-corner domain failures."""


class EnglishCornerValidationError(EnglishCornerError):
    """Raised when a learning pack or requested transition is invalid."""


class EnglishCornerNotFoundError(EnglishCornerError):
    """Raised when a requested English-corner entity does not exist."""


def learning_day_start(now: datetime | None = None) -> float:
    current = now or datetime.now()
    boundary = current.replace(hour=5, minute=0, second=0, microsecond=0)
    if current < boundary:
        boundary -= timedelta(days=1)
    return boundary.timestamp()


def context_limit_options(total: int) -> dict:
    total = max(0, int(total))
    if total == 0:
        return {"options": [0], "default": 0}

    options = list(range(10, total + 1, 10))
    if not options or options[-1] != total:
        options.append(total)
    return {"options": options, "default": total if total < 50 else 50}


def normalize_actor(actor: str) -> Literal["aion", "connor"]:
    if actor in {"aion", "connor"}:
        return actor
    raise ValueError("Invalid actor; expected 'aion' or 'connor'.")


def _required_nonblank_text(value, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EnglishCornerValidationError(f"{path} must be non-blank text.")
    return value.strip()


def parse_generation_payload(
    raw: str,
    allowed_speakers: set[str] | None = None,
) -> dict:
    if not isinstance(raw, str):
        raise EnglishCornerValidationError("Generated payload must be JSON text.")
    text = raw.strip()
    fenced = re.fullmatch(
        r"```(?:json)?[ \t]*\r?\n(?P<body>[\s\S]*?)\r?\n```",
        text,
        flags=re.IGNORECASE,
    )
    if fenced is not None:
        text = fenced.group("body").strip()
    try:
        payload = json.loads(text)
    except (TypeError, json.JSONDecodeError) as exc:
        raise EnglishCornerValidationError(
            "Generated payload is not one JSON object."
        ) from exc

    cards = payload.get("cards") if isinstance(payload, dict) else None
    if not isinstance(cards, list) or len(cards) != 3:
        raise EnglishCornerValidationError(
            "Generated payload must contain exactly three cards."
        )

    stable_speakers = {"user", "aion", "connor"}
    allowed = (
        stable_speakers
        if allowed_speakers is None
        else stable_speakers.intersection(allowed_speakers)
    )
    normalized_cards = []
    for card_index, card in enumerate(cards):
        if not isinstance(card, dict):
            raise EnglishCornerValidationError(f"Card {card_index} must be an object.")
        title = _required_nonblank_text(card.get("title"), f"Card {card_index} title")
        utterances = card.get("utterances")
        if not isinstance(utterances, list) or not 2 <= len(utterances) <= 3:
            raise EnglishCornerValidationError(
                f"Card {card_index} must contain two to three utterances."
            )
        normalized_utterances = []
        for utterance_index, utterance in enumerate(utterances):
            if not isinstance(utterance, dict):
                raise EnglishCornerValidationError(
                    f"Card {card_index} utterance {utterance_index} must be an object."
                )
            speaker = _required_nonblank_text(
                utterance.get("speaker"),
                f"Card {card_index} utterance {utterance_index} speaker",
            )
            if speaker not in allowed:
                raise EnglishCornerValidationError(
                    f"Card {card_index} utterance {utterance_index} "
                    f"uses unknown speaker {speaker!r}."
                )
            normalized_utterances.append(
                {
                    "speaker": speaker,
                    "english": _required_nonblank_text(
                        utterance.get("english"),
                        f"Card {card_index} utterance {utterance_index} English",
                    ),
                    "translation": _required_nonblank_text(
                        utterance.get("translation"),
                        f"Card {card_index} utterance {utterance_index} translation",
                    ),
                }
            )

        vocabulary = card.get("vocabulary")
        if not isinstance(vocabulary, list) or not vocabulary:
            raise EnglishCornerValidationError(
                f"Card {card_index} vocabulary must be a non-empty list."
            )
        normalized_vocabulary = []
        for vocabulary_index, item in enumerate(vocabulary):
            if not isinstance(item, dict):
                raise EnglishCornerValidationError(
                    f"Card {card_index} vocabulary {vocabulary_index} "
                    "must be an object."
                )
            normalized_vocabulary.append(
                {
                    field: _required_nonblank_text(
                        item.get(field),
                        f"Card {card_index} vocabulary {vocabulary_index} {field}",
                    )
                    for field in ("term", "ipa", "part_of_speech", "meaning")
                }
            )
        normalized_cards.append(
            {
                "title": title,
                "utterances": normalized_utterances,
                "vocabulary": normalized_vocabulary,
            }
        )
    return {"cards": normalized_cards}


def build_generation_messages(actor, timeline, role_config) -> list[dict]:
    actor = normalize_actor(actor)
    registry = {}
    for speaker_id in ("user", "aion", "connor"):
        configured = role_config.get(speaker_id, {}) if isinstance(role_config, dict) else {}
        registry[speaker_id] = {
            "name": str(configured.get("name") or speaker_id).strip(),
            "persona": str(configured.get("persona") or "").strip(),
        }
    registry_json = json.dumps(registry, ensure_ascii=False)
    schema = (
        '{"cards":[{"title":"...","utterances":['
        '{"speaker":"user|aion|connor","english":"...",'
        '"translation":"..."}],"vocabulary":[{"term":"...","ipa":"...",'
        '"part_of_speech":"...","meaning":"..."}]}]}'
    )
    system_prompt = f"""
请以生成者的稳定角色 ID“{actor}”所对应的口吻和视角，创建一个英语学习包。

角色注册表：{registry_json}
只能使用角色注册表中的稳定 speaker ID："user"、"aion" 和 "connor"。
不得把显示名称当作 speaker ID。

只返回一个 JSON 对象，不要附加解释、代码围栏或其他文字。"cards" 数组必须包含
恰好 3 张卡片。每张卡片需要有非空标题、2 或 3 句对话以及结合情境的词汇。
每句对话必须包含非空的 "speaker"、"english" 和 "translation" 字段；每个词汇
必须包含非空的 "term"、"ipa"、"part_of_speech" 和 "meaning" 字段。
严格使用以下结构：{schema}

英文难度大致保持在 CET-4 到 CET-6，使用自然、可复用的日常英语，不要使用学术化
表达、冗长复杂从句或生硬翻译腔。

生活气和趣味性是核心要求，不是可选点缀。让三个人像真正熟悉、共同生活的人一样说话，
充分结合角色注册表中的人设和关系。情境可以包含调侃、拌嘴、撒娇、反差、误会、
小意外、亲密互动、调情或一本正经地胡说八道。每张卡片都要有一个具体的小情境、情绪变化或小包袱，
三张卡片使用不同的趣味机制，避免重复同一种桥段。禁止写成英语教材、课堂问答、客服话术、面试练习或互相礼貌寒暄；
应当写成充满趣味的日常对话。

三张卡片可以共享当天的生活气息和人物风格，但必须是各自独立、完整的小场景，
不能依赖上一张卡片的剧情才能理解。

上下文只用于提供灵感：可以从话题、情绪、关系和普通生活细节中自由改编、扩展或
虚构。零上下文也是有效输入，此时根据角色注册表和人设自由创作自然有趣的日常
情境。
""".strip()
    if timeline:
        context_json = json.dumps(timeline, ensure_ascii=False)
        context_prompt = (
            "下面是按时间顺序渲染的合并时间线。它只用于提供灵感，请结合人物关系"
            "自由改编，不要机械复述：\n"
            f"{context_json}"
        )
    else:
        context_prompt = (
            "本次请求为零上下文。请根据已配置的人设、人物关系和自然日常情境"
            "进行自由创作。"
        )
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": context_prompt},
    ]


def _load_generation_role_config() -> dict:
    from chatroom import _read_connor_persona, get_chatroom_names
    from config import load_worldbook

    worldbook = load_worldbook()
    user_name, ai_name, connor_name = get_chatroom_names()
    return {
        "user": {
            "name": user_name or "用户",
            "persona": worldbook.get("user_persona") or "",
        },
        "aion": {
            "name": ai_name or "AI",
            "persona": worldbook.get("ai_persona") or "",
        },
        "connor": {
            "name": connor_name or "第二AI",
            "persona": _read_connor_persona() or "",
        },
    }


async def _main_generation_model_key() -> str:
    from config import DEFAULT_MODEL
    from database import get_db

    async with get_db() as db:
        cursor = await db.execute(
            "SELECT model FROM conversations ORDER BY updated_at DESC LIMIT 1"
        )
        row = await cursor.fetchone()
    return (row[0] if row and row[0] else DEFAULT_MODEL) or DEFAULT_MODEL


def _second_generation_model_key() -> str:
    from chatroom import load_chatroom_config

    configured = load_chatroom_config().get("connor_model") or "Codex"
    return str(configured).strip() or "Codex"


async def get_context_options(actor, *, now=None) -> dict:
    from context_builder import count_merged_timeline

    actor = normalize_actor(actor)
    current = now or datetime.now()
    day_start = learning_day_start(current)
    snapshot_end = current.timestamp()
    total = await count_merged_timeline(
        actor,
        since_ts=day_start,
        until_ts=snapshot_end,
    )
    choices = context_limit_options(total)
    return {
        "actor": actor,
        "learning_day_start": day_start,
        "learning_day_end": snapshot_end,
        "context_total": total,
        **choices,
    }


def _normalize_requested_context_limit(context_limit) -> int:
    if isinstance(context_limit, bool):
        raise EnglishCornerValidationError("Context limit must be an integer.")
    try:
        selected = int(context_limit)
    except (TypeError, ValueError) as exc:
        raise EnglishCornerValidationError("Context limit must be an integer.") from exc
    if selected < 0 or (
        isinstance(context_limit, float) and context_limit != selected
    ):
        raise EnglishCornerValidationError("Context limit is not an offered option.")
    return selected


async def _call_generation_actor(actor, messages, model_key) -> str:
    if actor == "connor":
        from chatroom import simple_connor_cli_call

        prompt = "\n\n".join(
            f"[{message['role']}]\n{message['content']}" for message in messages
        )
        return await simple_connor_cli_call(
            prompt,
            model_key=model_key,
            trace_label="english_corner_generation",
        )
    from ai_providers import simple_ai_call

    return await simple_ai_call(
        messages,
        model_key,
        temperature=0.4,
        trace_label="english_corner_generation",
    )


_generation_request_locks = weakref.WeakValueDictionary()


def _generation_request_lock(request_id: str) -> asyncio.Lock:
    key = (asyncio.get_running_loop(), request_id)
    lock = _generation_request_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _generation_request_locks[key] = lock
    return lock


async def generate_learning_pack(
    actor,
    context_limit,
    request_id,
    *,
    tts_voice="",
    now=None,
    snapshot_end=None,
) -> dict:
    if not isinstance(request_id, str) or not request_id.strip():
        raise EnglishCornerValidationError("A non-blank request_id is required.")
    request_id = request_id.strip()
    async with _generation_request_lock(request_id):
        return await _generate_learning_pack_once(
            actor,
            context_limit,
            request_id,
            tts_voice=tts_voice,
            now=now,
            snapshot_end=snapshot_end,
        )


async def _generate_learning_pack_once(
    actor,
    context_limit,
    request_id,
    *,
    tts_voice="",
    now=None,
    snapshot_end=None,
) -> dict:
    from context_builder import fetch_merged_timeline, render_merged_timeline

    existing = await get_pack_by_request_id(request_id)
    if existing is not None:
        return existing

    actor = normalize_actor(actor)
    snapshot_now = now
    if snapshot_end is not None:
        if isinstance(snapshot_end, bool):
            raise EnglishCornerValidationError(
                "Context snapshot end must be a finite timestamp."
            )
        try:
            normalized_snapshot_end = float(snapshot_end)
        except (TypeError, ValueError) as exc:
            raise EnglishCornerValidationError(
                "Context snapshot end must be a finite timestamp."
            ) from exc
        if (
            not math.isfinite(normalized_snapshot_end)
            or normalized_snapshot_end <= 0
        ):
            raise EnglishCornerValidationError(
                "Context snapshot end must be a finite timestamp."
            )
        try:
            snapshot_now = datetime.fromtimestamp(normalized_snapshot_end)
        except (OSError, OverflowError, ValueError) as exc:
            raise EnglishCornerValidationError(
                "Context snapshot end is outside the supported range."
            ) from exc
    context = await get_context_options(actor, now=snapshot_now)
    selected_limit = _normalize_requested_context_limit(context_limit)
    valid_limits = {0, *context["options"]}
    if selected_limit not in valid_limits:
        raise EnglishCornerValidationError(
            "Context limit must be zero, an offered ten-step option, "
            "or the actual total."
        )

    timeline = []
    rendered_timeline = []
    if selected_limit:
        timeline = await fetch_merged_timeline(
            actor,
            selected_limit,
            since_ts=context["learning_day_start"],
            until_ts=context["learning_day_end"],
        )
        timeline = sorted(
            timeline,
            key=lambda message: float(message.get("created_at") or 0),
        )
        rendered_timeline = render_merged_timeline(timeline, actor)

    role_config = _load_generation_role_config()
    messages = build_generation_messages(actor, rendered_timeline, role_config)
    model_key = (
        _second_generation_model_key()
        if actor == "connor"
        else await _main_generation_model_key()
    )
    raw = await _call_generation_actor(actor, messages, model_key)
    payload = parse_generation_payload(raw, allowed_speakers=set(role_config))
    selected_count = len(timeline)
    context_meta = {
        "model_key": model_key,
        "learning_day_start": context["learning_day_start"],
        "learning_day_end": context["learning_day_end"],
        "context_total": context["context_total"],
        "context_limit": selected_count,
        "context_start": timeline[0].get("created_at") if timeline else None,
        "context_end": timeline[-1].get("created_at") if timeline else None,
    }
    saved = await save_learning_pack(
        payload,
        request_id=request_id,
        generator=actor,
        tts_voice=tts_voice,
        context_meta=context_meta,
    )
    try:
        await prepare_pack_audio(saved["id"])
    except Exception:
        return saved
    refreshed = await get_pack_by_request_id(request_id)
    return refreshed or saved


async def ensure_english_corner_tables(db) -> None:
    await db.execute("PRAGMA foreign_keys = ON")
    await db.executescript(
        """
        CREATE TABLE IF NOT EXISTS english_learning_packs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id TEXT NOT NULL UNIQUE,
            generator TEXT NOT NULL,
            model_key TEXT NOT NULL DEFAULT '',
            tts_voice TEXT NOT NULL DEFAULT '',
            learning_day_start REAL,
            learning_day_end REAL,
            context_total INTEGER NOT NULL DEFAULT 0,
            context_limit INTEGER NOT NULL DEFAULT 0,
            context_start REAL,
            context_end REAL,
            created_at REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS english_learning_cards (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pack_id INTEGER NOT NULL,
            position INTEGER NOT NULL,
            title TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'learning'
                CHECK (status IN ('learning', 'learned')),
            learned_at REAL,
            updated_at REAL NOT NULL,
            FOREIGN KEY (pack_id) REFERENCES english_learning_packs(id)
                ON DELETE CASCADE,
            UNIQUE (pack_id, position)
        );

        CREATE TABLE IF NOT EXISTS english_learning_utterances (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            card_id INTEGER NOT NULL,
            position INTEGER NOT NULL,
            speaker TEXT NOT NULL,
            english TEXT NOT NULL,
            translation TEXT NOT NULL,
            FOREIGN KEY (card_id) REFERENCES english_learning_cards(id)
                ON DELETE CASCADE,
            UNIQUE (card_id, position)
        );

        CREATE TABLE IF NOT EXISTS english_learning_vocabulary (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            card_id INTEGER NOT NULL,
            position INTEGER NOT NULL,
            term TEXT NOT NULL,
            ipa TEXT NOT NULL,
            part_of_speech TEXT NOT NULL,
            meaning TEXT NOT NULL,
            FOREIGN KEY (card_id) REFERENCES english_learning_cards(id)
                ON DELETE CASCADE,
            UNIQUE (card_id, position)
        );

        CREATE TABLE IF NOT EXISTS english_learning_audio (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            utterance_id INTEGER NOT NULL UNIQUE,
            speaker TEXT NOT NULL,
            voice TEXT NOT NULL,
            file_path TEXT NOT NULL,
            status TEXT NOT NULL,
            error TEXT,
            updated_at REAL NOT NULL,
            FOREIGN KEY (utterance_id) REFERENCES english_learning_utterances(id)
                ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_english_learning_cards_status_order
            ON english_learning_cards(status, pack_id DESC, position);
        CREATE INDEX IF NOT EXISTS idx_english_learning_packs_created
            ON english_learning_packs(created_at DESC, id DESC);
        CREATE INDEX IF NOT EXISTS idx_english_learning_audio_utterance
            ON english_learning_audio(utterance_id);
        """
    )
    cursor = await db.execute("PRAGMA table_info(english_learning_packs)")
    pack_columns = {row[1] for row in await cursor.fetchall()}
    if "learning_day_end" not in pack_columns:
        await db.execute(
            "ALTER TABLE english_learning_packs ADD COLUMN learning_day_end REAL"
        )
    if "tts_voice" not in pack_columns:
        await db.execute(
            "ALTER TABLE english_learning_packs "
            "ADD COLUMN tts_voice TEXT NOT NULL DEFAULT ''"
        )
    await db.execute(
        """
        INSERT OR IGNORE INTO english_learning_audio (
            utterance_id, speaker, voice, file_path, status, error, updated_at
        )
        SELECT
            utterance.id,
            utterance.speaker,
            '',
            '',
            'failed',
            'Audio is unavailable.',
            ?
        FROM english_learning_utterances AS utterance
        WHERE utterance.speaker IN ('aion', 'connor')
        """,
        (time.time(),),
    )
    await db.commit()


def _open_english_corner_db(db_path: str | Path | None):
    if db_path is not None:
        return aiosqlite.connect(str(db_path))
    from database import get_db

    return get_db()


def _load_tts_voice_config() -> dict:
    from chatroom import load_chatroom_config

    config = load_chatroom_config()
    return {
        "tts_aion_voice": str(config.get("tts_aion_voice") or "").strip(),
        "tts_connor_voice": str(config.get("tts_connor_voice") or "").strip(),
    }


def _english_corner_audio_dir(audio_dir: str | Path | None) -> Path:
    if audio_dir is not None:
        return Path(audio_dir).resolve()
    from config import DATA_DIR

    return (DATA_DIR / "english_corner_audio").resolve()


def _new_utterance_audio_path(
    utterance_id: int,
    audio_dir: str | Path | None,
) -> Path:
    return _english_corner_audio_dir(audio_dir) / f"utterance-{utterance_id}.mp3"


def _concise_tts_error(exc: Exception) -> str:
    return "TTS synthesis failed."


async def _get_utterance_with_audio(db, utterance_id: int) -> dict | None:
    rows = await _fetch_dicts(
        db,
        """
        SELECT
            utterance.id,
            utterance.card_id,
            card.pack_id,
            pack.tts_voice AS pack_tts_voice,
            utterance.speaker,
            utterance.english,
            audio.id AS audio_id,
            audio.voice AS audio_voice,
            audio.file_path AS audio_file_path,
            audio.status AS audio_status,
            audio.error AS audio_error,
            audio.updated_at AS audio_updated_at
        FROM english_learning_utterances AS utterance
        JOIN english_learning_cards AS card ON card.id = utterance.card_id
        JOIN english_learning_packs AS pack ON pack.id = card.pack_id
        LEFT JOIN english_learning_audio AS audio
            ON audio.utterance_id = utterance.id
        WHERE utterance.id = ?
        """,
        (utterance_id,),
    )
    return rows[0] if rows else None


def _audio_from_utterance_row(row: dict, *, cached=False) -> dict | None:
    if row["audio_id"] is None:
        return None
    return {
        "id": row["audio_id"],
        "utterance_id": row["id"],
        "speaker": row["speaker"],
        "voice": row["audio_voice"],
        "file_path": row["audio_file_path"],
        "status": row["audio_status"],
        "error": row["audio_error"],
        "updated_at": row["audio_updated_at"],
        "cached": cached,
    }


async def _store_utterance_audio(
    db,
    *,
    utterance_id: int,
    speaker: str,
    voice: str,
    file_path: Path,
    status: str,
    error: str,
) -> dict:
    updated_at = time.time()
    await db.execute(
        """
        INSERT INTO english_learning_audio (
            utterance_id, speaker, voice, file_path, status, error, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(utterance_id) DO UPDATE SET
            speaker = excluded.speaker,
            voice = excluded.voice,
            file_path = excluded.file_path,
            status = excluded.status,
            error = excluded.error,
            updated_at = excluded.updated_at
        """,
        (
            utterance_id,
            speaker,
            voice,
            str(file_path),
            status,
            error,
            updated_at,
        ),
    )
    await db.commit()
    row = await _get_utterance_with_audio(db, utterance_id)
    if row is None:
        raise RuntimeError("Stored English-corner audio could not be reloaded.")
    return _audio_from_utterance_row(row)  # type: ignore[return-value]


async def get_utterance_audio(
    utterance_id,
    *,
    db_path=None,
) -> dict | None:
    async with _open_english_corner_db(db_path) as db:
        await ensure_english_corner_tables(db)
        row = await _get_utterance_with_audio(db, utterance_id)
        if (
            row is None
            or row["speaker"] not in {"user", "aion", "connor"}
            or row["audio_id"] is None
            or (
                row["speaker"] == "user"
                and not str(row["pack_tts_voice"] or "").strip()
            )
        ):
            return None
        audio = _audio_from_utterance_row(row)
        if (
            audio is not None
            and audio["status"] == "ready"
            and not Path(audio["file_path"]).is_file()
        ):
            return await _store_utterance_audio(
                db,
                utterance_id=int(row["id"]),
                speaker=row["speaker"],
                voice=str(audio["voice"] or ""),
                file_path=Path(audio["file_path"]),
                status="failed",
                error="Audio is unavailable.",
            )
        return audio


async def retry_utterance_audio(
    utterance_id,
    *,
    db_path=None,
    audio_dir=None,
) -> dict:
    async with _open_english_corner_db(db_path) as db:
        await ensure_english_corner_tables(db)
        row = await _get_utterance_with_audio(db, utterance_id)
    if row is None:
        raise EnglishCornerNotFoundError(
            f"Unknown English learning utterance: {utterance_id}."
        )
    speaker = row["speaker"]
    pack_tts_voice = str(row["pack_tts_voice"] or "").strip()
    if speaker == "user" and not pack_tts_voice:
        raise EnglishCornerValidationError(
            "A user utterance cannot have English-corner audio."
        )
    if speaker not in {"user", "aion", "connor"}:
        raise EnglishCornerValidationError(
            f"Unsupported English-corner audio speaker: {speaker}."
        )

    stored = _audio_from_utterance_row(row)
    if (
        stored is not None
        and stored["status"] == "ready"
        and Path(stored["file_path"]).is_file()
    ):
        return {**stored, "cached": True}

    configured_voices = _load_tts_voice_config()
    voice = (
        str(stored["voice"] or "").strip()
        if stored is not None
        else ""
    )
    if not voice:
        voice = pack_tts_voice
    if not voice and speaker in {"aion", "connor"}:
        voice = str(configured_voices.get(f"tts_{speaker}_voice") or "").strip()
    file_path = (
        Path(stored["file_path"])
        if stored is not None and stored["file_path"]
        else _new_utterance_audio_path(int(row["id"]), audio_dir)
    )

    if not voice:
        async with _open_english_corner_db(db_path) as db:
            await ensure_english_corner_tables(db)
            return await _store_utterance_audio(
                db,
                utterance_id=int(row["id"]),
                speaker=speaker,
                voice="",
                file_path=file_path,
                status="failed",
                error="Missing configured voice.",
            )

    from tts import synthesize_text_to_mp3

    try:
        await synthesize_text_to_mp3(row["english"], voice, file_path)
        if not file_path.is_file():
            raise RuntimeError("synthesizer produced no MP3 file")
    except Exception as exc:
        try:
            file_path.unlink(missing_ok=True)
        except OSError:
            pass
        status = "failed"
        error = _concise_tts_error(exc)
    else:
        status = "ready"
        error = ""

    async with _open_english_corner_db(db_path) as db:
        await ensure_english_corner_tables(db)
        return await _store_utterance_audio(
            db,
            utterance_id=int(row["id"]),
            speaker=speaker,
            voice=voice,
            file_path=file_path,
            status=status,
            error=error,
        )


async def prepare_pack_audio(
    pack_id,
    *,
    db_path=None,
    audio_dir=None,
) -> dict:
    async with _open_english_corner_db(db_path) as db:
        await ensure_english_corner_tables(db)
        pack_rows = await _fetch_dicts(
            db,
            "SELECT id, tts_voice FROM english_learning_packs WHERE id = ?",
            (pack_id,),
        )
        if not pack_rows:
            raise EnglishCornerNotFoundError(
                f"Unknown English learning pack: {pack_id}."
            )
        pack_tts_voice = str(pack_rows[0]["tts_voice"] or "").strip()
        utterances = await _fetch_dicts(
            db,
            """
            SELECT utterance.id
            FROM english_learning_utterances AS utterance
            JOIN english_learning_cards AS card ON card.id = utterance.card_id
            WHERE card.pack_id = ?
              AND (
                utterance.speaker IN ('aion', 'connor')
                OR (? <> '' AND utterance.speaker = 'user')
              )
            ORDER BY card.position, utterance.position
            """,
            (pack_id, pack_tts_voice),
        )

    items = []
    for utterance in utterances:
        items.append(
            await retry_utterance_audio(
                utterance["id"],
                db_path=db_path,
                audio_dir=audio_dir,
            )
        )
    return {
        "pack_id": pack_id,
        "items": items,
        "ready": sum(item["status"] == "ready" for item in items),
        "failed": sum(item["status"] == "failed" for item in items),
        "cached": sum(bool(item.get("cached")) for item in items),
    }


async def _fetch_dicts(db, sql: str, parameters=()) -> list[dict]:
    cursor = await db.execute(sql, parameters)
    rows = await cursor.fetchall()
    columns = [description[0] for description in cursor.description]
    return [dict(zip(columns, row)) for row in rows]


async def _hydrate_card_children(db, cards: list[dict]) -> list[dict]:
    for card in cards:
        card["utterances"] = await _fetch_dicts(
            db,
            """
            SELECT
                utterance.id,
                utterance.card_id,
                utterance.position,
                utterance.speaker,
                utterance.english,
                utterance.translation,
                audio.id AS audio_id,
                audio.utterance_id AS audio_utterance_id,
                audio.speaker AS audio_speaker,
                audio.voice AS audio_voice,
                audio.file_path AS audio_file_path,
                audio.status AS audio_status,
                audio.error AS audio_error,
                audio.updated_at AS audio_updated_at
            FROM english_learning_utterances AS utterance
            LEFT JOIN english_learning_audio AS audio
                ON audio.utterance_id = utterance.id
            WHERE utterance.card_id = ?
            ORDER BY utterance.position
            """,
            (card["id"],),
        )
        for utterance in card["utterances"]:
            audio_id = utterance.pop("audio_id")
            audio_fields = {
                "utterance_id": utterance.pop("audio_utterance_id"),
                "speaker": utterance.pop("audio_speaker"),
                "voice": utterance.pop("audio_voice"),
                "file_path": utterance.pop("audio_file_path"),
                "status": utterance.pop("audio_status"),
                "error": utterance.pop("audio_error"),
                "updated_at": utterance.pop("audio_updated_at"),
            }
            utterance["audio"] = (
                {"id": audio_id, **audio_fields} if audio_id is not None else None
            )
        card["vocabulary"] = await _fetch_dicts(
            db,
            """
            SELECT id, card_id, position, term, ipa, part_of_speech, meaning
            FROM english_learning_vocabulary
            WHERE card_id = ?
            ORDER BY position
            """,
            (card["id"],),
        )
    return cards


async def _read_cards_for_pack(db, pack_id: int) -> list[dict]:
    cards = await _fetch_dicts(
        db,
        """
        SELECT id, pack_id, position, title, status, learned_at, updated_at
        FROM english_learning_cards
        WHERE pack_id = ?
        ORDER BY position
        """,
        (pack_id,),
    )
    return await _hydrate_card_children(db, cards)


async def _get_pack_by_request_id_on_db(db, request_id: str) -> dict | None:
    packs = await _fetch_dicts(
        db,
        """
        SELECT
            id, request_id, generator, model_key, tts_voice, learning_day_start,
            learning_day_end,
            context_total, context_limit, context_start, context_end, created_at
        FROM english_learning_packs
        WHERE request_id = ?
        """,
        (request_id,),
    )
    if not packs:
        return None
    pack = packs[0]
    pack["cards"] = await _read_cards_for_pack(db, pack["id"])
    return pack


async def get_pack_by_request_id(request_id, *, db_path=None) -> dict | None:
    async with _open_english_corner_db(db_path) as db:
        await ensure_english_corner_tables(db)
        return await _get_pack_by_request_id_on_db(db, request_id)


def _validate_card_status(status: str) -> None:
    if status not in {"learning", "learned"}:
        raise EnglishCornerValidationError(
            "Invalid card status; expected 'learning' or 'learned'."
        )


async def list_cards(
    status,
    *,
    limit=50,
    offset=0,
    db_path=None,
) -> dict:
    _validate_card_status(status)
    limit = int(limit)
    offset = int(offset)
    if limit < 0 or offset < 0:
        raise EnglishCornerValidationError(
            "Card list limit and offset must be non-negative."
        )
    async with _open_english_corner_db(db_path) as db:
        await ensure_english_corner_tables(db)
        total_rows = await _fetch_dicts(
            db,
            "SELECT COUNT(*) AS total FROM english_learning_cards WHERE status = ?",
            (status,),
        )
        cards = await _fetch_dicts(
            db,
            """
            SELECT
                card.id,
                card.pack_id,
                card.position,
                card.title,
                card.status,
                card.learned_at,
                card.updated_at,
                pack.request_id AS pack_request_id,
                pack.generator AS pack_generator,
                pack.model_key AS pack_model_key,
                pack.learning_day_start AS pack_learning_day_start,
                pack.learning_day_end AS pack_learning_day_end,
                pack.context_total AS pack_context_total,
                pack.context_limit AS pack_context_limit,
                pack.context_start AS pack_context_start,
                pack.context_end AS pack_context_end,
                pack.created_at AS pack_created_at
            FROM english_learning_cards AS card
            JOIN english_learning_packs AS pack ON pack.id = card.pack_id
            WHERE card.status = ?
            ORDER BY pack.created_at DESC, pack.id DESC, card.position
            LIMIT ? OFFSET ?
            """,
            (status, limit, offset),
        )
        for card in cards:
            card["pack"] = {
                "id": card["pack_id"],
                "request_id": card.pop("pack_request_id"),
                "generator": card.pop("pack_generator"),
                "model_key": card.pop("pack_model_key"),
                "learning_day_start": card.pop("pack_learning_day_start"),
                "learning_day_end": card.pop("pack_learning_day_end"),
                "context_total": card.pop("pack_context_total"),
                "context_limit": card.pop("pack_context_limit"),
                "context_start": card.pop("pack_context_start"),
                "context_end": card.pop("pack_context_end"),
                "created_at": card.pop("pack_created_at"),
            }
        await _hydrate_card_children(db, cards)
        return {
            "items": cards,
            "total": total_rows[0]["total"],
            "limit": limit,
            "offset": offset,
        }


async def _get_card_by_id_on_db(db, card_id: int) -> dict | None:
    cards = await _fetch_dicts(
        db,
        """
        SELECT id, pack_id, position, title, status, learned_at, updated_at
        FROM english_learning_cards
        WHERE id = ?
        """,
        (card_id,),
    )
    if not cards:
        return None
    await _hydrate_card_children(db, cards)
    return cards[0]


async def set_card_status(card_id, status, *, db_path=None) -> dict:
    _validate_card_status(status)
    async with _open_english_corner_db(db_path) as db:
        await ensure_english_corner_tables(db)
        now = time.time()
        learned_at = now if status == "learned" else None
        cursor = await db.execute(
            """
            UPDATE english_learning_cards
            SET status = ?, learned_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (status, learned_at, now, card_id),
        )
        if cursor.rowcount == 0:
            await db.rollback()
            raise EnglishCornerNotFoundError(
                f"Unknown English learning card: {card_id}."
            )
        await db.commit()
        card = await _get_card_by_id_on_db(db, card_id)
        if card is None:
            raise RuntimeError("Updated English learning card could not be reloaded.")
        return card


async def get_unlearned_count(*, db_path=None) -> int:
    async with _open_english_corner_db(db_path) as db:
        await ensure_english_corner_tables(db)
        rows = await _fetch_dicts(
            db,
            """
            SELECT COUNT(*) AS total
            FROM english_learning_cards
            WHERE status = 'learning'
            """,
        )
        return rows[0]["total"]


def _validate_pack_for_persistence(payload) -> None:
    cards = payload.get("cards") if isinstance(payload, dict) else None
    if not isinstance(cards, list) or len(cards) != 3:
        raise EnglishCornerValidationError(
            "A learning pack must contain exactly three cards."
        )
    required_card_fields = ("title", "utterances", "vocabulary")
    required_utterance_fields = ("speaker", "english", "translation")
    required_vocabulary_fields = (
        "term",
        "ipa",
        "part_of_speech",
        "meaning",
    )
    for card_position, card in enumerate(cards):
        for field in required_card_fields:
            if not isinstance(card, dict) or field not in card:
                raise EnglishCornerValidationError(
                    f"Card {card_position} is missing {field}."
                )
        if not isinstance(card["utterances"], list):
            raise EnglishCornerValidationError(
                f"Card {card_position} utterances must be a list."
            )
        if not isinstance(card["vocabulary"], list):
            raise EnglishCornerValidationError(
                f"Card {card_position} vocabulary must be a list."
            )
        for utterance_position, utterance in enumerate(card["utterances"]):
            for field in required_utterance_fields:
                if not isinstance(utterance, dict) or field not in utterance:
                    raise EnglishCornerValidationError(
                        f"Card {card_position} utterance {utterance_position} "
                        f"is missing {field}."
                    )
        for vocabulary_position, vocabulary in enumerate(card["vocabulary"]):
            for field in required_vocabulary_fields:
                if not isinstance(vocabulary, dict) or field not in vocabulary:
                    raise EnglishCornerValidationError(
                        f"Card {card_position} vocabulary {vocabulary_position} "
                        f"is missing {field}."
                    )


async def save_learning_pack(
    payload,
    *,
    request_id,
    generator,
    tts_voice="",
    context_meta,
    db_path=None,
) -> dict:
    tts_voice = str(tts_voice or "").strip()
    if len(tts_voice) > 512:
        raise EnglishCornerValidationError(
            "TTS voice must contain at most 512 characters."
        )
    async with _open_english_corner_db(db_path) as db:
        await ensure_english_corner_tables(db)
        await db.commit()
        now = time.time()
        try:
            await db.execute("BEGIN IMMEDIATE")
            existing = await _get_pack_by_request_id_on_db(db, request_id)
            if existing is not None:
                await db.rollback()
                return existing
            _validate_pack_for_persistence(payload)
            cursor = await db.execute(
                """
                INSERT INTO english_learning_packs (
                    request_id, generator, model_key, tts_voice,
                    learning_day_start,
                    learning_day_end,
                    context_total, context_limit, context_start, context_end,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    request_id,
                    generator,
                    context_meta.get("model_key", payload.get("model_key", "")),
                    tts_voice,
                    context_meta.get("learning_day_start"),
                    context_meta.get("learning_day_end"),
                    context_meta.get("context_total", 0),
                    context_meta.get("context_limit", 0),
                    context_meta.get("context_start"),
                    context_meta.get("context_end"),
                    now,
                ),
            )
            pack_id = cursor.lastrowid
            for card_position, card in enumerate(payload["cards"]):
                cursor = await db.execute(
                    """
                    INSERT INTO english_learning_cards (
                        pack_id, position, title, status, learned_at, updated_at
                    ) VALUES (?, ?, ?, 'learning', NULL, ?)
                    """,
                    (pack_id, card_position, card["title"], now),
                )
                card_id = cursor.lastrowid
                for utterance_position, utterance in enumerate(card["utterances"]):
                    utterance_cursor = await db.execute(
                        """
                        INSERT INTO english_learning_utterances (
                            card_id, position, speaker, english, translation
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            card_id,
                            utterance_position,
                            utterance["speaker"],
                            utterance["english"],
                            utterance["translation"],
                        ),
                    )
                    if (
                        tts_voice
                        or utterance["speaker"] in {"aion", "connor"}
                    ):
                        await db.execute(
                            """
                            INSERT INTO english_learning_audio (
                                utterance_id, speaker, voice, file_path,
                                status, error, updated_at
                            ) VALUES (?, ?, ?, '', 'failed', ?, ?)
                            """,
                            (
                                utterance_cursor.lastrowid,
                                utterance["speaker"],
                                tts_voice,
                                "Audio is unavailable.",
                                now,
                            ),
                        )
                for vocabulary_position, vocabulary in enumerate(
                    card["vocabulary"]
                ):
                    await db.execute(
                        """
                        INSERT INTO english_learning_vocabulary (
                            card_id, position, term, ipa, part_of_speech, meaning
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            card_id,
                            vocabulary_position,
                            vocabulary["term"],
                            vocabulary["ipa"],
                            vocabulary["part_of_speech"],
                            vocabulary["meaning"],
                        ),
                    )
            await db.commit()
        except Exception:
            await db.rollback()
            raise

        pack = await _get_pack_by_request_id_on_db(db, request_id)
        if pack is None:
            raise RuntimeError("Saved English learning pack could not be reloaded.")
        return pack
