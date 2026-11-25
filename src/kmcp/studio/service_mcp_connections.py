"""External MCP connection management (Phase 3 Task E).

CRUD over external MCP server connections plus discovery/import helpers that
delegate to :mod:`kmcp.studio.mcp_client`.
"""

from __future__ import annotations

import logging
from typing import Any

from kmcp.db.manager import DBManager
from kmcp.studio.mcp_client import ExternalMCPClient, MCPClientError
from kmcp.studio.service_tools import import_external_mcp_tools

logger = logging.getLogger("kmcp.studio.service_mcp_connections")


def add_connection(
    db: DBManager,
    connection_id: str,
    transport: str,
    *,
    url: str | None = None,
    command: list[str] | None = None,
    timeout: float = 30.0,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    ExternalMCPClient(transport, url=url, command=command, timeout=timeout, headers=headers)
    # Validate construction (raises MCPClientError on bad config)
    return {
        "connection_id": connection_id,
        "transport": transport,
        "url": url,
        "command": command,
        "timeout": timeout,
    }


async def discover_and_import(
    db: DBManager,
    connection_id: str,
    client: ExternalMCPClient,
    *,
    visibility: str = "internal",
) -> list[dict[str, Any]]:
    """Discover tools from an external MCP server and import them into the DB."""
    try:
        return await import_external_mcp_tools(db, connection_id, client, visibility=visibility)
    except MCPClientError as exc:
        logger.error("External MCP discovery failed for %s: %s", connection_id, exc)
        raise


async def test_connection(client: ExternalMCPClient) -> dict[str, Any]:
    """Probe an external MCP server without nesting an event loop.

    Returns a structured result instead of a bare bool so the API can report
    *why* a connection failed. Errors are redacted before being returned.
    """
    from kmcp.studio.redaction import redact_log

    try:
        async with client:
            tools = await client.list_tools()
        return {
            "ok": True,
            "healthy": True,
            "tool_count": len(tools) if isinstance(tools, list) else None,
            "error": None,
        }
    except Exception as exc:  # noqa: BLE001 — probe failures are expected
        return {
            "ok": False,
            "healthy": False,
            "tool_count": None,
            "error": redact_log(f"{type(exc).__name__}: {exc}"),
        }


__all__ = ["add_connection", "discover_and_import", "test_connection"]
