"""Synchronous adapter for the InsightBench semantic stdio MCP server."""

from __future__ import annotations

import asyncio
import json
import re
import sys
import threading
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any, Optional

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


class InsightSemanticMCPClient:
    """Present MCP tools to the existing synchronous ToolCallingChat loop."""

    def __init__(
        self,
        csv_path: str,
        user_csv_path: Optional[str] = None,
        store_path: Optional[str] = None,
        domain: Optional[str] = None,
    ):
        root = Path(__file__).resolve().parents[2]
        args = [
            str(root / "tool_server" / "semantic_mcp.py"),
            "--csv",
            str(csv_path),
        ]
        if user_csv_path:
            args.extend(["--user-csv", str(user_csv_path)])
        if store_path:
            args.extend(["--store", str(store_path)])
        if domain:
            args.extend(["--domain", str(domain)])

        self._server_params = StdioServerParameters(
            command=sys.executable,
            args=args,
        )
        self._ready = threading.Event()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._stop_event: Optional[asyncio.Event] = None
        self._session: Optional[ClientSession] = None
        self._error: Optional[BaseException] = None
        self._session_manifest = ""
        self._tool_schemas: list[dict[str, Any]] = []
        self._events: list[dict[str, Any]] = []
        self._thread = threading.Thread(
            target=self._thread_main,
            name="insight-semantic-mcp",
            daemon=True,
        )
        self._thread.start()
        if not self._ready.wait(timeout=30):
            raise TimeoutError("Timed out while starting the semantic MCP server")
        if self._error is not None:
            raise RuntimeError("Failed to start the semantic MCP server") from self._error

    def _thread_main(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._lifecycle())
        except BaseException as exc:
            self._error = exc
            self._ready.set()
        finally:
            self._loop.close()

    async def _lifecycle(self) -> None:
        self._stop_event = asyncio.Event()
        async with AsyncExitStack() as stack:
            streams = await stack.enter_async_context(stdio_client(self._server_params))
            session = ClientSession(*streams)
            await stack.enter_async_context(session)
            await session.initialize()
            self._session = session

            tools = await session.list_tools()
            self._tool_schemas = [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description or "",
                        "parameters": tool.inputSchema,
                    },
                }
                for tool in tools.tools
            ]
            resource = await session.read_resource(
                "insight-bench-semantic://session-manifest"
            )
            self._session_manifest = next(
                (
                    str(content.text)
                    for content in resource.contents
                    if getattr(content, "text", None) is not None
                ),
                "",
            )
            self._ready.set()
            await self._stop_event.wait()

    def _submit(self, coroutine):
        if self._loop is None:
            raise RuntimeError("Semantic MCP event loop is not available")
        return asyncio.run_coroutine_threadsafe(coroutine, self._loop).result(timeout=60)

    @property
    def version(self) -> str:
        match = re.search(
            r"Ontology layer version:\s*(\S+)", self._session_manifest
        )
        return match.group(1) if match else "unknown"

    @property
    def available_tool_names(self) -> list[str]:
        return [tool["function"]["name"] for tool in self._tool_schemas]

    def tool_schemas(self) -> list[dict[str, Any]]:
        return list(self._tool_schemas)

    def session_manifest(self) -> str:
        return self._session_manifest

    async def _call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        if self._session is None:
            raise RuntimeError("Semantic MCP session is not connected")
        result = await self._session.call_tool(name, arguments)
        return next(
            (
                str(content.text)
                for content in result.content
                if getattr(content, "text", None) is not None
            ),
            json.dumps({"status": "error", "message": "MCP returned no text"}),
        )

    def execute_json(self, name: str, arguments_json: str, stage: str) -> str:
        arguments = json.loads(arguments_json or "{}")
        result = self._submit(self._call_tool(name, arguments))
        parsed = json.loads(result)
        self._events.append(
            {
                "type": name,
                "stage": stage,
                "semantic_version": self.version,
                "arguments": arguments,
                "status": parsed.get("status", "ok"),
            }
        )
        return result

    def reset_session(self) -> None:
        """The MCP process remains stable across follow-up questions."""

    def trace(self) -> dict[str, Any]:
        return {
            "semantic_version": self.version,
            "semantic_events": list(self._events),
        }

    def close(self) -> None:
        if self._loop is None or self._stop_event is None:
            return
        self._loop.call_soon_threadsafe(self._stop_event.set)
        self._thread.join(timeout=10)
        self._loop = None

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass
