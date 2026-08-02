# Supabase 迁移指南

将 Aion Chat 的 SQLite 数据库迁移到 Supabase (PostgreSQL)。

## 文件说明

```
supabase_migration/
├── supabase_schema.sql      # Supabase 建表 SQL（在 SQL Editor 中执行）
├── migrate_to_supabase.py   # Python 数据迁移脚本
└── README.md                # 本文件
```

## 第一步：Supabase 端建表

1. 登录 [Supabase Dashboard](https://supabase.com/dashboard)
2. 进入你的项目 → **SQL Editor**
3. 点击 **New query**
4. 将 `supabase_schema.sql` 的**全部内容**粘贴进去
5. 点击 **Run** 执行

执行成功后，左侧 **Table Editor** 中应能看到以下表：
- `conversations` / `messages` — Aion 私聊
- `chatroom_rooms` / `chatroom_messages` — Connor 聊天室 / 群聊
- `memories` / `chatroom_memories` — 记忆库
- `schedules` — 日程/闹铃
- `heart_whispers` — 心语
- `gifts` — 礼物
- `wishes` — 许愿池

## 第二步：获取 API 密钥

1. Supabase Dashboard → **Settings** → **API**
2. 复制以下两个值：
   - **Project URL**（形如 `https://xxxxxxxxxxxx.supabase.co`）
   - **service_role key**（以 `eyJhbGci...` 开头，**不是** anon key）

> ⚠️ `service_role key` 绕过 RLS，用于服务端批量写入。不要泄露到前端代码。

## 第三步：安装依赖

```bash
cd aion-chat
pip install supabase
```

## 第四步：运行迁移

```powershell
# 方式一：环境变量（推荐）
$env:SUPABASE_URL="https://xxxxxxxxxxxx.supabase.co"
$env:SUPABASE_SERVICE_KEY="eyJhbGci..."
cd supabase_migration
python migrate_to_supabase.py

# 方式二：直接修改脚本中的 SUPABASE_URL 和 SUPABASE_SERVICE_KEY
```

## 第五步：验证

在 Supabase **Table Editor** 中检查各表数据是否正确导入。

## 自定义迁移范围

编辑 `migrate_to_supabase.py` 中的 `TABLES_TO_MIGRATE` 列表，注释掉不需要的表：

```python
TABLES_TO_MIGRATE = [
    "conversations",
    "messages",
    # "chatroom_rooms",      # ← 注释掉不迁移
    # "chatroom_messages",
    ...
]
```

## 注意事项

| 问题 | 说明 |
|------|------|
| **Embedding 向量** | `memories` 和 `chatroom_memories` 的 `embedding` BLOB 列不会迁移（Supabase 中通常用 pgvector 扩展单独管理） |
| **本地图片** | `gifts` 表的 `image_path` 指向本地文件，导入后图片无法显示。礼物记录本身可以迁移，但图片需要单独上传到 Supabase Storage |
| **重复执行** | 脚本会跳过已存在的主键（duplicate key），可以安全重复执行 |
| **大表分批** | 消息表可能很大，脚本默认每 500 条一批插入，避免触发 API 限流 |
| **RLS** | 建表 SQL 默认未启用 RLS。如需多用户访问，请自行配置 Row Level Security 策略 |
