#!/usr/bin/env python3
"""Expose the DDR ontology layer through the two paper-defined MCP tools."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import mcp.types as types
from mcp.server import stdio
from mcp.server.lowlevel import NotificationOptions, Server
from mcp.server.models import InitializationOptions

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tceo.runtime import DDRSemanticLayer


class SemanticMCPServer:
    def __init__(self, store_path: str, version: str = ""):
        self.layer = DDRSemanticLayer.load(store_path, version=version or None)
        self.server = Server("ddr-semantic-mcp")
        self._register_handlers()

    def _register_handlers(self) -> None:
        @self.server.list_tools()
        async def list_tools() -> list[types.Tool]:
            return [
                types.Tool(
                    name="browse_semantics",
                    description="Discover semantic concepts relevant to an analytical need.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "query": {"type": "string"},
                            "kind": {
                                "type": "string",
                                "enum": [
                                    "term",
                                    "mapping",
                                    "relation",
                                    "constraint",
                                    "all",
                                ],
                            },
                            "limit": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": 6,
                            },
                        },
                        "required": ["query"],
                        "additionalProperties": False,
                    },
                ),
                types.Tool(
                    name="resolve_semantics",
                    description="Resolve selected concepts to grounded mappings and linked semantic objects.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "mentions": {
                                "type": "array",
                                "items": {"type": "string"},
                                "maxItems": 5,
                            },
                            "context": {"type": "string"},
                        },
                        "required": ["mentions"],
                        "additionalProperties": False,
                    },
                ),
            ]

        @self.server.call_tool()
        async def call_tool(
            name: str, arguments: dict[str, Any]
        ) -> list[types.TextContent]:
            if name not in {"browse_semantics", "resolve_semantics"}:
                result = {"status": "error", "message": f"Unknown tool: {name}"}
            else:
                try:
                    result = self.layer.execute(name, arguments)
                except Exception as exc:
                    result = {"status": "error", "message": str(exc)}
            return [
                types.TextContent(
                    type="text",
                    text=json.dumps(result, ensure_ascii=False, default=str),
                )
            ]

        @self.server.list_resources()
        async def list_resources() -> list[types.Resource]:
            return [
                types.Resource(
                    uri="ddr-semantic://session-manifest",
                    name="DDR semantic session manifest",
                    mimeType="text/plain",
                )
            ]

        @self.server.read_resource()
        async def read_resource(uri: str) -> str:
            if str(uri) != "ddr-semantic://session-manifest":
                raise ValueError(f"Unknown resource: {uri}")
            return self.layer.manifest()

    async def run(self) -> None:
        async with stdio.stdio_server() as (read_stream, write_stream):
            await self.server.run(
                read_stream,
                write_stream,
                InitializationOptions(
                    server_name="ddr-semantic-mcp",
                    server_version="1.0.0",
                    capabilities=self.server.get_capabilities(
                        notification_options=NotificationOptions(),
                        experimental_capabilities={},
                    ),
                ),
            )


async def main() -> None:
    parser = argparse.ArgumentParser(description="DDR semantic MCP server")
    parser.add_argument("--store", required=True)
    parser.add_argument("--version", default="",
                        help="Explicit semantic version to serve (default: active)")
    args = parser.parse_args()
    await SemanticMCPServer(args.store, args.version).run()


if __name__ == "__main__":
    asyncio.run(main())
