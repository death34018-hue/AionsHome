#!/usr/bin/env python3
"""将 dump_chatroom_messages.sql 转换为 JSON 格式"""

import re
import json
import os

SQL_FILE = os.path.join(os.path.dirname(__file__), "dump_chatroom_messages.sql")
JSON_FILE = os.path.join(os.path.dirname(__file__), "dump_chatroom_messages.json")

COLUMNS = [
    "id", "room_id", "sender", "content", "attachments",
    "created_at", "ai_feedback_rating", "ai_feedback_reason",
    "ai_feedback_created_at", "ai_feedback_updated_at", "reasoning_content"
]

def parse_sql_values(sql_text: str) -> list[dict]:
    """解析 PostgreSQL INSERT VALUES 为 dict 列表（支持多个 INSERT 块）"""
    
    rows = []
    
    # 匹配所有 VALUES ... ON CONFLICT 块
    for match in re.finditer(r'VALUES\s*\n(.*?)ON CONFLICT', sql_text, re.DOTALL):
        values_block = match.group(1)
        block_rows = _parse_values_block(values_block)
        rows.extend(block_rows)
        print(f"  块解析: {len(block_rows)} 条")
    
    if not rows:
        raise ValueError("未找到任何 VALUES 子句")
    
    return rows


def _parse_values_block(values_block: str) -> list[dict]:
    """解析单个 VALUES 块"""
    rows = []
    i = 0
    n = len(values_block)
    
    while i < n:
        # 跳过空白和逗号
        while i < n and values_block[i] in ' \t\n\r':
            i += 1
        if i >= n:
            break
        if values_block[i] == ',':
            i += 1
            continue
        if values_block[i] == ';':
            break
        
        # 必须以 ( 开始一行
        if values_block[i] != '(':
            # 可能是注释行，跳过
            while i < n and values_block[i] != '\n':
                i += 1
            continue
        
        # 解析一行
        row, i = _parse_row(values_block, i)
        if row is not None:
            rows.append(dict(zip(COLUMNS, row)))
    
    return rows


def _parse_row(text: str, start: int) -> tuple[list | None, int]:
    """从 ( 开始解析一行值，返回 (值列表, 新位置)"""
    i = start
    assert text[i] == '('
    i += 1  # 跳过 (
    
    values = []
    
    for col_idx in range(len(COLUMNS)):
        # 跳过空白
        while i < len(text) and text[i] in ' \t\n\r':
            i += 1
        
        if i >= len(text):
            break
        
        c = text[i]
        
        if c == '\'':
            # 字符串值
            val, i = _parse_string(text, i)
            values.append(val)
        elif c in '0123456789.-':
            # 数字
            val, i = _parse_number_or_null(text, i)
            values.append(val)
        elif text[i:i+4].upper() == 'NULL':
            values.append(None)
            i += 4
        elif c == ')':
            # 提前结束（不应该发生）
            break
        else:
            # 可能是 NULL 的其他写法
            val, i = _parse_number_or_null(text, i)
            values.append(val)
        
        # 跳过空白，检查逗号或 )
        while i < len(text) and text[i] in ' \t\n\r':
            i += 1
        
        if i < len(text) and text[i] == ',' and col_idx < len(COLUMNS) - 1:
            i += 1  # 跳过列间逗号
        elif i < len(text) and text[i] == ')':
            i += 1  # 跳过行结束 )
            break
    
    return values, i


def _parse_string(text: str, start: int) -> tuple[str, int]:
    """解析 PostgreSQL 字符串字面量（支持 '' 转义和换行）"""
    i = start
    assert text[i] == '\''
    i += 1
    
    chars = []
    while i < len(text):
        if text[i] == '\'':
            if i + 1 < len(text) and text[i + 1] == '\'':
                # 转义的单引号 ''
                chars.append('\'')
                i += 2
            else:
                # 字符串结束
                i += 1
                break
        else:
            chars.append(text[i])
            i += 1
    
    return ''.join(chars), i


def _parse_number_or_null(text: str, start: int):
    """解析数字或 NULL"""
    i = start
    while i < len(text) and text[i] not in ',)\n\r\t ':
        i += 1
    token = text[start:i].strip().upper()
    if token == 'NULL':
        return None, i
    try:
        return int(token), i
    except ValueError:
        try:
            return float(token), i
        except ValueError:
            return token, i


def main():
    print(f"读取 {SQL_FILE} ...")
    with open(SQL_FILE, 'r', encoding='utf-8') as f:
        sql_text = f.read()
    
    print("解析 SQL ...")
    rows = parse_sql_values(sql_text)
    
    print(f"共解析 {len(rows)} 条消息")
    
    # 按 room_id 分组
    rooms = {}
    for row in rows:
        rid = row['room_id']
        if rid not in rooms:
            rooms[rid] = []
        rooms[rid].append(row)
    
    output = {
        "total_messages": len(rows),
        "room_count": len(rooms),
        "rooms": {rid: {"message_count": len(msgs), "messages": msgs} for rid, msgs in rooms.items()}
    }
    
    print(f"写入 {JSON_FILE} ...")
    with open(JSON_FILE, 'w', encoding='utf-8') as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    
    print("完成！")
    print(f"\n房间列表：")
    for rid, msgs in rooms.items():
        print(f"  {rid}: {len(msgs)} 条消息")


if __name__ == '__main__':
    main()
