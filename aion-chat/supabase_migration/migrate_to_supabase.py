"""
Aion Chat SQLite → Supabase 数据迁移脚本
===========================================
将 chat.db 中的所有聊天数据导入 Supabase PostgreSQL。

使用方法：
    1. 先在 Supabase SQL Editor 中执行 supabase_schema.sql 建表
    2. 在 Supabase Dashboard → Settings → API 获取 URL 和 Service Role Key
    3. 设置环境变量或直接修改下方配置
    4. python migrate_to_supabase.py

依赖：pip install supabase sqlite3
"""

import os
import sys
import sqlite3
import json
import time
from datetime import datetime, timezone

# ============================================================
# 配置区 — 修改为你的 Supabase 信息
# ============================================================
SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://xxxxxxxxxxxx.supabase.co")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "eyJhbGciOi...")  # service_role key（不是 anon key！）

# SQLite 数据库路径
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "chat.db")

# 要迁移的表（可按需注释掉不需要的）
TABLES_TO_MIGRATE = [
    "conversations",
    "messages",
    "chatroom_rooms",
    "chatroom_messages",
    "memories",
    "chatroom_memories",
    "schedules",
    "heart_whispers",
    # "gifts",        # 礼物包含本地图片路径，导入后图片无法显示，按需开启
    # "wishes",        # 许愿池
    "digest_anchors",
    "chatroom_digest_anchors",
]

# 每批插入的行数（Supabase REST API 单次请求限制）
BATCH_SIZE = 500


# ============================================================
# 迁移逻辑
# ============================================================

def ts_to_iso(real_ts):
    """将 SQLite REAL 时间戳转为 ISO 8601 字符串"""
    if real_ts is None:
        return None
    try:
        dt = datetime.fromtimestamp(float(real_ts), tz=timezone.utc)
        return dt.isoformat()
    except (ValueError, OSError):
        return None


def read_sqlite_table(db_path: str, table: str) -> list[dict]:
    """读取 SQLite 表全部数据，返回 dict 列表"""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        cursor = conn.execute(f"SELECT * FROM {table}")
        rows = [dict(row) for row in cursor.fetchall()]
    except sqlite3.OperationalError as e:
        print(f"  ⚠ 表 {table} 不存在或读取失败: {e}")
        rows = []
    finally:
        conn.close()
    return rows


def transform_row(table: str, row: dict) -> dict:
    """转换行数据：时间戳 → ISO 字符串，移除不需要的列"""
    # 需要将 REAL 时间戳转为 ISO 字符串的列
    ts_columns = {
        "conversations": ["created_at", "updated_at"],
        "messages": ["created_at", "ai_feedback_created_at", "ai_feedback_updated_at"],
        "chatroom_rooms": ["created_at", "updated_at"],
        "chatroom_messages": ["created_at", "ai_feedback_created_at", "ai_feedback_updated_at"],
        "memories": ["created_at", "source_start_ts", "source_end_ts"],
        "chatroom_memories": ["created_at", "source_start_ts", "source_end_ts"],
        "schedules": ["trigger_at", "created_at"],
        "heart_whispers": ["created_at"],
        "gifts": ["created_at", "received_at"],
        "wishes": ["created_at", "updated_at", "source_start_ts", "source_end_ts",
                    "last_pulled_at", "fulfilled_at", "released_at"],
        "digest_anchors": ["anchor_ts"],
        "chatroom_digest_anchors": ["anchor_ts"],
    }

    # SQLite 专有列（不需要导入）
    skip_columns = {
        "memories": ["embedding"],           # BLOB 向量数据，Supabase 中通常不需要
        "chatroom_memories": ["embedding"],  # BLOB 向量数据
    }

    result = {}
    for key, value in row.items():
        # 跳过不需要的列
        if key in skip_columns.get(table, []):
            continue

        # 时间戳转换
        if key in ts_columns.get(table, []):
            result[key] = ts_to_iso(value)
        else:
            result[key] = value

    return result


def insert_batch(supabase, table: str, rows: list[dict]) -> int:
    """批量插入行到 Supabase，返回成功插入数"""
    if not rows:
        return 0

    try:
        # supabase-py 的 insert 会自动处理 JSON
        result = supabase.table(table).insert(rows).execute()
        return len(result.data) if result.data else 0
    except Exception as e:
        # 尝试逐行插入（排查问题行）
        success = 0
        for row in rows:
            try:
                supabase.table(table).insert(row).execute()
                success += 1
            except Exception as inner_e:
                # 跳过重复主键
                err_str = str(inner_e).lower()
                if "duplicate" in err_str or "unique" in err_str or "23505" in err_str:
                    pass  # 已存在，跳过
                else:
                    print(f"    ✗ 插入失败 (id={row.get('id', '?')[:20]}): {str(inner_e)[:120]}")
        return success


def migrate_table(supabase, db_path: str, table: str):
    """迁移单个表"""
    print(f"\n{'='*60}")
    print(f"📦 迁移表: {table}")
    print(f"{'='*60}")

    rows = read_sqlite_table(db_path, table)
    if not rows:
        print(f"  → 表中无数据，跳过")
        return 0, 0

    # 转换数据
    transformed = [transform_row(table, row) for row in rows]
    total = len(transformed)
    print(f"  → 读取到 {total} 条记录")

    # 分批插入
    inserted = 0
    for i in range(0, total, BATCH_SIZE):
        batch = transformed[i:i + BATCH_SIZE]
        n = insert_batch(supabase, table, batch)
        inserted += n
        print(f"  → 已插入 {inserted}/{total} 条", end="\r")
        if i + BATCH_SIZE < total:
            time.sleep(0.1)  # 避免触发限流

    print(f"  → 完成！成功插入 {inserted}/{total} 条")
    return total, inserted


def main():
    # 检查数据库
    if not os.path.exists(DB_PATH):
        print(f"❌ 数据库文件不存在: {DB_PATH}")
        print("   请确保在 aion-chat/supabase_migration/ 目录下运行，或修改 DB_PATH")
        sys.exit(1)

    # 检查配置
    if "xxxxxxxxxxxx" in SUPABASE_URL or "eyJhbGciOi..." in SUPABASE_SERVICE_KEY:
        print("=" * 60)
        print("⚠️  请先配置 Supabase 连接信息！")
        print("=" * 60)
        print()
        print("1. 打开 Supabase Dashboard → Settings → API")
        print("2. 复制 Project URL 和 service_role key")
        print("3. 设置环境变量：")
        print('   $env:SUPABASE_URL="https://xxxxx.supabase.co"')
        print('   $env:SUPABASE_SERVICE_KEY="eyJhbGci..."')
        print()
        print("或者直接修改本脚本顶部的 SUPABASE_URL / SUPABASE_SERVICE_KEY")
        sys.exit(1)

    # 导入 supabase
    try:
        from supabase import create_client, Client
    except ImportError:
        print("❌ 请先安装 supabase-py: pip install supabase")
        sys.exit(1)

    print("=" * 60)
    print("🚀 Aion Chat → Supabase 数据迁移")
    print("=" * 60)
    print(f"   数据库: {DB_PATH}")
    print(f"   Supabase: {SUPABASE_URL}")
    print(f"   待迁移表: {', '.join(TABLES_TO_MIGRATE)}")
    print()

    # 创建 Supabase 客户端
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

    # 逐表迁移
    results = {}
    for table in TABLES_TO_MIGRATE:
        total, inserted = migrate_table(supabase, DB_PATH, table)
        results[table] = {"total": total, "inserted": inserted}

    # 汇总
    print("\n" + "=" * 60)
    print("📊 迁移完成！汇总：")
    print("=" * 60)
    grand_total = 0
    grand_inserted = 0
    for table, stats in results.items():
        print(f"  {table:30s} → {stats['inserted']:>6d} / {stats['total']:>6d} 条")
        grand_total += stats["total"]
        grand_inserted += stats["inserted"]
    print(f"  {'─'*42}")
    print(f"  {'合计':30s} → {grand_inserted:>6d} / {grand_total:>6d} 条")
    print()


if __name__ == "__main__":
    main()
