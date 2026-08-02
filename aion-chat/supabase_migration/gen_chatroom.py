"""生成聊天室相关表的独立小 SQL dump"""
import os, sqlite3
from datetime import datetime, timezone

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "chat.db")
OUT = os.path.dirname(os.path.abspath(__file__))

def ts(v):
    if v is None: return 'NULL'
    try: return "'" + datetime.fromtimestamp(float(v), tz=timezone.utc).isoformat() + "'"
    except: return 'NULL'

def esc(v):
    if v is None: return 'NULL'
    if isinstance(v, (int, float)):
        return 'NULL' if (isinstance(v, float) and v != v) else str(v)
    return "'" + str(v).replace("'", "''").replace("\x00", "") + "'"

def dump_table(conn, table, ts_cols, out_path, per=10):
    cur = conn.execute(f"SELECT * FROM {table}")
    rows = [dict(r) for r in cur.fetchall()]
    if not rows: return 0
    cols = [c for c in rows[0].keys() if c != "embedding"]
    col_str = ", ".join(f'"{c}"' for c in cols)
    stmts = []
    for i in range(0, len(rows), per):
        batch = rows[i:i+per]
        vals = []
        for r in batch:
            vv = [ts(r[c]) if c in ts_cols else esc(r[c]) for c in cols]
            vals.append("(" + ", ".join(vv) + ")")
        stmts.append(
            f"INSERT INTO {table} ({col_str}) VALUES\n  "
            + ",\n  ".join(vals)
            + "\nON CONFLICT (id) DO NOTHING;"
        )
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(f"-- {table}\nBEGIN;\n\n")
        f.write("\n\n".join(stmts))
        f.write("\n\nCOMMIT;\n")
    return len(rows)

conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row

for tbl, ts_cols, per in [
    ("chatroom_rooms", {"created_at", "updated_at"}, 50),
    ("chatroom_messages", {"created_at", "ai_feedback_created_at", "ai_feedback_updated_at"}, 10),
    ("chatroom_memories", {"created_at", "source_start_ts", "source_end_ts"}, 10),
]:
    path = os.path.join(OUT, f"dump_{tbl}.sql")
    n = dump_table(conn, tbl, ts_cols, path, per=per)
    size_kb = os.path.getsize(path) / 1024 if n else 0
    print(f"  {tbl}: {n} rows → {path} ({size_kb:.0f} KB)")

conn.close()
print("\nDone! 逐个在 Supabase SQL Editor 中执行即可。")
