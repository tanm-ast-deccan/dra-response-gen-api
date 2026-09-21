"""
exec_server.py — MCP server that serves the DRA agent tools.

Single source of truth: this server does NOT define tools inline. It bridges
every tool registered in tools.TOOL_REGISTRY into FastMCP, so the agent sees
exactly the tools defined in tools.py — python_execute, bash_execute, read_file,
write_file, list_directory, search_in_file, web_search, web_fetch, calculator —
and any tool added to tools.py in future appears here automatically with no edit
to this file.

Transports:
    stdio  — started as a subprocess by mcp_client.py (default)
    SSE    — standalone HTTP server for shared/remote use

Usage:
    python exec_server.py                      # stdio (mcp_client starts this)
    python exec_server.py --list-tools         # print tools and exit
    python exec_server.py --transport sse --port 9402

The agent working directory is DRA_AGENT_WORKDIR, set by mcp_client before
launch. tools.py reads it per-call, so nothing here needs it directly — but we
fail loud if it is unset when the server starts standalone, to avoid silently
operating against the wrong directory.

Dependencies:
    pip install fastmcp duckduckgo-search
"""

from __future__ import annotations

import os
import sys
import logging
import argparse

from fastmcp import FastMCP

# Import the single tool library. Support both package and script execution.
try:
    from . import tools
except ImportError:
    import tools

logger = logging.getLogger("dra.exec_server")


# ── Fail loud if the agent workdir was not set (except for --list-tools) ──────
# mcp_client sets DRA_AGENT_WORKDIR before launching. If it is unset here, the
# tools would silently operate against cwd — the exact bug that made an agent
# scan the whole host for its inputs. Refuse instead.
def _require_workdir():
    if not os.environ.get("DRA_AGENT_WORKDIR"):
        raise RuntimeError(
            "DRA_AGENT_WORKDIR not set. mcp_client must set it before launching "
            "exec_server. Refusing to default to cwd."
        )


mcp = FastMCP(
    name="dra-exec-tools",
    instructions=(
        "Execution and file tools for deep research analysis. Use python_execute "
        "for calculations and data processing, bash_execute for shell operations, "
        "read_file / search_in_file / list_directory to inspect task files, "
        "write_file to produce deliverables, web_search / web_fetch for external "
        "information, and calculator for quick arithmetic."
    ),
)


# ── Explicit thin wrappers ────────────────────────────────────────────────────
# One wrapper per registered tool. Each has a real, introspectable signature
# (FastMCP/pydantic build the MCP schema from these type hints) and delegates to
# tools.execute() so ALL logic, defaults, and error handling live in tools.py.
# Parameter names/types/defaults mirror the @register_tool schemas in tools.py.
# When you add a tool to tools.py, add a matching wrapper here (and the startup
# consistency check below will remind you if you forget).

@mcp.tool()
def python_execute(code: str) -> str:
    return tools.execute("python_execute", {"code": code})


@mcp.tool()
def bash_execute(command: str) -> str:
    return tools.execute("bash_execute", {"command": command})


@mcp.tool()
def read_file(path: str) -> str:
    return tools.execute("read_file", {"path": path})


@mcp.tool()
def write_file(path: str, content: str) -> str:
    return tools.execute("write_file", {"path": path, "content": content})


@mcp.tool()
def list_directory(path: str = ".") -> str:
    return tools.execute("list_directory", {"path": path})


@mcp.tool()
def search_in_file(path: str, query: str, context_lines: int = 3) -> str:
    return tools.execute("search_in_file",
                         {"path": path, "query": query, "context_lines": context_lines})


@mcp.tool()
def web_search(query: str, max_results: int = 5) -> str:
    return tools.execute("web_search", {"query": query, "max_results": max_results})


@mcp.tool()
def web_fetch(url: str) -> str:
    return tools.execute("web_fetch", {"url": url})


@mcp.tool()
def calculator(expression: str) -> str:
    return tools.execute("calculator", {"expression": expression})


def _check_registry_parity():
    """Fail loud at startup if tools.py has tools this server doesn't expose (or
    vice-versa) — so adding a tool to tools.py without a wrapper here is caught
    immediately rather than silently hidden from the agent."""
    import asyncio
    _listed = mcp.list_tools()
    if asyncio.iscoroutine(_listed):
        _listed = asyncio.run(_listed)
    exposed = {t.name for t in _listed}
    registered = set(tools.TOOL_REGISTRY.keys())
    missing = registered - exposed
    extra = exposed - registered
    if missing:
        raise RuntimeError(
            f"exec_server is missing wrappers for tools in tools.py: {sorted(missing)}. "
            f"Add an @mcp.tool() wrapper for each."
        )
    if extra:
        logger.warning("exec_server exposes tools not in tools.py: %s", sorted(extra))
    logger.info("Tool parity OK: %d tools exposed, matching tools.TOOL_REGISTRY",
                len(exposed))


_check_registry_parity()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="MCP Execution Server — DRA agent tools")
    parser.add_argument("--transport", choices=["stdio", "sse"], default="stdio")
    parser.add_argument("--port", type=int, default=9402)
    parser.add_argument("--list-tools", action="store_true")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    if args.verbose:
        logging.basicConfig(level=logging.DEBUG,
                            format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
                            datefmt="%H:%M:%S")

    if args.list_tools:
        for name, tool in sorted(tools.TOOL_REGISTRY.items()):
            print(f"  {name}: {tool.description[:80]}")
        sys.exit(0)

    _require_workdir()

    if args.transport == "sse":
        mcp.run(transport="sse", port=args.port)
    else:
        mcp.run(transport="stdio")