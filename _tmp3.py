import asyncio, sys
sys.path.insert(0, r"D:\SJ\AionsHome-main\aion-chat")
from mcp_client import mcp_manager

async def main():
    try:
        await mcp_manager.disconnect("Ruota della Fortuna")
    except:
        pass
    tools = await mcp_manager.connect("Ruota della Fortuna")
    print(f"{len(tools)} tools:")
    for t in tools:
        print(f"  - {t['name']}: {(t.get('description') or '')[:80]}")
    result = await mcp_manager.call_tool("Ruota della Fortuna", "ero_slot_spin", {})
    for item in result:
        if item.get("type") == "text":
            print("\nSpin:", item["text"][:500])
    await mcp_manager.disconnect("Ruota della Fortuna")
    print("Done!")

asyncio.run(main())
