import asyncio, sys
sys.path.insert(0, r"D:\SJ\AionsHome-main\aion-chat")
from mcp_client import mcp_manager

async def main():
    tools = await mcp_manager.connect("Ruota della Fortuna")
    print(f"OK, {len(tools)} tools:")
    for t in tools:
        print(f"  - {t['name']}: {(t.get('description') or '')[:80]}")
    result = await mcp_manager.call_tool("Ruota della Fortuna", "ero_slot_dimensions", {})
    for item in result:
        if item.get("type") == "text":
            print("\nDimensions:", item["text"][:500])
    result2 = await mcp_manager.call_tool("Ruota della Fortuna", "ero_slot_spin", {})
    for item in result2:
        if item.get("type") == "text":
            print("\nSpin:", item["text"][:500])
    await mcp_manager.disconnect("Ruota della Fortuna")

asyncio.run(main())
