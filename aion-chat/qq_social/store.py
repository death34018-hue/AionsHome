"""QQ-only persistence; never writes to the home's database."""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

DEFAULTS = dict(enabled=False, epoch=0, enabled_at=0, mode='respond',
                interval_seconds=120, hourly_limit=6, total_hourly_limit=60,
                context_messages=35, image_wait_seconds=60, bring_home=True,
                bind_first_group=False, bound_actors=[])
ACTOR_FIELDS = ('enabled','mode','interval_seconds','hourly_limit','total_hourly_limit',
                'context_messages','image_wait_seconds')


def apply_settings(current, patch):
    if not isinstance(patch,dict):
        raise ValueError('settings must be an object')
    for key in ('enabled', 'bind_first_group', 'bring_home'):
        if key in patch:
            if type(patch[key]) is not bool:
                raise ValueError(f'{key} must be boolean')
            current[key] = patch[key]
    if 'mode' in patch:
        if patch['mode'] not in ('respond', 'natural'):
            raise ValueError('invalid mode')
        current['mode'] = patch['mode']
    for key, low, high in [('interval_seconds',10,3600),('hourly_limit',1,120),('total_hourly_limit',5,120),
                           ('context_messages',5,100),('image_wait_seconds',0,120)]:
        if key in patch:
            value = int(patch[key])
            if not low <= value <= high:
                raise ValueError(f'{key} outside {low}..{high}')
            current[key] = value
    current['epoch'] += 1
    current['enabled_at'] = time.time()
    return current


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS groups(
                    id INTEGER PRIMARY KEY, actor TEXT NOT NULL, group_id TEXT NOT NULL,
                    name TEXT NOT NULL DEFAULT '', enabled INTEGER NOT NULL DEFAULT 0,
                    memory_enabled INTEGER NOT NULL DEFAULT 1, full_messages INTEGER DEFAULT 0,
                    summary TEXT DEFAULT '', digest_cursor INTEGER DEFAULT 0,
                    UNIQUE(actor, group_id));
                CREATE TABLE IF NOT EXISTS messages(
                    id INTEGER PRIMARY KEY, actor TEXT, group_id TEXT, message_id TEXT,
                    member_id TEXT, name TEXT, content TEXT, is_bot INTEGER, direction TEXT,
                    created_at REAL, ref_id TEXT DEFAULT '', UNIQUE(actor,group_id,message_id,direction));
                CREATE INDEX IF NOT EXISTS qq_history ON messages(actor,group_id,id);
                CREATE TABLE IF NOT EXISTS members(
                    actor TEXT, group_id TEXT, member_id TEXT, name TEXT, alias TEXT DEFAULT '',
                    is_owner INTEGER DEFAULT 0, PRIMARY KEY(actor,group_id,member_id));
                CREATE TABLE IF NOT EXISTS memories(
                    id INTEGER PRIMARY KEY, actor TEXT, group_id TEXT, member_id TEXT,
                    content TEXT, sources TEXT, created_at REAL,
                    UNIQUE(actor,group_id,member_id,content));
            ''')
            columns = {r[1] for r in db.execute('PRAGMA table_info(messages)')}
            if 'attachments' not in columns:
                db.execute("ALTER TABLE messages ADD COLUMN attachments TEXT NOT NULL DEFAULT '[]'")
            columns = {r[1] for r in db.execute('PRAGMA table_info(memories)')}
            if 'archive_reason' not in columns:
                db.execute("ALTER TABLE memories ADD COLUMN archive_reason TEXT NOT NULL DEFAULT ''")

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=3)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def settings(self, actor=None):
        with self.db() as db:
            row = db.execute("SELECT value FROM settings WHERE key='config'").fetchone()
        current = {**DEFAULTS, **(json.loads(row[0]) if row else {})}
        if actor is None:
            return current
        own = self.actor_settings(actor, current)
        return {**current, **own, 'enabled':current['enabled'] and own['enabled'],
                'epoch':current['epoch']+own['epoch'],
                'enabled_at':max(current['enabled_at'],own['enabled_at'])}

    def actor_settings(self, actor, base=None):
        if actor not in ('aion','connor'):
            raise ValueError('unknown actor')
        base = self.settings() if base is None else base
        with self.db() as db:
            row = db.execute('SELECT value FROM settings WHERE key=?',('actor:'+actor,)).fetchone()
        # Existing installations inherit their old shared values until individually edited.
        return {**{key:base[key] for key in ACTOR_FIELDS},'enabled':True,'epoch':0,'enabled_at':0,'model':'',
                **(json.loads(row[0]) if row else {})}

    def configure_actor(self, actor, patch):
        if not isinstance(patch,dict) or any(key not in (*ACTOR_FIELDS,'model') for key in patch):
            raise ValueError('invalid actor settings')
        current = apply_settings(self.actor_settings(actor),patch)
        if 'model' in patch:
            if not isinstance(patch['model'],str):
                raise ValueError('model must be text')
            current['model'] = patch['model'].strip()
        with self.db() as db:
            db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',('actor:'+actor,json.dumps(current)))
        return current

    def configure(self, patch):
        current = apply_settings(self.settings(),patch)
        with self.db() as db:
            db.execute("INSERT OR REPLACE INTO settings VALUES ('config',?)", (json.dumps(current),))
        return current

    def discover(self, actor, group_id):
        with self.db() as db:
            row = db.execute('SELECT * FROM groups WHERE actor=? AND group_id=?', (actor, group_id)).fetchone()
            if row:
                return dict(row)
            cfg = self.settings()
            bind = cfg['bind_first_group'] and actor not in cfg['bound_actors']
            db.execute('INSERT INTO groups(actor,group_id,enabled) VALUES(?,?,?)', (actor, group_id, int(bind)))
            if bind:
                cfg['bound_actors'] = [*cfg['bound_actors'], actor]
                db.execute("INSERT OR REPLACE INTO settings VALUES ('config',?)", (json.dumps(cfg),))
            return dict(db.execute('SELECT * FROM groups WHERE actor=? AND group_id=?', (actor, group_id)).fetchone())

    def groups(self):
        with self.db() as db:
            return [dict(r) for r in db.execute('SELECT * FROM groups ORDER BY id')]

    def group(self, actor, group_id):
        with self.db() as db:
            row = db.execute('SELECT * FROM groups WHERE actor=? AND group_id=?', (actor, group_id)).fetchone()
            return dict(row) if row else None

    def update_group(self, row_id, patch):
        allowed = {k: v for k, v in patch.items() if k in ('enabled', 'memory_enabled', 'name')}
        for key in ('enabled', 'memory_enabled'):
            if key in allowed and type(allowed[key]) is not bool:
                raise ValueError('switch must be boolean')
        if 'name' in allowed:
            allowed['name'] = str(allowed['name'])[:80]
        with self.db() as db:
            for key, value in allowed.items():
                db.execute(f'UPDATE groups SET {key}=? WHERE id=?', (value, row_id))
        self.configure({})  # invalidate work already queued for any changed group

    def full_messages(self, actor, group_id):
        with self.db() as db:
            db.execute('UPDATE groups SET full_messages=1 WHERE actor=? AND group_id=?', (actor, group_id))

    def record(self, row):
        row = dict(row)
        row['attachments'] = json.dumps(row.get('attachments') or [],ensure_ascii=False)
        keys = ('actor','group_id','message_id','member_id','name','content','is_bot','direction','created_at','ref_id','attachments')
        with self.db() as db:
            cur = db.execute(f"INSERT OR IGNORE INTO messages({','.join(keys)}) VALUES({','.join('?' for _ in keys)})", tuple(row.get(k, '') for k in keys))
            if row['direction'] == 'in':
                db.execute('INSERT INTO members(actor,group_id,member_id,name) VALUES(?,?,?,?) ON CONFLICT(actor,group_id,member_id) DO UPDATE SET name=excluded.name', tuple(row[k] for k in ('actor','group_id','member_id','name')))
            return bool(cur.rowcount)

    def history(self, actor, group_id, limit=40, after=0, oldest=False):
        with self.db() as db:
            order = 'ASC' if oldest else 'DESC'
            rows = db.execute(f'SELECT * FROM messages WHERE actor=? AND group_id=? AND id>? ORDER BY id {order} LIMIT ?', (actor, group_id, after, limit)).fetchall()
            return [dict(r) for r in (rows if oldest else reversed(rows))]

    def members(self, actor, group_id):
        with self.db() as db:
            return [dict(r) for r in db.execute('SELECT * FROM members WHERE actor=? AND group_id=?', (actor, group_id))]

    def set_member(self, actor, group_id, member_id, alias, is_owner=False):
        with self.db() as db:
            if is_owner:
                db.execute('UPDATE members SET is_owner=0 WHERE actor=? AND group_id=?', (actor, group_id))
            db.execute('UPDATE members SET alias=?,is_owner=? WHERE actor=? AND group_id=? AND member_id=?', (str(alias)[:80], bool(is_owner), actor, group_id, member_id))

    def save_memory(self, actor, group_id, member_id, content, sources, merge_ids=()):
        """Merge only this member's facts; retain original rows and every source."""
        content = content.strip()
        if not content or len(content) > 800:
            raise ValueError('Memory must contain 1..800 characters')
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            originals = []
            for memory_id in dict.fromkeys(merge_ids):
                row = db.execute("SELECT * FROM memories WHERE id=? AND actor=? AND group_id=? AND member_id=? AND archive_reason=''",
                                 (memory_id,actor,group_id,member_id)).fetchone()
                if row is None:
                    raise ValueError('Merge target is not an active memory of this member')
                originals.append(row)
            existing = db.execute('SELECT * FROM memories WHERE actor=? AND group_id=? AND member_id=? AND content=?',
                                  (actor,group_id,member_id,content)).fetchone()
            evidence = []
            for row in originals + ([existing] if existing else []):
                evidence.extend(json.loads(row['sources']))
            evidence = json.dumps(list(dict.fromkeys(evidence + list(sources))),ensure_ascii=False)
            if existing:
                memory_id = existing['id']
                db.execute("UPDATE memories SET sources=?,archive_reason='' WHERE id=?",(evidence,memory_id))
            else:
                memory_id = db.execute('INSERT INTO memories(actor,group_id,member_id,content,sources,created_at) VALUES(?,?,?,?,?,?)',
                                       (actor,group_id,member_id,content,evidence,time.time())).lastrowid
            for row in originals:
                if row['id'] != memory_id:
                    db.execute('UPDATE memories SET archive_reason=? WHERE id=?',(f'merged:{memory_id}',row['id']))
            return memory_id

    def memories(self, actor, group_id, limit=100):
        with self.db() as db:
            return [dict(r) for r in db.execute("SELECT * FROM memories WHERE actor=? AND group_id=? AND archive_reason='' ORDER BY id DESC LIMIT ?", (actor,group_id,limit))]

    def member_memories(self, actor, group_id, member_id):
        with self.db() as db:
            return [dict(r) for r in db.execute("SELECT id,member_id,content FROM memories WHERE actor=? AND group_id=? AND member_id=? AND archive_reason='' ORDER BY id DESC LIMIT 40",
                                                (actor,group_id,member_id))]

    def delete_memory(self, memory_id):
        with self.db() as db:
            db.execute('DELETE FROM memories WHERE id=?', (memory_id,))

    def digest(self, actor, group_id, summary, cursor):
        with self.db() as db:
            db.execute('UPDATE groups SET summary=?,digest_cursor=? WHERE actor=? AND group_id=?', (summary[:3000],cursor,actor,group_id))

