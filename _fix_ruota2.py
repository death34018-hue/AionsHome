import json
cfg_path = r"D:\SJ\AionsHome-main\aion-chat\data\mcp_servers.json"
cfg = json.load(open(cfg_path, "r", encoding="utf-8"))
for s in cfg["servers"]:
    if s["name"] == "Ruota della Fortuna":
        s["command"] = r"C:\Program Files\node.exe"
        break
json.dump(cfg, open(cfg_path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
print("OK")
