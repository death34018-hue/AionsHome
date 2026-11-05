"""Kitchen bookkeeping can share a model request with recent-life analysis."""
from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import re
from datetime import datetime

import aiosqlite

import kitchen
from model_json import extract_json_object

log = logging.getLogger(__name__)
_worker = None
_wake = None
_enqueue_tasks = set()
_processing = asyncio.Lock()
_KITCHEN_WORDS = re.compile(r'冰箱|食材|库存|补货|做饭|吃|喝|午餐|晚餐|早餐|午饭|晚饭|黄瓜|青椒|西兰花|肥牛|鱼丸|虾|鸡蛋|玉米|牛奶|酸奶|鸡胸|蔬菜|冷冻|解冻|剩.{0,8}(?:盒|袋|根|颗|个)|(?:半|一|两|二|三|四|\d+)\s*(?:盒|袋|根|颗)')


def candidate(text):
    text = text or ''
    return bool(_KITCHEN_WORDS.search(text) or
                (re.search(r'买|补货|收到|拿到|领回|领到|到货|送到|用[了掉完]|消耗|下锅|[蒸煮放加]了',text) and
                 re.search(r'(?:\d+(?:\.\d+)?|[半一二两三四五六七八九十]+)\s*(?:千克|公斤|毫升|kg|ml|斤|克|包|瓶|个|棵|条|只|块|份|盒|袋|根|颗|升)',text,re.I)))


async def _broadcast(event):
    from ws import manager
    await manager.broadcast(event)


def wake():
    if _wake:
        _wake.set()


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
            and not re.search(r'买了|买到|收到了|到货|用了|用掉|剩|吃了|吃完|做了|喝了|(?:家里|冰箱).{0,6}有', text)):
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
            related = bool(re.search(r'买|用|剩|没了|完了|到货|拿到', text) and any(r['name'] in text for r in names))
        if not related and re.search(r'半|少量|一[点些]|\d|两|没了|完了|就这样|按这个', text):
            table, field = ('messages','conv_id') if private else ('chatroom_messages','room_id')
            recent = await (await db.execute(f'SELECT content FROM {table} WHERE {field}=? AND id!=? ORDER BY created_at DESC LIMIT 2', (parent, msg_id))).fetchall()
            related = any(candidate(r['content']) for r in recent)
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
    return [r['content'][-350:] for r in rows]


def analysis_prompt(job, recent, data):
    names = [{'name': i['name'], 'unit': i['unit']} for i in data['items'][:60]]
    return (
        '厨房部分只负责食材记账。只提取当前这条用户消息明确报告的、已经发生的购买/收到食材、消耗或余量校正。'
        '唯一入账依据是当前本人消息。上下文、他人台词、菜单、计划、假设、示例和引用不能提供用料或数量。'
        '“就吃这些，等会一点我做”“按菜单做好了”“开饭啦”均返回空数组。领取和购买意向也等本人明确拿到再加。'
        '明确开做并说出正在用的食材时，逐样记消耗；“打算今晚做”仍是计划。“青椒今天不吃”要跳过青椒。'
        '逐句判断当前消息是否明确报告了额外用料，即使和晒菜写在一起，也只记新增量。'
        '每项用evidence逐字摘录本条中说明实际动作及食材的完整原话，保留限定语。ingredient填本人原话里的食材称呼，name匹配库存名。'
        '例：“给你们看看成品，刚才又多拍了一根黄瓜”只记新增黄瓜，evidence为“刚才又多拍了一根黄瓜”。'
        '整条只是拍图分享、评价味道或复述同一顿饭时，返回{"changes":[]}。菜名、图片和菜单本身都不能证明又用了一份。'
        '逐样判断，用库存原名。吃掉或用掉具体食材用consume，数量不明仍用consume、amount=null；买入数量不明同理用add。'
        '例如早饭吃掉两颗鸡蛋和一小截玉米：鸡蛋consume 2颗，玉米单独判断；不要因一项含糊影响另一项。不换算袋/颗/克。'
        '数量只取本人报出的本次实际量，没报数量用null。明确说某食材全部用光才用empty。'
        '只输出JSON：{"changes":[{"op":"consume","name":"冷冻黑虎虾","ingredient":"黑虎虾","evidence":"刚用了六只黑虎虾","amount":6,"unit":"只"}]}。支持add/set/consume/empty。'
        '无明确变化返回{"changes":[]}。可选category=vegetable/protein/staple/dairy/fruit/other，storage=fridge/freezer/pantry。\n'
        f'消息时间：{datetime.fromtimestamp(job["source_ts"]).isoformat(timespec="minutes")}\n'
        f'当前消息：{job["content"][:2000]}\n'
        f'之前已经记过的本人消息（仅用于排除同一顿的复述）：{json.dumps(recent, ensure_ascii=False)}\n'
        f'食材名称（仅用于对应名称）：{json.dumps(names, ensure_ascii=False)}'
    )


_CLAUSE_BREAK = re.compile(r'[，,。！!？?；;\n~～]')
_NOT_A_REPORT = re.compile(r'打算|计划|准备(?:要|用|拿|买|做|蒸|煮|吃|领)|要去|等会|待会|到时候|如果|假如|要是|想(?:要|用|买|做|吃)|'
                           r'还没|没(?:有)?(?:用|吃|买|做|拿|领|收)|(?:不用|不吃|不做)|你说|你建议|(?:菜单|菜谱)(?:说|写|里|上)')
_STOCK_ACTION = {
    'consume': re.compile(r'用[了掉光完]|消耗|[吃喝](?:的是|[了掉光完])|[煮蒸炒烤煎切放加打拍投剥下]了|'
                          r'[已刚又正].{0,8}[用煮蒸炒烤煎加放]|下[锅肚]|[蒸煮]上|[放倒加]进|做成'),
    'empty': re.compile(r'[用吃喝耗]光|[用吃喝]完|没了|空了|清空|见底'),
    'add': re.compile(r'买[了到回]|补[了货]|收[到下]|[领拿带取搬][了到回]|到[了货家]|入[手库]|囤了|购入|送[到来]|盘点|录入'),
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


def reported_changes(message, changes):
    """Only book named ingredients supported by this author's actual report."""
    result = []
    for change in changes:
        if not isinstance(change, dict):
            continue
        action = _STOCK_ACTION.get(change.get('op')) if isinstance(change.get('op'), str) else None
        quote = change.get('evidence')
        name = change.get('name')
        ingredient = change.get('ingredient', name)
        if not (action and isinstance(quote, str) and quote.strip() and quote in message
                and isinstance(name, str) and isinstance(ingredient, str) and ingredient.strip()
                and ingredient in quote and ingredient in name):
            continue
        # Include the surrounding clause so cropping “准备用了…” cannot turn a plan into a fact.
        start = message.index(quote)
        prefix = _CLAUSE_BREAK.split(message[:start])[-1]
        after = message[start + len(quote):]
        suffix = _CLAUSE_BREAK.split(after, maxsplit=1)[0]
        clause = prefix + quote + suffix
        if (_NOT_A_REPORT.search(clause) or not action.search(quote)
                or after[len(suffix):].startswith(('?', '？')) or '?' in quote or '？' in quote):
            continue
        amount = kitchen.amount_value(change.get('amount'))
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
            (json.dumps({**payload, 'version': 2},ensure_ascii=False) if isinstance(payload, dict) else '',
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
                        if not isinstance(parsed,dict) or parsed.get('version') != 2 or not isinstance(parsed.get('changes'),list):
                            parsed = await _call_model(analysis_prompt(job, recent, data))
                            parsed['version'] = 2  # Reanalyze pending pre-evidence results once.
                        if await _recent(job) != recent:
                            status = 'skipped'
                        else:
                            # Reuse a completed analysis after interruption, even before ledger commit.
                            async with kitchen.get_db() as db:
                                await db.execute("UPDATE kitchen_jobs SET analysis=? WHERE source_id=? AND epoch=?",
                                                 (json.dumps(parsed,ensure_ascii=False),job['source_id'],job['epoch']))
                                await db.commit()
                            changes = reported_changes(job['content'], parsed['changes'])
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
