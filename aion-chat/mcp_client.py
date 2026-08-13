"""
MCP 连接管理器：管理多个 MCP Server 的连接生命周期
支持 Streamable HTTP（远程）和 stdio（本地进程）两种传输
"""

import asyncio
import json, logging
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

import anyio
import httpcore
import httpx

from mcp import ClientSession, McpError
from mcp.client.streamable_http import streamablehttp_client
from mcp.client.stdio import stdio_client, StdioServerParameters
from mcp.client.sse import sse_client
from mcp.shared.session import CONNECTION_CLOSED

from config import DATA_DIR

logger = logging.getLogger("mcp_client")

MCP_SERVERS_PATH = DATA_DIR / "mcp_servers.json"


class MCPToolProtocolError(RuntimeError):
    """The remote tool response cannot be interpreted as one JSON object."""


class MCPToolTransportError(RuntimeError):
    """The SDK call failed before a classifiable tool payload was returned."""


_ANYIO_STREAM_ERRORS = (
    anyio.BrokenResourceError,
    anyio.ClosedResourceError,
    anyio.EndOfStream,
    anyio.IncompleteRead,
)
_HTTPX_TRANSIENT_TRANSPORT_ERRORS = (
    httpx.TimeoutException,
    httpx.NetworkError,
    httpx.ProxyError,
    httpx.RemoteProtocolError,
    httpx.CloseError,
)
_HTTPCORE_TRANSPORT_ERRORS = (
    httpcore.NetworkError,
    httpcore.TimeoutException,
    httpcore.ConnectionNotAvailable,
    httpcore.ProxyError,
    httpcore.RemoteProtocolError,
)


def _is_known_tool_transport_failure(error: Exception) -> bool:
    if isinstance(error, (TimeoutError,) + _HTTPX_TRANSIENT_TRANSPORT_ERRORS):
        return True
    if isinstance(error, _ANYIO_STREAM_ERRORS + _HTTPCORE_TRANSPORT_ERRORS):
        return True
    if isinstance(error, McpError):
        code = error.error.code
        message = error.error.message
        return code == CONNECTION_CLOSED and message == "Connection closed"
    if isinstance(error, ExceptionGroup):
        return bool(error.exceptions) and all(
            isinstance(child, Exception)
            and _is_known_tool_transport_failure(child)
            for child in error.exceptions
        )
    return False

# ── 默认配置 ──────────────────────────────────────
_DEFAULT_CONFIG = {
    "servers": [
        {
            "name": "AI小镇",
            "type": "http",
            "url": "https://aisay.top/chatroom/mcp",
            "headers": {},
            "enabled": True
        }
    ]
}


def _load_config() -> dict:
    if MCP_SERVERS_PATH.exists():
        try:
            return json.loads(MCP_SERVERS_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    # 首次使用，自动创建默认配置
    MCP_SERVERS_PATH.write_text(
        json.dumps(_DEFAULT_CONFIG, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return dict(_DEFAULT_CONFIG)


def _save_config(cfg: dict):
    MCP_SERVERS_PATH.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
    )


class MCPManager:
    """管理所有 MCP Server 连接"""

    def __init__(self):
        # server_name -> { session, read_stream, write_stream, tools, cm, transport_cm }
        self._connections: dict[str, dict] = {}

    # ── 列出所有配置 ────────────────────────
    def list_servers(self) -> list[dict]:
        cfg = _load_config()
        result = []
        for s in cfg.get("servers", []):
            if s.get("visible", True) is False:
                continue
            result.append({
                "name": s["name"],
                "type": s.get("type", "http"),
                "url": s.get("url", ""),
                "enabled": s.get("enabled", True),
                "connected": s["name"] in self._connections,
                "tool_count": len(self._connections[s["name"]]["tools"]) if s["name"] in self._connections else 0,
            })
        return result

    # ── 连接 ────────────────────────────────
    async def connect(self, server_name: str) -> list[dict]:
        """连接指定 server，返回工具列表"""
        if server_name in self._connections:
            return self._connections[server_name]["tools"]

        cfg = _load_config()
        server_cfg = None
        for s in cfg.get("servers", []):
            if s["name"] == server_name:
                server_cfg = s
                break
        if not server_cfg:
            raise ValueError(f"未找到 MCP Server 配置: {server_name}")

        srv_type = server_cfg.get("type", "http")

        if srv_type in {"http", "streamablehttp", "streamable_http"}:
            return await self._connect_http(server_name, server_cfg)
        elif srv_type == "sse":
            return await self._connect_sse(server_name, server_cfg)
        elif srv_type == "stdio":
            return await self._connect_stdio(server_name, server_cfg)
        else:
            raise ValueError(f"不支持的传输类型: {srv_type}")

    async def connect_ephemeral(
        self,
        connection_id: str,
        url: str,
        headers: dict[str, str],
    ) -> list[dict]:
        if connection_id in self._connections:
            return self._connections[connection_id]["tools"]
        return await self._connect_http(connection_id, {
            "name": connection_id,
            "type": "streamablehttp",
            "url": url,
            "headers": dict(headers),
            "ephemeral": True,
        })

    async def _connect_http(self, name: str, cfg: dict) -> list[dict]:
        url = cfg["url"]
        headers = cfg.get("headers", {})

        async with AsyncExitStack() as initialization:
            transport_cm = streamablehttp_client(url=url, headers=headers)
            streams = await initialization.enter_async_context(transport_cm)
            read_stream, write_stream, _ = streams

            session = ClientSession(read_stream, write_stream)
            await initialization.enter_async_context(session)
            await session.initialize()

            tools_result = await session.list_tools()
            tools = [self._tool_to_dict(t) for t in tools_result.tools]

            self._connections[name] = {
                "session": session,
                "transport_cm": transport_cm,
                "tools": tools,
            }
            initialization.pop_all()
            logger.info(f"[MCP] 已连接 {name}，{len(tools)} 个工具可用")
            return tools

    async def _connect_sse(self, name: str, cfg: dict) -> list[dict]:
        url = cfg["url"]
        headers = cfg.get("headers", {})

        # SSE 传输（旧版 MCP 协议，部分远程服务仍在使用）
        transport_cm = sse_client(
            url=url,
            headers=headers,
            httpx_client_factory=lambda **kw: httpx.AsyncClient(
                verify=False, proxy=None, **kw
            ),
        )
        streams = await transport_cm.__aenter__()
        read_stream, write_stream = streams

        session = ClientSession(read_stream, write_stream)
        await session.__aenter__()
        await session.initialize()

        tools_result = await session.list_tools()
        tools = [self._tool_to_dict(t) for t in tools_result.tools]

        self._connections[name] = {
            "session": session,
            "transport_cm": transport_cm,
            "tools": tools,
        }
        logger.info(f"[MCP] 已连接 {name} (SSE)，{len(tools)} 个工具可用")
        return tools

    async def _connect_stdio(self, name: str, cfg: dict) -> list[dict]:
        cmd = cfg.get("command", "")
        args = cfg.get("args", [])

        params = StdioServerParameters(command=cmd, args=args)
        transport_cm = stdio_client(params)
        streams = await transport_cm.__aenter__()
        read_stream, write_stream = streams

        session = ClientSession(read_stream, write_stream)
        await session.__aenter__()
        await session.initialize()

        tools_result = await session.list_tools()
        tools = [self._tool_to_dict(t) for t in tools_result.tools]

        self._connections[name] = {
            "session": session,
            "transport_cm": transport_cm,
            "tools": tools,
        }
        logger.info(f"[MCP] 已连接 {name} (stdio)，{len(tools)} 个工具可用")
        return tools

    # ── 断开 ────────────────────────────────
    async def disconnect(self, server_name: str):
        conn = self._connections.pop(server_name, None)
        if not conn:
            return
        try:
            await conn["session"].__aexit__(None, None, None)
        except Exception:
            logger.warning("[MCP] 关闭 session 异常")
        try:
            await conn["transport_cm"].__aexit__(None, None, None)
        except Exception:
            logger.warning("[MCP] 关闭 transport 异常")
        logger.info(f"[MCP] 已断开 {server_name}")

    # ── 调用工具 ────────────────────────────
    async def call_tool(self, server_name: str, tool_name: str, arguments: dict) -> Any:
        conn = self._connections.get(server_name)
        if not conn:
            raise RuntimeError(f"MCP Server {server_name} 未连接")
        result = await conn["session"].call_tool(tool_name, arguments)
        # 将 result.content 序列化为可 JSON 化的格式
        contents = []
        for item in result.content:
            if hasattr(item, "text"):
                contents.append({"type": "text", "text": item.text})
            elif hasattr(item, "data"):
                contents.append({"type": "blob", "data": str(item.data)[:500]})
            else:
                contents.append({"type": "unknown", "value": str(item)[:500]})
        return contents

    # ── 工具列表 → OpenAI function calling 格式 ──
    async def call_tool_json(
        self, connection_id: str, tool_name: str, arguments: dict
    ) -> dict[str, object]:
        conn = self._connections.get(connection_id)
        if not conn:
            raise MCPToolTransportError("MCP tool transport failed")
        try:
            result = await conn["session"].call_tool(tool_name, arguments)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if _is_known_tool_transport_failure(error):
                raise MCPToolTransportError("MCP tool transport failed") from None
            raise MCPToolProtocolError("MCP tool payload is invalid") from None
        structured = getattr(result, "structuredContent", None)
        if isinstance(structured, dict):
            return structured
        if structured is not None:
            raise MCPToolProtocolError("MCP tool payload is invalid")
        content = getattr(result, "content", None)
        if not isinstance(content, list):
            raise MCPToolProtocolError("MCP tool payload is invalid")
        texts = [
            item.text
            for item in content
            if hasattr(item, "text") and isinstance(item.text, str)
        ]
        if len(texts) != 1:
            raise MCPToolProtocolError("MCP tool payload is invalid")
        try:
            parsed = json.loads(texts[0])
        except (TypeError, ValueError):
            raise MCPToolProtocolError("MCP tool payload is invalid") from None
        if not isinstance(parsed, dict):
            raise MCPToolProtocolError("MCP tool payload is invalid")
        return parsed

    def get_tools_for_ai(self, server_name: str) -> list[dict]:
        conn = self._connections.get(server_name)
        if not conn:
            return []
        result = []
        for tool in conn["tools"]:
            result.append({
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "parameters": tool.get("input_schema", {"type": "object", "properties": {}}),
                }
            })
        return result

    # ── 辅助 ────────────────────────────────
    @staticmethod
    def _tool_to_dict(tool) -> dict:
        return {
            "name": tool.name,
            "description": getattr(tool, "description", "") or "",
            "input_schema": tool.inputSchema if hasattr(tool, "inputSchema") else {},
        }

    def is_connected(self, server_name: str) -> bool:
        return server_name in self._connections

    def get_tools(self, server_name: str) -> list[dict]:
        conn = self._connections.get(server_name)
        return conn["tools"] if conn else []

    # ── 服务器配置管理 ────────────────────
    def add_server(self, name: str, srv_type: str, url: str) -> dict:
        """添加一个新的 MCP Server 配置"""
        cfg = _load_config()
        for s in cfg.get("servers", []):
            if s["name"] == name:
                raise ValueError(f"服务器名称已存在: {name}")
        new_server = {
            "name": name,
            "type": srv_type,
            "url": url,
            "headers": {},
            "enabled": True,
        }
        cfg.setdefault("servers", []).append(new_server)
        _save_config(cfg)
        return new_server

    def upsert_server(
        self,
        name: str,
        srv_type: str,
        url: str,
        *,
        headers: dict | None = None,
        enabled: bool = True,
        visible: bool = True,
    ) -> dict:
        """Create or update an MCP server config."""
        cfg = _load_config()
        servers = cfg.setdefault("servers", [])
        for s in servers:
            if s.get("name") == name:
                s.update({
                    "type": srv_type,
                    "url": url,
                    "headers": headers or {},
                    "enabled": enabled,
                    "visible": visible,
                })
                _save_config(cfg)
                return s

        new_server = {
            "name": name,
            "type": srv_type,
            "url": url,
            "headers": headers or {},
            "enabled": enabled,
            "visible": visible,
        }
        servers.append(new_server)
        _save_config(cfg)
        return new_server

    async def remove_server(self, name: str):
        """移除一个 MCP Server 配置（如果已连接则先断开）"""
        if name in self._connections:
            await self.disconnect(name)
        cfg = _load_config()
        cfg["servers"] = [s for s in cfg.get("servers", []) if s["name"] != name]
        _save_config(cfg)


# 全局单例
mcp_manager = MCPManager()
