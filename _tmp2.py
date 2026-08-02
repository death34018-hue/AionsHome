import asyncio, sys, time
sys.path.insert(0, r"D:\SJ\AionsHome-main\aion-chat")
from mcp_client import mcp_manager

async def main():
    print("Starting...", flush=True)
    try:
        # disconnect old if any
        try:
            await mcp_manager.disconnect("Ruota della Fortuna")
        except:
            pass
        print("Connecting...", flush=True)
        tools = await asyncio.wait_for(mcp_manager.connect("Ruota della Fortuna"), timeout=10)
        print(f"OK, {len(tools)} tools:", flush=True)
        for t in tools:
            print(f"  - {t['name']}", flush=True)
    except asyncio.TimeoutError:
        print("TIMEOUT!", flush=True)
    except Exception as e:
        print(f"ERROR: {type(e).__name__}: {e}", flush=True)

asyncio.run(main())
