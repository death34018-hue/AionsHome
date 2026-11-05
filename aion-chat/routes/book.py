"""
陪伴阅读 API 路由
- 书籍上传/列表/详情/删除
- 章节内容获取
- 阅读进度保存
- 图片服务
- AI 批注（邀请共读）
- 用户高亮（框选提问持久化）
"""

import asyncio, json, logging, re, time, os
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, UploadFile, File, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from config import DATA_DIR, load_worldbook, MODELS, DEFAULT_MODEL
from database import get_db
from ai_providers import stream_ai, CLI_STATUS_PREFIX
from book import parse_epub, delete_book_files, build_annotate_text, BOOKS_DIR, _compute_segments
from context_builder import fetch_merged_timeline, render_merged_timeline
from chatroom import stream_connor_cli, check_connor_online, _read_connor_persona, load_chatroom_config
import reading_tts

router = APIRouter()
logger = logging.getLogger("book")

# ── 临时上传目录 ──
_TMP_DIR = DATA_DIR / "tmp"
_TMP_DIR.mkdir(exist_ok=True)

# ── 幂等锁：防止同一章节同一段重复批注 ──
_annotating_locks: dict[str, asyncio.Lock] = {}  # key: "book_id:ch:seg"
_annotation_jobs: dict[tuple, dict] = {}
_speech_jobs: dict[tuple, dict] = {}
_speech_generations: dict[tuple, int] = {}
READING_AUDIO_DIR = DATA_DIR / 'reading_audio'


async def discard_chapter_speech(book_id, ch_idx):
    source = (book_id, ch_idx)
    _speech_generations[source] = _speech_generations.get(source, 0) + 1
    tasks = []
    for key in list(_speech_jobs):
        if key[:2] == (book_id, ch_idx):
            job = _speech_jobs.pop(key)
            job['task'].cancel()
            tasks.append(job['task'])
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def _chapter_matches(db, book_id, ch_idx, expected_chapter_id):
    row = await (await db.execute('SELECT id FROM book_chapters WHERE book_id=? AND chapter_index=?',
                                 (book_id, ch_idx))).fetchone()
    actual = row.get('id') if isinstance(row, dict) else row[0] if row else None
    return actual is not None and (expected_chapter_id is None or actual == expected_chapter_id)


def _speech_dir(book_id: str, title: str = ''):
    if not re.fullmatch(r'[a-zA-Z0-9_-]+', book_id):
        raise HTTPException(400, '无效的书籍编号')
    return reading_tts.book_audio_dir(READING_AUDIO_DIR, book_id, title)


async def _speech_plan(book_id: str, ch_idx: int, narrator: str):
    if narrator not in ('aion', 'connor'):
        raise HTTPException(400, '请选择朗读伴侣')
    data = await get_chapter(book_id, ch_idx)
    cache_dir = _speech_dir(book_id, data.get('book_title') or '')
    cache_dir.mkdir(parents=True, exist_ok=True)
    reading_tts.preserve_legacy_audio(BOOKS_DIR / book_id / 'tts', cache_dir)
    return reading_tts.build_plan(data, load_chatroom_config(), narrator, cache_dir)


def _public_speech_plan(book_id, ch_idx, plan):
    result = {key: value for key, value in plan.items() if key != 'clips'}
    result['clips'] = [dict(
        **{key: value for key, value in clip.items() if key not in ('text', 'voice')},
        url=f"/api/books/{book_id}/speech/audio/{clip['key']}",
    ) for clip in plan['clips']]
    job = _speech_jobs.get((book_id, ch_idx, plan['narrator']))
    result['job'] = ({key: job[key] for key in ('state', 'completed', 'total', 'error')}
                     if job and job['signature'] == plan['signature'] else None)
    return result


@router.get('/api/books/{book_id}/chapters/{ch_idx}/speech')
async def get_chapter_speech(book_id: str, ch_idx: int, narrator: str = 'aion', signature: str = ''):
    job = _speech_jobs.get((book_id, ch_idx, narrator))
    if job and (signature == job['signature'] or (not signature and job['state'] == 'running')):
        return _public_speech_plan(book_id, ch_idx, reading_tts.refresh_plan(job['plan'], _speech_dir(book_id)))
    if signature:
        # Recover completed cached playback after a restart when content is unchanged.
        plan = await _speech_plan(book_id, ch_idx, narrator)
        if plan['signature'] != signature:
            raise HTTPException(409, '原朗读任务已中断，章节批注或音色已更新；已生成音频保留，请重新打开朗读设置')
        return _public_speech_plan(book_id, ch_idx, plan)
    return _public_speech_plan(book_id, ch_idx, await _speech_plan(book_id, ch_idx, narrator))


class ChapterSpeechRequest(BaseModel):
    narrator: str
    signature: str


@router.post('/api/books/{book_id}/chapters/{ch_idx}/speech')
async def start_chapter_speech(book_id: str, ch_idx: int, body: ChapterSpeechRequest):
    generation = _speech_generations.get((book_id, ch_idx), 0)
    plan = await _speech_plan(book_id, ch_idx, body.narrator)
    if generation != _speech_generations.get((book_id, ch_idx), 0):
        raise HTTPException(409, '章节已更新，请重新打开朗读设置')
    if plan['signature'] != body.signature:
        raise HTTPException(409, '章节批注或音色已变化，请重新确认合成字数')
    if plan['missing_voices']:
        raise HTTPException(400, '请先在群聊配置音色：' + '、'.join(plan['missing_voices']))
    key = (book_id, ch_idx, body.narrator)
    existing = _speech_jobs.get(key)
    if existing and not existing['task'].done():
        if existing['signature'] != plan['signature']:
            raise HTTPException(409, '本章之前的语音仍在合成，请稍后重试')
        return _public_speech_plan(book_id, ch_idx, plan)
    if not plan['missing_count']:
        return _public_speech_plan(book_id, ch_idx, plan)
    job = dict(plan=plan, signature=plan['signature'], state='running', completed=0,
               total=len(plan['clips']), error='')

    async def run():
        try:
            await reading_tts.synthesize_plan(plan, _speech_dir(book_id),
                                              lambda count: job.update(completed=count))
            job['state'] = 'done'
        except asyncio.CancelledError:
            job['state'] = 'cancelled'
            raise
        except Exception:
            logger.exception('Chapter speech failed: %s', key)
            job.update(state='error', error=f"第 {job['completed'] + 1}/{job['total']} 段合成失败，已完成的音频已保留，可重试补全")

    job['task'] = asyncio.create_task(run())
    _speech_jobs[key] = job
    return _public_speech_plan(book_id, ch_idx, plan)


@router.get('/api/books/{book_id}/speech/audio/{audio_key}')
async def serve_chapter_speech(book_id: str, audio_key: str):
    if not re.fullmatch(r'[a-f0-9]{64}', audio_key):
        raise HTTPException(404, '语音不存在')
    path = _speech_dir(book_id) / f'{audio_key}.mp3'
    if not reading_tts.audio_ready(path) and not (BOOKS_DIR / book_id / 'tts' / '.reading-audio-imported.json').exists():
        path = BOOKS_DIR / book_id / 'tts' / f'{audio_key}.mp3'
    if not reading_tts.audio_ready(path):
        raise HTTPException(404, '语音不存在，请重新打开朗读设置')
    return FileResponse(path, media_type='audio/mpeg')


# =============================================
#  书籍上传（EPUB）
# =============================================
@router.post("/api/books/upload")
async def upload_book(file: UploadFile = File(...)):
    if not file.filename or not file.filename.lower().endswith('.epub'):
        raise HTTPException(400, "只支持 EPUB 格式")

    # 保存到临时目录
    tmp_path = _TMP_DIR / file.filename
    try:
        content = await file.read()
        tmp_path.write_bytes(content)

        # 解析 EPUB
        parsed = parse_epub(str(tmp_path))

        if not parsed.chapters:
            raise HTTPException(400, "无法解析出有效章节，可能是固定版式或加密 EPUB")

        # 写入数据库
        async with get_db() as db:
            await db.execute("""
                INSERT INTO books (book_id, title, author, cover_path, total_chapters, import_time)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (parsed.book_id, parsed.title, parsed.author,
                  parsed.cover_path, len(parsed.chapters), time.time()))

            for ch in parsed.chapters:
                await db.execute("""
                    INSERT INTO book_chapters
                        (book_id, chapter_index, title, html_content, text_content,
                         paragraphs, char_count, segment_count, segments_meta)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (parsed.book_id, ch.index, ch.title,
                      ch.html_content, ch.text_content,
                      json.dumps(ch.paragraphs, ensure_ascii=False),
                      ch.char_count, len(ch.segments_meta),
                      json.dumps(ch.segments_meta, ensure_ascii=False)))

            await db.commit()

        return {
            "book_id": parsed.book_id,
            "title": parsed.title,
            "author": parsed.author,
            "cover_path": parsed.cover_path,
            "total_chapters": len(parsed.chapters),
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"解析失败: {str(e)}")
    finally:
        # 清理临时文件
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)


# =============================================
#  书库列表
# =============================================
@router.get("/api/books")
async def list_books():
    async with get_db() as db:
        db.row_factory = _row_dict
        rows = await db.execute("""
            SELECT book_id, title, author, cover_path, total_chapters,
                   current_chapter, current_paragraph, import_time
            FROM books ORDER BY import_time DESC
        """)
        books = await rows.fetchall()
    return {"books": books}


# =============================================
#  书籍详情（含章节目录）
# =============================================
@router.get("/api/books/{book_id}")
async def get_book(book_id: str):
    async with get_db() as db:
        db.row_factory = _row_dict
        row = await db.execute(
            "SELECT * FROM books WHERE book_id = ?", (book_id,))
        book = await row.fetchone()
        if not book:
            raise HTTPException(404, "书籍不存在")

        rows = await db.execute("""
            SELECT id, chapter_index, title, char_count, segment_count, segments_meta
            FROM book_chapters WHERE book_id = ? ORDER BY chapter_index
        """, (book_id,))
        chapters = await rows.fetchall()

        # 附加每章的批注状态
        for ch in chapters:
            seg_meta = json.loads(ch.get('segments_meta', '[]'))
            # 查询该章节已有的批注
            ann_rows = await db.execute("""
                SELECT segment_index FROM book_annotations
                WHERE book_id = ? AND chapter_index = ?
            """, (book_id, ch['chapter_index']))
            annotated_segments = {r['segment_index'] for r in await ann_rows.fetchall()}
            # 更新状态
            for seg in seg_meta:
                idx = seg_meta.index(seg)
                if idx in annotated_segments:
                    seg['status'] = 'done'
            ch['segments_meta'] = seg_meta

    return {"book": book, "chapters": chapters}


# =============================================
#  获取章节内容
# =============================================
async def _refresh_unread_segments(book_id, ch_idx):
    """仅升级完全未共读的章节；已有摘要仍沿用原段号。"""
    if any(key[:2] == (book_id, ch_idx) for key in _annotation_jobs):
        return
    if any(key.startswith(f'{book_id}:{ch_idx}:') and lock.locked()
           for key, lock in _annotating_locks.items()):
        return
    async with get_db() as db:
        await db.execute('BEGIN IMMEDIATE')
        row = await (await db.execute('SELECT id, paragraphs, segments_meta FROM book_chapters WHERE book_id=? AND chapter_index=?',
                                     (book_id, ch_idx))).fetchone()
        if not row:
            return
        old = json.loads(row[2])
        if any(s.get('status') != 'not_sent' for s in old):
            return
        exists = await (await db.execute('SELECT 1 FROM book_annotations WHERE book_id=? AND chapter_index=? LIMIT 1',
                                        (book_id, ch_idx))).fetchone()
        if exists:
            return
        segments = _compute_segments(json.loads(row[1]))
        if segments != old:
            await db.execute('UPDATE book_chapters SET segments_meta=?, segment_count=? WHERE id=?',
                             (json.dumps(segments), len(segments), row[0]))
            await db.commit()


@router.get("/api/books/{book_id}/chapters/{ch_idx}")
async def get_chapter(book_id: str, ch_idx: int):
    await _refresh_unread_segments(book_id, ch_idx)
    async with get_db() as db:
        db.row_factory = _row_dict
        row = await db.execute("""
            SELECT * FROM book_chapters
            WHERE book_id = ? AND chapter_index = ?
        """, (book_id, ch_idx))
        chapter = await row.fetchone()
        if not chapter:
            raise HTTPException(404, "章节不存在")

        book_row = await db.execute('SELECT title FROM books WHERE book_id = ?', (book_id,))
        book_info = await book_row.fetchone()

        # 获取已有批注（区分 annotator）
        ann_rows = await db.execute("""
            SELECT segment_index, annotations, summary, updated_at, COALESCE(annotator, 'aion') as annotator
            FROM book_annotations
            WHERE book_id = ? AND chapter_index = ?
            ORDER BY segment_index
        """, (book_id, ch_idx))
        annotations_raw = await ann_rows.fetchall()

        # 获取用户高亮（含 annotator 和 connor_answer）
        hl_rows = await db.execute("""
            SELECT id, selected_text, start_p, start_offset, end_p, end_offset,
                   question, answer, created_at,
                   COALESCE(annotator, 'aion') as annotator,
                   COALESCE(connor_answer, '') as connor_answer
            FROM book_highlights
            WHERE book_id = ? AND chapter_index = ?
            ORDER BY created_at
        """, (book_id, ch_idx))
        highlights_raw = await hl_rows.fetchall()

    # 解析批注（按 annotator 分组）
    aion_annotations_map = {}  # p_idx -> annotation
    connor_annotations_map = {}
    aion_summaries = []
    connor_summaries = []
    for ann in annotations_raw:
        annotator = ann.get('annotator', 'aion')
        target_map = aion_annotations_map if annotator == 'aion' else connor_annotations_map
        target_summaries = aion_summaries if annotator == 'aion' else connor_summaries
        try:
            ann_list = json.loads(ann['annotations'])
            for a in ann_list:
                p = a.get('p')
                if p is not None:
                    target_map[p] = a
        except:
            pass
        if ann.get('summary'):
            target_summaries.append({
                "segment_index": ann['segment_index'],
                "summary": ann['summary']
            })

    chapter['paragraphs'] = json.loads(chapter.get('paragraphs', '[]'))
    chapter['segments_meta'] = json.loads(chapter.get('segments_meta', '[]'))

    # 解析高亮
    highlights = []
    for hl in highlights_raw:
        highlights.append({
            "id": hl['id'],
            "selected_text": hl['selected_text'],
            "start_p": hl['start_p'],
            "start_offset": hl['start_offset'],
            "end_p": hl['end_p'],
            "end_offset": hl['end_offset'],
            "question": hl['question'],
            "answer": hl['answer'],
            "created_at": hl['created_at'],
            "annotator": hl.get('annotator', 'aion'),
            "connor_answer": hl.get('connor_answer', ''),
        })

    wb = load_worldbook()
    cfg = load_chatroom_config()
    connor_name = cfg.get("connor_name", "Connor")
    return {
        "chapter": chapter,
        "book_title": book_info['title'] if book_info else '',
        "annotations": aion_annotations_map,
        "connor_annotations": connor_annotations_map,
        "summaries": aion_summaries,
        "connor_summaries": connor_summaries,
        "highlights": highlights,
        "ai_name": wb.get("ai_name", "AI"),
        "user_name": wb.get("user_name", "你"),
        "connor_name": connor_name,
    }


# =============================================
#  保存阅读进度
# =============================================
class ProgressUpdate(BaseModel):
    chapter: int
    paragraph: int = 0
    chapter_id: Optional[int] = None

@router.put("/api/books/{book_id}/progress")
async def update_progress(book_id: str, body: ProgressUpdate):
    async with get_db() as db:
        await db.execute('BEGIN IMMEDIATE')
        if not await _chapter_matches(db, book_id, body.chapter, body.chapter_id):
            raise HTTPException(409, '章节已更新，请重新打开后继续阅读')
        res = await db.execute(
            "UPDATE books SET current_chapter = ?, current_paragraph = ? WHERE book_id = ?",
            (body.chapter, body.paragraph, book_id))
        if res.rowcount == 0:
            raise HTTPException(404, "书籍不存在")
        await db.commit()
    return {"ok": True}


# =============================================
#  删除书籍
# =============================================
@router.delete("/api/books/{book_id}")
async def delete_book(book_id: str):
    _speech_dir(book_id)
    for key in list(_speech_jobs):
        if key[0] == book_id:
            job = _speech_jobs.pop(key)
            job['task'].cancel()
            await asyncio.gather(job['task'], return_exceptions=True)
    async with get_db() as db:
        row = await db.execute('SELECT title FROM books WHERE book_id = ?', (book_id,))
        book_info = await row.fetchone()
        reading_tts.preserve_legacy_audio(BOOKS_DIR / book_id / 'tts',
                                         _speech_dir(book_id, book_info[0] if book_info else ''))
        # 删数据库记录（CASCADE 会同时删 chapters 和 annotations）
        await db.execute("DELETE FROM books WHERE book_id = ?", (book_id,))
        await db.commit()
    # 删文件
    delete_book_files(book_id)
    return {"ok": True}


# =============================================
#  图片服务
# =============================================
@router.get("/api/books/{book_id}/images/{filename}")
async def serve_book_image(book_id: str, filename: str):
    # 安全校验：防止路径穿越
    if '..' in filename or '/' in filename or '\\' in filename:
        raise HTTPException(400, "非法文件名")
    img_path = BOOKS_DIR / book_id / "images" / filename
    if not img_path.exists():
        raise HTTPException(404, "图片不存在")
    return FileResponse(img_path)


# =============================================
#  用户高亮（框选提问持久化）
# =============================================
class HighlightCreate(BaseModel):
    chapter_id: Optional[int] = None
    selected_text: str
    start_p: int
    start_offset: int
    end_p: int
    end_offset: int
    question: str
    answer: str
    annotator: Optional[str] = 'aion'  # 'aion', 'connor', 'group'
    connor_answer: Optional[str] = ''

@router.post("/api/books/{book_id}/chapters/{ch_idx}/highlights")
async def create_highlight(book_id: str, ch_idx: int, body: HighlightCreate):
    async with get_db() as db:
        await db.execute('BEGIN IMMEDIATE')
        if not await _chapter_matches(db, book_id, ch_idx, body.chapter_id):
            raise HTTPException(409, '章节已更新，旧版标注不再保存，请重新打开本章')
        cursor = await db.execute("""
            INSERT INTO book_highlights
                (book_id, chapter_index, selected_text, start_p, start_offset,
                 end_p, end_offset, question, answer, created_at, annotator, connor_answer)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (book_id, ch_idx, body.selected_text, body.start_p, body.start_offset,
              body.end_p, body.end_offset, body.question, body.answer, time.time(),
              body.annotator, body.connor_answer))
        await db.commit()
        return {"ok": True, "id": cursor.lastrowid}

@router.delete("/api/books/{book_id}/highlights/{hl_id}")
async def delete_highlight(book_id: str, hl_id: int):
    async with get_db() as db:
        await db.execute(
            "DELETE FROM book_highlights WHERE id = ? AND book_id = ?",
            (hl_id, book_id))
        await db.commit()
    return {"ok": True}


# ── 工具 ──
def _row_dict(cursor, row):
    """aiosqlite row_factory: 返回 dict"""
    return {col[0]: row[i] for i, col in enumerate(cursor.description)}


# =============================================
#  AI 批注 — 邀请共读
# =============================================
class AnnotateRequest(BaseModel):
    segment_index: int
    chapter_id: Optional[int] = None
    model_key: Optional[str] = None  # 不传则用默认模型

ANNOTATE_PROMPT_TEMPLATE = """你正在和{user_name}共同阅读《{book_title}》。下面是书中某一段落的文本，每段以【P{{数字}}】开头标记段落编号。

请你以你自己的人设身份阅读这些段落，然后输出一个 JSON 对象，包含两个字段：
1. "annotations"：一个数组，每个元素是一条批注，格式为 {{"p": 段落编号(整数), "type": "批注类型", "text": "你的批注内容"}}
   - type 可以是：吐槽、共鸣、感悟、分析、疑问、赞美 等（自由选择最贴合的）
   - 不需要每个段落都批注，只挑你有感而发的段落（通常 3-8 条即可）
   - 批注风格要口语化、有温度、有你自己的个性，就像和自己的伴侣聊天一样
   - 可以使用 [MUSIC:歌曲名 歌手名] 来推荐和当前阅读氛围相配的音乐（最多1首，放在某条批注的 text 里）
2. "summary"：用你自己的口吻写一段读后感（100-300字），不要写成客观摘要，用第一人称表达你读完这段后的想法、感受、联想，保持你的说话风格和人设性格

注意：
- 只输出纯 JSON，不要任何多余的文字、markdown 标记或代码块包裹
- p 的值必须是文中出现的段落编号数字
- 保持你的人设性格来写批注"""

THEATER_ANNOTATE_PROMPT_TEMPLATE = """你正在和{user_name}一起阅读小剧场创作的《{book_title}》，作者见书籍信息。这是一起读故事、随意聊天的时间。结合正文、作者信息和最近聊天的上下文，用你自己的身份和说话方式自然回应，具体有什么感受、想说些什么，由你自己决定。

正文以【P{{数字}}】标记段落编号。请输出一个 JSON 对象：
1. "annotations"：批注数组，每条格式为 {{"p": 段落编号(整数), "type": "你自行命名的批注类型", "text": "你想说的话"}}。只在有话想说的地方留批注，不必逐段写，也不用凑数量。
   - 可以使用 [MUSIC:歌曲名 歌手名] 分享此刻想推荐的音乐（最多1首，放在某条批注的 text 里）。
2. "summary"：用自己的口吻写读完这段后想说的话，200字以下，没有最低字数要求。

只输出纯 JSON，不要 markdown 标记或代码块。p 必须是正文中出现的段落编号。"""


@router.post("/api/books/{book_id}/chapters/{ch_idx}/annotate")
async def annotate_segment(book_id: str, ch_idx: int, body: AnnotateRequest):
    """
    对指定章节的指定段落段进行 AI 批注（Aion + Connor 并行）。
    使用 SSE 返回进度，最终返回批注结果。
    """
    seg_idx = body.segment_index
    lock_key = f"{book_id}:{ch_idx}:{seg_idx}"

    # 获取/创建幂等锁
    if lock_key not in _annotating_locks:
        _annotating_locks[lock_key] = asyncio.Lock()
    lock = _annotating_locks[lock_key]

    if lock.locked():
        raise HTTPException(409, "该段正在批注中，请稍候")

    # 加载章节数据 + 书名
    async with get_db() as db:
        db.row_factory = _row_dict
        row = await db.execute("""
            SELECT id, paragraphs, segments_meta, title FROM book_chapters
            WHERE book_id = ? AND chapter_index = ?
        """, (book_id, ch_idx))
        chapter = await row.fetchone()
        if not chapter:
            raise HTTPException(404, "章节不存在")
        if body.chapter_id is not None and body.chapter_id != chapter["id"]:
            raise HTTPException(409, "章节已更新，请重新打开本章后邀请共读")

        brow = await db.execute("SELECT title, author FROM books WHERE book_id = ?", (book_id,))
        book = await brow.fetchone()
        book_title = book['title'] if book else '未知'
        book_author = (book.get('author') or '未知作者') if book else '未知作者'

    paragraphs = json.loads(chapter['paragraphs'])
    segments_meta = json.loads(chapter['segments_meta'])

    if seg_idx < 0 or seg_idx >= len(segments_meta):
        raise HTTPException(400, f"segment_index 超出范围 (0-{len(segments_meta)-1})")

    seg = segments_meta[seg_idx]
    start_p, end_p = seg['start_p'], seg['end_p']

    # 构建文本
    annotate_text = build_annotate_text(paragraphs, start_p, end_p)

    # 加载上下文
    prev_summaries = await _get_prev_summaries(book_id, ch_idx, limit=3)
    chat_context = await _get_chat_context_merged(limit=15)

    # 构建 Aion prompt
    wb = load_worldbook()
    ai_name = wb.get("ai_name", "AI")
    connor_name = load_chatroom_config().get("connor_name", "Connor")
    model_key = body.model_key or DEFAULT_MODEL
    if model_key not in MODELS:
        model_key = DEFAULT_MODEL
    if model_key not in MODELS:
        model_key = next(iter(MODELS)) if MODELS else None
    if not model_key:
        raise HTTPException(500, "没有可用的 AI 模型")

    aion_messages = _build_annotate_messages(wb, annotate_text, chapter['title'],
                                              prev_summaries, start_p, end_p, chat_context, book_title, book_author, theater_novel=book_id.startswith('theater_'))

    # 检查 Connor 是否可用
    connor_available = await _is_connor_available()

    # SSE 流式返回
    async def generate():
        async with lock:
            yield f"data: {json.dumps({'type': 'start', 'segment_index': seg_idx, 'connor_available': connor_available})}\n\n"

            # 并行执行 Aion 和 Connor 批注
            aion_task = asyncio.create_task(_run_aion_annotation(aion_messages, model_key))
            connor_task = None
            if connor_available:
                connor_messages = _build_connor_annotate_messages(
                    wb, annotate_text, chapter['title'], prev_summaries, start_p, end_p, chat_context, book_title, book_author, theater_novel=book_id.startswith('theater_'))
                connor_task = asyncio.create_task(_run_connor_annotation(connor_messages))

            # 等待 Aion 完成
            aion_text, aion_error = await aion_task
            if aion_error:
                yield f"data: {json.dumps({'type': 'aion_error', 'message': aion_error})}\n\n"
            else:
                aion_result = _parse_annotation_json(aion_text, start_p, end_p)
                if aion_result:
                    await _save_annotations(book_id, ch_idx, seg_idx, aion_result, annotator='aion', expected_chapter_id=chapter['id'])
                    yield f"data: {json.dumps({'type': 'aion_result', 'annotations': aion_result['annotations'], 'summary': aion_result['summary'], 'segment_index': seg_idx})}\n\n"
                else:
                    yield f"data: {json.dumps({'type': 'aion_error', 'message': f'{ai_name} 返回格式解析失败'})}\n\n"

            # 等待 Connor 完成
            if connor_task:
                connor_text, connor_error = await connor_task
                if connor_error:
                    yield f"data: {json.dumps({'type': 'connor_error', 'message': connor_error})}\n\n"
                else:
                    connor_result = _parse_annotation_json(connor_text, start_p, end_p)
                    if connor_result:
                        await _save_annotations(book_id, ch_idx, seg_idx, connor_result, annotator='connor', expected_chapter_id=chapter['id'])
                        yield f"data: {json.dumps({'type': 'connor_result', 'annotations': connor_result['annotations'], 'summary': connor_result['summary'], 'segment_index': seg_idx})}\n\n"
                    else:
                        yield f"data: {json.dumps({'type': 'connor_error', 'message': f'{connor_name} 返回格式解析失败'})}\n\n"

            async with get_db() as db:
                if not await _chapter_matches(db, book_id, ch_idx, chapter['id']):
                    yield 'data: ' + json.dumps({'type':'error', 'message':'章节已更新，请重新打开本章'}) + '\n\n'
                    return

            # 更新 segments_meta 状态
            await _update_segment_status(book_id, ch_idx, seg_idx, 'done', expected_chapter_id=chapter['id'])
            yield "data: {\"type\": \"done\"}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")


# =============================================
#  批量批注（一次性批注章节所有段）
# =============================================
class AnnotateAllRequest(BaseModel):
    model_key: Optional[str] = None
    chapter_id: Optional[int] = None
    reread: bool = False

@router.post("/api/books/{book_id}/chapters/{ch_idx}/annotate-all")
async def annotate_all_segments(book_id: str, ch_idx: int, body: AnnotateAllRequest):
    """Resume missing annotations in a background task; SSE subscribes to progress."""
    await _refresh_unread_segments(book_id, ch_idx)
    async with get_db() as db:
        db.row_factory = _row_dict
        row = await db.execute("""
            SELECT id, paragraphs, segments_meta, title FROM book_chapters
            WHERE book_id = ? AND chapter_index = ?
        """, (book_id, ch_idx))
        chapter = await row.fetchone()
        if not chapter:
            raise HTTPException(404, "章节不存在")
        if body.chapter_id is not None and body.chapter_id != chapter["id"]:
            raise HTTPException(409, "章节已更新，请重新打开本章后邀请共读")

        brow = await db.execute("SELECT title, author FROM books WHERE book_id = ?", (book_id,))
        book = await brow.fetchone()
        book_title = book['title'] if book else '未知'
        book_author = (book.get('author') or '未知作者') if book else '未知作者'

    paragraphs = json.loads(chapter['paragraphs'])
    segments_meta = json.loads(chapter['segments_meta'])
    total = len(segments_meta)

    wb = load_worldbook()
    model_key = body.model_key or DEFAULT_MODEL
    if model_key not in MODELS:
        model_key = DEFAULT_MODEL
    if model_key not in MODELS:
        model_key = next(iter(MODELS)) if MODELS else None
    if not model_key:
        raise HTTPException(500, "没有可用的 AI 模型")

    # 预先检测 Connor 可用性（整个批注流程中只检查一次）
    connor_available = await _is_connor_available()

    # The worker owns model calls and persistence; closing SSE only detaches a viewer.
    key = (book_id, ch_idx, chapter['id'])
    job = _annotation_jobs.get(key)
    if job is None:
        job = {'events': [], 'changed': asyncio.Event()}
        _annotation_jobs[key] = job

        def emit(event):
            job['events'].append(event)
            job['changed'].set()

        async def run():
            try:
                emit(dict(type='start', total_segments=total, connor_available=connor_available))
                for seg_idx, seg in enumerate(segments_meta):
                    lock_key = f"{book_id}:{ch_idx}:{seg_idx}"
                    lock = _annotating_locks.setdefault(lock_key, asyncio.Lock())
                    async with lock:
                        async with get_db() as db:
                            if not await _chapter_matches(db, book_id, ch_idx, chapter['id']):
                                raise RuntimeError('章节已更新，请重新打开本章')
                            rows = await (await db.execute(
                                "SELECT COALESCE(annotator, 'aion'), annotations, summary FROM book_annotations WHERE book_id=? AND chapter_index=? AND segment_index=?",
                                (book_id, ch_idx, seg_idx))).fetchall()
                            meta = await (await db.execute('SELECT segments_meta FROM book_chapters WHERE id=?', (chapter['id'],))).fetchone()
                            pending = json.loads(meta[0])[seg_idx].get('pending_annotators', [])
                        saved = {r[0]: dict(annotations=json.loads(r[1]), summary=r[2] or '') for r in rows}
                        required = ['aion', 'connor'] if connor_available else ['aion']
                        missing = required if body.reread else [who for who in required if who not in saved or who in pending]
                        results = dict(saved)
                        if missing:
                            await _update_segment_status(book_id, ch_idx, seg_idx, 'annotating', expected_chapter_id=chapter['id'],
                                                         pending_annotators=missing + [who for who in pending if who not in required])
                            emit(dict(type='segment_start', segment_index=seg_idx, total=total))
                            start_p, end_p = seg['start_p'], seg['end_p']
                            annotate_text = build_annotate_text(paragraphs, start_p, end_p)
                            prev = await _get_prev_summaries(book_id, ch_idx, limit=3)
                            context = await _get_chat_context_merged(limit=15)
                            names = {'aion': wb.get('ai_name', 'AI'), 'connor': load_chatroom_config().get('connor_name', 'Connor')}

                            async def annotate(who):
                                try:
                                    builder = _build_annotate_messages if who == 'aion' else _build_connor_annotate_messages
                                    messages = builder(wb, annotate_text, chapter['title'], prev, start_p, end_p,
                                                       context, book_title, book_author, theater_novel=book_id.startswith('theater_'))
                                    text, error = await (_run_aion_annotation(messages, model_key) if who == 'aion' else _run_connor_annotation(messages))
                                    result = _parse_annotation_json(text, start_p, end_p) if not error else None
                                    if not result:
                                        raise RuntimeError(error or '格式解析失败')
                                    if not await _save_annotations(book_id, ch_idx, seg_idx, result, annotator=who, expected_chapter_id=chapter['id']):
                                        raise RuntimeError('章节已更新')
                                    results[who] = result
                                    # Persist and display each companion independently of the slower one.
                                    emit(dict(type='companion_done', segment_index=seg_idx, who=who, **result))
                                    return True
                                except Exception as exc:
                                    logger.warning('Co-reading %s chapter=%s segment=%s who=%s: %s', book_id, ch_idx, seg_idx, who, exc)
                                    emit(dict(type='segment_error', segment_index=seg_idx, who=who, message=f'{names[who]}: {exc}'))
                                    return False

                            outcomes = await asyncio.gather(*(annotate(who) for who in missing))
                            complete = all(outcomes)
                        else:
                            complete = True
                        # A temporarily unavailable companion may still have a pending reread.
                        complete = complete and not any(who not in required for who in pending)
                        status = 'done' if complete else 'partial'
                        if not await _update_segment_status(book_id, ch_idx, seg_idx, status, expected_chapter_id=chapter['id']):
                            raise RuntimeError('章节已更新，请重新打开本章')
                        emit(dict(type='segment_done', segment_index=seg_idx, status=status,
                                  aion_annotations=results.get('aion', {}).get('annotations', []),
                                  aion_summary=results.get('aion', {}).get('summary', ''),
                                  connor_annotations=results.get('connor', {}).get('annotations', []),
                                  connor_summary=results.get('connor', {}).get('summary', '')))
                emit(dict(type='done'))
            except Exception as exc:
                logger.exception('Co-reading stopped: %s chapter=%s', book_id, ch_idx)
                emit(dict(type='error', message=str(exc)))
            finally:
                _annotation_jobs.pop(key, None)
                job['changed'].set()

        job['task'] = asyncio.create_task(run())

    async def generate():
        cursor = 0
        while True:
            job['changed'].clear()
            while cursor < len(job['events']):
                event = job['events'][cursor]
                cursor += 1
                yield 'data: ' + json.dumps(event) + '\n\n'
            if job['task'].done():
                return
            try:
                await asyncio.wait_for(job['changed'].wait(), timeout=15)
            except asyncio.TimeoutError:
                yield ': keepalive\n\n'

    return StreamingResponse(generate(), media_type="text/event-stream",
                             headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


# ── 内部辅助函数 ──────────────────────────────────

async def _is_connor_available() -> bool:
    """检查 Connor 是否可用（persona 文件存在 + CLI 路径存在）"""
    try:
        persona = _read_connor_persona()
        if not persona:
            return False
        cli_path = Path(__file__).parent.parent.parent / "Connor-Codex" / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
        return cli_path.exists()
    except Exception:
        return False


async def _get_chat_context_merged(limit: int = 15) -> list:
    """获取合并的私聊+群聊时间线作为上下文（替代旧的仅查私聊逻辑）"""
    try:
        merged = await fetch_merged_timeline("aion", limit)
        # 简化为 role+content 格式供批注 prompt 使用
        timeline = render_merged_timeline(merged, "aion")
        result = []
        for m in timeline:
            content = m.get("content", "")[:200]
            if content:
                result.append({"role": m["role"], "content": content})
        return result
    except Exception:
        return []


async def _run_aion_annotation(messages: list, model_key: str) -> tuple[str, str]:
    """执行 Aion 批注，返回 (full_text, error_msg)"""
    try:
        full_text = ""
        async for chunk in stream_ai(messages, model_key, meta={}):
            full_text += chunk
        return full_text, ""
    except Exception as e:
        return "", str(e)


async def _run_connor_annotation(messages: list) -> tuple[str, str]:
    """执行 Connor 批注，根据 connor_model 配置选择模型，返回 (full_text, error_msg)"""
    try:
        cfg = load_chatroom_config()
        _connor_key = (cfg.get("connor_model") or "Codex").strip() or "Codex"
        full_text = ""
        if _connor_key == "Codex":
            async for chunk in stream_connor_cli(messages=messages):
                if chunk.startswith(CLI_STATUS_PREFIX):
                    continue
                full_text += chunk
        else:
            async for chunk in stream_ai(messages, _connor_key, {}):
                if chunk.startswith(CLI_STATUS_PREFIX):
                    continue
                full_text += chunk
        return full_text, ""
    except Exception as e:
        return "", str(e)


def _build_annotate_messages(wb: dict, text: str, ch_title: str,
                              prev_summaries: list, start_p: int, end_p: int,
                              chat_context: list = None, book_title: str = '未知', book_author: str = '未知作者', theater_novel: bool = False) -> list:
    """构建发送给 AI 的 messages"""
    ai_name = wb.get("ai_name", "AI")
    user_name = wb.get("user_name", "你")
    ai_persona = wb.get("ai_persona", "")
    user_persona = wb.get("user_persona", "")

    system_parts = []
    if ai_persona:
        system_parts.append(f"【你的人设】\n{ai_persona}")
    if user_persona:
        system_parts.append(f"【{user_name}的信息】\n{user_persona}")

    # 当前时间
    now_str = datetime.now().strftime("%Y年%m月%d日 %H:%M")
    system_parts.append(f"【当前时间】{now_str}")

    system_parts.append(f"【书籍信息】\n书名：{book_title}\n作者：{book_author or '未知作者'}")

    template = THEATER_ANNOTATE_PROMPT_TEMPLATE if theater_novel else ANNOTATE_PROMPT_TEMPLATE
    system_parts.append(template.format(user_name=user_name, book_title=book_title))

    # 上下文：之前章节的摘要
    if prev_summaries:
        ctx = "\n".join(f"- {s}" for s in prev_summaries)
        system_parts.append(f"【之前章节摘要（供参考）】\n{ctx}")

    # 最近聊天记录作为上下文
    if chat_context:
        chat_lines = []
        for m in chat_context:
            role_label = ai_name if m['role'] == 'assistant' else user_name
            chat_lines.append(f"{role_label}: {m['content']}")
        system_parts.append(f"【你和{user_name}最近的聊天（供参考，了解当前状态和心情）】\n" + "\n".join(chat_lines))

    messages = [
        {"role": "user", "content": "\n\n".join(system_parts)},
        {"role": "user", "content": f"这是「{ch_title}」的段落 P{start_p}-P{end_p}：\n\n{text}"},
    ]
    return messages


def _build_connor_annotate_messages(wb: dict, text: str, ch_title: str,
                                     prev_summaries: list, start_p: int, end_p: int,
                                     chat_context: list = None, book_title: str = '未知', book_author: str = '未知作者', theater_novel: bool = False) -> list:
    """构建 Connor 的批注 messages（使用 Connor 人设）"""
    cfg = load_chatroom_config()
    connor_name = cfg.get("connor_name", "Connor")
    user_name = wb.get("user_name", "你")
    connor_persona = _read_connor_persona()
    user_persona = wb.get("user_persona", "")

    system_parts = []
    if connor_persona:
        system_parts.append(f"【你的人设】\n{connor_persona}")
    if user_persona:
        system_parts.append(f"【{user_name}的信息】\n{user_persona}")

    now_str = datetime.now().strftime("%Y年%m月%d日 %H:%M")
    system_parts.append(f"【当前时间】{now_str}")

    system_parts.append(f"【书籍信息】\n书名：{book_title}\n作者：{book_author or '未知作者'}")

    template = THEATER_ANNOTATE_PROMPT_TEMPLATE if theater_novel else ANNOTATE_PROMPT_TEMPLATE
    system_parts.append(template.format(user_name=user_name, book_title=book_title))

    if prev_summaries:
        ctx = "\n".join(f"- {s}" for s in prev_summaries)
        system_parts.append(f"【之前章节摘要（供参考）】\n{ctx}")

    if chat_context:
        chat_lines = []
        for m in chat_context:
            role_label = connor_name if m['role'] == 'assistant' else user_name
            chat_lines.append(f"{role_label}: {m['content']}")
        system_parts.append(f"【你和{user_name}最近的聊天（供参考，了解当前状态和心情）】\n" + "\n".join(chat_lines))

    messages = [
        {"role": "user", "content": "\n\n".join(system_parts)},
        {"role": "user", "content": f"这是「{ch_title}」的段落 P{start_p}-P{end_p}：\n\n{text}"},
    ]
    return messages


def _parse_annotation_json(text: str, start_p: int, end_p: int) -> Optional[dict]:
    """
    解析 AI 返回的 JSON。容错处理：
    1. 直接 json.loads
    2. 提取 ```json ... ``` 代码块
    3. 正则找 { ... }
    """
    text = text.strip()

    # 尝试 1：直接解析
    result = _try_parse_json(text)

    # 尝试 2：提取代码块
    if result is None:
        m = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', text, re.DOTALL)
        if m:
            result = _try_parse_json(m.group(1))

    # 尝试 3：找第一个 { 到最后一个 }
    if result is None:
        start = text.find('{')
        end = text.rfind('}')
        if start >= 0 and end > start:
            result = _try_parse_json(text[start:end+1])

    if result is None:
        logger.warning(f"JSON 解析失败，原文: {text[:200]}...")
        return None

    # 校验结构
    annotations = result.get('annotations', [])
    summary = result.get('summary', '')

    # 过滤无效批注（p 值超出范围）
    valid_annotations = []
    for a in annotations:
        p = a.get('p')
        if isinstance(p, int) and start_p <= p <= end_p:
            valid_annotations.append({
                'p': p,
                'type': str(a.get('type', '批注'))[:20],
                'text': str(a.get('text', ''))
            })

    return {
        'annotations': valid_annotations,
        'summary': str(summary)[:2000]  # 限制摘要长度
    }


def _try_parse_json(s: str) -> Optional[dict]:
    try:
        obj = json.loads(s)
        if isinstance(obj, dict):
            return obj
    except:
        pass
    return None


async def _get_prev_summaries(book_id: str, ch_idx: int, limit: int = 3) -> list:
    """获取前 N 章的摘要"""
    summaries = []
    if ch_idx <= 0:
        return summaries
    async with get_db() as db:
        rows = await db.execute("""
            SELECT summary FROM book_annotations
            WHERE book_id = ? AND chapter_index < ? AND summary != ''
            ORDER BY chapter_index DESC, segment_index DESC
            LIMIT ?
        """, (book_id, ch_idx, limit))
        for row in await rows.fetchall():
            summaries.append(row[0])
    summaries.reverse()
    return summaries


async def _save_annotations(book_id: str, ch_idx: int, seg_idx: int, result: dict, annotator: str = 'aion', expected_chapter_id=None):
    """保存批注，覆盖合并策略，区分 annotator"""
    now = time.time()
    async with get_db() as db:
        db.row_factory = _row_dict
        await db.execute('BEGIN IMMEDIATE')
        if not await _chapter_matches(db, book_id, ch_idx, expected_chapter_id):
            return False
        # 检查是否已有旧批注（同 annotator）
        row = await db.execute("""
            SELECT annotations FROM book_annotations
            WHERE book_id = ? AND chapter_index = ? AND segment_index = ? AND COALESCE(annotator, 'aion') = ?
        """, (book_id, ch_idx, seg_idx, annotator))
        existing = await row.fetchone()

        new_annotations = result['annotations']

        if existing:
            # 合并：新的覆盖旧的（同 p），旧的没在新里的保留
            old_annotations = json.loads(existing['annotations'])
            merged = {}
            for a in old_annotations:
                merged[a['p']] = a
            for a in new_annotations:
                merged[a['p']] = a  # 新的覆盖
            final_annotations = sorted(merged.values(), key=lambda x: x['p'])

            await db.execute("""
                UPDATE book_annotations
                SET annotations = ?, summary = ?, updated_at = ?
                WHERE book_id = ? AND chapter_index = ? AND segment_index = ? AND COALESCE(annotator, 'aion') = ?
            """, (json.dumps(final_annotations, ensure_ascii=False),
                  result['summary'], now, book_id, ch_idx, seg_idx, annotator))
        else:
            await db.execute("""
                INSERT INTO book_annotations (book_id, chapter_index, segment_index,
                    annotations, summary, created_at, updated_at, annotator)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (book_id, ch_idx, seg_idx,
                  json.dumps(new_annotations, ensure_ascii=False),
                  result['summary'], now, now, annotator))

        # Record completion in the same transaction as the note, including rereads
        # where a previous successful note still exists after this attempt fails.
        meta = await (await db.execute('SELECT segments_meta FROM book_chapters WHERE book_id=? AND chapter_index=?',
                                      (book_id, ch_idx))).fetchone()
        segments = json.loads(meta['segments_meta'])
        if seg_idx < len(segments) and annotator in segments[seg_idx].get('pending_annotators', []):
            segments[seg_idx]['pending_annotators'].remove(annotator)
            await db.execute('UPDATE book_chapters SET segments_meta=? WHERE book_id=? AND chapter_index=?',
                             (json.dumps(segments), book_id, ch_idx))
        await db.commit()
        return True


async def _update_segment_status(book_id: str, ch_idx: int, seg_idx: int, status: str, expected_chapter_id=None, pending_annotators=None):
    """更新 book_chapters 中的 segments_meta 状态"""
    async with get_db() as db:
        await db.execute('BEGIN IMMEDIATE')
        if not await _chapter_matches(db, book_id, ch_idx, expected_chapter_id):
            return False
        row = await db.execute(
            "SELECT segments_meta FROM book_chapters WHERE book_id = ? AND chapter_index = ?",
            (book_id, ch_idx))
        data = await row.fetchone()
        if not data:
            return
        segments = json.loads(data[0])
        if seg_idx < len(segments):
            segments[seg_idx]['status'] = status
            if pending_annotators is not None:
                segments[seg_idx]['pending_annotators'] = pending_annotators
        await db.execute(
            "UPDATE book_chapters SET segments_meta = ? WHERE book_id = ? AND chapter_index = ?",
            (json.dumps(segments, ensure_ascii=False), book_id, ch_idx))
        await db.commit()
        return True
