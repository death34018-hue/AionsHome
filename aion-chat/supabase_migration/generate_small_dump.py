"""
仅导出 chatroom_messages 和 chatroom_memories，分小批，适配 Supabase SQL Editor
"""
import os, sys, sqlite3
from datetime import datetime, timezone

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "chat.db")
OUT_DIR = os.path.dirname(os.path.abspath(__file__))
ROWS_PER_INSERT = 500

def ts_to_sql(real_ts):
    if real_ts is None: return "NULL"
    try:
        dt = datetime.fromtimestamp(float(real_ts), tz=timezone.utc)
        return f"'{dt.isoformat()}'"
    except: return "NULL"

def escape_sql(value):
    if value is None: return "NULL"
    if isinstance(value, (int, float)):
        if isinstance(value, float) and value != value: return "NULL"
        return str(value)
    return "'" + str(value).replace("'", "''").replace("\x00", "") + "'"

TS_COLS = {
    "chatroom_messages": {"created_at", "ai_feedback_created_at", "ai_feedback_updated_at"},
    "chatroom_memories": {"created_at", "source_start_ts", "source_end_ts"},
}

def col_val(table, col, value):
    if col == "embedding": return None
    return ts_to_sql(value) if col in TS_COLS.get(table, set()) else escape_sql(value)

def generate_table_sql(table):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in conn.execute(f"SELECT * FROM {table}")]
    except:
        conn.close(); return []
    conn.close()

    if not rows: return []
    cols = [c for c in rows[0].keys() if c != "embedding"]
    col_str = ", ".join(f'"{c}"' for c in cols)

    stmts = []
    for i in range(0, len(rows), ROWS_PER_INSERT):
        batch = rows[i:i+ROWS_PER_INSERT]
        vals = []
        for row in batch:
            vs = [col_val(table, c, row.get(c)) for c in cols]
            vs = [v for v in vs if v is not None]
            vals.append("(" + ", ".join(vs) + ")")
        stmts.append(f"INSERT INTO {table} ({col_str}) VALUES\n  " + ",\n  ".join(vals) + "\nON CONFLICT (id) DO NOTHING;")
    return stmts

def main():
    if not os.path.exists(DB_PATH):
        print(f"❌ 数据库不存在: {DB_PATH}"); sys.exit(1)

    for table in ["chatroom_messages", "chatroom_memories"]:
        out_path = os.path.join(OUT_DIR, f"dump_{table}.sql")
        stmts = generate_table_sql(table)
        if not stmts:
            print(f"⚠ {table}: 无数据")
            continue

        lines = [
            f"-- {table} 数据迁移",
            f"-- 生成时间: {datetime.now().isoformat()}",
            f"-- 共 {len(stmts)} 条 INSERT 语句",
            "",
            "BEGIN;",
            "",
        ]
        for s in stmts:
            lines.append(s); lines.append("")
        lines.append("COMMIT;")

        with open(out_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))

        kb = os.path.getsize(out_path) / 1024
        print(f"✅ {out_path}  ({kb:.1f} KB)")

    print("\n下一步：在 Supabase SQL Editor 中依次执行 dump_chatroom_messages.sql 和 dump_chatroom_memories.sql")

if __name__ == "__main__":
    main()
