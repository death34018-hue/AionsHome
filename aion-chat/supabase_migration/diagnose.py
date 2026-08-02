import re

with open(r'D:\SJ\AionsHome-main\aion-chat\supabase_migration\dump_chatroom_messages.sql', 'r', encoding='utf-8') as f:
    sql = f.read()

# Count message IDs
ids = re.findall(r"'cm_\d+_[cu]'", sql)
print(f"Total cm_ ids found: {len(ids)}")

# Count unique room IDs
rooms = set(re.findall(r"'cr_\d+'", sql))
print(f"Unique rooms: {len(rooms)}: {rooms}")

# Count '(' at line starts in VALUES block
match = re.search(r'VALUES\s*\n(.*?)(?:ON CONFLICT|COMMIT)', sql, re.DOTALL)
if match:
    block = match.group(1)
    lines = block.split('\n')
    row_lines = [l for l in lines if l.strip().startswith("('cm_")]
    print(f"Lines starting with ('cm_ in VALUES block: {len(row_lines)}")
    
    # Show first few and last few
    for l in row_lines[:3]:
        print(f"  FIRST: {l[:80]}...")
    for l in row_lines[-3:]:
        print(f"  LAST:  {l[:80]}...")
