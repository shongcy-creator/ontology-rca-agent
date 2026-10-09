#!/usr/bin/env python3
"""Implementation for the insightbench.tool_server.semantic_mcp module."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Dict, Optional

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from insightbench.tceo.retriever import InsightSemanticLayer


class SemanticMCPService:
    """Implementation of SemanticMCPService."""

    def __init__(
        self,
        csv_path: str,
        user_csv_path: Optional[str] = None,
        store_path: Optional[str] = None,
        domain: Optional[str] = None,
    ):
        main = pd.read_csv(csv_path)
        user = pd.read_csv(user_csv_path) if user_csv_path else None
        self.layer = InsightSemanticLayer.from_tables(
            main,
            user,
            store_path=store_path,
            domain=domain,
        )

    @staticmethod
    def tool_schemas() -> list[Dict[str, Any]]:
        return [schema["function"] for schema in InsightSemanticLayer.tool_schemas()]

    def call(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        if name not in {"browse_semantics", "resolve_semantics"}:
            raise ValueError(f"Unknown tool: {name}")
        return self.layer.execute_tool(name, arguments, stage="mcp")


def create_server(service: SemanticMCPService):
    from mcp.server.lowlevel import Server
    import mcp.types as types

    server = Server("insight-bench-semantic-mcp")

    @server.list_tools()
    async def list_tools():
        return [types.Tool(
            name=schema["name"],
            description=schema["description"],
            inputSchema=schema["parameters"],
        ) for schema in service.tool_schemas()]

    @server.call_tool()
    async def call_tool(name: str, arguments: Dict[str, Any]):
        try:
            result = service.call(name, arguments)
        except Exception as exc:
            result = {"status": "error", "error": str(exc)}
        return [types.TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]

    @server.list_resources()
    async def list_resources():
        return [types.Resource(
            uri="insight-bench-semantic://session-manifest",
            name="Insight-Bench Task Semantic Manifest",
            mimeType="text/plain",
        )]

    @server.read_resource()
    async def read_resource(uri: str):
        if str(uri) != "insight-bench-semantic://session-manifest":
            raise ValueError(f"Unknown resource: {uri}")
        return service.layer.manifest()

    return server


async def _run(service: SemanticMCPService) -> None:
    import mcp.server.stdio
    from mcp.server.lowlevel import NotificationOptions
    from mcp.server.models import InitializationOptions

    server = create_server(service)
    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name="insight-bench-semantic-mcp",
                server_version="1.0.0",
                capabilities=server.get_capabilities(NotificationOptions(), {}),
            ),
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Insight-Bench semantic MCP server")
    parser.add_argument("--csv", required=True, help="Primary CSV for the current flag")
    parser.add_argument("--user-csv", default=None, help="Optional auxiliary CSV for the current flag")
    parser.add_argument("--store", default=None, help="Semantic-store directory")
    parser.add_argument("--domain", default=None, help="Optional task-domain filter")
    args = parser.parse_args()
    asyncio.run(_run(SemanticMCPService(args.csv, args.user_csv, args.store, args.domain)))


if __name__ == "__main__":
    main()
