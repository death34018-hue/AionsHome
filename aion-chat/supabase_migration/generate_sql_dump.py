"""
Aion Chat SQLite → Supabase SQL Dump 生成器
=============================================
读取 chat.db，生成可直接在 Supabase SQL Editor 中执行的 INSERT 语句。

使用方法：
    python generate_sql_dump.py
    然后打开生成的 dump.sql 文件，粘贴到 Supabase SQL Editor 执行。

不需要安装任何额外依赖（仅内置 sqlite3）。
"""

import os
import sys
import sqlite3
import json
from datetime import datetime, timezone

# SQLite 数据库路径
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "chat.db")

# 输出文件
OUTPUT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dump.sql")

# 要迁移的表（可按需注释）
TABLES_TO_MIGRATE = [
    "conversations",
    "messages",
    "chatroom_rooms",
    "chatroom_messages",
    "memories",
    "chatroom_memories",
    "schedules",
    "heart_whispers",
    "wishes",
    "digest_anchors",
    "chatroom_digest_anchors",
]

# 每行 INSERT 最多包含的行数
ROWS_PER_INSERT = 100


def ts_to_sql(real_ts):
    """SQLite REAL 时间戳 → PostgreSQL TIMESTAMPTZ 字符串"""
    if real_ts is None:
        return "NULL"
    try:
        dt = datetime.fromtimestamp(float(real_ts), tz=timezone.utc)
        return f"'{dt.isoformat()}'"
    except (ValueError, OSError):
        return "NULL"


def escape_sql(value):
    """转义 SQL 字符串值"""
    if value is None:
        return "NULL"
    if isinstance(value, (int, float)):
        if isinstance(value, float) and value != value:  # NaN
            return "NULL"
        return str(value)
    # 字符串：转义单引号
    s = str(value).replace("'", "''")
    # 移除 null 字节（PostgreSQL 不接受）
    s = s.replace("\x00", "")
    return f"'{s}'"


def get_column_value(table: str, col: str, value) -> str:
    """将列值转为 SQL 字面量"""
    # 时间戳列 → TIMESTAMPTZ
    ts_columns = {
        "conversations": {"created_at", "updated_at"},
        "messages": {"created_at", "ai_feedback_created_at", "ai_feedback_updated_at"},
        "chatroom_rooms": {"created_at", "updated_at"},
        "chatroom_messages": {"created_at", "ai_feedback_created_at", "ai_feedback_updated_at"},
        "memories": {"created_at", "source_start_ts", "source_end_ts"},
        "chatroom_memories": {"created_at", "source_start_ts", "source_end_ts"},
        "schedules": {"trigger_at", "created_at"},
        "heart_whispers": {"created_at"},
        "gifts": {"created_at", "received_at"},
        "wishes": {"created_at", "updated_at", "source_start_ts", "source_end_ts",
                    "last_pulled_at", "fulfilled_at", "released_at", "last_mentioned_at"},
        "digest_anchors": {"anchor_ts"},
        "chatroom_digest_anchors": {"anchor_ts"},
    }

    # 跳过 embedding BLOB 列
    skip_cols = {"embedding"}

    if col in skip_cols:
        return None  # 标记跳过

    if col in ts_columns.get(table, set()):
        return ts_to_sql(value)
    else:
        return escape_sql(value)


def generate_inserts(db_path: str, table: str) -> list[str]:
    """为一个表生成 INSERT 语句列表"""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        cursor = conn.execute(f"SELECT * FROM {table}")
        rows = [dict(row) for row in cursor.fetchall()]
    except sqlite3.OperationalError as e:
        print(f"  ⚠ 表 {table} 不存在或读取失败: {e}")
        return []
    finally:
        conn.close()

    if not rows:
        return []

    # 获取列名（排除跳过的列）
    all_cols = list(rows[0].keys())
    cols = [c for c in all_cols if c != "embedding"]
    col_str = ", ".join(f'"{c}"' for c in cols)

    statements = []
    total = len(rows)

    for i in range(0, total, ROWS_PER_INSERT):
        batch = rows[i:i + ROWS_PER_INSERT]
        values_list = []
        for row in batch:
            vals = []
            for col in cols:
                sql_val = get_column_value(table, col, row.get(col))
                if sql_val is not None:
                    vals.append(sql_val)
            if vals:
                values_list.append("(" + ", ".join(vals) + ")")

        if values_list:
            sql = f"INSERT INTO {table} ({col_str}) VALUES\n  " + ",\n  ".join(values_list) + "\nON CONFLICT (id) DO NOTHING;"
            statements.append(sql)

    return statements


def main():
    if not os.path.exists(DB_PATH):
        print(f"❌ 数据库文件不存在: {DB_PATH}")
        sys.exit(1)

    print("=" * 60)
    print("🔧 Aion Chat SQLite → Supabase SQL Dump 生成器")
    print("=" * 60)
    print(f"  数据库: {DB_PATH}")
    print(f"  输出:   {OUTPUT_FILE}")
    print(f"  表:     {', '.join(TABLES_TO_MIGRATE)}")
    print()

    all_sql_lines = [
        "-- ============================================================",
        "-- Aion Chat → Supabase 数据迁移 SQL",
        f"-- 生成时间: {datetime.now().isoformat()}",
        "-- ",
        "-- 使用方法：",
        "--   1. 先在 Supabase SQL Editor 中执行 supabase_schema.sql 建表",
        "--   2. 再执行本文件导入数据",
        "--   3. 如果数据量大，可分批次执行",
        "-- ============================================================",
        "",
        "BEGIN;",
        "",
    ]

    total_rows = 0
    for table in TABLES_TO_MIGRATE:
        print(f"📦 处理表: {table} ...", end=" ")
        statements = generate_inserts(DB_PATH, table)
        if statements:
            all_sql_lines.append(f"-- ──────────────────────────────────────────────")
            all_sql_lines.append(f"-- 表: {table}  ({len(statements)} 条 INSERT)")
            all_sql_lines.append(f"-- ──────────────────────────────────────────────")
            all_sql_lines.append("")
            for stmt in statements:
                all_sql_lines.append(stmt)
                all_sql_lines.append("")
            table_rows = sum(s.count("(") - s.count("ON CONFLICT") for s in statements)  # rough estimate
            print(f"{sum(s.count('(') - 1 for s in statements)} 行")
            total_rows += sum(len(s.split("),\n")) for s in statements)
        else:
            print("空表，跳过")

    all_sql_lines.append("COMMIT;")
    all_sql_lines.append("")
    all_sql_lines.append("-- 迁移完成！")

    # 写入文件
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(all_sql_lines))

    file_size = os.path.getsize(OUTPUT_FILE)
    print(f"\n✅ 生成完成: {OUTPUT_FILE}")
    print(f"   文件大小: {file_size / 1024:.1f} KB")
    print(f"\n下一步：")
    print(f"   1. 打开 {OUTPUT_FILE}")
    print(f"   2. 在 Supabase SQL Editor 中先执行 supabase_schema.sql 建表")
    print(f"   3. 再粘贴 dump.sql 全部内容执行")


if __name__ == "__main__":
    main()
