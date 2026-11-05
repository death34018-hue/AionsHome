"""Explicit, resumable last-seven-day initialization; never scan memories or old history."""
import asyncio
import contextvars
import json
import logging
import re
import time
from datetime import datetime

import aiosqlite
import post_sentinel as profile
from profile_digest import prepare, store_result, _source

log = logging.getLogger(__name__)
_task = None
MODEL = 'Codex-5.6-Sol'
BATCH_CHARS = 96000


def parse_week_result(raw):
    """Accept a complete final object after a provider repeats its streamed draft."""
    from model_json import extract_json_object
    result = extract_json_object(raw or '')
    if isinstance(result, dict) and isinstance(result.get('profile_updates'), list):
        return result
    for match in reversed(list(re.finditer(r'\{\s*"profile_updates"\s*:', raw or ''))):
        try:
            result, end = json.JSONDecoder().raw_decode(raw[match.start():])
            if not raw[match.start()+end:].strip().strip('`') and isinstance(result.get('profile_updates'), list):
                return result
        except (ValueError, AttributeError):
            pass
    raise ValueError('本批返回格式无效，进度保留，未自动重试')


async def _update_progress(**changes):
    async with profile.get_db() as db:
        await db.execute('BEGIN IMMEDIATE')
        _, document = await profile._read(db)
        progress = document.setdefault('initialization', {})
        progress.update(changes)
        document['needs_initialization'] = progress.get('status') != 'done'
        await db.execute('UPDATE post_sentinel_document SET payload=?,revision=revision+1 WHERE id=1',
                         (json.dumps(document, ensure_ascii=False),))
        await db.commit()
    await profile._notify_changed()


async def _collect_sources(start=None, end=None):
    end = time.time() if end is None else end
    start = end-7*86400 if start is None else start
    found = []
    async with profile.get_db() as db:
        for scope, table, role in [('private', 'messages', 'role'), ('chatroom', 'chatroom_messages', 'sender')]:
            rows = await (await db.execute(
                f'SELECT id,created_at FROM {table} WHERE created_at>=? AND created_at<=? '
                f"AND {role}!='system' AND content!='' ORDER BY created_at", (start, end))).fetchall()
            found.extend((r[1], f'{scope}:{r[0]}') for r in rows)
    return [sid for _, sid in sorted(found)]


async def run():
    from ai_providers import simple_ai_call
    from chatroom import get_chatroom_names
    from model_json import extract_json_object
    await profile.ensure_schema()
    async with profile.get_db() as db:
        _, document = await profile._read(db)
    progress = document.get('initialization', {})
    if progress.get('status') == 'done':
        return
    # Only the v5 seven-day job is resumable. A v4 source list is never reused.
    if progress.get('mode') != 'recent_week':
        end = time.time()
        ids = await _collect_sources(end-7*86400, end)
        progress = {'mode': 'recent_week', 'source_ids': ids, 'cursor': 0, 'total': len(ids),
                    'window_start': end-7*86400, 'window_end': end, 'calls': 0,
                    'input_tokens': 0, 'output_tokens': 0, 'cached_tokens': 0}
        await _update_progress(**progress)
    ids, cursor = progress['source_ids'], progress['cursor']
    user_name, ai_name, actor_name = get_chatroom_names()
    await _update_progress(status='running', model=MODEL, error='')
    try:
        while cursor < len(ids) and profile.enabled():
            group, used, end = [], 0, cursor
            async with profile.get_db() as db:
                db.row_factory = aiosqlite.Row
                while end < len(ids):
                    sid = ids[end]
                    source = await _source(db, sid)
                    if source:
                        if group and used + len(source['content']) > BATCH_CHARS:
                            break
                        group.append({'_source_id': sid, 'content': source['content'],
                                      'created_at': source['created_at'], 'sender': source['speaker'],
                                      'name': {'user': user_name, 'assistant': ai_name, 'aion': ai_name,
                                               'connor': actor_name}.get(source['speaker'], source['speaker'])})
                        used += len(source['content'])
                    end += 1
            if group:
                context = await prepare(group, actor_name, user_name)
                if context is None:
                    break
                # Compact metadata; every original message body stays intact.
                transcript = '\n'.join(f'[{m["_source_id"]}][{datetime.fromtimestamp(m["created_at"]).isoformat(timespec="minutes")}][{m["name"]}] {m["content"]}' for m in group)
                prompt = (context['prompt'] + '\n本次只整理固定最近7天窗口，不输出memories，只输出profile_updates。'
                          '先通读整批，只留本人明确定下、正在推进或明确持续且仍值得跟进的事项。允许整批为空。'
                          '已经完成且没有后续的事情、稳定身份关系、旧事件回忆、工具使用规则一律不新增。'
                          '随口想法直接跳过。不要擅自修改别的系统或定时任务。\n【原文】\n' + transcript)
                usage = {}
                raw = await simple_ai_call([{'role': 'user', 'content': prompt}], MODEL,
                                           trace_label='recent_life_week', usage=usage)
                progress['calls'] += 1
                for target, source_key in [('input_tokens', 'prompt_tokens'), ('output_tokens', 'completion_tokens')]:
                    progress[target] += int(usage.get(source_key) or 0)
                progress['cached_tokens'] += int((usage.get('raw') or {}).get('cached_input_tokens') or 0)
                await _update_progress(**{k: progress[k] for k in ('calls','input_tokens','output_tokens','cached_tokens')})
                result = parse_week_result(raw)
                await store_result(context, result)
                async with profile.get_db() as db:
                    job = await (await db.execute('SELECT status,error FROM post_sentinel_digest_jobs WHERE batch_key=?',
                                                  (context['batch_key'],))).fetchone()
                if not job or job[0] != 'done':
                    raise ValueError(job[1] if job else '本批未写入，进度保留')
            cursor = end
            await _update_progress(cursor=cursor)
        await _update_progress(status='done' if cursor >= len(ids) else 'pending',
                               finished_at=time.time() if cursor >= len(ids) else None)
    except asyncio.CancelledError:
        await _update_progress(status='pending')
        raise
    except Exception as exc:
        log.exception('Recent-life week paused with saved progress')
        await _update_progress(status='error', error=str(exc))


async def start():
    global _task
    if _task and not _task.done():
        return
    _task = asyncio.create_task(run(), context=contextvars.Context())


async def stop():
    global _task
    if _task and not _task.done():
        _task.cancel()
        await asyncio.gather(_task, return_exceptions=True)
    _task = None
