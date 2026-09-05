"""Read-only stdio MCP bridge to the installed official Taobao native CLI.

This is an AionsHome bridge, not Taobao's built-in HTTP MCP server.
Only search_products is exposed. No history, chat, cart or payment tools.
"""
import asyncio
import json
from pathlib import Path
import subprocess

from mcp.server.fastmcp import FastMCP

server = FastMCP("AionsHome Taobao native search bridge")


def native_command():
    # Pin the exact local runtime/script used by the successful direct test.
    # Do not silently fall back to launching the Taobao Electron executable.
    node = "C:/Program Files/nodejs/node.exe"
    script = "H:/taobao/bin/cli-rpc.js"
    if not Path(node).is_file() or not Path(script).is_file():
        raise RuntimeError("已验证的 Node 或淘宝脚本路径不存在，请检查本机安装路径")
    return [node, script, "--stdin"]


@server.tool()
async def search_products(keyword: str, type: str = "all", sourceApp: str = "AionsHome") -> dict:
    """Search real products through the official installed Taobao native tool."""
    if not keyword.strip() or len(keyword) > 120:
        raise ValueError("搜索词应为 1 到 120 字")
    request = {"tool": "search_products", "arguments": {
        "keyword": keyword.strip(), "type": "all", "sourceApp": "AionsHome",
    }}
    # Match the direct subprocess.run call, off-thread to keep MCP responsive.
    process = await asyncio.to_thread(
        subprocess.run, native_command(),
        input=json.dumps(request, ensure_ascii=False),
        encoding="utf-8", capture_output=True, timeout=120,
    )
    try:
        payload = json.loads(process.stdout)
    except (UnicodeError, ValueError) as exc:
        raise RuntimeError("淘宝未返回有效商品数据，请确认已启动、登录并启用 AI 代理") from exc
    if payload.get("error") or process.returncode:
        raise RuntimeError(str(payload.get("error") or "淘宝原生搜索失败"))
    result = payload.get("result")
    if not isinstance(result, dict) or not isinstance(result.get("products"), list):
        raise RuntimeError("淘宝搜索未返回商品列表，可能需要登录或完成验证")
    return result


if __name__ == "__main__":
    server.run(transport="stdio")
