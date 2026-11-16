"""Kitchen bookkeeping can share a model request with recent-life analysis."""
from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import re
import time
from datetime import datetime
from pathlib import Path

import aiosqlite

import kitchen
from model_json import extract_json_object

log = logging.getLogger(__name__)
_worker = None
_wake = None
_enqueue_tasks = set()
_processing = asyncio.Lock()
_PROMPT_PATH = Path(__file__).with_name('prompts') / 'kitchen_sentinel.md'
_ANALYSIS_VERSION = 3
_INTAKE_WORDS = re.compile(r'入库|采购|进货|补货|收货|到货|买回|生鲜|带回家')
_KITCHEN_WORDS = re.compile(r'冰箱|食材|库存|补货|做饭|吃|喝|午餐|晚餐|早餐|午饭|晚饭|黄瓜|青椒|西兰花|肥牛|鱼丸|虾|鸡蛋|玉米|牛奶|酸奶|鸡胸|蔬菜|冷冻|解冻|剩.{0,8}(?:盒|袋|根|颗|个)|(?:半|一|两|二|三|四|\d+)\s*(?:盒|袋|根|颗)')


def candidate(text):
    text = text or ''
    return bool(_KITCHEN_WORDS.search(text) or _INTAKE_WORDS.search(text) or
                (re.search(r'买|补货|收到|拿到|领回|领到|到货|送到|用[了掉完]|消耗|下锅|[蒸煮放加]了',text) and
                 re.search(r'(?:\d+(?:\.\d+)?|[半一二两三四五六七八九十]+)\s*(?:千克|公斤|毫升|kg|ml|斤|克|包|瓶|个|棵|条|只|块|份|盒|袋|根|颗|升)',text,re.I)))


async def _broadcast(event):
    from ws import manager
    await manager.broadcast(event)


def wake():
    if _wake:
        _wake.set()


async def _previous_messages(db, private, parent, source_ts, limit):
    table, field, role = ('messages', 'conv_id', 'role') if private else ('chatroom_messages', 'room_id', 'sender')
    rows = await (await db.execute(
        f'SELECT content,attachments FROM {table} WHERE {field}=? AND created_at<? '
        f"AND {role} IN ('user','assistant','aion','connor') AND created_at >= "
        '(SELECT activated_at FROM kitchen_state WHERE id=1) ORDER BY created_at DESC, rowid DESC LIMIT ?',
        (parent, source_ts, limit))).fetchall()
    messages = []
    for row in rows:
        excluded = any(isinstance(a, dict) and a.get('type') in ('ambient_voice', 'kitchen_excluded')
                       for a in json.loads(row['attachments'] or '[]'))
        messages.append('' if excluded else (row['content'] or ''))
    return messages


async def enqueue_event(event, *, expected_generation=None, wake=True):
    if expected_generation is None:
        expected_generation = kitchen.generation()
    if not kitchen.enabled() or event.get('type') not in ('msg_created','chatroom_msg_created'):
        return False
    value = event.get('data') or {}
    private = event['type'] == 'msg_created'
    if value.get('role' if private else 'sender') != 'user' or value.get('duplicate'):
        return False
    if any(isinstance(a, dict) and a.get('type') in ('ambient_voice','kitchen_excluded') for a in value.get('attachments', [])):
        return False
    text = value.get('content') or ''
    msg_id = value.get('id')
    parent = value.get('conv_id' if private else 'room_id')
    if not msg_id or not parent or not text.strip():
        return False
    if (re.search(r'吃(?:啥|什么)|(?:安排|推荐|换).{0,10}(?:菜单|食谱|午餐|晚餐)', text)
            and not _INTAKE_WORDS.search(text)
            and not re.search(r'买了|买到|收到了|到货|用了|用掉|用完|消耗|剩|吃了|吃完|做了|喝了|(?:家里|冰箱).{0,6}有', text)):
        return False  # A menu question alone needs a read, not another recording-model call.
    async with kitchen.get_db() as db:
        db.row_factory = aiosqlite.Row
        if not private:
            room = await (await db.execute('SELECT type FROM chatroom_rooms WHERE id=?', (parent,))).fetchone()
            if not room or room['type'] not in ('group','connor_1v1'):
                return False
        related = candidate(text)
        if not related:
            names = await (await db.execute('SELECT name FROM kitchen_items')).fetchall()
            related = any(r['name'] in text for r in names)
            if not related:
                recent = await _previous_messages(db, private, parent, value.get('created_at', 0), 2)
                # A reply to a named stock item can report its fate in the user's own words.
                related = bool(recent and any(r['name'] in recent[0] for r in names))
                if not related and re.search(r'半|少量|一[点些]|\d|两|没了|完了|就这样|按这个', text):
                    related = any(candidate(s) for s in recent)
        if not related:
            return False
        current = await (await db.execute('SELECT enabled,epoch,activated_at FROM kitchen_state WHERE id=1')).fetchone()
        source_ts = float(value.get('created_at') or 0)
        if (not kitchen.enabled() or not current['enabled'] or expected_generation != kitchen.generation()
                or source_ts < current['activated_at']):
            return False
        scope = 'private' if private else 'chatroom'
        await db.execute('INSERT OR IGNORE INTO kitchen_jobs (source_id,scope,parent_id,message_id,content,source_ts,epoch) VALUES (?,?,?,?,?,?,?)',
                         (f'{scope}:{msg_id}', scope, parent, msg_id, text, value.get('created_at', 0), current['epoch']))
        await db.commit()
    if wake and _wake:
        _wake.set()
    return True


def observe_message_event(event):
    if not kitchen.enabled() or event.get('type') not in ('msg_created','chatroom_msg_created'):
        return
    source_generation = kitchen.generation()
    async def enqueue():
        try:
            await enqueue_event(event, expected_generation=source_generation)
        except Exception as error:
            log.warning('Kitchen enqueue unavailable: %s', type(error).__name__)
    task = asyncio.create_task(enqueue(), context=contextvars.Context())
    _enqueue_tasks.add(task)
    task.add_done_callback(_enqueue_tasks.discard)


async def _recent(job):
    private = job['scope'] == 'private'
    table, field, role = ('messages','conv_id','role') if private else ('chatroom_messages','room_id','sender')
    async with kitchen.get_db() as db:
        db.row_factory = aiosqlite.Row
        current = await (await db.execute(f'SELECT content FROM {table} WHERE id=? AND {role}=?', (job['message_id'], 'user'))).fetchone()
        if not current or current['content'] != job['content']:
            return None
        rows = await (await db.execute(
            f'SELECT m.content FROM {table} m JOIN kitchen_events e ON e.source_id=? || m.id '
            f'WHERE m.{field}=? AND m.{role}=? AND m.created_at<? AND e.epoch=? '
            "AND json_extract(e.changes,'$.updated')=1 ORDER BY m.created_at DESC LIMIT 3",
            (job['scope'] + ':', job['parent_id'], 'user', job['source_ts'], job['epoch']))).fetchall()
        previous = await _previous_messages(db, private, job['parent_id'], job['source_ts'], 1)
    return {'booked': [r['content'][-350:] for r in rows], 'previous': previous[0] if previous else ''}


def analysis_prompt(job, recent, data, *, intake=False):
    names = [{'name': i['name'], 'unit': i['unit']} for i in data['items'][:60]]
    return (
        f'{_PROMPT_PATH.read_text(encoding="utf-8").strip()}\n'
        f'来源：{"小厨房入库口" if intake else "聊天消息"}\n'
        f'消息时间：{datetime.fromtimestamp(job["source_ts"]).isoformat(timespec="minutes")}\n'
        f'之前已经记过的本人消息（用于识别复述）：{json.dumps(recent.get("booked", []), ensure_ascii=False)}\n'
        f'食材名称（仅用于对应名称）：{json.dumps(names, ensure_ascii=False)}\n'
        f'同一聊天的上一句（用于辨认指代的食材）：{recent.get("previous", "")[:800]}\n'
        f'本人现在说：{job["content"]}'
    )


_CLAUSE_BREAK = re.compile(r'[，,。！!？?；;\n~～]')
_NOT_A_REPORT = re.compile(r'打算|计划|准备(?:要|用|拿|买|做|蒸|煮|吃|领)|要去|等会|待会|到时候|如果|假如|要是|想(?:要|用|买|做|吃)|'
                           r'还没|没(?:有)?(?:用|吃|买|做|拿|领|收)|(?:不用|不吃|不做)|你说|你建议|(?:菜单|菜谱)(?:说|写|里|上)')
_NOT_ACQUIRED = re.compile(r'如果|假如|要是|你说|你建议|未到货|'
                           r'(?:没(?:有)?|尚未|不(?:打算|准备)?|打算|计划|想|准备|等会儿?|待会儿?|明天|下次)'
                           r'(?:再|才|要|去|会)*(?:买|采购|补货|收货|收到|到货|拿|领|入库)')
_STOCK_ACTION = {
    'add': re.compile(r'买[了到回]|采购齐|补[了货]|收[到下]|[领拿带取搬][了到回]|到[了货家]|入[手库]|囤了|购入|送[到来了]|盘点|录入'),
    'set': re.compile(r'剩|还[有余]|余量|库存|盘点|录入|[冰箱家]里?.{0,8}有|改[成为]|实际|记[下作成]'),
}
_NUMBER = re.compile(r'\d+(?:\.\d+)?(?:/\d+)?|[零一二两三四五六七八九十百半]+(?:分之[一二三四五六七八九十]+)?')


def _reported_number(text):
    if '/' in text:
        top, bottom = text.split('/')
        return float(top) / int(bottom) if int(bottom) else None
    if '分之' in text:
        bottom, top = map(_reported_number, text.split('分之'))
        return top / bottom if bottom and top is not None else None
    if '十' in text and '百' not in text:
        tens, ones = text.split('十', 1)
        high = kitchen.amount_value(tens or '一')
        low = kitchen.amount_value(ones or '0')
        return high * 10 + low if high is not None and low is not None else None
    return kitchen.amount_value(text)


def _quote_clause(message, quote):
    start = message.index(quote)
    prefix = _CLAUSE_BREAK.split(message[:start])[-1]
    after = message[start + len(quote):]
    suffix = _CLAUSE_BREAK.split(after, maxsplit=1)[0]
    question = after[len(suffix):].startswith(('?', '？')) or '?' in quote or '？' in quote
    return prefix + quote + suffix, question


def _reported_stock_context(message, stock_context):
    if isinstance(stock_context, str) and stock_context.strip() and stock_context in message:
        clause, question = _quote_clause(message, stock_context)
        return not question and not _NOT_ACQUIRED.search(clause)
    # Older saved analyses omit stock_context. An affirmative intake clause can cover their list.
    clauses = re.findall(r'[^，,。！!？?；;\n~～]+[？?]?', message)
    return any(_STOCK_ACTION['add'].search(clause) and not _NOT_ACQUIRED.search(clause)
               and not clause.endswith(('?', '？')) for clause in clauses)


def reported_changes(message, changes, *, stock_context=None, intake=False, stock_names=(), previous=''):
    """Only book named ingredients supported by this author's actual report."""
    result = []
    shared_add = intake or _reported_stock_context(message, stock_context)
    for change in changes:
        if not isinstance(change, dict):
            continue
        if intake and change.get('op') != 'add':
            continue
        op = change.get('op')
        quote = change.get('evidence')
        name = change.get('name')
        ingredient = change.get('ingredient', name)
        if not (op in ('consume','empty','set','add') and isinstance(quote, str) and quote.strip() and quote in message
                and isinstance(name, str) and name.strip() and isinstance(ingredient, str) and ingredient.strip()):
            continue
        uncertain = change.get('needs_confirmation') is True
        if ingredient not in quote or change.get('reference'):
            reference = change.get('reference') or ingredient
            if (op != 'empty' or not isinstance(reference, str) or ingredient not in reference
                    or reference not in previous):
                continue
            if {n for n in stock_names if n in previous} != {name}:
                change = {**change, 'needs_confirmation': True, 'note': '请确认用完的是哪份食材。'}
                uncertain = True
        if op in ('consume','empty') and ingredient in stock_names:
            name = ingredient
            change = {**change, 'name': name}
        elif ingredient not in name and not (op in ('consume','empty') and name in stock_names):
            if not uncertain:
                continue
            change = {**change, 'name': ingredient}
            name = ingredient
        # Include the surrounding clause so cropping “准备用了…” cannot turn a plan into a fact.
        clause, question = _quote_clause(message, quote)
        if question and not uncertain:
            continue
        if change['op'] == 'add':
            if _NOT_ACQUIRED.search(clause) or not shared_add:
                continue
        elif _NOT_A_REPORT.search(clause) or (op == 'set' and not _STOCK_ACTION['set'].search(quote)):
            continue
        amount = kitchen.amount_value(change.get('amount'))
        if op == 'set' and amount is None:
            balance = change.get('balance')
            if not isinstance(balance, str) or not balance.strip() or balance not in quote:
                continue  # Mentioning leftovers alone does not report a new balance.
        unit = str(change.get('unit') or '')
        units = ('个', '颗', '枚') if name in ('鸡蛋', '鸭蛋', '鹌鹑蛋') and unit in ('个', '颗', '枚') else (unit,)
        counts = (_reported_number(m.group()) for m in _NUMBER.finditer(quote)
                  if quote[m.end():].lstrip().startswith(units))
        if amount is not None and amount not in counts:
            change = {**change, 'amount': None}  # Reported usage with an unsupported count needs confirmation.
        result.append(change)
    return result


async def _call_model(prompt):
    from config import get_sentinel_config
    from memory import _call_sentinel_text
    cfg = get_sentinel_config()
    if not cfg.get('ready'):
        raise RuntimeError('请先配置哨兵模型')
    if cfg.get('provider') == 'codex':
        from ai_providers import call_codex_sentinel
        text = await call_codex_sentinel(prompt, model=cfg['model'], timeout=60, use_chat_slot=False)
    else:
        text = await _call_sentinel_text(cfg, prompt, timeout=60)
    parsed = extract_json_object(text or '')
    if not isinstance(parsed, dict) or not isinstance(parsed.get('changes'), list):
        raise ValueError('厨房记录结果格式不完整')
    return parsed


async def record_intake(request_id, text):
    source_id = f'intake:{request_id}'
    text = text.strip()
    if not text:
        raise ValueError('请填写本次入库的食材。')
    source_ts = time.time()
    receipt = await kitchen.intake_receipt(source_id, text)
    if receipt is not None:
        return receipt
    data = await kitchen.snapshot()
    if not data['enabled']:
        raise ValueError('请先开启厨房 AI。')
    prompt = analysis_prompt({'content': text, 'source_ts': source_ts}, {}, data, intake=True)
    parsed = await _call_model(prompt)
    changes = reported_changes(text, parsed['changes'], intake=True)
    if not changes:
        # A bare list can be misread as casual chat. Recheck it once before returning an empty receipt.
        parsed = await _call_model(prompt + '\n复核小厨房入库口的本次清单：逐样核对新增食材和待确认项；只有计划时仍返回空清单。')
        changes = reported_changes(text, parsed['changes'], intake=True)
    result = await kitchen.apply_changes(source_id, changes, epoch=data['epoch'], source_ts=source_ts, intake_text=text)
    receipt = await kitchen.intake_receipt(source_id, text)
    if receipt is None:
        raise ValueError('厨房 AI 开关已变更，请重新发送。')
    if result['updated']:
        await kitchen.notify()
    return receipt


async def prepare_joint_analysis(profile_job, db_factory):
    if not kitchen.enabled():
        return None
    generation = kitchen.generation()
    async with db_factory() as db:
        db.row_factory = aiosqlite.Row
        if not await (await db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='kitchen_jobs'")).fetchone():
            return None
        row = await (await db.execute(
            "SELECT j.* FROM kitchen_jobs j JOIN kitchen_state s ON s.id=1 "
            "WHERE j.source_id=? AND j.content=? AND j.status='pending' AND j.analysis='' "
            "AND j.epoch=s.epoch AND s.enabled=1",
            (profile_job['source_id'], profile_job['content']))).fetchone()
    if not row:
        return None
    job = dict(row)
    recent = await _recent(job)
    if recent is None or await kitchen.recorded_result(job['source_id']) is not None:
        return None
    data = await kitchen.snapshot()
    current = await kitchen.state()
    if not current['enabled'] or current['epoch'] != job['epoch']:
        return None
    return {'job':job, 'recent':recent, 'generation':generation, 'prompt':analysis_prompt(job,recent,data)}


async def save_joint_analysis(prepared, payload):
    job = prepared['job']
    if not kitchen.enabled() or await _recent(job) != prepared['recent']:
        return
    async with kitchen.get_db() as db:
        await db.execute(
            "UPDATE kitchen_jobs SET analysis=? WHERE source_id=? AND epoch=? AND status='pending' "
            "AND epoch=(SELECT epoch FROM kitchen_state WHERE id=1 AND enabled=1)",
            (json.dumps({**payload, 'version': _ANALYSIS_VERSION},ensure_ascii=False) if isinstance(payload, dict) else '',
             job['source_id'],job['epoch']))
        await db.commit()
    wake()


async def _waiting_for_profile(db, job):
    import post_sentinel
    if not post_sentinel.enabled():
        return False
    if not await (await db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='post_sentinel_jobs'")).fetchone():
        return False
    row = await (await db.execute(
        "SELECT j.status,j.error FROM post_sentinel_jobs j JOIN post_sentinel_document d ON d.id=1 "
        "WHERE j.source_id=? AND j.content=? AND j.epoch=d.epoch",
        (job['source_id'],job['content']))).fetchone()
    return bool(row and (row['status']=='running' or (row['status']=='pending' and not row['error'])))


async def _mark_message(job):
    private = job['scope'] == 'private'
    table = 'messages' if private else 'chatroom_messages'
    async with kitchen.get_db() as db:
        db.row_factory = aiosqlite.Row
        row = await (await db.execute(f'SELECT * FROM {table} WHERE id=?', (job['message_id'],))).fetchone()
        if not row:
            return
        message = dict(row)
        attachments = json.loads(message.get('attachments') or '[]')
        if not any(isinstance(a, dict) and a.get('type') == 'kitchen_updated' for a in attachments):
            attachments.append({'type': 'kitchen_updated'})
            await db.execute(f'UPDATE {table} SET attachments=? WHERE id=?', (json.dumps(attachments, ensure_ascii=False), job['message_id']))
            await db.commit()
        message['attachments'] = attachments
    await _broadcast({'type': 'msg_updated' if private else 'chatroom_msg_updated', 'data': message})


async def run_pending_once():
    if not kitchen.enabled():
        return False
    async with _processing:
        from sentinel_dispatch import queue_lock
        async with queue_lock, kitchen.get_db() as db:
            db.row_factory = aiosqlite.Row
            row = await (await db.execute("SELECT * FROM kitchen_jobs WHERE status='pending' ORDER BY source_ts LIMIT 1")).fetchone()
            if not row:
                return False
            job = dict(row)
            if not job['analysis'] and await _waiting_for_profile(db,job):
                return False
            await db.execute("UPDATE kitchen_jobs SET status='running' WHERE source_id=?", (job['source_id'],))
            await db.commit()
        status, error = 'done', ''
        try:
            current = await kitchen.state()
            recent = await _recent(job)
            if not current['enabled'] or current['epoch'] != job['epoch'] or recent is None:
                status = 'skipped'
            else:
                result = await kitchen.recorded_result(job['source_id'])
                if result is None:
                    data = await kitchen.snapshot()
                    dispatch = await kitchen.state()
                    if not dispatch['enabled'] or dispatch['epoch'] != job['epoch']:
                        status = 'skipped'
                    else:
                        parsed = json.loads(job['analysis']) if job['analysis'] else None
                        if not isinstance(parsed,dict) or parsed.get('version') != _ANALYSIS_VERSION or not isinstance(parsed.get('changes'),list):
                            parsed = await _call_model(analysis_prompt(job, recent, data))
                            parsed['version'] = _ANALYSIS_VERSION
                        if await _recent(job) != recent:
                            status = 'skipped'
                        else:
                            # Reuse a completed analysis after interruption, even before ledger commit.
                            async with kitchen.get_db() as db:
                                await db.execute("UPDATE kitchen_jobs SET analysis=? WHERE source_id=? AND epoch=?",
                                                 (json.dumps(parsed,ensure_ascii=False),job['source_id'],job['epoch']))
                                await db.commit()
                            changes = reported_changes(job['content'], parsed['changes'], stock_context=parsed.get('stock_context'),
                                                       stock_names={i['name'] for i in data['items']}, previous=recent['previous'])
                            result = await kitchen.apply_changes(job['source_id'], changes, epoch=job['epoch'], source_ts=job['source_ts'])
                if result is not None:
                    if result['updated']:
                        await _mark_message(job)
                        await kitchen.notify()
                    if (await kitchen.state())['epoch'] != job['epoch']:
                        status = 'skipped'
        except asyncio.CancelledError:
            async with kitchen.get_db() as db:
                await db.execute("UPDATE kitchen_jobs SET status='pending' WHERE source_id=? AND status='running'", (job['source_id'],))
                await db.commit()
            raise
        except Exception as exc:
            status = 'failed'
            error = '厨房记录暂未成功，请在小厨房核对或手动修改。'
            log.warning('Kitchen analysis failed: %s', type(exc).__name__)
        async with kitchen.get_db() as db:
            await db.execute('UPDATE kitchen_jobs SET status=?,error=? WHERE source_id=? AND epoch=(SELECT epoch FROM kitchen_state WHERE id=1)', (status, error, job['source_id']))
            await db.commit()
        return True


async def flush_pending():
    from sentinel_dispatch import flush_enqueued
    await flush_enqueued()
    if _enqueue_tasks:
        await asyncio.gather(*list(_enqueue_tasks), return_exceptions=True)
    async with asyncio.timeout(70):
        while kitchen.enabled():
            if await run_pending_once():
                continue
            async with kitchen.get_db() as db:
                pending = await (await db.execute("SELECT 1 FROM kitchen_jobs WHERE status='pending' LIMIT 1")).fetchone()
            if not pending:
                break
            # A fridge lookup must wait for the shared analysis before reading inventory.
            import post_sentinel
            if not post_sentinel.enabled() or not await post_sentinel.run_pending_once():
                if not await run_pending_once():
                    break


async def _loop():
    while True:
        try:
            _wake.clear()
            if await run_pending_once():
                continue
            await asyncio.wait_for(_wake.wait(), timeout=120)
        except asyncio.TimeoutError:
            async with kitchen.get_db() as db:
                await kitchen.cleanup_menus(db)
                await db.commit()
        except asyncio.CancelledError:
            return
        except Exception as error:
            log.warning('Kitchen worker unavailable: %s', type(error).__name__)
            await asyncio.sleep(2)


async def start():
    global _worker, _wake
    await kitchen.ensure_schema()
    if _worker is None or _worker.done():
        _wake = asyncio.Event()
        async with kitchen.get_db() as db:
            await db.execute("UPDATE kitchen_jobs SET status='pending' WHERE status='running'")
            await db.commit()
        _worker = asyncio.create_task(_loop(), context=contextvars.Context())


async def stop():
    global _worker
    if _worker:
        _worker.cancel()
        await asyncio.gather(_worker, return_exceptions=True)
        _worker = None
    tasks = list(_enqueue_tasks)
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
