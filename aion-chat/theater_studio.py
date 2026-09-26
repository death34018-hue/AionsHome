"""Small, persisted novel workspace and opt-in streaming speech for the theater."""
import asyncio
import json
import re
import time
import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from config import THEATER_TTS_CACHE_DIR, UPLOADS_DIR
from database import get_db
from ai_providers import stream_ai, CLI_STATUS_PREFIX
from stream_safety import consume_safe_stream, THEATER_STREAM_POLICY
from tts import _request_tts_audio, _find_cut_position_for_text, _strip_tags

router = APIRouter(prefix="/studio")
_locks = {}
_writing = {}
_speech = {}
_pictures = {}
_finishing = set()
_live = {}


def uid(prefix):
    return prefix + uuid.uuid4().hex


def lock(key):
    return _locks.setdefault(key, asyncio.Lock())


async def setup(db):
    await db.execute("CREATE TABLE IF NOT EXISTS theater_studio (id TEXT PRIMARY KEY, kind TEXT NOT NULL, conv_id TEXT NOT NULL, data TEXT NOT NULL)")
    await db.execute("CREATE INDEX IF NOT EXISTS theater_studio_conv ON theater_studio(conv_id, kind)")


async def load(key):
    async with get_db() as db:
        await setup(db)
        cur = await db.execute("SELECT data FROM theater_studio WHERE id=?", (key,))
        row = await cur.fetchone()
    return json.loads(row[0]) if row else None


async def save(doc):
    async with get_db() as db:
        await setup(db)
        await db.execute("INSERT INTO theater_studio VALUES (?,?,?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data",
                         (doc['id'], doc['kind'], doc.get('conv_id', ''), json.dumps(doc, ensure_ascii=False)))
        await db.commit()
    return doc


async def change(key, **fields):
    async with lock(key):
        doc = await load(key)
        if not doc:
            raise HTTPException(404, '内容不存在')
        doc.update(fields)
        await save(doc)
        return doc


async def rows(conv_id=None, kind=None):
    async with get_db() as db:
        await setup(db)
        cur = await db.execute("SELECT data FROM theater_studio WHERE (? IS NULL OR conv_id=?) AND (? IS NULL OR kind=?)", (conv_id, conv_id, kind, kind))
        return [json.loads(r[0]) for r in await cur.fetchall()]


async def conversation(cid):
    async with get_db() as db:
        cur = await db.execute("SELECT title,model,persona_id FROM theater_conversations WHERE id=?", (cid,))
        row = await cur.fetchone()
    if not row:
        raise HTTPException(404, '故事不存在')
    return row


class BookPatch(BaseModel):
    mode: str = 'dialogue'
    premise: str = ''
    outline: str = ''
    style: str = '温柔细腻，有画面感的小说插画'
    target_chars: int = Field(6500, ge=1000, le=16000)
    min_chars: int | None = Field(None, ge=1000, le=30000)
    max_chars: int | None = Field(None, ge=1000, le=30000)
    anchors: list[str] = Field(default_factory=list)
    illustrations: bool = True
    consensus: str | None = None


def chapter_limits(book, chapter=None):
    legacy = book.get('target_chars', 6500)
    chapter = chapter or {}
    return chapter.get('min_chars') or book.get('min_chars') or legacy, chapter.get('max_chars') or book.get('max_chars') or legacy


def prose_length(text):
    return sum(not ch.isspace() for ch in text)


def chapter_constraints(book, existing='', chapter=None):
    lower, upper = chapter_limits(book, chapter)
    used = prose_length(existing)
    prompt = (f'本章完整正文须在{lower}至{upper}字之间（含标点，不计空白），按本章内容选择长短，不必写满上限。'
              f'在预算内展开场景和对话，不得为凑字数重复或挪用后续章节剧情。'
              f'接近上限前主动收束，在本章计划的结束节点停笔。\n'
              f'本次只执行当前章节计划，写完本章结束节点即停笔；'
              f'计划中提及的后章边界只用于限定本章范围，不得继续展开后续章节。\n')
    if existing:
        prompt += (f'本章已有{used}字，本次新增预算为{max(0, lower-used)}至{max(0, upper-used)}字，'
                   f'这是整章合计范围，不是再写一章。从已有正文末尾继续，不重复前文。\n')
    return prompt


@router.get('/books')
async def list_books():
    return await rows(kind='book')


@router.get('/books/{cid}')
async def get_book(cid: str, chapter: str = ''):
    await conversation(cid)
    book = await load(cid)
    if not book:
        book = await save(dict(id=cid, kind='book', conv_id=cid, **BookPatch().model_dump()))
    from theater_planning import state, persona_for, discussion
    book, chapters = await state(cid)
    person = await persona_for(cid)
    book = dict(book, writer_name=person.get('name', '') if person else '', writer_ready=bool(person and person.get('persona', '').strip()))
    for i, c in enumerate(chapters):
        if c.get('needs_review'):
            c = await change(c['id'], needs_review=False)
            chapters[i] = c
        if c.get('status') == 'writing' and c['id'] not in _writing:
            c = await change(c['id'], status='interrupted', error='写作已中断，可续写本章')
            chapters[i] = c
        c['writing'] = c['id'] in _writing
        c['processing'] = c['id'] in _finishing or c['id'] in _pictures
        c['word_count'] = prose_length(c.get('content', ''))
        if chapter and c['id'] != chapter:
            chapters[i] = {k: v for k, v in c.items() if k not in ('content', 'summary', 'images', 'versions')}
    draft = await load('outline_draft_'+cid)
    return {'book': book, 'chapters': chapters, 'discussion': await discussion(cid),
            'outline_draft': draft if draft and not draft.get('confirmed_conversation') else None}


@router.put('/books/{cid}')
async def put_book(cid: str, body: BookPatch):
    await conversation(cid)
    if body.mode not in ('dialogue', 'novel'):
        raise HTTPException(400, '未知模式')
    async with lock('planning_'+cid):
        old = await load(cid) or dict(id=cid, kind='book', conv_id=cid)
        fields = body.model_dump(exclude_none=True)
        lower, upper = chapter_limits({**old, **fields})
        if lower > upper:
            raise HTTPException(422, '字数下限不能大于上限')
        changed = any(k in fields and fields[k] != old.get(k, '') for k in ('premise', 'outline', 'consensus'))
        if changed:
            from theater_planning import idle
            await idle(cid)
        old.update(fields)
        if changed and old.get('outline'):
            old['phase'] = 'review'
        return await save(old)



class CopyOutlineRequest(BaseModel):
    title: str = Field(default='', max_length=200)
    model: str = ''


@router.post('/books/{cid}/copy-outline')
async def copy_outline(cid: str, body: CopyOutlineRequest):
    async with lock('planning_'+cid):
        await conversation(cid)
        source = await load(cid)
        chapters = sorted(await rows(cid, 'chapter'), key=lambda c: c['number'])
        if not source or source.get('mode') != 'novel' or not source.get('outline', '').strip() or not chapters:
            raise HTTPException(409, '请先生成全书大纲和章节计划，再复制')
        return await create_outline_copy(cid, source, chapters, body)


async def create_outline_copy(cid, source, chapters, body, confirmation=None):
    """Caller holds the planning lock; confirmation and copy commit together."""
    original_conv = await conversation(cid)
    new_id, now = uid('tc_'), time.time()
    conv = dict(id=new_id, title=body.title.strip() or original_conv[0]+'（大纲副本）',
                    model=body.model.strip() or original_conv[1], persona_id=original_conv[2],
                    created_at=now, updated_at=now)
    book = dict(id=new_id, kind='book', conv_id=new_id,
                    **BookPatch(**source).model_dump(exclude_none=True), phase='writing', outline_versions=[])
    copies = []
    for number, c in enumerate(chapters, 1):
        copies.append(dict(id=uid('nc_'), kind='chapter', conv_id=new_id, number=number,
                               title=c['title'], plan=c['plan'],
                               **{k: c[k] for k in ('min_chars', 'max_chars') if c.get(k) is not None},
                               content='', summary='', status='planned', revision=1, images=[], versions=[], audio=None))
    # Create the sidebar entry and all empty chapters together, without copying prose or history.
    async with get_db() as db:
        await setup(db)
        await db.execute(
                'INSERT INTO theater_conversations (id,title,model,persona_id,created_at,updated_at) VALUES (?,?,?,?,?,?)',
                (new_id,conv['title'],conv['model'],conv['persona_id'],now,now))
        for doc in [book, *copies]:
            await db.execute('INSERT INTO theater_studio VALUES (?,?,?,?)',
                                 (doc['id'],doc['kind'],new_id,json.dumps(doc,ensure_ascii=False)))
        if confirmation:
            confirmation = dict(confirmation, confirmed_conversation=conv)
            await db.execute('UPDATE theater_studio SET data=? WHERE id=?',
                             (json.dumps(confirmation,ensure_ascii=False),confirmation['id']))
        await db.commit()
    from ws import manager
    await manager.broadcast({'type':'theater_conv_created','data':conv})
    return conv


class Anchor(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    aliases: str = ''
    appearance: str = ''
    image: str = ''


def reference_path(url):
    if not url.startswith('/uploads/'):
        raise HTTPException(400, '请上传人物照片')
    root = Path(UPLOADS_DIR).resolve()
    path = (root / url.removeprefix('/uploads/')).resolve()
    if not path.is_relative_to(root) or not path.is_file() or path.suffix.lower() not in ('.jpg', '.jpeg', '.png', '.webp'):
        raise HTTPException(400, '人物照片无效')
    return path


@router.get('/anchors')
async def get_anchors():
    return await rows(kind='anchor')


@router.put('/anchors/{aid}')
async def put_anchor(aid: str, body: Anchor):
    if body.image:
        reference_path(body.image)
    if not re.fullmatch(r'[\w-]{1,80}', aid):
        raise HTTPException(400, '无效角色编号')
    return await save(dict(id='anchor_'+aid, kind='anchor', conv_id='', **body.model_dump()))


class ModelRequestError(RuntimeError):
    def __init__(self, label, response):
        super().__init__('模型接口返回错误：'+label)
        self.response = response


def _provider_error_detail(raw):
    text = str(raw or '').strip()
    prefix = ''
    if text.startswith('HTTP '):
        prefix, _, text = text.partition(':')
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            detail = parsed.get('error') or parsed.get('message') or parsed.get('detail')
            if isinstance(detail, dict):
                detail = detail.get('message') or detail.get('detail') or detail.get('code')
            if detail:
                text = str(detail)
    except (ValueError, TypeError):
        pass
    return (prefix + ('：' if prefix and text else '') + ' '.join(text.split()))[:400]


async def generate_text(cid, prompt, on_chunk=None, *, max_tokens=None, messages=None):
    conv = await conversation(cid)
    from routes.theater import _load_personas
    persona = next((p.get('persona', '') for p in _load_personas() if p['id'] == conv[2]), '')
    history = [{'role': 'user', 'content': (f'你以以下人设陪用户讨论和创作，保留自己的性格、情感与说话方式。故事角色的身份和关系遵守平行宇宙设定，不把现实关系强加给初识剧情。\n{persona}\n\n' if persona else '') + prompt}]
    if messages is not None:
        history[0]['role'] = 'system'
        history.extend({'role': m['role'], 'content': m['content']} for m in messages)
    provider_error = None
    provider_meta = {}
    async def source():
        nonlocal provider_error
        async for chunk in stream_ai(history, conv[1], meta=provider_meta, max_tokens=max_tokens if max_tokens is not None else (24000 if on_chunk else 6000), include_device_context=False, request_timeout=300):
            if provider_meta.get('provider_error'):
                provider_error = ModelRequestError(_provider_error_detail(provider_meta['provider_error']), str(chunk))
                raise provider_error
            marker = re.match(r'\s*(\[(?:(?:硅基流动|Gemini(?:CLI)?|CodexCLI|AntigravityCLI|自定义中转站)?错误)[^\]]*\])', chunk)
            if marker:
                provider_error = ModelRequestError(_provider_error_detail(chunk), chunk)
                raise provider_error
            if not chunk.startswith(CLI_STATUS_PREFIX):
                yield chunk
    parts = []
    async def commit(chunk):
        parts.append(chunk)
        if on_chunk:
            await on_chunk(chunk)
    result = await consume_safe_stream(source(), THEATER_STREAM_POLICY, commit)
    if provider_error:
        raise provider_error
    if provider_meta.get('provider_timeout'):
        raise RuntimeError('本次等待 5 分钟仍未收到回复' if not parts else '模型连接等待超过 5 分钟，已停止生成')
    if result.stop_reason:
        if result.stop_reason == 'transport' and result.diagnostic_error:
            raise RuntimeError('模型连接中断：' + _provider_error_detail(result.diagnostic_error))
        raise RuntimeError(result.notice or '生成中断')
    return ''.join(parts)


def parse_json(text):
    start, end = text.find('{'), text.rfind('}')
    return json.loads(text[start:end+1])


async def cast_text(book):
    cast = [a for a in await get_anchors() if a['id'] in book.get('anchors', [])]
    return cast, '\n'.join(f"{a['name']}（别名：{a['aliases']}）：{a['appearance']}" for a in cast)


@router.post('/books/{cid}/outline')
async def make_outline(cid: str):
    from theater_planning import make_outline as plan
    return await plan(cid)


class ChapterPatch(BaseModel):
    min_chars: int | None = Field(None, ge=1000, le=100000)
    max_chars: int | None = Field(None, ge=1000, le=100000)
    title: str | None = None
    plan: str | None = None
    content: str | None = None


async def stop_audio(key):
    task = _speech.pop(key, None)
    if task:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@router.patch('/chapters/{key}')
async def edit_chapter(key: str, body: ChapterPatch):
    initial = await load(key)
    if not initial or initial['kind'] != 'chapter':
        raise HTTPException(404, '章节不存在')
    async with lock('planning_'+initial['conv_id']), lock('operation_'+key):
        if key in _writing:
            raise HTTPException(409, '请先停止写作，再编辑正文或计划')
        c = await load(key)
        if not c or c['kind'] != 'chapter':
            raise HTTPException(404, '章节不存在')
        fields = body.model_dump(exclude_none=True)
        book = await load(c['conv_id'])
        lower, upper = chapter_limits(book, {**c, **fields})
        if lower > upper:
            raise HTTPException(422, '本章字数下限不能大于上限')
        if any(k in fields and fields[k] != c.get(k) for k in ('title', 'plan')):
            from theater_planning import idle
            await idle(c['conv_id'])
            await change(c['conv_id'], phase='review')
        if body.content is not None and body.content != c['content']:
            await stop_audio(key)
            _live.pop(key, None)
            # Editing a partial draft must not silently mark the chapter finished.
            fields.update(revision=c['revision']+1, summary='', images=[], audio=None,
                          status='ready' if c['status'] == 'ready' else 'interrupted', error='',
                          versions=(c.get('versions', [])+[snapshot(c)])[-3:])
        return await change(key, **fields)


def snapshot(c):
    return {k: c.get(k) for k in ('content', 'summary', 'images', 'audio', 'revision', 'status')}


@router.post('/chapters/{key}/restore')
async def restore_chapter(key: str):
    async with lock('operation_'+key):
        if key in _writing:
            raise HTTPException(409, '请先停止写作')
        c = await load(key)
        if not c or not c.get('versions'):
            raise HTTPException(400, '没有上一版')
        await stop_audio(key)
        _live.pop(key, None)
        old = c['versions'][-1]
        # New revision prevents late illustration/audio jobs from attaching.
        old.update(revision=c['revision']+1, audio=None, versions=c['versions'][:-1]+[snapshot(c)])
        return await change(key, **old)


class WriteRequest(BaseModel):
    instruction: str = ''
    rewrite: bool = False


async def summarize(c):
    text = await generate_text(c['conv_id'], '仅根据以下已写正文，记录已发生事件、人物当前关系/位置/知情范围、未解伏笔；不要把未来计划当事实。600字以内。\n'+c['content'])
    current = await load(c['id'])
    if current and current['revision'] == c['revision']:
        await change(c['id'], summary=text)
    return text


async def write_chapter(key, instruction):
    c = await load(key)
    text = c['content']
    last_save = 0
    try:
        book = await load(c['conv_id'])
        all_chapters = sorted(await rows(c['conv_id'], 'chapter'), key=lambda p: p['number'])
        previous = [p for p in all_chapters if p['number'] < c['number']]
        memories = []
        for p in previous:
            memories.append(f"第{p['number']}章："+(p.get('summary') or await summarize(p)))
        _, cast = await cast_text(book)
        target = f"第{c['number']}章《{c['title']}》"
        prompt = f'本次只写{target}，不得跳章或改写成其他章节。只输出本章正文，不输出解释、配图提示或总结。\n'
        prompt += chapter_constraints(book, text, c)
        prompt += '本章计划若标明期望字数及场景预算，按其详略分配推进；字数范围以上面的当前设置为准，过时预算超出范围时按比例调整。略写场景不要扩成完整支线；核心变化与结束节点完成后收尾，不为填满上限重复动作、对白或情绪解释。\n'
        prompt += f"【背景参考，不代表剧情已经发生】\n已确认的故事共识：{book.get('consensus') or ''}\n人物：{cast}\n"
        prompt += f"【前章已发生剧情】\n{chr(10).join(memories) if previous else '无前章，尚无已发生剧情。这是故事的第一章。'}\n"
        prompt += f"【当前唯一写作任务】\n当前{target}计划（待执行，不是已写正文）：\n{c['plan']}\n用户本次指导：{instruction}\n"
        if text:
            prompt += f"本次续写{target}，从下面已有正文末尾继续，不重写开头，也不进入下一章。\n本章已有正文（从末尾继续，不重复，写至本章结束）：\n{text}"
        else:
            prompt += f"现在从头写{target}。本章尚无正文，从本章计划的第一个场景开始，依次展开至本章结束节点；不得把本章计划当作已经发生的剧情，再接着写下一章。\n"
        from theater_planning import persona_for
        person = await persona_for(c['conv_id'], required=True)
        await change(key, context_characters={
            '角色人设': len(person['persona']), '故事共识': len(book.get('consensus') or ''),
            '人物外貌文字': len(cast), '全书大纲': 0,
            '前章剧情摘要': len(chr(10).join(memories)), '本章计划': len(c['plan']),
            '本次指导': len(instruction), '上一章结尾': 0,
            '本章已有正文': len(c['content']), '历史讨论原文': 0,
        })
        async def commit(chunk):
            nonlocal text, last_save
            text += chunk
            _live[key] = {'content': text, 'done': False, 'revision': c['revision'], 'conv_id': c['conv_id']}
            if time.monotonic()-last_save > .5:
                await change(key, content=text)
                last_save = time.monotonic()
        await generate_text(c['conv_id'], prompt, commit)
        if not text.strip():
            raise RuntimeError('未生成正文，请重试')
        # A clean transport end does not establish that the story chapter is complete.
        await change(key, content=text, status='draft', error='', needs_review=False)
    except asyncio.CancelledError:
        await change(key, content=text, status='interrupted', error='已停止写作，可续写')
    except Exception as e:
        await change(key, content=text, status='interrupted', error=str(e)[:500])
    finally:
        _live[key] = {'content': text, 'done': True, 'revision': c['revision'], 'conv_id': c['conv_id']}
        _finishing.add(key)
        _writing.pop(key, None)
    try:
        current = await load(key)
        if current and current['status'] in ('draft', 'ready'):
            try:
                await summarize(current)
            except Exception:
                pass  # Rebuilt before the next chapter if unavailable.
            await illustrate(key)
    finally:
        _finishing.discard(key)


@router.post('/chapters/{key}/write')
async def start_writing(key: str, body: WriteRequest):
    initial = await load(key)
    if not initial or initial['kind'] != 'chapter':
        raise HTTPException(404, '章节不存在')
    async with lock('planning_'+initial['conv_id']), lock('operation_'+key):
        c = await load(key)
        if not c or c['kind'] != 'chapter':
            raise HTTPException(404, '章节不存在')
        from theater_planning import state, persona_for
        book, _ = await state(c['conv_id'])
        if book['phase'] != 'writing':
            raise HTTPException(409, '请先确认大纲，再开始写正文')
        await persona_for(c['conv_id'], required=True)
        if key in _writing or any(p['id'] in _writing for p in await rows(c['conv_id'], 'chapter')):
            raise HTTPException(409, '这本故事已有章节正在写')
        previous = [p for p in await rows(c['conv_id'], 'chapter') if p['number'] < c['number']]
        if any(p['status'] != 'ready' for p in previous):
            raise HTTPException(409, '请先完成前面的章节')
        if c.get('status') == 'ready' and c['content'] and not body.rewrite:
            raise HTTPException(409, '本章已确认完成；如需续写，请先选择「本章还没写完」')
        if not body.rewrite and prose_length(c['content']) >= chapter_limits(book, c)[1]:
            raise HTTPException(409, '本章已达到字数上限；请在菜单「章节计划」中调高本章上限后再续写')
        await stop_audio(key)
        updates = dict(status='writing', error='')
        if body.instruction.strip():
            updates['plan'] = c['plan']+'\n本次创作指导：'+body.instruction.strip()
        if body.rewrite:
            updates.update(content='', summary='', images=[], audio=None, revision=c['revision']+1,
                           versions=(c.get('versions', [])+[snapshot(c)])[-3:])
        elif c['content']:
            updates.update(summary='', images=[], revision=c['revision']+1,
                           versions=(c.get('versions', [])+[snapshot(c)])[-3:])
        # Continuing a partial chapter also starts a fresh audio session on request.
        updates['audio'] = None
        c = await change(key, **updates)
        _live[key] = dict(content=c['content'], done=False, revision=c['revision'], conv_id=c['conv_id'])
        _writing[key] = asyncio.create_task(write_chapter(key, body.instruction))
        return c


@router.post('/chapters/{key}/stop')
async def stop_writing(key: str):
    task = _writing.get(key)
    if task:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        _writing.pop(key, None)
        c = await load(key)
        if c and c['status'] == 'writing':
            await change(key, status='interrupted', error='已停止写作，可续写')
        finish_live(key)
    return {'ok': True}


@router.post('/chapters/{key}/confirm')
async def confirm_chapter(key: str):
    return await change(key, needs_review=False)


class ChapterCompletion(BaseModel):
    completed: bool


@router.post('/chapters/{key}/completion')
async def set_chapter_completion(key: str, body: ChapterCompletion):
    initial = await load(key)
    if not initial or initial.get('kind') != 'chapter':
        raise HTTPException(404, '章节不存在')
    async with lock('planning_'+initial['conv_id']), lock('operation_'+key):
        if any(c['id'] in _writing for c in await rows(initial['conv_id'], 'chapter')):
            raise HTTPException(409, '请先停止正在进行的章节写作')
        c = await load(key)
        if not c or c.get('kind') != 'chapter':
            raise HTTPException(404, '章节不存在')
        if not c.get('content', '').strip():
            raise HTTPException(409, '本章还没有正文')
        return await change(key, status='ready' if body.completed else 'draft', error='')


async def illustrate(key):
    if key in _pictures:
        return
    _pictures[key] = True
    try:
        c = await load(key)
        book = await load(c['conv_id'])
        if not book.get('illustrations') or c.get('images'):
            return
        cast, names = await cast_text(book)
        plan = parse_json(await generate_text(c['conv_id'], f"为本章选择0至3个真正值得画的精彩场景，不强制配图。人物：{names}\n返回JSON {{\"scenes\":[{{\"quote\":\"正文中连续且唯一的一句原文，用来定位插图\",\"prompt\":\"场景、服饰、人物位置与动作，明确名字\",\"characters\":[\"人物名字\"]}}]}}\n正文：{c['content']}"))
        scenes = []
        for s in plan.get('scenes', [])[:3]:
            quote = str(s.get('quote', ''))
            if quote and quote in c['content']:
                scenes.append(dict(id=uid('pic_'), after=c['content'].index(quote)+len(quote), prompt=str(s['prompt']), characters=s.get('characters', []), status='pending', url=''))
        current = await load(key)
        if not current or current['revision'] != c['revision']:
            return
        await change(key, images=scenes, image_error='')
        for s in scenes:
            current = await load(key)
            if not current or current['revision'] != c['revision']:
                return
            from image_gen import generate_image
            refs = [{'name': a['name'], 'path': str(reference_path(a['image']))} for a in cast if a['image'] and (a['name'] in s['characters'] or any(alias.strip() in s['characters'] for alias in a['aliases'].split(',') if alias.strip()))]
            filename = await generate_image(f"统一画风：{book['style']}\n人物固定外貌：{names}\n{s['prompt']}", references=refs)
            s.update(status='ready' if filename else 'failed', url=('/uploads/'+filename.lstrip('/')) if filename else '')
            current = await load(key)
            if current and current['revision'] == c['revision']:
                await change(key, images=scenes)
    except Exception:
        if await load(key):
            await change(key, image_error='配图未完成，可重试')
    finally:
        _pictures.pop(key, None)


@router.post('/chapters/{key}/illustrate')
async def retry_images(key: str):
    c = await load(key)
    if not c or c.get('status') not in ('draft', 'ready'):
        raise HTTPException(409, '请先完成本章正文')
    if key in _pictures:
        return {'ok': True}
    await change(key, images=[])
    asyncio.create_task(illustrate(key))
    return {'ok': True}


# Dialogue feeds the same opt-in speech source without changing its SSE protocol.
def begin_live(key, cid):
    _live[key] = dict(content='', done=False, revision=1, conv_id=cid)


def append_live(key, chunk):
    if key in _live:
        _live[key]['content'] += chunk


def finish_live(key):
    if key in _live:
        _live[key]['done'] = True


async def source_state(key):
    if key in _live:
        return _live[key]
    doc = await load(key)
    if doc and doc['kind'] == 'chapter':
        return dict(content=doc['content'], done=True, revision=doc['revision'], conv_id=doc['conv_id'])
    async with get_db() as db:
        cur = await db.execute("SELECT content,conv_id FROM theater_messages WHERE id=? AND role='assistant'", (key,))
        row = await cur.fetchone()
    if row:
        return dict(content=row[0], done=True, revision=1, conv_id=row[1])
    raise HTTPException(404, '正文不存在')


async def invalidate_message(key):
    await stop_audio(key)
    _live.pop(key, None)
    docs = [a for a in await rows(kind='audio') if a['source'] == key]
    await delete_recording_files(docs)
    async with get_db() as db:
        await setup(db)
        for a in docs:
            await db.execute('DELETE FROM theater_studio WHERE id=?', (a['id'],))
        await db.commit()
    from theater_tts_cache import delete_message_audio_files
    await asyncio.to_thread(delete_message_audio_files, [key], THEATER_TTS_CACHE_DIR)


async def delete_recording_files(docs):
    from theater_tts_cache import delete_message_audio_files, delete_studio_audio_files
    await asyncio.to_thread(delete_studio_audio_files, [a['id'] for a in docs], THEATER_TTS_CACHE_DIR)
    legacy_sources = [a['source'] for a in docs if a.get('legacy')]
    if legacy_sources:
        await asyncio.to_thread(delete_message_audio_files, legacy_sources, THEATER_TTS_CACHE_DIR)


class SpeechRequest(BaseModel):
    voice: str = ''
    prefer_cached: bool = True
    allow_generation: bool = False


async def legacy_message_audio(key, src):
    """Link existing message recordings without copying files or guessing their voice."""
    from theater_tts_cache import list_message_audio_segments
    if not re.fullmatch(r'[\w-]+', key):
        return None
    root = Path(THEATER_TTS_CACHE_DIR)
    merged = root / f'{key}.mp3'
    files = [merged] if merged.is_file() and merged.stat().st_size else [
        path for _, path in list_message_audio_segments(key, root) if path.stat().st_size]
    if not files:
        return None
    doc = dict(id='legacy_'+key, kind='audio', conv_id=src['conv_id'], source=key,
               revision=src['revision'], voice='', legacy=True, offset=len(src['content']),
               segments=[dict(seq=i, url=f'/api/theater/tts/audio/{path.stem}', chars=0)
                         for i, path in enumerate(files)], status='ready')
    if await load(doc['id']) != doc:
        await save(doc)
    return doc


async def run_speech(key, aid):
    try:
        while True:
            audio = await load(aid)
            src = await source_state(key)
            if src['revision'] != audio['revision']:
                break
            remaining = src['content'][audio['offset']:]
            cut = _find_cut_position_for_text(remaining, 300, 500) if len(remaining) >= 300 else None
            if cut is None and src['done'] and remaining:
                cut = min(len(remaining), 500)-1
            if cut is None:
                if src['done']:
                    await change(aid, status='ready')
                    return
                await asyncio.sleep(.3)
                continue
            raw = remaining[:cut+1]
            text = _strip_tags(raw).strip()
            if not text:
                await change(aid, offset=audio['offset']+len(raw))
                continue
            seq = len(audio['segments'])
            data = await _request_tts_audio(text, audio['voice'], seq=seq)
            if not data:
                await change(aid, status='failed', error=f'第{seq+1}段合成失败，点击重试继续')
                return
            path = Path(THEATER_TTS_CACHE_DIR) / f'{aid}_{seq}.mp3'
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            audio['segments'].append(dict(seq=seq, url=f'/api/theater/studio/audio/{aid}/{seq}',
                                          chars=len(raw), start=audio['offset'], end=audio['offset']+len(raw)))
            await change(aid, segments=audio['segments'], offset=audio['offset']+len(raw))
    except asyncio.CancelledError:
        await change(aid, status='stopped')
    except Exception:
        await change(aid, status='failed', error='语音合成中断，可重试继续')
    finally:
        if _speech.get(key) is asyncio.current_task():
            _speech.pop(key, None)


@router.post('/speech/{key}')
async def start_speech(key: str, body: SpeechRequest):
    async with lock('speech_'+key):
        src = await source_state(key)
        source_doc = await load(key)
        existing = await rows(src['conv_id'], 'audio')
        is_chapter = source_doc and source_doc.get('kind') == 'chapter'
        if body.prefer_cached:
            cached = None if is_chapter else await legacy_message_audio(key, src)
            if not cached:
                candidates = [a for a in existing if a['source'] == key and a['revision'] == src['revision']
                              and not a.get('legacy') and (a.get('segments') or
                                  (a['status'] == 'running' and key in _speech))]
                # Listening chooses the most complete recording, independent of the selected voice.
                linked_id = source_doc.get('audio') if is_chapter else None
                cached = max(candidates, key=lambda a: (a['status'] == 'ready', a.get('offset', 0),
                                                       a['id'] == linked_id), default=None)
                if cached and not all((p := Path(THEATER_TTS_CACHE_DIR)/f"{cached['id']}_{seg['seq']}.mp3").is_file()
                                      and p.stat().st_size for seg in cached['segments']):
                    raise HTTPException(409, '已有语音记录的文件缺失，请检查缓存；未重新生成')
            if cached:
                if cached['status'] != 'running':
                    await stop_audio(key)
                cached = await audio_status(cached['id'])
                if is_chapter and source_doc.get('audio') != cached['id']:
                    await change(key, audio=cached['id'])
                return cached
            if any(a['source'] == key and a.get('legacy') for a in existing):
                raise HTTPException(409, '旧语音文件暂时找不到，请检查缓存；未重新生成')
        if not body.allow_generation:
            return {'needs_confirmation': True}
        if not body.voice.strip():
            raise HTTPException(400, '没有可重听的语音，请先选择音色再生成')
        audio = next((a for a in reversed(existing) if a['source'] == key and a['revision'] == src['revision'] and a['voice'] == body.voice), None)
        if key in _speech:
            running = next((a for a in existing if a['source'] == key and a['status'] == 'running'), None)
            if running and running['voice'] == body.voice:
                return running
            await stop_audio(key)
        if not audio:
            # Explicit new-voice requests retain a separate cache; old recordings stay intact.
            audio = await save(dict(id=uid('na_'), kind='audio', conv_id=src['conv_id'], source=key, revision=src['revision'], voice=body.voice,
                                    offset=0, segments=[], status='running'))
        if audio['status'] != 'ready' or audio['offset'] < len(src['content']) or not src['done']:
            audio = await change(audio['id'], status='running', error='')
            _speech[key] = asyncio.create_task(run_speech(key, audio['id']))
        doc = await load(key)
        if doc and doc['kind'] == 'chapter':
            await change(key, audio=audio['id'])
        return audio


@router.get('/speech/status/{aid}')
async def audio_status(aid: str):
    doc = await load(aid)
    if not doc or doc['kind'] != 'audio':
        raise HTTPException(404, '语音不存在')
    if doc['status'] == 'running' and doc['source'] not in _speech:
        doc = await change(aid, status='stopped')
    return doc


@router.post('/speech/{key}/stop')
async def cancel_speech(key: str):
    await stop_audio(key)
    return {'ok': True}


@router.get('/audio/{aid}/{seq}')
async def audio_file(aid: str, seq: int):
    if not re.fullmatch(r'na_[a-f0-9]{32}', aid) or seq < 0:
        raise HTTPException(404)
    path = Path(THEATER_TTS_CACHE_DIR) / f'{aid}_{seq}.mp3'
    if not path.is_file():
        raise HTTPException(404)
    return FileResponse(path, media_type='audio/mpeg')


async def cleanup_conversation(cid):
    from theater_planning import cleanup
    await cleanup(cid)
    docs = await rows(cid)
    for doc in docs:
        key = doc['id']
        if key in _writing:
            await stop_writing(key)
        await stop_audio(doc['source'] if doc['kind'] == 'audio' else key)
    for key, state in list(_live.items()):
        if state['conv_id'] == cid:
            await stop_audio(key)
            _live.pop(key, None)
    # Re-read after stopping synthesis to include the last persisted recording/chunk.
    await delete_recording_files(await rows(cid, 'audio'))
    # Generated illustrations remain in the album even after their story is deleted.
    async with get_db() as db:
        await setup(db)
        await db.execute('DELETE FROM theater_studio WHERE conv_id=?', (cid,))
        await db.commit()


from theater_planning import router as planning_router
router.include_router(planning_router)
