import json, sys
sys.path.insert(0, r"D:\SJ\AionsHome-main\aion-chat")
from mcp_client import mcp_manager

# 注册 Ruota della Fortuna（stdio）
srv = mcp_manager.upsert_server(
    "Ruota della Fortuna",
    "stdio",
    "",  # url 不适用
    enabled=True,
)
# 手动写入 command 和 args（upsert_server 不支持这些字段）
cfg_path = r"D:\SJ\AionsHome-main\aion-chat\data\mcp_servers.json"
cfg = json.load(open(cfg_path, "r", encoding="utf-8"))
for s in cfg["servers"]:
    if s["name"] == "Ruota della Fortuna":
        s["command"] = "node"
        s["args"] = [r"D:\安装包\Ruota-della-Fortuna-main\mcp-server.js"]
        break
json.dump(cfg, open(cfg_path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
print("OK - config updated")
