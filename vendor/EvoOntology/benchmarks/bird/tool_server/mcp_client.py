#!/usr/bin/env python3
"""Implementation for the bird.tool_server.mcp_client module."""

import json
import os
import sys
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any, Dict, List, Optional

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


class MCPClientManager:
    """Implementation of MCPClientManager."""

    def __init__(self):
        self.mcp_sessions: Dict[str, ClientSession] = {}
        self._exit_stack: Optional[AsyncExitStack] = None
        self._tool_routing: Dict[str, str] = {}

    async def connect_to_servers(self, server_configs: List[dict]) -> bool:
        """Implement connect to servers."""
        self._exit_stack = AsyncExitStack()
        success = False

        for config in server_configs:
            try:
                module_path = config["module"]
                server_args = config.get("args", [])


                if "/" in module_path or "\\" in module_path:
                    script_path = module_path
                else:

                    parts = module_path.split(".")
                    script_path = str(
                        Path(__file__).resolve().parent.parent / Path(*parts)
                    ) + ".py"

                args = [script_path] + server_args

                server_params = StdioServerParameters(
                    command=sys.executable,
                    args=args,
                    env=os.environ.copy(),
                )

                stdio_ctx = stdio_client(server_params)
                read_stream, write_stream = await self._exit_stack.enter_async_context(stdio_ctx)

                mcp_session = ClientSession(read_stream, write_stream)
                await self._exit_stack.enter_async_context(mcp_session)
                await mcp_session.initialize()

                self.mcp_sessions[config["name"]] = mcp_session
                print(f"  [MCP] Connected: {config['name']}")
                success = True

            except Exception as e:
                print(f"  [MCP] Connection to {config.get('name', 'unknown')} failed: {e}")

        return success

    async def get_available_tools(self) -> Dict[str, Dict[str, Any]]:
        """Return available tools."""
        all_tools = {}
        self._tool_routing.clear()

        for server_name, session in self.mcp_sessions.items():
            try:
                tools_result = await session.list_tools()
                server_tools = {}

                for tool in tools_result.tools:
                    server_tools[tool.name] = {
                        "description": tool.description,
                        "inputSchema": tool.inputSchema,
                    }
                    self._tool_routing[tool.name] = server_name

                all_tools[server_name] = server_tools
                print(f"  [MCP] {server_name}: {len(server_tools)} itemstools")

            except Exception as e:
                print(f"  [MCP] Retrieving {server_name} tool list failed: {e}")

        return all_tools

    async def execute_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Any:
        """Execute tool."""

        target_server = self._tool_routing.get(tool_name)
        if target_server and target_server in self.mcp_sessions:
            try:
                result = await self.mcp_sessions[target_server].call_tool(tool_name, arguments)
                return self._format_tool_result(result)
            except Exception as e:
                return {"error": True, "message": str(e)}


        for server_name, session in self.mcp_sessions.items():
            try:
                result = await session.call_tool(tool_name, arguments)
                return self._format_tool_result(result)
            except Exception:
                continue

        return {"error": True, "message": f"Tool '{tool_name}' was not found or failed"}

    def _format_tool_result(self, result: Any) -> Any:
        """Format tool result."""
        if hasattr(result, "content") and result.content:
            content = result.content[0]
            if hasattr(content, "text"):
                try:
                    return json.loads(content.text)
                except (json.JSONDecodeError, TypeError):
                    return content.text
        return result

    async def close(self):
        """Implement close."""
        try:
            if self._exit_stack:
                await self._exit_stack.aclose()
        except RuntimeError:
            pass
        except Exception as e:
            print(f"  [MCP] Error while closing the connection: {e}")
        finally:
            self._exit_stack = None
            self.mcp_sessions.clear()
