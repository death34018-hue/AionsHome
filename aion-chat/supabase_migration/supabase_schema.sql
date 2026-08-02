-- ============================================================
-- Aion Chat → Supabase 迁移：建表 SQL
-- 在 Supabase SQL Editor 中执行此文件
-- ============================================================

-- 1. 私聊对话表（Aion 私聊）
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    model TEXT NOT NULL DEFAULT 'gemini-3-flash',
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

-- 2. 私聊消息表
CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    conv_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'system', 'cam_user', 'cam_log', 'cam_trigger')),
    content TEXT NOT NULL,
    attachments TEXT DEFAULT '[]',
    starred INTEGER DEFAULT 0,
    reasoning_content TEXT DEFAULT '',
    ai_feedback_rating TEXT DEFAULT '',
    ai_feedback_reason TEXT DEFAULT '',
    ai_feedback_created_at TIMESTAMPTZ,
    ai_feedback_updated_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_conv_id ON messages(conv_id, created_at);
CREATE INDEX IF NOT EXISTS idx_messages_created ON messages(created_at DESC);

-- 3. 聊天室房间表
CREATE TABLE IF NOT EXISTS chatroom_rooms (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    type TEXT NOT NULL DEFAULT 'group' CHECK (type IN ('group', 'connor_1v1')),
    aion_persona TEXT DEFAULT '',
    connor_persona TEXT DEFAULT '',
    context_minutes INTEGER DEFAULT 30,
    ai_chat_rounds INTEGER DEFAULT 3,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

-- 4. 聊天室消息表（Connor 私聊 + 群聊）
CREATE TABLE IF NOT EXISTS chatroom_messages (
    id TEXT PRIMARY KEY,
    room_id TEXT NOT NULL REFERENCES chatroom_rooms(id) ON DELETE CASCADE,
    sender TEXT NOT NULL,
    content TEXT NOT NULL,
    attachments TEXT DEFAULT '[]',
    reasoning_content TEXT DEFAULT '',
    ai_feedback_rating TEXT DEFAULT '',
    ai_feedback_reason TEXT DEFAULT '',
    ai_feedback_created_at TIMESTAMPTZ,
    ai_feedback_updated_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chatroom_msg_room ON chatroom_messages(room_id, created_at);
CREATE INDEX IF NOT EXISTS idx_chatroom_msg_created ON chatroom_messages(created_at DESC);

-- 5. 记忆库表（Aion）
CREATE TABLE IF NOT EXISTS memories (
    id TEXT PRIMARY KEY,
    content TEXT NOT NULL,
    type TEXT DEFAULT 'event',
    keywords TEXT DEFAULT '',
    importance REAL DEFAULT 0.5,
    source_start_ts TIMESTAMPTZ,
    source_end_ts TIMESTAMPTZ,
    unresolved INTEGER DEFAULT 0,
    source_msg_id TEXT,
    compression_stage INTEGER DEFAULT 0,
    evidence_summary TEXT DEFAULT '',
    evidence_detail_level TEXT DEFAULT 'summary',
    persona TEXT DEFAULT 'aion',
    source_conv TEXT,
    created_at TIMESTAMPTZ NOT NULL
);

-- 6. 聊天室记忆表（Connor / 群聊）
CREATE TABLE IF NOT EXISTS chatroom_memories (
    id TEXT PRIMARY KEY,
    room_id TEXT NOT NULL,
    scope TEXT DEFAULT 'group',
    content TEXT NOT NULL,
    keywords TEXT DEFAULT '',
    importance REAL DEFAULT 0.5,
    source_start_ts TIMESTAMPTZ,
    source_end_ts TIMESTAMPTZ,
    unresolved INTEGER DEFAULT 0,
    source_msg_id TEXT,
    memory_kind TEXT DEFAULT 'long_term',
    compression_stage INTEGER DEFAULT 0,
    evidence_summary TEXT DEFAULT '',
    evidence_detail_level TEXT DEFAULT 'summary',
    created_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chatroom_mem_room ON chatroom_memories(room_id, created_at);

-- 7. 日程/闹铃表
CREATE TABLE IF NOT EXISTS schedules (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL CHECK (type IN ('alarm', 'reminder', 'monitor')),
    trigger_at TIMESTAMPTZ NOT NULL,
    content TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    origin TEXT DEFAULT 'aion',
    origin_room_id TEXT DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL
);

-- 8. 心语表（AI 秘密日记）
CREATE TABLE IF NOT EXISTS heart_whispers (
    id TEXT PRIMARY KEY,
    conv_id TEXT,
    msg_id TEXT,
    content TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
);

-- 9. 礼物表
CREATE TABLE IF NOT EXISTS gifts (
    id TEXT PRIMARY KEY,
    image_path TEXT NOT NULL,
    message TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'received')),
    created_at TIMESTAMPTZ NOT NULL,
    received_at TIMESTAMPTZ
);

-- 10. 许愿池表
CREATE TABLE IF NOT EXISTS wishes (
    id TEXT PRIMARY KEY,
    author TEXT NOT NULL,
    author_name TEXT DEFAULT '',
    content TEXT NOT NULL,
    category TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active',
    visibility TEXT DEFAULT 'shared',
    origin TEXT DEFAULT 'manual',
    source_type TEXT DEFAULT '',
    source_ref TEXT DEFAULT '',
    source_start_ts TIMESTAMPTZ,
    source_end_ts TIMESTAMPTZ,
    pulled_count INTEGER NOT NULL DEFAULT 0,
    last_pulled_at TIMESTAMPTZ,
    fulfilled_at TIMESTAMPTZ,
    released_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

-- 11. 总结锚点表
CREATE TABLE IF NOT EXISTS digest_anchors (
    id TEXT PRIMARY KEY,
    anchor_ts TIMESTAMPTZ NOT NULL DEFAULT '1970-01-01'
);

-- 12. 聊天室总结锚点表
CREATE TABLE IF NOT EXISTS chatroom_digest_anchors (
    room_id TEXT PRIMARY KEY,
    anchor_ts TIMESTAMPTZ NOT NULL DEFAULT '1970-01-01'
);

-- ============================================================
-- 启用 Row Level Security（推荐）
-- ============================================================
-- 如果只需要单人使用，可以跳过 RLS 或使用简单的策略

-- 示例：允许 anon 角色读取（公开访问）
-- ALTER TABLE conversations ENABLE ROW LEVEL SECURITY;
-- CREATE POLICY "Allow all" ON conversations FOR ALL USING (true);
