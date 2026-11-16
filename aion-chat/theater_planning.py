"""Persona-led story discussion and explicit outline approval."""
import asyncio
import json
import logging
import time

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, ValidationError

import theater_studio as s
from theater_outline_format import parse_outline

router = APIRouter(prefix='/books')
_discussing = {}


async def persona_for(cid, required=False):
    conv = await s.conversation(cid)
    from routes.theater import _load_personas
    person = next((p for p in _load_personas() if p['id'] == conv[2]), None)
    if required and (not person or not person.get('persona', '').strip()):
        raise HTTPException(400, '请先在右上角设置中选择有完整人设的角色，再一起讨论故事')
    return person


async def state(cid):
    book = await s.load(cid)
    chapters = sorted(await s.rows(cid, 'chapter'), key=lambda c: c['number'])
    if 'phase' not in book:
        # Existing written stories remain usable; an unused outline needs approval.
        book = await s.change(cid, phase='writing' if any(c.get('content') for c in chapters) else 'review' if chapters else 'discussion',
                              consensus=book.get('premise', ''), outline_versions=[])
    return book, chapters


async def discussion(cid):
    doc = await s.load('discussion_'+cid)
    if not doc:
        doc = await s.save(dict(id='discussion_'+cid, kind='discussion', conv_id=cid, messages=[], notes='', summarized=0, status='idle'))
    if doc['status'] == 'running' and cid not in _discussing:
        doc = await s.change(doc['id'], status='interrupted', error='讨论已中断，可以继续发送或重试')
    return doc


async def idle(cid):
    if cid in _discussing:
        raise HTTPException(409, '请等这一轮讨论结束')
    if any(c['id'] in s._writing for c in await s.rows(cid, 'chapter')):
        raise HTTPException(409, '请先停止正在进行的章节写作，再商量大纲')


async def compact(cid, doc):
    complete = [m for m in doc['messages'] if m.get('content')]
    end = max(0, len(complete)-8)
    start = doc.get('summarized', 0)
    if len(complete)-start > 20:
        notes = await s.generate_text(cid, '整理故事讨论记录，约1500字。保留用户明确确认、否定、待定的内容并区分，保留人物关系、世界观、文风和禁忌，不将提议当成已确认事实。\n已有讨论摘要：'+doc.get('notes', '')+'\n补充记录：'+json.dumps(complete[start:end], ensure_ascii=False), auxiliary=True)
        doc = await s.change(doc['id'], notes=notes, summarized=end)
    return doc


def discussion_context(doc):
    complete = [m for m in doc['messages'] if m.get('content')]
    return '较早讨论摘要：\n'+doc.get('notes', '')+'\n最近讨论：\n'+json.dumps(complete[doc.get('summarized', 0):], ensure_ascii=False)


class Talk(BaseModel):
    content: str = Field(min_length=1, max_length=12000)


async def reply(cid, message_id):
    text = ''
    last_save = 0
    try:
        doc = await compact(cid, await discussion(cid))
        complete = [m for m in doc['messages'] if m.get('content')]
        messages = complete[doc.get('summarized', 0):]
        prompt = ('你正在以自己的人设和熟悉的语气，陪用户闲聊或讨论故事。默认顺着用户当前的话题自然回应，接住用户的情绪和想法，让用户主导表达；用户可能只是随口聊天，或正在自己讲述设定，不必把每句话都推进成剧情策划。'
                  '只有用户主动、明确要求你发散脑洞、提供创意或帮忙构思时，才在用户要求的范围内提出具体的世界观、身份、相遇或情感走向，给用户留修改空间。一次邀请不代表之后每轮都要发散；用户回到闲聊或继续表达自己的想法时，跟随用户的节奏。'
                  '用户没有邀请发散时，不主动添加新设定、剧情分支或成套方案，也不在每次回复末尾追问故事方向。不要像问卷一样逐条盘问，不要擅自开始小说正文或生成正式章节大纲。分清你与用户的现实交流关系和故事中可能互不相识的角色。\n'
                  '回应最新一条用户消息；只依据这段聊天交流，不假定自己看过聊天之外的大纲或正文。\n')
        if doc.get('notes'):
            prompt += '以下是较早聊天的摘要，仅供理解对话，当前要回应的内容以最新用户消息为准：\n'+doc['notes']
        async def persist():
            current = await s.load(doc['id'])
            if not current:
                raise asyncio.CancelledError()
            for m in current['messages']:
                if m['id'] == message_id:
                    m['content'] = text
            await s.change(doc['id'], messages=current['messages'])
        async def commit(chunk):
            nonlocal text, last_save
            text += chunk
            if time.monotonic()-last_save > .5:
                await persist()
                last_save = time.monotonic()
        await s.generate_text(cid, prompt, commit, messages=messages)
        await persist()
        if not text.strip():
            raise RuntimeError('讨论未返回内容')
        await s.change(doc['id'], status='idle', error='')
    except asyncio.CancelledError:
        if await s.load('discussion_'+cid):
            await s.change('discussion_'+cid, status='interrupted', error='讨论已停止')
    except Exception:
        if await s.load('discussion_'+cid):
            await s.change('discussion_'+cid, status='interrupted', error='这一轮未完成，可以重试；你的输入已保存')
    finally:
        _discussing.pop(cid, None)


@router.get('/{cid}/discussion')
async def get_discussion(cid: str):
    await s.conversation(cid)
    return await discussion(cid)


@router.post('/{cid}/discuss')
async def send_discussion(cid: str, body: Talk):
    async with s.lock('planning_'+cid):
        await idle(cid)
        await persona_for(cid, required=True)
        book, _ = await state(cid)
        doc = await discussion(cid)
        mid = s.uid('talk_')
        messages = doc['messages']+[dict(id=s.uid('talk_'), role='user', content=body.content.strip()), dict(id=mid, role='assistant', content='')]
        if not book.get('outline'):
            await s.change(cid, phase='discussion', premise=book.get('premise') or body.content.strip())
        await s.change(doc['id'], messages=messages, status='running', error='')
        _discussing[cid] = asyncio.create_task(reply(cid, mid))
        return {'ok': True}


async def discussion_target(cid, mid):
    await s.conversation(cid)
    await idle(cid)
    doc = await discussion(cid)
    message = next((m for m in doc['messages'] if m['id'] == mid), None)
    if message is None:
        raise HTTPException(404, '讨论消息不存在')
    return doc, message


@router.put('/{cid}/discussion/messages/{mid}')
async def edit_discussion_message(cid: str, mid: str, body: Talk):
    async with s.lock('planning_'+cid):
        doc, message = await discussion_target(cid, mid)
        if message['role'] != 'user':
            raise HTTPException(400, '只能编辑自己的消息')
        content = body.content.strip()
        if not content:
            raise HTTPException(400, '消息不能为空')
        book, _ = await state(cid)
        if message is doc['messages'][0] and book.get('premise') == message['content']:
            await s.change(cid, premise=content)
        message['content'] = content
        return await s.change(doc['id'], messages=doc['messages'], notes='', summarized=0, error='')


@router.delete('/{cid}/discussion/messages/{mid}')
async def delete_discussion_message(cid: str, mid: str):
    async with s.lock('planning_'+cid):
        doc, message = await discussion_target(cid, mid)
        book, _ = await state(cid)
        if message is doc['messages'][0] and message['role'] == 'user' and book.get('premise') == message['content']:
            await s.change(cid, premise='')
        return await s.change(doc['id'], messages=[m for m in doc['messages'] if m['id'] != mid],
                              notes='', summarized=0, error='')


@router.post('/{cid}/discussion/messages/{mid}/regenerate')
async def regenerate_discussion_message(cid: str, mid: str):
    async with s.lock('planning_'+cid):
        doc, message = await discussion_target(cid, mid)
        if message['role'] != 'assistant':
            raise HTTPException(400, '只能重新生成 AI 的回复')
        await persona_for(cid, required=True)
        # Match the dialogue theater: replace this reply with a new turn at the end.
        replacement = s.uid('talk_')
        messages = [m for m in doc['messages'] if m['id'] != mid]
        messages.append(dict(id=replacement, role='assistant', content=''))
        await s.change(doc['id'], messages=messages, notes='', summarized=0, status='running', error='')
        _discussing[cid] = asyncio.create_task(reply(cid, replacement))
        return {'ok': True}


@router.post('/{cid}/discussion-mode')
async def discussion_mode(cid: str):
    async with s.lock('planning_'+cid):
        await idle(cid)
        book, _ = await state(cid)
        return book


@router.post('/{cid}/use-current-outline')
async def use_current_outline(cid: str):
    """Explicitly keep the current outline, including stories locked by old navigation."""
    async with s.lock('planning_'+cid):
        await idle(cid)
        book, chapters = await state(cid)
        if not book.get('outline') or not chapters:
            raise HTTPException(409, '还没有可沿用的大纲，请先生成大纲')
        return await s.change(cid, phase='writing')


def outline_snapshot(book, chapters):
    return dict(outline=book.get('outline', ''), consensus=book.get('consensus', ''),
                plans=[dict(title=c['title'], plan=c['plan'], number=c['number']) for c in chapters if not c.get('content')])


async def install_outline(cid, book, chapters, proposal, versions, confirmation=None):
    frozen = [c for c in chapters if c.get('content')]
    last = max((c['number'] for c in frozen), default=0)
    new_chapters = []
    remaining = [p for p in proposal['plans'] if p.get('number', last+1) > last]
    for i, p in enumerate(remaining):
        new_chapters.append(dict(id=s.uid('nc_'), kind='chapter', conv_id=cid, number=last+i+1,
                                 title=p['title'], plan=p['plan'],
                                 **{k: p[k] for k in ('min_chars', 'max_chars') if p.get(k) is not None},
                                 content='', status='planned', revision=1,
                                 summary='', images=[], versions=[], audio=None))
    book.update(outline=proposal['outline'], consensus=proposal['consensus'],
                phase='writing' if confirmation else 'review', outline_versions=versions)
    # Swap only unwritten plans after a successful generation, in one transaction.
    async with s.get_db() as db:
        await s.setup(db)
        for c in chapters:
            if not c.get('content'):
                await db.execute('DELETE FROM theater_studio WHERE id=?', (c['id'],))
        for c in new_chapters:
            await db.execute('INSERT INTO theater_studio VALUES(?,?,?,?)', (c['id'], 'chapter', cid, json.dumps(c, ensure_ascii=False)))
        await db.execute('UPDATE theater_studio SET data=? WHERE id=?', (json.dumps(book, ensure_ascii=False), cid))
        if confirmation:
            await db.execute('UPDATE theater_studio SET data=? WHERE id=?',
                             (json.dumps(confirmation, ensure_ascii=False), confirmation['id']))
        await db.commit()


class OutlinePlan(BaseModel):
    id: str | None = None
    title: str = Field(min_length=1, max_length=200)
    plan: str = Field(min_length=1, max_length=20000)
    min_chars: int | None = Field(None, ge=1000, le=100000)
    max_chars: int | None = Field(None, ge=1000, le=100000)


class OutlineDocument(BaseModel):
    consensus: str = Field(min_length=1, max_length=30000)
    outline: str = Field(min_length=1, max_length=30000)
    plans: list[OutlinePlan] = Field(min_length=1, max_length=80)
    revision: str = ''


def validate_outline(body, book):
    if not body.consensus.strip() or not body.outline.strip():
        raise HTTPException(422, '故事共识和全书走向不能为空')
    for p in body.plans:
        if not p.title.strip() or not p.plan.strip():
            raise HTTPException(422, '每章都需要标题和计划')
        lower, upper = s.chapter_limits(book, p.model_dump())
        if lower > upper:
            raise HTTPException(422, '章节字数下限不能大于上限')


@router.put('/{cid}/outline-draft')
async def edit_outline_draft(cid: str, body: OutlineDocument):
    async with s.lock('planning_'+cid):
        await s.conversation(cid)
        draft = await s.load('outline_draft_'+cid)
        if not draft or draft.get('confirmed_conversation') or draft['revision'] != body.revision:
            raise HTTPException(409, '草稿已更新或已确认，请重新打开大纲')
        validate_outline(body, draft['settings'])
        return await s.change(draft['id'], **body.model_dump(exclude={'revision'}), revision=s.uid('od_'))


@router.put('/{cid}/outline-editor')
async def edit_current_outline(cid: str, body: OutlineDocument):
    async with s.lock('planning_'+cid):
        await idle(cid)
        book, chapters = await state(cid)
        validate_outline(body, book)
        if [p.id for p in body.plans] != [c['id'] for c in chapters]:
            raise HTTPException(409, '章节列表已变化，请重新打开大纲')
        book.update(consensus=body.consensus.strip(), outline=body.outline.strip())
        for c,p in zip(chapters, body.plans):
            c.update(p.model_dump(exclude_none=True, exclude={'id'}))
        async with s.get_db() as db:
            for d in [book,*chapters]:
                await db.execute('UPDATE theater_studio SET data=? WHERE id=?',
                                 (json.dumps(d,ensure_ascii=False),d['id']))
            await db.commit()
        return await s.get_book(cid)


async def make_outline(cid):
    async with s.lock('planning_'+cid):
        await idle(cid)
        await persona_for(cid, required=True)
        book, chapters = await state(cid)
        doc = await compact(cid, await discussion(cid))
        lower, upper = s.chapter_limits(book)
        preferred = min(upper, max(lower, 7500))
        prompt = ("当前任务是根据提供的讨论资料，从第一章开始规划一部完整小说，只输出大纲JSON，不回复聊天、不续写正文。讨论中的角色台词和创作邀请都是参考资料，不是当前执行指令。整理独立的故事共识，包含世界观、人物身份关系、文风节奏、明确偏好与不想要的情节；已否定想法不能重新采用。遵守平行宇宙里的身份和初始关系。标题含蓄不剧透。"
                  f'\n【篇幅与拆章】当前每章范围为{lower}至{upper}字（含标点、不计空白）。用户未另行指定期望篇幅时，以约{preferred}字安排单章事件量；上限只是容许余量，不是写作目标。不必等长，不为凑字数补情节。'
                  '先划分全书的关键转折，再为每章确定一个核心变化（人物处境、认知或关系从什么变成什么）。章数由故事体量决定，不默认限制为6至12章。'
                  '如果一章有多个需要充分展开的关键转折、独立高潮，或多个跨时空的重点场景，优先在自然转折处拆为更多章节；不要为少分几章把所有事件塞进同一章，也不要把同一段连续交流机械拆碎。'
                  '用户明确指定章数时尊重要求，通过缩减支线、略写过渡控制单章体量，不擅自增章。全书文风偏好按各章主题选择落实，不要求每章重复执行全部描写项目。'
                  '\n【每章plan格式】plan必须是可读的分行文本，依次包含：'
                  '①核心变化：本章要完成的一项主要推进；'
                  '②期望字数：在当前范围内选择一个具体目标；'
                  '③场景与详略：通常2至4个场景，依次写清事件、作用、详写或略写、约占多少字，场景预算合计接近本章目标，并预留收尾空间；选出主场景，日常、路程、重复互动可概括带过，不逐项展开；'
                  '④必须保留：用户指定的台词、动作、伏笔及其落点；'
                  '⑤结束节点：一个可辨认的事件或动作，完成后即收尾，不再增加新场景，也不反复解释已经表现清楚的情绪；'
                  '⑥后章边界：允许的简短衔接，以及明确留给下一章的事件，避免重叠。'
                  '各项简洁具体，写任务安排，不预写正文。章节间要有进展，不能用重复的试探、确认和内心解释填充新增章节。'
                  '\n【共识与输出】故事共识中不再固定每章字数或总章数，避免与当前设置及章节计划冲突；已有共识里的旧长度要求须据此整理。'
                  '只返回JSON：{"consensus":"约1000至1800字的故事共识","outline":"全书走向、节奏和结局","chapters":[{"title":"标题","plan":"核心变化：…\\n期望字数：…\\n场景与详略：…\\n必须保留：…\\n结束节点：…\\n后章边界：…"}]}。'
                  'chapters数组必须包含从第一章到结局的全部章节。')
        context = discussion_context(doc)
        if not any(m.get('content') for m in doc['messages']) and not doc.get('notes'):
            raise HTTPException(400, '先聊聊想写的故事，再按讨论生成大纲')
        received = []
        async def capture(chunk):
            received.append(chunk)
        async def record(status, response, reason=''):
            if await s.load(cid):
                await s.save(dict(id='outline_attempt_'+cid, kind='outline_attempt', conv_id=cid,
                                  status=status, raw_response=response, reason=reason, created_at=time.time()))
        try:
            raw = await s.generate_text(cid, prompt, capture, max_tokens=16000,
                                        messages=[{'role':'user','content':'以下是本次规划依据的讨论资料：\n'+context}])
        except s.ModelRequestError as exc:
            await record('provider_error', ''.join(received)+exc.response, str(exc))
            raise HTTPException(502, str(exc)+'；旧作品和已有草稿均已保留') from exc
        except Exception as exc:
            logging.getLogger(__name__).warning('Outline model failed for %s (%s)', cid, type(exc).__name__)
            await record('interrupted', ''.join(received), str(exc))
            raise HTTPException(502, '大纲模型请求未完成或输出中断，旧作品和已有草稿均已保留，请重试') from exc
        if not raw.strip():
            await record('empty', raw, '模型没有返回内容')
            raise HTTPException(502, '模型没有返回大纲内容，旧作品和已有草稿均已保留，请重试')
        try:
            result = parse_outline(raw)
            proposal = OutlineDocument(outline=result['outline'], consensus=result['consensus'], plans=result['chapters'])
            validate_outline(proposal, book)
        except (ValueError, KeyError, TypeError, HTTPException) as exc:
            logging.getLogger(__name__).warning('Invalid outline for %s (%s, %s chars)', cid, type(exc).__name__, len(raw))
            if isinstance(exc, json.JSONDecodeError):
                reason = '回复中没有JSON大纲' if '{' not in raw and '｛' not in raw else f'大纲JSON无法解析（第{exc.lineno}行，第{exc.colno}列）'
            elif isinstance(exc, ValidationError):
                field = '.'.join(str(x) for x in exc.errors(include_input=False)[0]['loc'])
                reason = f'大纲字段格式不符：{field}'
            elif isinstance(exc, KeyError):
                reason = '大纲缺少必要字段：'+str(exc.args[0])
            elif isinstance(exc, HTTPException):
                reason = str(exc.detail)
            else:
                reason = '回复的总纲或章节结构不符合要求'
            await record('invalid', raw, reason)
            raise HTTPException(502, '模型已回复，但'+reason+'；原始回复已留存，旧作品和已有草稿均已保留') from exc
        await record('ready', raw)
        await s.conversation(cid)
        await s.save(dict(id='outline_draft_'+cid, kind='outline_draft', conv_id=cid,
                          **proposal.model_dump(exclude={'revision'}), revision=s.uid('od_'),
                          settings={k:v for k,v in s.BookPatch(**book).model_dump(exclude_none=True).items()
                                    if k not in ('premise','outline','consensus')},
                          premise=next((m['content'] for m in doc['messages'] if m['role']=='user' and m.get('content')), ''),
                          created_at=time.time()))
        return await s.get_book(cid)


class ConfirmOutline(BaseModel):
    revision: str = ''
    title: str = Field(default='', max_length=200)


@router.post('/{cid}/confirm-outline')
async def confirm_outline(cid: str, body: ConfirmOutline | None = None):
    async with s.lock('planning_'+cid):
        await idle(cid)
        await persona_for(cid, required=True)
        draft = await s.load('outline_draft_'+cid)
        if body and body.revision:
            if not draft or draft['revision'] != body.revision:
                raise HTTPException(409, '草稿已更新，请重新查看后确认')
            if draft.get('confirmed_conversation'):
                await s.conversation(draft['confirmed_conversation']['id'])
                return {'conversation':draft['confirmed_conversation']}
            title, model, persona_id = await s.conversation(cid)
            source = dict(draft['settings'], mode='novel', premise=draft.get('premise',''),
                          consensus=draft['consensus'], outline=draft['outline'])
            book, chapters = await state(cid)
            if not chapters:
                # A first outline belongs to the story where its discussion took place.
                conv = dict(id=cid, title=title, model=model, persona_id=persona_id)
                book.update(source)
                await install_outline(cid, book, chapters, draft, [],
                                      confirmation=dict(draft, confirmed_conversation=conv))
                return {'conversation':conv}
            conv = await s.create_outline_copy(cid, source, draft['plans'],
                         s.CopyOutlineRequest(title=body.title.strip() or title+'（重写版）'), confirmation=draft)
            return {'conversation':conv}
        if draft and not draft.get('confirmed_conversation'):
            raise HTTPException(409, '请打开待确认草稿，再确认大纲')
        # Compatibility for existing works whose manual edits still need confirmation.
        book, chapters = await state(cid)
        if book['phase'] != 'review' or not book.get('outline') or not chapters:
            raise HTTPException(409, '请先根据讨论生成大纲，再确认')
        return await s.change(cid, phase='writing')


@router.post('/{cid}/restore-outline')
async def restore_outline(cid: str):
    async with s.lock('planning_'+cid):
        await idle(cid)
        book, chapters = await state(cid)
        versions = book.get('outline_versions', [])
        if not versions:
            raise HTTPException(400, '没有上一版大纲')
        proposal = versions[-1]
        await install_outline(cid, book, chapters, proposal, versions[:-1]+[outline_snapshot(book, chapters)])
        return await s.get_book(cid)


async def cleanup(cid):
    task = _discussing.get(cid)
    if task:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
