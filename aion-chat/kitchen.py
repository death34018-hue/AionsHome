"""Shared kitchen records. Models suggest changes; this module owns the ledger."""
from __future__ import annotations

import asyncio
import json
import math
import re
import time
from datetime import date, timedelta
from uuid import uuid4

import aiosqlite

from config import SETTINGS, save_settings
from database import get_db


DEFAULT_PREFERENCES = {
    'breakfast': '两个蒸鸡蛋、一节玉米', 'max_minutes': 20, 'servings': 1,
    'methods': '蒸、煮、少油快炒、空气炸锅', 'avoid': '油多的煎炸',
    'dislikes': '', 'notes': '', 'use_only_stock': True,
}
_generation = 0
_transitioning = False
_toggle_lock = asyncio.Lock()


class MenuChanged(ValueError):
    pass


def enabled() -> bool:
    return not _transitioning and bool(SETTINGS.get('kitchen_enabled', False))


def generation():
    return _generation


async def ensure_schema():
    async with get_db() as db:
        await db.executescript('''
            CREATE TABLE IF NOT EXISTS kitchen_state (
                id INTEGER PRIMARY KEY CHECK(id=1), enabled INTEGER NOT NULL DEFAULT 0,
                epoch INTEGER NOT NULL DEFAULT 0, preferences TEXT NOT NULL DEFAULT '{}',
                activated_at REAL NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS kitchen_manual_edits (name TEXT PRIMARY KEY, edited_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS kitchen_items (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, quantity REAL, amount TEXT NOT NULL,
                unit TEXT NOT NULL DEFAULT '', category TEXT NOT NULL DEFAULT 'other',
                storage TEXT NOT NULL DEFAULT 'fridge', soon INTEGER NOT NULL DEFAULT 0,
                uncertain INTEGER NOT NULL DEFAULT 0, note TEXT NOT NULL DEFAULT '',
                updated_at REAL NOT NULL, manual_at REAL NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS kitchen_events (
                source_id TEXT PRIMARY KEY, epoch INTEGER NOT NULL, source_ts REAL NOT NULL,
                changes TEXT NOT NULL, created_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS kitchen_pending (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL,
                source_id TEXT NOT NULL, payload TEXT NOT NULL, created_at REAL NOT NULL,
                resolved_at REAL);
            CREATE TABLE IF NOT EXISTS kitchen_menus (
                id TEXT PRIMARY KEY, date TEXT NOT NULL UNIQUE, actor TEXT NOT NULL,
                payload TEXT NOT NULL, updated_at REAL NOT NULL, confirmed_at REAL NOT NULL DEFAULT 0,
                proposal_id TEXT NOT NULL DEFAULT '');
            CREATE TABLE IF NOT EXISTS kitchen_proposals (
                id TEXT PRIMARY KEY, date TEXT NOT NULL, actor TEXT NOT NULL,
                payload TEXT NOT NULL, updated_at REAL NOT NULL, confirmed_at REAL NOT NULL DEFAULT 0,
                confirmed_meals TEXT NOT NULL DEFAULT '{}');
            CREATE TABLE IF NOT EXISTS kitchen_jobs (
                source_id TEXT PRIMARY KEY, scope TEXT NOT NULL, parent_id TEXT NOT NULL,
                message_id TEXT NOT NULL, content TEXT NOT NULL, source_ts REAL NOT NULL,
                epoch INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                error TEXT NOT NULL DEFAULT '');
        ''')
        columns = {r[1] for r in await (await db.execute('PRAGMA table_info(kitchen_state)')).fetchall()}
        if 'activated_at' not in columns:
            await db.execute('ALTER TABLE kitchen_state ADD COLUMN activated_at REAL NOT NULL DEFAULT 0')
        columns = {r[1] for r in await (await db.execute('PRAGMA table_info(kitchen_jobs)')).fetchall()}
        if 'analysis' not in columns:
            await db.execute("ALTER TABLE kitchen_jobs ADD COLUMN analysis TEXT NOT NULL DEFAULT ''")
        columns = {r[1] for r in await (await db.execute('PRAGMA table_info(kitchen_menus)')).fetchall()}
        if 'confirmed_at' not in columns:
            await db.execute('ALTER TABLE kitchen_menus ADD COLUMN confirmed_at REAL NOT NULL DEFAULT 0')
        if 'proposal_id' not in columns:
            await db.execute("ALTER TABLE kitchen_menus ADD COLUMN proposal_id TEXT NOT NULL DEFAULT ''")
        columns = {r[1] for r in await (await db.execute('PRAGMA table_info(kitchen_proposals)')).fetchall()}
        if 'confirmed_meals' not in columns:
            await db.execute("ALTER TABLE kitchen_proposals ADD COLUMN confirmed_meals TEXT NOT NULL DEFAULT '{}'")
            rows = await (await db.execute('SELECT id,payload,confirmed_at FROM kitchen_proposals WHERE confirmed_at>0')).fetchall()
            for menu_id,payload,confirmed_at in rows:
                history = {m['meal']:confirmed_at for m in json.loads(payload)['meals']}
                await db.execute('UPDATE kitchen_proposals SET confirmed_meals=? WHERE id=?', (json.dumps(history),menu_id))
        # The first version never asked for confirmation. Preserve its card ids as proposals.
        await db.execute('INSERT OR IGNORE INTO kitchen_proposals (id,date,actor,payload,updated_at) '
                         'SELECT id,date,actor,payload,updated_at FROM kitchen_menus WHERE confirmed_at=0')
        await db.execute('DELETE FROM kitchen_menus WHERE confirmed_at=0')
        await db.execute('INSERT OR IGNORE INTO kitchen_state (id,enabled) VALUES (1,?)', (int(enabled()),))
        await db.commit()


async def state() -> dict:
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        row = dict(await (await db.execute('SELECT * FROM kitchen_state WHERE id=1')).fetchone())
    return {**row, 'enabled': enabled() and bool(row['enabled']),
            'preferences': {**DEFAULT_PREFERENCES, **json.loads(row['preferences'])}}


async def set_enabled(value: bool) -> dict:
    global _generation, _transitioning
    async with _toggle_lock:
        _generation += 1
        _transitioning = True
        try:
            async with get_db() as db:
                await db.execute('BEGIN IMMEDIATE')
                await db.execute('UPDATE kitchen_state SET enabled=?,epoch=epoch+1,activated_at=? WHERE id=1', (int(value),time.time()))
                await db.execute("UPDATE kitchen_jobs SET status='skipped' WHERE status IN ('pending','running','failed')")
                await db.commit()
            SETTINGS['kitchen_enabled'] = bool(value)
            save_settings(SETTINGS)
        finally:
            _transitioning = False
    return await state()


def amount_value(value):
    text = str(value if value is not None else '').strip()[:30]
    chinese = {'半': .5, '一': 1, '两': 2, '二': 2, '三': 3, '四': 4, '五': 5,
               '六': 6, '七': 7, '八': 8, '九': 9, '十': 10, '四分之一': .25}
    if text in chinese:
        return chinese[text]
    try:
        number = float(text)
        return number if math.isfinite(number) and 0 <= number <= 100000 else None
    except (TypeError, ValueError):
        return None


def amount_text(number) -> str:
    if number == .5:
        return '半'
    return format(number, 'g')


def _item(row):
    result = dict(row)
    for key in ('soon', 'uncertain'):
        result[key] = bool(result[key])
    return result


async def _find_items(db, name):
    return await (await db.execute('SELECT * FROM kitchen_items WHERE name=?', (name,))).fetchall()


def _same_unit(name, first, second):
    egg_counts = ('个', '颗', '枚')
    return first == second or (name in ('鸡蛋', '鸭蛋', '鹌鹑蛋') and first in egg_counts and second in egg_counts)


async def _write_item(db, payload, *, manual=False):
    now = time.time()
    old = None
    if payload.get('id'):
        old = await (await db.execute('SELECT * FROM kitchen_items WHERE id=?', (payload['id'],))).fetchone()
        if old is None:
            raise ValueError('这份食材已不存在，请刷新后重试')
    name = str(payload.get('name') or (old['name'] if old else '')).strip()[:60]
    if not name:
        raise ValueError('请填写食材名称')
    if old is None:
        candidates = await _find_items(db, name)
        old = next((r for r in candidates if r['unit'] == str(payload.get('unit', '')).strip()
                    and r['storage'] == payload.get('storage', 'fridge')), None)
    previous = dict(old) if old else {}
    value = {**previous, **payload}
    raw = str(value.get('amount', '少量')).strip()[:30] or '少量'
    quantity = amount_value(raw)
    item = {
        'id': previous.get('id') or 'ki_' + uuid4().hex, 'name': name,
        'quantity': quantity, 'amount': amount_text(quantity) if quantity is not None else raw,
        'unit': str(value.get('unit', '')).strip()[:16],
        'category': value.get('category') if value.get('category') in ('vegetable','protein','staple','dairy','fruit','other') else guess_category(name),
        'storage': value.get('storage') if value.get('storage') in ('fridge','freezer','pantry') else 'fridge',
        'soon': int(bool(value.get('soon'))), 'uncertain': int(bool(value.get('uncertain', False))),
        'note': str(value.get('note', ''))[:200], 'updated_at': now,
        'manual_at': now if manual else previous.get('manual_at', 0),
    }
    keys = list(item)
    await db.execute(f"INSERT INTO kitchen_items ({','.join(keys)}) VALUES ({','.join('?' for _ in keys)}) "
                     f"ON CONFLICT(id) DO UPDATE SET {','.join(k+'=excluded.'+k for k in keys if k != 'id')}",
                     tuple(item.values()))
    if manual:
        for edited_name in {name, previous.get('name', name)}:
            await db.execute('INSERT OR REPLACE INTO kitchen_manual_edits VALUES (?,?)', (edited_name,now))
            await db.execute('UPDATE kitchen_pending SET resolved_at=? WHERE name=? AND resolved_at IS NULL', (now, edited_name))
    return _item(item)


async def upsert_item(payload: dict) -> dict:
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        item = await _write_item(db, {**payload, 'uncertain': False}, manual=True)
        await db.commit()
    return item


async def delete_item(item_id):
    async with get_db() as db:
        row = await (await db.execute('SELECT name FROM kitchen_items WHERE id=?',(item_id,))).fetchone()
        if row:
            now = time.time()
            await db.execute('INSERT OR REPLACE INTO kitchen_manual_edits VALUES (?,?)',(row[0],now))
            await db.execute('UPDATE kitchen_pending SET resolved_at=? WHERE name=? AND resolved_at IS NULL',(now,row[0]))
        await db.execute('DELETE FROM kitchen_items WHERE id=?', (item_id,))
        await db.commit()


def guess_category(name):
    groups = [('dairy', '奶|酸奶|芝士|奶酪'), ('fruit', '苹果|香蕉|橙|桃|梨|莓|葡萄|西瓜|哈密瓜'),
              ('protein', '鸡|肉|牛|虾|鱼|丸|蛋|豆腐|豆干|火腿'),
              ('staple', '米|面|燕麦|土豆|红薯|馒头|饼'),
              ('vegetable', '菜|西兰花|瓜|椒|茄|菌|菇|笋|萝卜')]
    return next((category for category, words in groups if re.search(words, name)), 'other')


async def bulk_items(text):
    parts = [p.strip() for p in re.split(r'[\n,，、;；]+', str(text)) if p.strip()]
    if not parts or len(parts) > 80:
        raise ValueError('请填写1～80项食材，每行一项')
    count = r'(少量|一些|一点|大半|小半|半|[一二两三四五六七八九十]+|\d+(?:\.\d+)?)'
    unit = r'(公斤|千克|毫升|kg|ml|根|颗|棵|个|盒|袋|瓶|包|块|罐|斤|克|份|节|把|片|只|升|g|L)?'
    payloads = []
    for part in parts:
        part = re.sub(r'^(?:蔬菜|肉类|主食|水果|奶制品|其他)\s*[:：]', '', part).strip()
        match = re.fullmatch(r'(.+?)\s*[:：]?\s*' + count + r'\s*' + unit, part)
        if match:
            name, amount, item_unit = match.groups()
        else:
            match = re.fullmatch(count + r'\s*' + unit + r'\s*(.+)', part)
            if not match:
                raise ValueError(f'「{part[:25]}」还不够清楚，试试写成“黄瓜 2 根”')
            amount, item_unit, name = match.groups()
        name = name.strip().rstrip(':：')
        if not name:
            raise ValueError('请填写食材名称')
        payloads.append({'name': name, 'amount': amount, 'unit': item_unit or '',
                         'category': guess_category(name), 'storage': 'freezer' if '冷冻' in name else 'fridge'})
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        result = [await _write_item(db, p, manual=True) for p in payloads]
        await db.commit()
    return result


async def save_preferences(payload):
    current = await state()
    prefs = current['preferences']
    for key in DEFAULT_PREFERENCES:
        if key in payload:
            if key in ('max_minutes','servings'):
                prefs[key] = max(1, min(60 if key == 'max_minutes' else 8, int(payload[key])))
            elif key == 'use_only_stock':
                prefs[key] = bool(payload[key])
            else:
                prefs[key] = str(payload[key]).strip()[:500]
    async with get_db() as db:
        await db.execute('UPDATE kitchen_state SET preferences=? WHERE id=1', (json.dumps(prefs, ensure_ascii=False),))
        await db.commit()
    return prefs


async def _pending(db, source_id, change, reason, items=()):
    name = str(change.get('name') or '这次用餐')[:60]
    await db.execute('INSERT INTO kitchen_pending VALUES (?,?,?,?,?,?,NULL)',
                     ('kp_' + uuid4().hex, name, str(change.get('note') or reason)[:300], source_id,
                      json.dumps(change, ensure_ascii=False), time.time()))
    for item in items:
        await db.execute('UPDATE kitchen_items SET uncertain=1 WHERE id=?', (item['id'],))


async def _attach_record_marker(db, source_id):
    scope, _, message_id = source_id.partition(':')
    table = {'private':'messages','chatroom':'chatroom_messages'}.get(scope)
    if not table or not await (await db.execute('SELECT 1 FROM sqlite_master WHERE type=? AND name=?',('table',table))).fetchone():
        return
    row = await (await db.execute(f'SELECT attachments FROM {table} WHERE id=?',(message_id,))).fetchone()
    if not row:
        return
    attachments = json.loads(row[0] or '[]')
    if not any(isinstance(a,dict) and a.get('type')=='kitchen_updated' for a in attachments):
        attachments.append({'type':'kitchen_updated'})
        await db.execute(f'UPDATE {table} SET attachments=? WHERE id=?',(json.dumps(attachments,ensure_ascii=False),message_id))


async def recorded_result(source_id):
    async with get_db() as db:
        row = await (await db.execute('SELECT changes FROM kitchen_events WHERE source_id=?',(source_id,))).fetchone()
    if not row:
        return None
    saved = json.loads(row[0])
    return {'updated': bool(saved.get('updated')) if isinstance(saved,dict) else bool(saved), 'duplicate':True}


async def apply_changes(source_id: str, changes: list, *, epoch: int, source_ts: float) -> dict:
    result = {'updated': False, 'duplicate': False, 'pending': 0}
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        await db.execute('BEGIN IMMEDIATE')
        saved = await (await db.execute('SELECT enabled,epoch,activated_at FROM kitchen_state WHERE id=1')).fetchone()
        if not enabled() or not saved['enabled'] or saved['epoch'] != epoch or source_ts < saved['activated_at']:
            return result
        if await (await db.execute('SELECT 1 FROM kitchen_events WHERE source_id=?', (source_id,))).fetchone():
            return {**result, 'duplicate': True}
        for change in changes[:30]:
            if not isinstance(change, dict) or change.get('op') == 'meal':
                # A menu only describes a plan, including on replay of older model results.
                continue
            name = str(change.get('name') or '').strip()[:60]
            manual = await (await db.execute('SELECT edited_at FROM kitchen_manual_edits WHERE name=?',(name,))).fetchone()
            if manual and manual['edited_at'] > source_ts:
                continue
            candidates = await _find_items(db, name)
            unit = str(change.get('unit') or '').strip()[:16]
            matches = [r for r in candidates if (not unit or _same_unit(name, r['unit'], unit))
                       and (not change.get('storage') or r['storage'] == change['storage'])]
            item = matches[0] if len(matches) == 1 else None
            if item and item['manual_at'] > source_ts:
                continue
            op = change.get('op')
            quantity = amount_value(change.get('amount'))
            uncertain_base = item and item['uncertain']
            if uncertain_base and op == 'consume':
                # Known usage can be booked alongside other unresolved relative changes.
                uncertain_base = await (await db.execute(
                    "SELECT 1 FROM kitchen_pending WHERE name=? AND resolved_at IS NULL AND "
                    "COALESCE(json_extract(payload,'$.op'),'') NOT IN ('consume','add') LIMIT 1", (name,))).fetchone()
            if (op not in ('add','set','empty','consume') or not name or
                (op != 'empty' and quantity is None) or len(matches) > 1 or
                (candidates and not item) or (op in ('consume','empty') and not item) or
                (op in ('add','consume') and uncertain_base) or
                (op in ('add','consume') and item and item['quantity'] is None) or
                (op == 'consume' and item and quantity > item['quantity'])):
                await _pending(db, source_id, change, '请确认这次的食材和数量', matches or candidates)
                result['pending'] += 1
                result['updated'] = True
                continue
            if op == 'empty':
                value = 0
            elif op == 'set' or not item:
                value = quantity
            else:
                value = item['quantity'] + (quantity if op == 'add' else -quantity)
            payload = {**(dict(item) if item else {}), **change, 'name': name,
                       'unit': item['unit'] if item else unit, 'amount': amount_text(value),
                       'uncertain': bool(item and item['uncertain'] and op == 'consume')}
            await _write_item(db, payload)
            if op in ('set','empty'):
                await db.execute('UPDATE kitchen_pending SET resolved_at=? WHERE name=? AND resolved_at IS NULL',(time.time(),name))
            result['updated'] = True
        if result['updated']:
            await _attach_record_marker(db, source_id)
        await db.execute('INSERT INTO kitchen_events VALUES (?,?,?,?,?)',
                         (source_id, epoch, source_ts, json.dumps({'changes':changes,'updated':result['updated']}, ensure_ascii=False), time.time()))
        await db.commit()
    return result


async def resolve_pending(pending_id: str, payload: dict):
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        await db.execute('BEGIN IMMEDIATE')
        row = await (await db.execute('SELECT * FROM kitchen_pending WHERE id=? AND resolved_at IS NULL', (pending_id,))).fetchone()
        if not row:
            raise ValueError('这条待确认事项已经处理过了')
        action = payload.get('action', 'apply')
        if action in ('consume','add'):
            item = await (await db.execute('SELECT * FROM kitchen_items WHERE id=?', (payload.get('item_id'),))).fetchone()
            qty = amount_value(payload.get('amount'))
            if qty is None or qty <= 0:
                raise ValueError('请填本次的数量，例如 2 或 半')
            name = item['name'] if item else str(payload.get('name') or row['name']).strip()
            unit = str(payload.get('unit') or (item['unit'] if item else '')).strip()
            if not item and (payload.get('item_id') or await _find_items(db, name)):
                raise ValueError('请选择要更新的库存食材')
            if not name or name == '这次用餐':
                raise ValueError('请填写本次变动的食材')
            previous = dict(item) if item else {}
            old_qty = item['quantity'] if item else 0
            comparable = not item or _same_unit(name, item['unit'], unit)
            new_qty = None if not comparable or old_qty is None else old_qty + (qty if action == 'add' else -qty)
            # An acknowledged amount can be known even when its remaining balance cannot.
            balance = amount_text(new_qty) if new_qty is not None and new_qty >= 0 else '待估'
            if item or action == 'add':
                await _write_item(db, {**previous, 'name':name, 'amount':balance,
                                       'unit':item['unit'] if item else unit})
            change = json.loads(row['payload'])
            change['resolution'] = {'op':action,'name':name,'amount':qty,'unit':unit,
                                    'before':old_qty,'stock_unit':previous.get('unit',unit)}
            await db.execute('UPDATE kitchen_pending SET payload=? WHERE id=?',
                             (json.dumps(change, ensure_ascii=False),pending_id))
        elif action in ('apply','set'):
            item_id = payload.get('item_id')
            matches = await _find_items(db, row['name'])
            if item_id:
                item = await (await db.execute('SELECT * FROM kitchen_items WHERE id=?', (item_id,))).fetchone()
                if not item:
                    raise ValueError('这份库存已更新，请重新选择')
            else:
                if len(matches) > 1:
                    raise ValueError('请选择要更新的库存食材')
                item = matches[0] if matches else None
            if not str(payload.get('amount','')).strip():
                raise ValueError('请填写数量')
            await _write_item(db, {**(dict(item) if item else {}), 'amount':payload['amount'],
                                  'unit':payload.get('unit', item['unit'] if item else ''),
                                  'name':payload.get('name') or (item['name'] if item else row['name']),
                                  'uncertain':False}, manual=True)
        elif action != 'ignore':
            raise ValueError('请选择记录方式')
        await db.execute('UPDATE kitchen_pending SET resolved_at=? WHERE id=?', (time.time(), pending_id))
        remaining = await (await db.execute('SELECT 1 FROM kitchen_pending WHERE name=? AND resolved_at IS NULL', (row['name'],))).fetchone()
        if not remaining:
            await db.execute('UPDATE kitchen_items SET uncertain=0 WHERE name=?', (row['name'],))
        await db.commit()


def normalize_menu(payload):
    menu_date = date.fromisoformat(str(payload.get('date') or date.today().isoformat())).isoformat()
    meals = []
    entries = payload.get('meals', [])
    if 'options' in payload:
        options = payload['options']
        if not isinstance(options,list) or len(options) != 2 or not all(isinstance(v,dict) for v in options):
            raise ValueError('请给出两份备选')
        # Keep the established record keys for old cards. Only confirmation assigns a serving time.
        entries = [{**option,'meal':key} for key,option in zip(('lunch','dinner'),options)]
    for raw in entries[:3]:
        meal = {'午餐': 'lunch', '晚餐': 'dinner'}.get(raw.get('meal'), raw.get('meal'))
        if meal not in ('lunch','dinner') or not str(raw.get('name', '')).strip():
            continue
        ingredients = [{'name': str(i.get('name',''))[:60], 'amount': i.get('amount', '少量'),
                        'unit': str(i.get('unit',''))[:16], 'missing': bool(i.get('missing', False))}
                       for i in raw.get('ingredients', [])[:15] if isinstance(i, dict) and i.get('name')]
        steps = [str(s).strip()[:300] for s in raw.get('steps', [])[:8] if str(s).strip()]
        if not ingredients or not steps:
            raise ValueError('菜单还缺食材或做法，请再生成一次')
        minutes = int(raw.get('minutes', 20))
        if not 1 <= minutes <= 120:
            raise ValueError('请填写合理的制作时间')
        meals.append({'meal': meal, 'name': str(raw['name']).strip()[:100], 'minutes': minutes,
                      'ingredients': ingredients, 'steps': steps, 'note': str(raw.get('note',''))[:300]})
    if not meals:
        raise ValueError('还没有拿到可保存的午餐或晚餐菜单')
    if 'options' in payload and len({m['name'] for m in meals}) != 2:
        raise ValueError('还没有两份不同的备选')
    return {'date': menu_date, 'meals': meals, 'message': str(payload.get('message') or '').strip()[:400],
            **({'free_choice':True} if 'options' in payload else {})}


async def cleanup_menus(db):
    cutoff = (date.today() - timedelta(days=6)).isoformat()
    await db.execute('DELETE FROM kitchen_menus WHERE date<?', (cutoff,))
    await db.execute('DELETE FROM kitchen_proposals WHERE date<?', (cutoff,))


def _choice_key(source, destination):
    return source if source == destination else f'{source}:{destination}'


def _menu(row, *, proposal=False, selected=None):
    result = {**json.loads(row['payload']), 'id':row['id'], 'date':row['date'],
              'actor':row['actor'], 'updated_at':row['updated_at'], 'kind':'proposal' if proposal else 'confirmed'}
    if not proposal:
        result['meals'] = [{**m, 'actor':m.get('actor',row['actor']),
                           'message':m.get('message',result.get('message','')),
                           'proposal_id':m.get('proposal_id',row['proposal_id']),
                           'source_meal':m.get('source_meal',m['meal']),
                           'confirmed_at':m.get('confirmed_at',row['confirmed_at'])} for m in result['meals']]
        result.update(status='confirmed', message='', meal_status={m['meal']:'confirmed' for m in result['meals']})
        return result
    history = json.loads(row['confirmed_meals'])
    slots = {m['meal']:{k:m[k] for k in ('proposal_id','source_meal','name','confirmed_at')}
             for m in (selected or {}).get('meals',[])}
    result['table_slots'] = slots
    statuses = {}
    for m in result['meals']:
        source = m['meal']
        previous_slots = [slot for slot in ('lunch','dinner') if _choice_key(source,slot) in history]
        if any(s['proposal_id'] == row['id'] and s['source_meal'] == source for s in slots.values()):
            statuses[source] = 'confirmed'
        elif previous_slots:
            statuses[source] = 'superseded' if any(slot in slots for slot in previous_slots) else 'removed'
        else:
            statuses[source] = 'proposed'
    result['meal_status'] = statuses
    result['selection_at'] = {slot:slots.get(slot,{}).get('confirmed_at',0) for slot in ('lunch','dinner')}
    result['status'] = ('confirmed' if all(s=='confirmed' for s in statuses.values()) else
                        'superseded' if all(s=='superseded' for s in statuses.values()) else 'proposed')
    return result


async def save_menu(payload: dict, *, actor: str, epoch: int, target_meal: str = '', proposal_id: str = '', scope='', parent_id=''):
    """Save an AI suggestion; the table changes only in confirm_menu."""
    menu = normalize_menu(payload)
    if target_meal in ('lunch','dinner'):
        menu['meals'] = [m for m in menu['meals'] if m['meal']==target_meal]
        if not menu['meals']:
            raise ValueError('这次还没有拿到要替换的那一餐')
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        await db.execute('BEGIN IMMEDIATE')
        current = await (await db.execute('SELECT * FROM kitchen_state WHERE id=1')).fetchone()
        if not enabled() or not current['enabled'] or current['epoch'] != epoch:
            return None
        old = None
        if proposal_id:
            old = await (await db.execute('SELECT * FROM kitchen_proposals WHERE id=?', (proposal_id,))).fetchone()
            if not old:
                old = await (await db.execute('SELECT * FROM kitchen_menus WHERE id=?', (proposal_id,))).fetchone()
            if not old or old['date'] != menu['date']:
                raise ValueError('这份菜单已过期，请重新安排')
        elif target_meal:
            old = await (await db.execute('SELECT * FROM kitchen_menus WHERE date=?', (menu['date'],))).fetchone()
        previous = json.loads(old['payload']) if old else {}
        if target_meal and old and 'confirmed_meals' in old.keys() and old['actor'] == actor:
            if scope == 'kitchen':
                await _check_proposal_slot(db, old, target_meal)
            menu['replaces'] = old['id']
        previous_origin = previous.get('origin') or next((m.get('origin') for m in previous.get('meals',[]) if m['meal']==target_meal), None)
        if scope == 'kitchen' and previous_origin:
            menu['origin'] = previous_origin
        elif scope:
            menu['origin'] = {'scope':scope, 'parent_id':parent_id}
        elif previous_origin:
            menu['origin'] = previous_origin
        # Record only the replaced slot. Concurrent lunch/dinner swaps can then compose.
        proposal = await (await db.execute('SELECT id FROM kitchen_proposals WHERE id=? AND actor=? AND confirmed_at=0',
                                           (proposal_id,actor))).fetchone() if proposal_id and not target_meal else None
        menu_id = proposal['id'] if proposal else 'kp_' + uuid4().hex
        now = time.time()
        await db.execute('INSERT INTO kitchen_proposals (id,date,actor,payload,updated_at) VALUES (?,?,?,?,?) ON CONFLICT(id) DO UPDATE '
                         "SET payload=excluded.payload,updated_at=excluded.updated_at,confirmed_at=0,confirmed_meals='{}'",
                         (menu_id, menu['date'], actor, json.dumps(menu, ensure_ascii=False), now))
        await cleanup_menus(db)
        await db.commit()
    return {**menu, 'id':menu_id, 'actor':actor, 'updated_at':now, 'status':'proposed', 'kind':'proposal',
            'meal_status':{m['meal']:'proposed' for m in menu['meals']}}


async def confirm_menu(proposal_id, *, meal='', serve_as='', reselect=False, expected_updated_at=None, expected_selection_at=None):
    """An explicit user action, also allowed while kitchen AI is off."""
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        await db.execute('BEGIN IMMEDIATE')
        await cleanup_menus(db)
        if serve_as and (serve_as not in ('lunch','dinner') or meal not in ('lunch','dinner')):
            raise ValueError('请选择一道菜和用餐时间')
        row = await (await db.execute('SELECT * FROM kitchen_proposals WHERE id=?', (proposal_id,))).fetchone()
        if not row and serve_as:
            # The chosen table can still contain an older dish after its author sent new options.
            table = await (await db.execute('SELECT * FROM kitchen_menus WHERE id=?', (proposal_id,))).fetchone()
            if table:
                if expected_updated_at != table['updated_at']:
                    raise MenuChanged('餐桌刚有变化，刷新后再选一次吧')
                saved = next((m for m in _menu(table)['meals'] if m['meal'] == meal), None)
                if saved:
                    proposal_id, meal = saved['proposal_id'], saved['source_meal']
                    row = await (await db.execute('SELECT * FROM kitchen_proposals WHERE id=?', (proposal_id,))).fetchone()
                    expected_updated_at = None  # The table snapshot above identifies the dish already on it.
        if not row:
            raise ValueError('这份提案已过期，请重新安排')
        if row['date'] != date.today().isoformat():
            raise ValueError('只能确认今天的菜单，请重新安排今天的饭菜')
        if expected_updated_at is not None and row['updated_at'] != expected_updated_at:
            raise MenuChanged('这份提案刚有变化，刷新后再选一次吧')
        proposal = json.loads(row['payload'])
        if proposal.get('free_choice') and not serve_as:
            raise ValueError('请点选餐，选择作午餐还是作晚餐')
        requested = [m for m in proposal['meals'] if not meal or m['meal']==meal]
        if not requested:
            raise ValueError('这份提案里没有要选择的那一餐')
        if meal and expected_updated_at is not None:
            await _check_proposal_slot(db, row, meal)
        old = await (await db.execute('SELECT * FROM kitchen_menus WHERE date=?', (row['date'],))).fetchone()
        menu = _menu(old) if old else {'date':row['date'], 'meals':[], 'message':''}
        by_meal = {m['meal']:m for m in menu['meals']}
        history = json.loads(row['confirmed_meals'])
        changes = {}
        for recipe in requested:
            source = recipe['meal']
            slot = serve_as or source
            current = by_meal.get(slot,{})
            if current.get('proposal_id') == proposal_id and current.get('source_meal') == source:
                continue
            if reselect or _choice_key(source,slot) not in history:
                changes[slot] = recipe
        if not changes:
            await db.commit()
            return menu
        if expected_selection_at is not None and any(by_meal.get(slot,{}).get('confirmed_at',0) != expected_selection_at for slot in changes):
            raise MenuChanged('餐桌上的选择刚有变化，刷新后再选一次吧')
        menu_id, now = old['id'] if old else 'km_' + uuid4().hex, time.time()
        for slot, recipe in changes.items():
            source = recipe['meal']
            by_meal[slot] = {**recipe, 'meal':slot, 'source_meal':source, 'actor':row['actor'],
                             'message':proposal.get('message',''), 'proposal_id':proposal_id,
                             'confirmed_at':now, 'origin':proposal.get('origin',{})}
            history[_choice_key(source,slot)] = now
        menu = {'date':row['date'], 'meals':[by_meal[k] for k in ('lunch','dinner') if k in by_meal], 'message':''}
        await db.execute('INSERT INTO kitchen_menus (id,date,actor,payload,updated_at,confirmed_at,proposal_id) VALUES (?,?,?,?,?,?,?) '
                         'ON CONFLICT(date) DO UPDATE SET actor=excluded.actor,payload=excluded.payload,'
                         'updated_at=excluded.updated_at,confirmed_at=excluded.confirmed_at,proposal_id=excluded.proposal_id',
                         (menu_id,row['date'],row['actor'],json.dumps(menu,ensure_ascii=False),now,now,proposal_id))
        await db.execute('UPDATE kitchen_proposals SET confirmed_at=?,confirmed_meals=? WHERE id=?', (now,json.dumps(history),proposal_id))
        from kitchen_notices import record_notice, publish
        events = [await record_notice(db,menu_id=menu_id,proposal_id=proposal_id,meal=by_meal[slot],
                                      action='confirm',origin=proposal.get('origin',{})) for slot in changes]
        await db.commit()
    await publish(events)
    return {**menu, 'id':menu_id, 'actor':row['actor'], 'updated_at':now, 'status':'confirmed', 'kind':'confirmed',
            'meal_status':{m['meal']:'confirmed' for m in menu['meals']}}


async def remove_menu(menu_id, *, meal='', expected_updated_at=None):
    from kitchen_notices import record_notice, publish
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        await db.execute('BEGIN IMMEDIATE')
        row = await (await db.execute('SELECT * FROM kitchen_menus WHERE id=?', (menu_id,))).fetchone()
        if not row:
            return {'ok':True}
        if expected_updated_at is not None and row['updated_at'] != expected_updated_at:
            raise MenuChanged('餐桌刚有变化，刷新后再移除吧')
        menu = _menu(row)
        removed = [m for m in menu['meals'] if not meal or m['meal']==meal]
        menu['meals'] = [m for m in menu['meals'] if meal and m['meal']!=meal]
        if menu['meals']:
            await db.execute('UPDATE kitchen_menus SET payload=?,updated_at=? WHERE id=?', (json.dumps(menu,ensure_ascii=False),time.time(),menu_id))
        else:
            await db.execute('DELETE FROM kitchen_menus WHERE id=?', (menu_id,))
        events = [await record_notice(db,menu_id=menu_id,proposal_id=m.get('proposal_id',''),meal=m,action='remove',origin=m.get('origin',{})) for m in removed]
        await db.commit()
    await publish(events)
    return {'ok':True}


def _proposal_card_index(proposals):
    """Each new full menu starts a card. Single-meal revisions update that author's card for that day."""
    cards, by_id = {}, {}
    for p in reversed(proposals):  # Query order is newest first; apply revisions oldest first.
        key = p['date'], p['actor']
        parent = by_id.get(p.get('replaces'))
        if parent and (parent['date'], parent['actor']) == key:
            cards[key] = parent
        elif key not in cards or {m['meal'] for m in p['meals']} == {'lunch','dinner'}:
            cards[key] = {}
        card = cards[key]
        meals = {m['meal']:m for m in card.get('meals', [])}
        meals.update({m['meal']:{**m,'source_id':p['id'],'source_revision':p['updated_at']} for m in p['meals']})
        statuses = {**card.get('meal_status', {}), **p['meal_status']}
        selection = {**card.get('selection_at', {}), **p['selection_at']}
        card.update(p, meals=[meals[k] for k in ('lunch','dinner') if k in meals],
                    meal_status=statuses, selection_at=selection)
        card['status'] = next(iter(statuses.values())) if len(set(statuses.values())) == 1 else 'proposed'
        by_id[p['id']] = card
    return by_id


async def _proposal_card(db, row, selected=None):
    rows = await (await db.execute('SELECT * FROM kitchen_proposals WHERE date=? AND actor=? '
                                   'ORDER BY updated_at DESC, rowid DESC', (row['date'],row['actor']))).fetchall()
    proposals = [_menu(r,proposal=True,selected=selected) for r in rows]
    return _proposal_card_index(proposals)[row['id']]


async def _check_proposal_slot(db, row, meal):
    card = await _proposal_card(db, row)
    if not any(m['meal'] == meal and m['source_id'] == row['id'] for m in card['meals']):
        raise MenuChanged('这道菜刚刚换过，刷新后再选一次吧')


async def get_menu(menu_id, *, as_card=False):
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        await cleanup_menus(db)
        row = await (await db.execute('SELECT * FROM kitchen_proposals WHERE id=?', (menu_id,))).fetchone()
        if row:
            chosen = await (await db.execute('SELECT * FROM kitchen_menus WHERE date=?', (row['date'],))).fetchone()
            selected = _menu(chosen) if chosen else None
            if as_card:
                result = await _proposal_card(db,row,selected)
            else:
                result = _menu(row,proposal=True,selected=selected)
        else:
            row = await (await db.execute('SELECT * FROM kitchen_menus WHERE id=?', (menu_id,))).fetchone()
            result = _menu(row) if row else None
        await db.commit()
    return result


async def snapshot(*, as_cards=False):
    current = await state()
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        await cleanup_menus(db)
        items = [_item(r) for r in await (await db.execute('SELECT * FROM kitchen_items ORDER BY soon DESC,updated_at DESC')).fetchall()]
        pending = [dict(r) for r in await (await db.execute('SELECT * FROM kitchen_pending WHERE resolved_at IS NULL ORDER BY created_at')).fetchall()]
        menus = [_menu(r) for r in await (await db.execute('SELECT * FROM kitchen_menus ORDER BY date DESC')).fetchall()]
        by_date = {m['date']:m for m in menus}
        proposals = [_menu(r,proposal=True,selected=by_date.get(r['date'])) for r in await (await db.execute('SELECT * FROM kitchen_proposals ORDER BY updated_at DESC, rowid DESC')).fetchall()]
        if as_cards:
            cards = _proposal_card_index(proposals)
            proposals = [cards[p['id']] for p in proposals if cards[p['id']]['id'] == p['id']]
        errors = [dict(r) for r in await (await db.execute("SELECT source_id,error FROM kitchen_jobs WHERE status='failed' ORDER BY source_ts DESC LIMIT 3")).fetchall()]
        await db.commit()
    return {**current, 'items': items, 'pending': pending, 'menus': menus, 'proposals':proposals, 'errors': errors}


async def notify():
    from ws import manager
    data = await snapshot()
    await manager.broadcast({'type': 'kitchen_changed', 'data': {'enabled': data['enabled'], 'pending_count': len(data['pending'])}})
