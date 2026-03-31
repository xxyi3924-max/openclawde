"""
MCP (Model Context Protocol) manager.

Reads server configs from:
  ~/.claude/mcp.json            (user-global)
  {project}/mcp.json            (project-local, takes priority)

Config format (same as Claude Code):
  {
    "mcpServers": {
      "github": {
        "type": "stdio",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-github"],
        "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": "ghp_..."}
      },
      "postgres": {
        "type": "stdio",
        "command": "python",
        "args": ["-m", "mcp_server_postgres"],
        "env": {"DATABASE_URL": "postgresql://..."}
      }
    }
  }

Supported transport types: stdio, sse (http/ws planned)
Tools are exposed as mcp__{server}__{tool} in the agent's tool list.
"""

import asyncio
import json
import os
import threading
from pathlib import Path
from typing import Any

_MCP_AVAILABLE = False
try:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    _MCP_AVAILABLE = True
except ImportError:
    pass

_SSE_AVAILABLE = False
try:
    from mcp.client.sse import sse_client
    _SSE_AVAILABLE = True
except ImportError:
    pass

_BASE = Path(__file__).parent
_PROJECT_MCP_CONFIG = _BASE / "mcp.json"
_USER_MCP_CONFIG = Path.home() / ".claude" / "mcp.json"


def _load_config() -> dict[str, dict]:
    """Load and merge mcp.json configs. Project takes priority over user."""
    servers = {}
    for cfg_path in [_USER_MCP_CONFIG, _PROJECT_MCP_CONFIG]:
        if cfg_path.exists():
            try:
                data = json.loads(cfg_path.read_text(encoding="utf-8"))
                servers.update(data.get("mcpServers", {}))
                print(f"[MCP] Loaded config: {cfg_path}")
            except Exception as e:
                print(f"[MCP] Config error {cfg_path}: {e}")
    return servers


class _AsyncLoop:
    """Persistent async event loop running in a background daemon thread."""

    def __init__(self):
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, daemon=True, name="mcp-event-loop")
        self._thread.start()

    def _run(self):
        self._loop.run_forever()

    def run(self, coro, timeout: float = 30.0):
        """Submit a coroutine and block until result (or timeout)."""
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=timeout)


_loop = _AsyncLoop()


class MCPServer:
    """
    Connection wrapper for a single MCP server.
    Keeps a persistent async session to avoid process-per-call overhead for stdio.
    """

    def __init__(self, name: str, config: dict):
        self.name = name
        self.config = config
        self._tools: list[dict] = []
        self._session = None
        self._cm = None  # async context manager stack
        self._read = None
        self._write = None
        self._connected = False

    async def _connect(self):
        """Open a persistent connection to the MCP server."""
        server_type = self.config.get("type", "stdio")
        env = {**os.environ, **self.config.get("env", {})}

        if server_type == "stdio":
            params = StdioServerParameters(
                command=self.config["command"],
                args=self.config.get("args", []),
                env=env,
            )
            self._transport_cm = stdio_client(params)
            self._read, self._write = await self._transport_cm.__aenter__()
            self._session_cm = ClientSession(self._read, self._write)
            self._session = await self._session_cm.__aenter__()
            await self._session.initialize()
            self._connected = True
        elif server_type in ("sse", "http") and _SSE_AVAILABLE:
            url = self.config["url"]
            headers = self.config.get("headers", {})
            self._transport_cm = sse_client(url, headers=headers)
            self._read, self._write = await self._transport_cm.__aenter__()
            self._session_cm = ClientSession(self._read, self._write)
            self._session = await self._session_cm.__aenter__()
            await self._session.initialize()
            self._connected = True
        else:
            raise ValueError(f"Unsupported MCP transport type: {server_type}")

    async def _disconnect(self):
        if self._session_cm:
            try:
                await self._session_cm.__aexit__(None, None, None)
            except Exception:
                pass
        if self._transport_cm:
            try:
                await self._transport_cm.__aexit__(None, None, None)
            except Exception:
                pass
        self._connected = False

    async def _list_tools_async(self) -> list[dict]:
        if not self._connected:
            await self._connect()
        result = await self._session.list_tools()
        return [
            {
                "name": t.name,
                "description": t.description or "",
                "input_schema": t.inputSchema if hasattr(t, "inputSchema") else {},
            }
            for t in result.tools
        ]

    async def _call_tool_async(self, tool_name: str, arguments: dict) -> str:
        if not self._connected:
            await self._connect()
        result = await self._session.call_tool(tool_name, arguments=arguments)
        parts = []
        for c in result.content:
            if hasattr(c, "text"):
                parts.append(c.text)
            elif hasattr(c, "data"):
                parts.append(f"[binary data: {len(c.data)} bytes]")
        return "\n".join(parts) or "(no output)"

    def list_tools(self) -> list[dict]:
        try:
            self._tools = _loop.run(_list_tools_async_wrapper(self), timeout=15.0)
            return self._tools
        except Exception as e:
            print(f"[MCP:{self.name}] list_tools failed: {e}")
            return []

    def call_tool(self, tool_name: str, arguments: dict) -> str:
        try:
            return _loop.run(_call_tool_async_wrapper(self, tool_name, arguments), timeout=30.0)
        except Exception as e:
            return f"[MCP:{self.name}] Error calling {tool_name}: {e}"


async def _list_tools_async_wrapper(server: MCPServer) -> list[dict]:
    return await server._list_tools_async()


async def _call_tool_async_wrapper(server: MCPServer, tool_name: str, arguments: dict) -> str:
    return await server._call_tool_async(tool_name, arguments)


class MCPManager:
    """Manages all MCP server connections and provides tools to the agent."""

    def __init__(self):
        self._servers: dict[str, MCPServer] = {}
        self._tool_to_server: dict[str, tuple[str, str]] = {}  # prefixed_name -> (server_name, orig_name)
        self.tool_defs: list[dict] = []  # Anthropic API format tool defs

    def load(self):
        """Load config and connect to all configured MCP servers."""
        if not _MCP_AVAILABLE:
            print("[MCP] 'mcp' package not installed. Run: pip install mcp")
            return

        configs = _load_config()
        if not configs:
            return

        for server_name, config in configs.items():
            try:
                server = MCPServer(server_name, config)
                raw_tools = server.list_tools()
                if not raw_tools:
                    continue
                self._servers[server_name] = server
                for t in raw_tools:
                    prefixed = f"mcp__{server_name}__{t['name']}"
                    self._tool_to_server[prefixed] = (server_name, t["name"])
                    self.tool_defs.append({
                        "name": prefixed,
                        "description": f"[MCP:{server_name}] {t['description']}",
                        "input_schema": t["input_schema"] or {"type": "object", "properties": {}},
                    })
                print(f"[MCP] {server_name}: loaded {len(raw_tools)} tool(s)")
            except Exception as e:
                print(f"[MCP] Failed to connect to {server_name}: {e}")

    def call(self, prefixed_name: str, inputs: dict) -> str:
        """Dispatch a prefixed tool call to the right MCP server."""
        entry = self._tool_to_server.get(prefixed_name)
        if not entry:
            return f"[MCP] Unknown tool: {prefixed_name}"
        server_name, orig_name = entry
        server = self._servers.get(server_name)
        if not server:
            return f"[MCP] Server '{server_name}' not connected"
        return server.call_tool(orig_name, inputs)

    def is_mcp_tool(self, name: str) -> bool:
        return name in self._tool_to_server

    @property
    def loaded(self) -> bool:
        return bool(self._servers)


# Module-level singleton
_manager: MCPManager | None = None


def get_manager() -> MCPManager:
    global _manager
    if _manager is None:
        _manager = MCPManager()
    return _manager


def load_mcp() -> MCPManager:
    """Load MCP servers and return the manager. Call once at startup."""
    m = get_manager()
    if not m.loaded:
        m.load()
    return m
