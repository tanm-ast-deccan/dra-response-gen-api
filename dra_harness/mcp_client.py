"""
mcp_client.py — MCP client for the dra harness.

Manages the lifecycle of MCP tool servers, discovers tools dynamically,
converts between MCP and OpenAI schemas, and routes tool calls.

Architecture:
    MCPToolClient
      ├── start()         → launch exec_server.py as subprocess
      ├── discover()      → list tools, convert to OpenAI schemas
      ├── call_tool()     → route a tool call through MCP
      ├── stop()          → shut down the server
      └── openai_schemas  → cached OpenAI-format tool definitions

Usage:
    async with MCPToolClient(staging_dir="/tmp/eval") as client:
        schemas = client.openai_schemas          # pass to driver.chat()
        result = await client.call_tool("python_execute", {"code": "print(42)"})

    # Or manual lifecycle:
    client = MCPToolClient(staging_dir="/tmp/eval")
    await client.start()
    schemas = client.openai_schemas
    result = await client.call_tool("web_search", {"query": "FRED PPI 2025"})
    await client.stop()
"""

from __future__ import annotations

import os
import sys
import logging
from fastmcp import Client
from fastmcp.client.transports import PythonStdioTransport
from typing import Optional

logger = logging.getLogger("dra.mcp_client")


class MCPToolClient:
    """
    Async MCP client that manages an exec_server.py subprocess.

    Starts the server on __aenter__, discovers tools, and provides
    call_tool() for the runner's agentic loop. Stops on __aexit__.
    """

    def __init__(
        self,
        staging_dir: str = ".",
        server_script: Optional[str] = None,
    ):
        """
        Args:
            staging_dir: working directory for tool execution
                         (set as DRA_AGENT_WORKDIR env var)
            server_script: path to exec_server.py (auto-detected if None)
        """
        self.staging_dir = os.path.abspath(staging_dir)
        self.server_script = server_script or self._find_server()

        self._fastmcp_client = None      # fastmcp.Client instance
        self._session = None             # active MCP session
        self._mcp_tools = []             # raw MCP tool definitions
        self._openai_schemas: list[dict] = []  # converted OpenAI schemas
        self._tool_names: set[str] = set()
        self._started = False

    # ─── Lifecycle ────────────────────────────────────────────────

    async def start(self):
        """Start the MCP server subprocess and discover tools."""
        if self._started:
            return

        try:
            from fastmcp import Client
            from fastmcp.client.transports import PythonStdioTransport
        except ImportError:
            raise ImportError(
                "fastmcp is required for MCP tool support.\n"
                "  pip install fastmcp"
            )

        # Set staging dir so the server knows where files are
        os.environ["DRA_AGENT_WORKDIR"] = self.staging_dir

        logger.info(
            "Starting MCP exec server: %s (staging: %s)",
            self.server_script, self.staging_dir,
        )

        # Create client — this will start the subprocess on connect
        self._fastmcp_client = Client(
            transport=PythonStdioTransport(
                script_path=self.server_script,
                python_cmd=sys.executable,
                env={
                    **os.environ,
                    "DRA_AGENT_WORKDIR": self.staging_dir,
                },
            )
        )

        # Start the subprocess and MCP handshake
        await self._fastmcp_client.__aenter__()

        # Discover tools
        await self._discover_tools()

        self._started = True
        logger.info(
            "MCP client ready: %d tools available — %s",
            len(self._openai_schemas),
            sorted(self._tool_names),
        )

    async def stop(self):
        """Shut down the MCP server subprocess."""
        if not self._started:
            return

        try:
            await self._fastmcp_client.__aexit__(None, None, None)
        except Exception as e:
            logger.warning("MCP server shutdown error: %s", e)
        finally:
            self._session = None
            self._fastmcp_client = None
            self._started = False
            logger.info("MCP client stopped")

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.stop()
        return False

    # ─── Tool discovery ───────────────────────────────────────────

    async def _discover_tools(self):
        """List tools from the MCP server and convert to OpenAI format."""
        self._mcp_tools = await self._fastmcp_client.list_tools()
        self._openai_schemas = []
        self._tool_names = set()

        for tool in self._mcp_tools:
            name = tool.name
            description = tool.description or ""

            # MCP uses inputSchema; extract it robustly
            input_schema = {}
            if hasattr(tool, "inputSchema"):
                input_schema = tool.inputSchema
            elif hasattr(tool, "input_schema"):
                input_schema = tool.input_schema
            elif hasattr(tool, "parameters"):
                input_schema = tool.parameters

            # Convert to OpenAI function-calling format
            self._openai_schemas.append({
                "type": "function",
                "function": {
                    "name": name,
                    "description": description,
                    "parameters": input_schema,
                },
            })
            self._tool_names.add(name)

    @property
    def openai_schemas(self) -> list[dict]:
        """OpenAI-compatible tool schemas for passing to the LLM driver."""
        return self._openai_schemas

    @property
    def tool_names(self) -> set[str]:
        """Set of available tool names."""
        return self._tool_names

    @property
    def is_ready(self) -> bool:
        return self._started and bool(self._openai_schemas)

    # ─── Tool execution ──────────────────────────────────────────
    
    async def call_tool(self, name: str, arguments: dict) -> str:
        if not self._started:
            return f"[error] MCP client not started — cannot call {name}"

        if name not in self._tool_names:
            return (
                f"[error] unknown tool: {name}. "
                f"Available: {sorted(self._tool_names)}"
            )

        try:
            result = await self._fastmcp_client.call_tool(name, arguments)

            # FastMCP 3.4: CallToolResult with .data shortcut or .content list
            if hasattr(result, 'data') and result.data is not None:
                return str(result.data)

            # CallToolResult has .content (list of TextContent objects)
            if hasattr(result, 'content'):
                parts = []
                for item in result.content:
                    if hasattr(item, 'text'):
                        parts.append(item.text)
                    else:
                        parts.append(str(item))
                return "\n".join(parts)

            return str(result)

        except Exception as e:
            logger.error("MCP tool call failed: %s(%s) → %s", name, arguments, e)
            return f"[tool error] {name} failed: {e}"

    # async def call_tool(self, name: str, arguments: dict) -> str:
    #     """
    #     Execute a tool via the MCP server.

    #     Args:
    #         name: tool name (e.g. "python_execute")
    #         arguments: tool arguments dict

    #     Returns:
    #         Tool result as a string (up to 15,000 chars from the server)
    #     """
    #     if not self._started:
    #         return f"[error] MCP client not started — cannot call {name}"

    #     if name not in self._tool_names:
    #         return (
    #             f"[error] unknown tool: {name}. "
    #             f"Available: {sorted(self._tool_names)}"
    #         )

    #     try:
    #         result = await self._session.call_tool(name, arguments)

    #         # fastmcp returns various result types — normalize to string
    #         if isinstance(result, list):
    #             # List of TextContent / ImageContent objects
    #             parts = []
    #             for item in result:
    #                 if hasattr(item, "text"):
    #                     parts.append(item.text)
    #                 elif isinstance(item, str):
    #                     parts.append(item)
    #                 else:
    #                     parts.append(str(item))
    #             return "\n".join(parts)

    #         if hasattr(result, "text"):
    #             return result.text

    #         if isinstance(result, str):
    #             return result

    #         return str(result)

    #     except Exception as e:
    #         logger.error("MCP tool call failed: %s(%s) → %s", name, arguments, e)
    #         return f"[tool error] {name} failed: {e}"

    # ─── Internal helpers ─────────────────────────────────────────

    def _find_server(self) -> str:
        """Locate exec_server.py relative to this file."""
        here = os.path.dirname(os.path.abspath(__file__))
        candidates = [
            os.path.join(here, "exec_server.py"),
            os.path.join(here, "..", "mcp_servers", "exec_server.py"),
        ]
        for path in candidates:
            if os.path.exists(path):
                return path

        raise FileNotFoundError(
            "exec_server.py not found. Expected locations:\n"
            + "\n".join(f"  {c}" for c in candidates)
        )

    def __repr__(self) -> str:
        status = "ready" if self._started else "stopped"
        tools = len(self._openai_schemas)
        return f"MCPToolClient({status}, {tools} tools, staging={self.staging_dir})"