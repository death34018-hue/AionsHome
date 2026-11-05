"""Independent, non-blocking maintenance of the shared user profile and activity."""
from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import math
import re
import time
import uuid
from datetime import datetime

import aiosqlite

from config import get_sentinel_config, load_worldbook
from database import get_db
from user_profile_policy import (CATEGORIES, SUBCATEGORIES, KINDS, STATUSES, PHASES, PROMPT_LIMIT,
                                 MAX_ENTRIES, MAX_SAVED_CHARS, estimate_tokens, subcategory, relevant_fact,
                                 category, timestamp, normalize, lifecycle, maintain,
                                 render_context, unfinished_task, personalize_text, remove_manual_protection)
from user_profile_policy import STATE_MAX_CHARS

log = logging.getLogger(__name__)
KEY_RE = re.compile(r'^[a-zA-Z0-9_.-]{1,100}$')
BUDGET_ERROR = '近期清单超过约2000 tokens或24项，请先合并同一事项、精简正文；未完成待办不能省略'
_worker: asyncio.Task | None = None
_enqueue_tasks: set[asyncio.Task] = set()
_processing = asyncio.Lock()
_wake: asyncio.Event | None = None


class _Analysis(dict):
    """Carry bounded diagnostics without changing the JSON contract."""
    raw = ''


def enabled() -> bool:
    # Lazy import avoids coupling the existing capability registry to startup.
    from capabilities import is_capability_enabled
    return is_capability_enabled('post_sentinel')


def _profile_user_name():
    return (load_worldbook().get('user_name') or '').strip() or '你'


async def ensure_schema():
    async with get_db() as db:
        await db.execute('BEGIN IMMEDIATE')
        await db.execute('CREATE TABLE IF NOT EXISTS post_sentinel_document ('
                         'id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL, '
                         'revision INTEGER NOT NULL DEFAULT 0, epoch INTEGER NOT NULL DEFAULT 0, '
                         'updated_at REAL NOT NULL DEFAULT 0, last_checked_at REAL NOT NULL DEFAULT 0, '
                         "last_error TEXT NOT NULL DEFAULT '')")
        await db.execute('INSERT OR IGNORE INTO post_sentinel_document (id,payload) VALUES (1,?)',
                         (json.dumps({'version': 5, 'entries': {}, 'current_state': None, 'needs_initialization': False}),))
        await db.execute('CREATE TABLE IF NOT EXISTS post_sentinel_jobs ('
                         'source_id TEXT PRIMARY KEY, scope TEXT NOT NULL, parent_id TEXT NOT NULL, '
                         'message_id TEXT NOT NULL, source_ts REAL NOT NULL, content TEXT NOT NULL, '
                         "status TEXT NOT NULL DEFAULT 'pending', epoch INTEGER NOT NULL, "
                         "error TEXT NOT NULL DEFAULT '')")
        await db.execute('CREATE INDEX IF NOT EXISTS idx_post_sentinel_pending '
                         'ON post_sentinel_jobs(status,source_ts)')
        columns = {r[1] for r in await (await db.execute('PRAGMA table_info(post_sentinel_jobs)')).fetchall()}
        for name, definition in [('attempts', 'INTEGER NOT NULL DEFAULT 0'),
                                 ('next_attempt_at', 'REAL NOT NULL DEFAULT 0'),
                                 ('model_output', "TEXT NOT NULL DEFAULT ''")]:
            if name not in columns:
                await db.execute(f'ALTER TABLE post_sentinel_jobs ADD COLUMN {name} {definition}')
        await db.execute('CREATE TABLE IF NOT EXISTS post_sentinel_backups ('
                         'reason TEXT PRIMARY KEY, payload TEXT NOT NULL, created_at REAL NOT NULL)')
        await db.execute('CREATE TABLE IF NOT EXISTS post_sentinel_digest_jobs ('
                         'batch_key TEXT PRIMARY KEY, payload TEXT NOT NULL, '
                         "status TEXT NOT NULL DEFAULT 'pending', error TEXT NOT NULL DEFAULT '', "
                         'created_at REAL NOT NULL)')
        row, document = await _read(db)
        migrated = document.get('version') != 5
        if migrated:
            if document.get('entries'):
                await db.execute('INSERT OR IGNORE INTO post_sentinel_backups VALUES (?,?,?)',
                                 (f'profile-v{document.get("version", 1)}-before-v5', row['payload'], time.time()))
            document = {'version': 5, 'entries': {}, 'current_state': None, 'needs_initialization': False}
            await db.execute('UPDATE post_sentinel_document SET epoch=epoch+1 WHERE id=1')
            await db.execute("UPDATE post_sentinel_jobs SET status='skipped' WHERE status IN ('pending','running','failed')")
            await db.execute("UPDATE post_sentinel_digest_jobs SET status='skipped',payload='{}',error='' WHERE status='pending'")
        unprotected = remove_manual_protection(document)
        if unprotected and not migrated:
            await db.execute('INSERT OR IGNORE INTO post_sentinel_backups VALUES (?,?,?)',
                             ('profile-manual-protection', row['payload'], time.time()))
        if migrated or unprotected:
            await db.execute('UPDATE post_sentinel_document SET payload=?,revision=revision+1 WHERE id=1',
                             (json.dumps(document, ensure_ascii=False),))
        await db.commit()


async def _read(db):
    db.row_factory = aiosqlite.Row
    row = await (await db.execute('SELECT * FROM post_sentinel_document WHERE id=1')).fetchone()
    return dict(row), json.loads(row['payload'])


async def snapshot(*, query=''):
    async with get_db() as db:
        row, document = await _read(db)
        pending = (await (await db.execute(
            "SELECT count(*) FROM post_sentinel_jobs WHERE status IN ('pending','running')"
        )).fetchone())[0]
        errors = await (await db.execute(
            "SELECT error FROM post_sentinel_jobs WHERE error!='' AND status IN ('pending','failed') "
            'ORDER BY source_ts DESC LIMIT 1'
        )).fetchone()
        failed = (await (await db.execute("SELECT count(*) FROM post_sentinel_jobs WHERE status='failed'")).fetchone())[0]
        digest_pending = (await (await db.execute("SELECT count(*) FROM post_sentinel_digest_jobs WHERE status='pending'")).fetchone())[0]
        digest_error = await (await db.execute("SELECT error FROM post_sentinel_digest_jobs WHERE error!='' AND status='pending' ORDER BY created_at DESC LIMIT 1")).fetchone()
    now = time.time()
    context, included = render_context(document, _profile_user_name(), now, query=query)
    within_budget = len(included) <= MAX_ENTRIES and estimate_tokens(context) <= PROMPT_LIMIT

    def visible_error(error):
        # Keep retry diagnostics intact; only hide an obsolete instruction to delete content.
        if within_budget:
            error = re.sub(r'(?:画像：|状态：|ValueError：)?' + re.escape(BUDGET_ERROR), '', error)
        return '；'.join(part for part in error.split('；') if part)

    last_error = '；'.join(filter(None, (
        visible_error(errors[0] if errors else row['last_error']),
        visible_error(digest_error[0] if digest_error else ''),
    )))
    if not enabled():
        context, included = '', set()
    entries = [{**normalize(v), 'view_status': lifecycle(v, now), 'included': v['key'] in included,
                'omission_reason': '' if v['key'] in included else '总开关已关闭' if not enabled()
                                   else '已结束或已淡出' if lifecycle(v, now) in ('done','cancelled','retired')
                                   else '未列入近期跟进'}
               for v in document['entries'].values() if not v.get('deleted')]
    return {
        'version': 5, 'revision': row['revision'], 'entries': entries,
        'current_state': document.get('current_state'), 'enabled': enabled(),
        'updated_at': row['updated_at'], 'last_checked_at': row['last_checked_at'],
        'last_error': last_error,
        'pending': pending, 'failed': failed, 'digest_pending': digest_pending,
        'last_digest_at': document.get('last_digest_at'), 'needs_initialization': document.get('needs_initialization', False),
        'initialization': {k: v for k, v in document.get('initialization', {}).items() if k != 'source_ids'},
        'model': get_sentinel_config().get('model', ''), 'categories': CATEGORIES, 'phases': PHASES,
        'subcategories': SUBCATEGORIES, 'kinds': KINDS, 'statuses': STATUSES, 'prompt_limit': PROMPT_LIMIT,
        'prompt_tokens': estimate_tokens(context), 'max_entries': MAX_ENTRIES,
        'prompt_chars': len(context), 'prompt_preview': context,
        'saved_chars': sum(len(v['content']) for v in entries),
        'omitted': sum(lifecycle(v, now) in ('active', 'unknown', 'stale') and not v['included'] for v in entries) if enabled() else 0,
    }


def _entry(value):
    if not isinstance(value, dict):
        raise ValueError('画像条目格式无效')
    key, cat = value.get('key'), category(value.get('category'))
    content = value.get('content')
    if not isinstance(key, str) or not KEY_RE.fullmatch(key):
        raise ValueError('画像条目的标识或分类无效')
    if not isinstance(content, str) or not content.strip() or len(content.strip()) > 160:
        raise ValueError('每条近期事项请填写 1–160 字')
    return key, cat, content.strip()


def _check_budget(document):
    context, entries = render_context(document, _profile_user_name())
    if len(entries) > MAX_ENTRIES or estimate_tokens(context) > PROMPT_LIMIT:
        raise ValueError(BUDGET_ERROR)


async def save_manual(payload: dict, revision: int):
    values = payload.get('entries')
    if not isinstance(values, list):
        raise ValueError('画像条目必须是列表')
    now = time.time()
    async with get_db() as db:
        await db.execute('BEGIN IMMEDIATE')
        row, document = await _read(db)
        if revision != row['revision']:
            raise ValueError('画像已更新，请刷新后再保存')
        previous = document['entries']
        next_entries, seen = {}, set()
        for value in values:
            key, category, content = _entry(value)
            if key in seen:
                raise ValueError('画像条目标识不能重复')
            seen.add(key)
            old = previous.get(key, {})
            extra = {name: value.get(name, old.get(name, default)) for name, default in (
                ('topic', ''), ('kind', 'context'), ('status', 'active'), ('sustained', False),
                ('importance', 0.6), ('certainty', 'fact'), ('keywords', []), ('next_step', ''))}
            if not isinstance(extra['next_step'], str) or len(extra['next_step']) > 80:
                raise ValueError('下一步最多80字')
            extra['subcategory'] = subcategory({**old, **value, 'category': category})
            if extra['kind'] not in KINDS or extra['status'] not in STATUSES or len(extra['topic']) > 40:
                raise ValueError('画像的主题、类型或状态无效')
            if not isinstance(extra['importance'], (int, float)) or not math.isfinite(extra['importance']) or not 0 <= extra['importance'] <= 1:
                raise ValueError('重要程度需要在0到1之间')
            if extra['certainty'] not in ('fact', 'inferred', 'conflict'):
                raise ValueError('事实确认状态无效')
            if not isinstance(extra['keywords'], list) or len(extra['keywords']) > 8 or any(not isinstance(k, str) or len(k) > 40 for k in extra['keywords']):
                raise ValueError('检索词格式无效')
            for field in ('available_at', 'due_at', 'expires_at', 'event_at'):
                extra[field] = timestamp(value.get(field, old.get(field)))
            if (extra['kind'] in ('idea', 'context') and content != old.get('content')
                    and extra['expires_at'] and extra['expires_at'] <= now and 'expires_at' not in value):
                extra['expires_at'] = None
            if category != 'recent' and old.get('category') == 'recent':
                extra.update(kind='context', status='active', sustained=False,
                             available_at=None, due_at=None, expires_at=None)
            changed = (content != old.get('content') or category != old.get('category') or old.get('deleted')
                       or any(extra[k] != old.get(k, v) for k, v in extra.items()))
            next_entries[key] = {**old, 'key': key, 'category': category, 'content': content,
                                 **extra,
                                 'locked': False, 'deleted': False,
                                 'updated_at': now if changed else old.get('updated_at', now),
                                 'sources': ['manual'] if changed else old.get('sources', ['manual']),
                                 'source_ts': now if changed else old.get('source_ts', now)}
            if category != 'recent' and old.get('category') == 'recent':
                next_entries[key].pop('retired_at', None)
        for key, old in previous.items():
            if key not in seen:
                next_entries[key] = {**old, 'deleted': True, 'locked': True, 'updated_at': now}
        document['entries'] = next_entries
        state = payload.get('current_state')
        if state is None:
            document['state_cleared_at'] = now
        if state is not None:
            if not isinstance(state, dict) or state.get('phase') not in PHASES:
                raise ValueError('当前状态格式无效')
            text = str(state.get('text') or '').strip()
            expires_at = float(state.get('expires_at') or now + 3600)
            if not text or len(text) > STATE_MAX_CHARS or not math.isfinite(expires_at):
                raise ValueError(f'当前状态请填写 1–{STATE_MAX_CHARS} 字和有效时间')
            old = document.get('current_state') or {}
            changed = any(state.get(k) != old.get(k) for k in ('text', 'phase', 'expires_at'))
            state = {**old, 'text': text, 'phase': state['phase'], 'expires_at': expires_at,
                     'locked': False,
                     'event_at': now if changed else old.get('event_at', now),
                     'updated_at': now if changed else old.get('updated_at', now),
                     'sources': ['manual'] if changed else old.get('sources', ['manual'])}
        document['current_state'] = state
        _check_budget(document)
        await db.execute('UPDATE post_sentinel_document SET payload=?,revision=revision+1,updated_at=? WHERE id=1',
                         (json.dumps(document, ensure_ascii=False), now))
        await db.commit()
    await _notify_changed()
    return await snapshot()


async def invalidate_pending():
    # Epoch fencing also protects a disabled -> enabled interval during a call.
    async with get_db() as db:
        await db.execute('UPDATE post_sentinel_document SET epoch=epoch+1 WHERE id=1')
        await db.execute("UPDATE post_sentinel_jobs SET status='skipped' WHERE status IN ('pending','running')")
        await db.commit()
    from profile_initialize import stop as stop_initialization
    await stop_initialization()


def wake():
    if _wake:
        _wake.set()


async def enqueue_event(event: dict, *, wake=True):
    if not enabled() or event.get('type') not in ('msg_created', 'chatroom_msg_created'):
        return
    value = event.get('data') or {}
    private = event['type'] == 'msg_created'
    if value.get('role' if private else 'sender') != 'user' or value.get('duplicate'):
        return
    if any(a.get('type') == 'ambient_voice' for a in value.get('attachments', []) if isinstance(a, dict)):
        return
    msg_id, parent = value.get('id'), value.get('conv_id' if private else 'room_id')
    text, ts = value.get('content'), value.get('created_at')
    if not msg_id or not parent or not isinstance(text, str) or not text.strip():
        return
    if not isinstance(ts, (int, float)) or not math.isfinite(ts):
        return
    scope = 'private' if private else 'chatroom'
    async with get_db() as db:
        row, _ = await _read(db)
        await db.execute('INSERT OR IGNORE INTO post_sentinel_jobs '
                         '(source_id,scope,parent_id,message_id,source_ts,content,epoch) VALUES (?,?,?,?,?,?,?)',
                         (f'{scope}:{msg_id}', scope, parent, msg_id, ts, text, row['epoch']))
        await db.commit()
    if wake and _wake:
        _wake.set()


def observe_message_event(event: dict):
    """A synchronous side hook: no waiting, and no inherited chat cancellation context."""
    if _worker is None or event.get('type') not in ('msg_created', 'chatroom_msg_created'):
        return
    task = asyncio.create_task(_safe_enqueue(event), context=contextvars.Context())
    _enqueue_tasks.add(task)
    task.add_done_callback(_enqueue_tasks.discard)


async def _safe_enqueue(event):
    try:
        await enqueue_event(event)
    except Exception:
        log.warning('Post-sentinel message hook unavailable', exc_info=True)


async def _context_for_job(job):
    private = job['scope'] == 'private'
    table, parent, role = ('messages', 'conv_id', 'role') if private else ('chatroom_messages', 'room_id', 'sender')
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        current = await (await db.execute(f'SELECT content FROM {table} WHERE id=? AND {role}=?',
                                          (job['message_id'], 'user'))).fetchone()
        if not current or current['content'] != job['content']:
            return None, None
        rows = await (await db.execute(
            f'SELECT id,{role} AS speaker,content,created_at FROM {table} '
            f'WHERE {parent}=? AND created_at<=? AND {role}!=? ORDER BY created_at DESC LIMIT 12',
            (job['parent_id'], job['source_ts'], 'system'),
        )).fetchall()
    sources, messages = {}, []
    job['_source_times'] = {}
    for r in reversed(rows):
        label = f'U{len(sources)+1}' if r['speaker'] == 'user' else ''
        if label:
            sources[label] = f"{job['scope']}:{r['id']}"
            job['_source_times'][label] = r['created_at']
        messages.append({'speaker': r['speaker'], 'source': label,
                         'time': datetime.fromtimestamp(r['created_at']).astimezone().isoformat(timespec='seconds'),
                         'text': r['content']})
    return messages, sources


def _analysis_prompt(document, messages, sources, current_id):
    from profile_prompts import sentinel_prompt
    return sentinel_prompt(document, messages, sources, current_id, _profile_user_name())

async def _call_model(prompt):
    from memory import _call_sentinel_text
    from model_json import extract_json_object
    cfg = get_sentinel_config()
    if not cfg.get('ready'):
        raise RuntimeError('哨兵模型未配置')
    if cfg.get('provider') == 'codex':
        from ai_providers import call_codex_sentinel
        raw = await call_codex_sentinel(prompt, model=cfg['model'], timeout=60, use_chat_slot=False)
    else:
        raw = await _call_sentinel_text(cfg, prompt, timeout=60)
    parsed = extract_json_object(raw or '')
    if not isinstance(parsed, dict):
        error = ValueError('模型返回无法解析的 JSON 对象')
        error.model_output = (raw or '')[:12000]
        raise error
    result = _Analysis(parsed)
    result.raw = (raw or '')[:12000]
    return result


def _profile_refs(document):
    return {f'P{n}': e for n, e in enumerate(document['entries'].values(), 1)}


def _normalize_analysis(parsed, sources):
    """Accept equivalent model formats without inventing facts or evidence."""
    if not isinstance(parsed, dict) or not any(
            k in parsed for k in ('updates', 'profile_updates', 'state', 'current_state')):
        raise ValueError('后置哨兵返回格式无效，需要updates或state')
    updates = parsed.get('updates', parsed.get('profile_updates'))
    if updates is None:
        updates = []
    if isinstance(updates, dict):
        updates = [updates]
    if not isinstance(updates, list):
        raise ValueError('画像updates需要是列表或单条对象')
    source_labels = {source_id: label for label, source_id in sources.items()}

    def item(value, *, state=False):
        if not isinstance(value, dict):
            return value
        value = {k: v for k, v in value.items() if v is not None}
        field, alias = ('text', 'content') if state else ('content', 'text')
        if not value.get(field) and alias in value:
            value[field] = value[alias]
        refs = value.get('sources')
        if isinstance(refs, str):
            refs = [refs.strip()]
        if isinstance(refs, list):
            value['sources'] = [source_labels.get(r.strip(), r.strip()) if isinstance(r, str) else r for r in refs]
        enums = {'phase': PHASES} if state else {'category': CATEGORIES, 'kind': KINDS, 'status': STATUSES}
        for name, choices in enums.items():
            if isinstance(value.get(name), str):
                raw = value[name].strip().casefold()
                value[name] = {label: key for key, label in choices.items()}.get(raw, raw)
        if state:
            value.setdefault('phase', 'unknown')
            if isinstance(value['phase'], str):
                aliases = {'in_progress': 'ongoing', 'completed': 'finished', 'done': 'finished'}
                value['phase'] = aliases.get(value['phase'], value['phase'])
            try:
                ttl = float(value.get('ttl_minutes', 60))
                if isinstance(value.get('ttl_minutes'), bool) or not math.isfinite(ttl) or ttl <= 0:
                    ttl = 60
            except (ValueError, TypeError):
                ttl = 60
            value['ttl_minutes'] = min(ttl, 600)
        return value

    return {'updates': [item(v) for v in updates],
            'state': item(parsed.get('state', parsed.get('current_state')), state=True)}


def _apply_analysis(document, parsed, sources, job):
    parsed = _normalize_analysis(parsed, sources)
    refs = job.get('_refs') or _profile_refs(document)
    user_name = _profile_user_name()
    limit = 24 if job.get('digest') else 10
    errors = [f'本轮超过{limit}项，其余请在重试时继续'] if len(parsed['updates']) > limit else []

    def lineage(item, old=None):
        refs = item.get('sources')
        if not refs and old and job.get('consolidation'):
            return old.get('sources', []), old.get('source_ts', 0)
        if not isinstance(refs, list) or not refs or any(not isinstance(r, str) or r not in sources for r in refs):
            raise ValueError('画像更新缺少有效用户来源')
        ids = list(dict.fromkeys(sources[r] for r in refs))
        if not job.get('digest') and job.get('source_id') and job['source_id'] not in ids:
            raise ValueError('本次变动必须有当前用户消息的依据，旧上下文只用于理解')
        confirmed = max(job.get('_source_times', {}).get(r, job['source_ts']) for r in refs)
        return ids, confirmed

    now, changed = time.time(), False
    ended = ('done', 'cancelled', 'retired')

    def check_idea_end(previous, replacement, item):
        # Check the resulting lifecycle, including dates and merges, not just op=remove.
        if (previous.get('kind') == 'idea' and not item.get('sources')
                and lifecycle(previous, now) not in ended and lifecycle(replacement, now) in ended):
            raise ValueError('一周内仍有效的想法不能在整理中提前结束，需要本人完成、取消或替代的来源')

    absorbed = set()
    for item in parsed['updates'][:limit]:
        try:
            if not isinstance(item, dict):
                raise ValueError('画像更新格式无效')
            old = refs.get(item.get('ref')) or document['entries'].get(item.get('key'), {})
            if old.get('key') in absorbed:
                continue
            if item.get('ref') and not old:
                if not re.fullmatch(r'P\d{1,4}', str(item['ref'])) or not item.get('sources'):
                    raise ValueError('未知画像短标签，且缺少新增事实的有效来源')
                # Models sometimes number new records themselves. A valid new fact needn't be lost for that.
            if old:
                if old['key'] not in document['entries']:
                    continue  # Another maintenance pass already absorbed this reference.
                old = document['entries'][old['key']]
            cat = category(item.get('category', old.get('category', 'recent')))
            topic = personalize_text(item.get('topic', old.get('topic', '')).strip(), user_name)
            if len(topic) > 40:
                raise ValueError('主题最多40字')
            if not old:
                old = next((e for e in document['entries'].values()
                            if e.get('content') == item.get('content') and e.get('category') == cat), {})
            if not old and topic:
                old = next((e for e in document['entries'].values()
                            if e.get('category') == cat and e.get('topic', '').strip().casefold() == topic.casefold()
                            and (cat != 'facts' or set(e.get('sources', [])) &
                                 {sources[r] for r in item.get('sources', []) if isinstance(r, str) and r in sources})
                            and ((item.get('kind') != 'task' and e.get('kind') != 'task') or e.get('content') == item.get('content'))), {})
            if old.get('deleted'):
                continue
            merging = item.get('merge_from') or []
            if not isinstance(merging, list) or any(r not in refs for r in merging):
                raise ValueError('合并来源标签无效')
            merge_entries = [refs[r] for r in merging]
            if any(document['entries'].get(e['key']) != e for e in merge_entries):
                continue  # Never absorb an item edited/deleted since the model's snapshot.
            if any(e['key'] in absorbed for e in merge_entries):
                raise ValueError('合并来源已在本轮整理中合并，请引用保留项')
            if merge_entries and any(e.get('deleted') for e in merge_entries):
                raise ValueError('不能合并已删除资料')
            if merge_entries and not item.get('sources') and job.get('consolidation'):
                ids = list(dict.fromkeys(s for e in merge_entries for s in e.get('sources', [])))
                confirmed = max(e.get('source_ts', 0) for e in merge_entries)
            else:
                ids, confirmed = lineage(item, old)
            if merge_entries:
                ids = list(dict.fromkeys([*ids, *(s for e in merge_entries for s in e.get('sources', []))]))
                confirmed = max(confirmed, old.get('source_ts', 0), *(e.get('source_ts', 0) for e in merge_entries))
            if old.get('source_ts', 0) > confirmed:
                continue
            baseline = job.get('_base_entries', {}).get(old.get('key'))
            if baseline and old != baseline and confirmed <= old.get('source_ts', 0):
                continue  # A delayed digest cannot replace a newer interpretation/manual correction.
            if item.get('op', 'upsert') == 'remove':
                if unfinished_task(old):
                    raise ValueError('未完成待办不能直接删除，请更新完成或取消状态')
                if old:
                    replacement = {**old, 'status': 'retired', 'retired_at': now, 'updated_at': now}
                    check_idea_end(old, replacement, item)
                    document['entries'][old['key']] = replacement
                    changed = True
                continue
            if item.get('op', 'upsert') not in ('upsert', 'add', 'update'):
                raise ValueError('未知画像操作')
            key = old.get('key') or 'fact.' + uuid.uuid4().hex
            _, cat, content = _entry({'key': key, 'category': cat, 'content': item.get('content', old.get('content'))})
            content = personalize_text(content, user_name)
            if len(content) > 160:
                raise ValueError('使用配置姓名后的正文最多160字，请简写或省略主语')
            group = item.get('subcategory') or old.get('subcategory')
            kind = item.get('kind') or old.get('kind') or {'plans': 'task', 'projects': 'project'}.get(group, 'context')
            status = item.get('status', 'active' if item.get('sources') else old.get('status', 'active'))
            if kind not in KINDS or status not in STATUSES:
                raise ValueError('近期类型或状态无效')
            if any(e['key'] != key and unfinished_task(e) for e in merge_entries):
                raise ValueError('未完成待办不能在合并中消失，请分别维护')
            if status not in ended and (not old or item.get('sources')) and 'manual' not in old.get('sources', []):
                if (kind == 'idea' or group in ('ideas', 'preferences') or
                        (kind == 'context' and item.get('sustained', old.get('sustained')) is not True)):
                    continue
            if cat != 'recent':
                kind = 'context'
                if old.get('category') == 'recent' and 'status' not in item:
                    status = 'active'
            if not old and cat == 'recent':
                # Finished activities belong to the chat history, not a new fact archive.
                if status in ('done', 'cancelled', 'retired'):
                    continue
                reason = item.get('reason')
                if not isinstance(reason, str) or not reason.strip() or len(reason) > 120:
                    raise ValueError('新增近期资料需说明后续参考价值（reason，最多120字）；日常琐事只更新活动状态')
            if cat == 'profile' and (not old or old.get('category') != 'profile') and not merge_entries:
                reason = item.get('reason')
                if not isinstance(reason, str) or not reason.strip() or len(reason) > 120:
                    raise ValueError('新增核心画像需说明长期理解价值；具体物品等零散信息请放事实档案')
            if kind == 'task' and status == 'retired':
                raise ValueError('待办不能自动退役，请依据用户确认更新为完成或取消')
            if unfinished_task(old):
                if status == 'retired' or ((status in ('done', 'cancelled') or kind != 'task')
                                          and (not item.get('sources'))):
                    raise ValueError('未完成待办必须依据用户确认收尾，不能在压缩时退役或改为普通近况')
                if kind != 'task' and status not in ('done', 'cancelled') and not (item.get('continues') is True and item.get('sources')):
                    raise ValueError('未完成待办不能改为普通近况')
            value = {**old, 'key': key, 'category': cat, 'topic': topic, 'content': content,
                     'kind': kind, 'status': status, 'sustained': bool(item.get('sustained', old.get('sustained', False))),
                     'locked': False, 'deleted': False, 'updated_at': now, 'source_ts': confirmed,
                     'sources': list(dict.fromkeys([*old.get('sources', []), *ids]))}
            value['next_step'] = item.get('next_step', old.get('next_step', ''))
            if not isinstance(value['next_step'], str) or len(value['next_step']) > 80:
                raise ValueError('下一步最多80字')
            value['subcategory'] = subcategory({**value, 'subcategory': item.get('subcategory', old.get('subcategory'))})
            importance = float(item.get('importance', old.get('importance', 0.6)))
            certainty = item.get('certainty', old.get('certainty', 'fact'))
            keywords = item.get('keywords', old.get('keywords', []))
            if not math.isfinite(importance) or not 0 <= importance <= 1 or certainty not in ('fact', 'inferred', 'conflict'):
                raise ValueError('重要程度或事实确认状态无效')
            if not isinstance(keywords, list) or len(keywords) > 8 or any(not isinstance(k, str) or len(k) > 40 for k in keywords):
                raise ValueError('检索词格式无效')
            value.update(importance=importance, certainty=certainty, keywords=list(dict.fromkeys(k.strip() for k in keywords if k.strip())))
            if item.get('reason'):
                value['reason'] = item['reason'].strip()
            for field in ('available_at', 'due_at', 'expires_at', 'event_at'):
                try:
                    # null usually means "not supplied", not evidence that a known time was cancelled.
                    value[field] = timestamp(item.get(field) or old.get(field))
                except ValueError as exc:
                    value[field] = old.get(field)
                    errors.append(f'画像{field}：{exc}；正文已保留')
            clear_dates = item.get('clear_dates') or []
            if (kind in ('idea', 'context') and item.get('sources') and confirmed > old.get('source_ts', 0)
                    and value.get('expires_at') and value['expires_at'] <= confirmed
                    and not item.get('expires_at') and item.get('duration_days') is None):
                value['expires_at'] = None  # A newly confirmed temporary interest gets a fresh week.
            if not isinstance(clear_dates, list) or any(f not in ('available_at', 'due_at', 'expires_at') for f in clear_dates):
                raise ValueError('清除日期字段无效')
            if clear_dates and not item.get('sources'):
                raise ValueError('清除旧阶段日期需要本人来源')
            for field in clear_dates:
                value[field] = None
            if cat != 'recent' and old.get('category') == 'recent':
                value.update(sustained=False, available_at=None, due_at=None, expires_at=None)
            if cat == 'recent' and item.get('duration_days') is not None:
                try:
                    duration = float(item['duration_days'])
                    if not math.isfinite(duration) or not 0 < duration <= 365:
                        raise ValueError('持续天数需要在0到365之间')
                    value['expires_at'] = confirmed + duration*86400
                except (ValueError, TypeError) as exc:
                    errors.append(f'持续时间：{exc}；正文已保留')
            if status in ('done', 'cancelled', 'retired'):
                value['retired_at'] = old.get('retired_at') or now
            elif item.get('sources') or (cat != 'recent' and old.get('category') == 'recent'):
                value.pop('retired_at', None)
            for previous in [old, *merge_entries]:
                check_idea_end(previous, value, item)
            # Identical consolidation must not refresh either confirmation or storage time.
            if all(old.get(k) == v for k, v in value.items() if k != 'updated_at'):
                if not any(e['key'] != key for e in merge_entries):
                    continue
                value['updated_at'] = old.get('updated_at', now)
            candidate = {**document['entries'], key: value}
            for merged in merge_entries:
                if merged['key'] != key:
                    candidate.pop(merged['key'], None)
            if not job.get('defer_budget'):
                _check_budget({**document, 'entries': candidate})
            document['entries'][key] = value
            for merged in merge_entries:
                if merged['key'] != key:
                    # The merged fact keeps all sources; duplicates need no second archive.
                    document['entries'].pop(merged['key'], None)
                    absorbed.add(merged['key'])
            changed = True
        except (ValueError, TypeError, AttributeError) as exc:
            errors.append(f'画像：{exc}')
    state = parsed.get('state')
    if state is not None:
        try:
            if not isinstance(state, dict):
                raise ValueError('活动状态格式无效')
            ids, confirmed = lineage(state)
            text, phase, ttl = state.get('text'), state.get('phase'), state.get('ttl_minutes', 60)
            if isinstance(text, str):
                text = personalize_text(text.strip(), user_name)
            if not isinstance(text, str) or not text:
                raise ValueError('活动状态缺少文字内容（text或content）')
            if len(text) > STATE_MAX_CHARS:
                raise ValueError(f'活动状态共{len(text)}字，最多{STATE_MAX_CHARS}字，请简写')
            if not isinstance(phase, str) or phase not in PHASES:
                raise ValueError('活动阶段无效，请用planned/ongoing/finished/unknown')
            try:
                ttl = float(ttl) if not isinstance(ttl, bool) else float('nan')
            except (ValueError, TypeError):
                raise ValueError('活动状态有效期无效') from None
            if not math.isfinite(ttl) or ttl <= 0:
                raise ValueError('活动状态有效期无效')
            ttl = min(ttl, 600)
            old = document.get('current_state') or {}
            if (old.get('event_at', 0) <= confirmed
                    and document.get('state_cleared_at', 0) <= confirmed):
                next_state = {'text': text.strip(), 'phase': phase, 'locked': False,
                                             'event_at': confirmed, 'updated_at': now,
                                             'expires_at': confirmed + ttl*60, 'sources': ids}
                _check_budget({**document, 'current_state': next_state})
                document['current_state'] = next_state
                changed = True
        except (ValueError, TypeError) as exc:
            errors.append(f'状态：{exc}')
    document['_analysis_errors'] = errors
    return changed


async def run_pending_once():
    async with _processing:
        return await _run_pending_once()


async def _run_pending_once():
    if not enabled():
        return False
    from sentinel_dispatch import queue_lock
    async with queue_lock, get_db() as db:
        await db.execute('BEGIN IMMEDIATE')
        row, document = await _read(db)
        raw = await (await db.execute(
            "SELECT * FROM post_sentinel_jobs WHERE status='pending' AND next_attempt_at<=? ORDER BY source_ts LIMIT 1",
            (time.time(),)
        )).fetchone()
        if not raw:
            return False
        job = dict(raw)
        job['attempts'] += 1
        await db.execute("UPDATE post_sentinel_jobs SET status='running',attempts=? WHERE source_id=?",
                         (job['attempts'], job['source_id']))
        await db.commit()
    status, error, changed, output = 'done', '', False, ''
    try:
        messages, sources = await _context_for_job(job)
        if not messages or job['epoch'] != row['epoch']:
            status = 'skipped'
        else:
            async with get_db() as db:
                before_call, _ = await _read(db)
            if not enabled() or before_call['epoch'] != row['epoch']:
                status = 'skipped'
            elif before_call['revision'] != row['revision']:
                status = 'pending'
            else:
                prompt = _analysis_prompt(document, messages, sources, job['source_id'])
                if job['error']:
                    prompt += f'\n上次未处理完整：{job["error"]}。正确部分已经保存；核对现有资料，修复问题，勿重复新增。'
                import kitchen_sentinel
                prepared = None
                try:
                    prepared = await kitchen_sentinel.prepare_joint_analysis(job,get_db)
                except Exception:
                    log.warning('Kitchen analysis will use its own worker',exc_info=True)
                # Preparing inventory can yield while either feature is switched off.
                async with get_db() as db:
                    dispatch, _ = await _read(db)
                if not enabled() or dispatch['epoch'] != row['epoch']:
                    status = 'skipped'
                else:
                    if prepared and (not kitchen_sentinel.kitchen.enabled() or
                                     kitchen_sentinel.kitchen.generation() != prepared['generation']):
                        prepared = None
                    if prepared:
                        from profile_prompts import joint_sentinel_prompt
                        prompt = joint_sentinel_prompt(prompt,prepared['prompt'])
                    parsed = await _call_model(prompt)
                    output = getattr(parsed, 'raw', '') or json.dumps(parsed, ensure_ascii=False)[:12000]
                    if prepared:
                        try:
                            await kitchen_sentinel.save_joint_analysis(prepared,parsed.get('kitchen'))
                        except Exception:
                            log.warning('Kitchen analysis could not be saved',exc_info=True)
                        parsed = parsed.get('life',parsed)
                    changed = _apply_analysis(document, parsed, sources, job)
                    errors = document.pop('_analysis_errors', [])
                    if errors:
                        error = '；'.join(errors)[:1000]
                        status = 'failed' if job['attempts'] >= 3 else 'pending'
                # Earlier user statements may also supply facts or resolve pronouns.
                after_messages, after_sources = await _context_for_job(job)
                if after_messages != messages or after_sources != sources:
                    status, changed = 'skipped', False
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        status, error, changed = ('failed' if job['attempts'] >= 3 else 'pending'), f'{type(exc).__name__}：{exc}'[:1000], False
        output = getattr(exc, 'model_output', '') or output
        log.warning('Post-sentinel analysis failed: %s', error)
    async with get_db() as db:
        await db.execute('BEGIN IMMEDIATE')
        latest, _ = await _read(db)
        if not enabled() or latest['epoch'] != row['epoch']:
            status, changed = 'skipped', False
        elif latest['revision'] != row['revision']:
            # A user edit is not a reason to permanently lose the queued message.
            status, changed, error = 'pending', False, ''
            job['attempts'] -= 1
        if changed:
            await db.execute('UPDATE post_sentinel_document SET payload=?,revision=revision+1,updated_at=? WHERE id=1',
                             (json.dumps(document, ensure_ascii=False), time.time()))
        await db.execute('UPDATE post_sentinel_document SET last_checked_at=?,last_error=? WHERE id=1',
                         (time.time(), error))
        delay = (30 if job['attempts'] < 2 else 120) if status == 'pending' else 0
        await db.execute('UPDATE post_sentinel_jobs SET status=?,error=?,attempts=?,next_attempt_at=?,model_output=? WHERE source_id=?',
                         (status, error, job['attempts'], time.time()+delay, output if error else '', job['source_id']))
        await db.commit()
    await _notify_changed()
    from kitchen_sentinel import wake as wake_kitchen
    wake_kitchen()
    return True


async def build_context(*, now=None, query=''):
    if not enabled():
        return ''
    try:
        async with get_db() as db:
            _, document = await _read(db)
    except Exception:
        return ''  # Unavailable new tables never block existing reply paths.
    return render_context(document, _profile_user_name(), now, query=query)[0]


async def run_maintenance_once(*, now=None):
    if not enabled():
        return False
    now = time.time() if now is None else now
    async with get_db() as db:
        await db.execute('BEGIN IMMEDIATE')
        row, document = await _read(db)
        if now-document.get('last_maintenance_at', 0) < 86400:
            return False
        maintain(document, now)
        if now-document.get('last_consolidation_at', now) >= 7*86400:
            document.pop('consolidation_output', None)
        document['last_maintenance_at'] = now
        await db.execute('UPDATE post_sentinel_document SET payload=?,revision=revision+1 WHERE id=1',
                         (json.dumps(document, ensure_ascii=False),))
        # Raw failure diagnostics are useful briefly, not a permanent duplicate transcript.
        await db.execute("UPDATE post_sentinel_jobs SET model_output='' WHERE source_ts<?", (now-7*86400,))
        await db.execute("UPDATE post_sentinel_jobs SET content='' WHERE status IN ('done','skipped') AND source_ts<?", (now-7*86400,))
        await db.commit()
    await _notify_changed()
    return True


async def consolidate_once():
    # Kept for callers from older builds; large-model memory digests own consolidation.
    return False

async def _notify_changed():
    try:
        from ws import manager
        await manager.broadcast({'type': 'user_profile_changed'})
    except Exception:
        pass


async def _loop():
    while True:
        try:
            _wake.clear()
            while await run_pending_once():
                pass
            await run_maintenance_once()
            from profile_digest import apply_pending
            await apply_pending()
            await asyncio.wait_for(_wake.wait(), timeout=60)
        except asyncio.TimeoutError:
            pass
        except asyncio.CancelledError:
            raise
        except Exception:
            log.warning('Post-sentinel worker unavailable', exc_info=True)
            await asyncio.sleep(5)


async def start():
    global _worker, _wake
    if _worker is not None:
        return
    await ensure_schema()
    async with get_db() as db:
        await db.execute("UPDATE post_sentinel_jobs SET status='pending' WHERE status='running'")
        _, document = await _read(db)
        progress = document.get('initialization', {})
        if progress.get('mode') == 'recent_week' and progress.get('status') == 'running':
            progress['status'] = 'pending'
            document['needs_initialization'] = True
            await db.execute('UPDATE post_sentinel_document SET payload=?,revision=revision+1 WHERE id=1',
                             (json.dumps(document, ensure_ascii=False),))
        await db.commit()
    _wake = asyncio.Event()
    _worker = asyncio.create_task(_loop(), context=contextvars.Context())
    # Historical replay is never automatically started or resumed by server startup.


async def stop():
    global _worker, _wake
    from profile_initialize import stop as stop_initialization
    await stop_initialization()
    tasks = list(_enqueue_tasks)
    if _worker:
        tasks.append(_worker)
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    _enqueue_tasks.clear()
    _worker, _wake = None, None
