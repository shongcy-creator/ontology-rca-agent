#!/usr/bin/env python3
"""
MCP client management for the autonomous data analysis agent
"""

import logging
import os
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Dict, Any, Optional
import sys

# Add parent directory to Python path to allow imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from agent.models import MCPServerConfig

logger = logging.getLogger("mcp-client")

class MCPClientManager:
    """Manages connections to MCP servers"""

    def __init__(self):
        self.mcp_sessions: Dict[str, ClientSession] = {}
        self._exit_stack: Optional[AsyncExitStack] = None
        self._tool_routing: Dict[str, str] = {}  # tool_name → server_name

    async def connect_to_servers(self, configs: list[MCPServerConfig]) -> bool:
        """Connect to all configured MCP servers"""
        self._exit_stack = AsyncExitStack()
        success = False

        for config in configs:
            try:
                # Validate server script exists
                script_path = Path(config.script_path)
                if not script_path.exists():
                    logger.error(f"MCP server script not found: {script_path}")
                    continue

                # Create server parameters with script path and arguments
                args = [str(script_path)] + (config.args or [])
                server_params = StdioServerParameters(
                    command=sys.executable,
                    args=args,
                    env=os.environ.copy()  # Pass current environment variables to MCP server
                )

                # Use AsyncExitStack to properly manage async context managers.
                # This ensures __aenter__ / __aexit__ are tracked through a single
                # exit stack, avoiding the anyio "Attempted to exit cancel scope
                # in a different task than it was entered in" error caused by
                # manually calling __aenter__ / __aexit__ on stdio_client and
                # ClientSession from potentially different asyncio tasks.
                stdio_ctx = stdio_client(server_params)
                read_stream, write_stream = await self._exit_stack.enter_async_context(stdio_ctx)

                # Create client session and register it with the exit stack
                mcp_session = ClientSession(read_stream, write_stream)
                await self._exit_stack.enter_async_context(mcp_session)
                await mcp_session.initialize()

                self.mcp_sessions[config.name] = mcp_session

                logger.info(f"Connected to MCP server: {config.name}")
                success = True

            except Exception as e:
                logger.error(f"Failed to connect to MCP server {config.name}: {e}")

        return success

    async def get_available_tools(self) -> Dict[str, Dict[str, Any]]:
        """Get available tools from all MCP servers"""
        all_tools = {}
        self._tool_routing.clear()

        for server_name, session in self.mcp_sessions.items():
            try:
                tools_result = await session.list_tools()
                server_tools = {}

                for tool in tools_result.tools:
                    server_tools[tool.name] = {
                        "description": tool.description,
                        "inputSchema": tool.inputSchema
                    }
                    self._tool_routing[tool.name] = server_name

                all_tools[server_name] = server_tools
                logger.info(f"Got {len(server_tools)} tools from {server_name}")

            except Exception as e:
                logger.error(f"Failed to get tools from {server_name}: {e}")

        return all_tools

    async def execute_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Any:
        """Execute a tool call, routed to the correct MCP server"""
        # Route to the correct server using the tool→server mapping built during discovery
        target_server = self._tool_routing.get(tool_name)
        if target_server and target_server in self.mcp_sessions:
            try:
                result = await self.mcp_sessions[target_server].call_tool(tool_name, arguments)
                return self._format_tool_result(result)
            except Exception as e:
                logger.error(f"Tool '{tool_name}' failed on server '{target_server}': {e}")

        # Fallback: try all servers (for tools that might have been added after discovery)
        for server_name, session in self.mcp_sessions.items():
            if server_name == target_server:
                continue  # already tried above
            try:
                result = await session.call_tool(tool_name, arguments)
                return self._format_tool_result(result)
            except Exception as e:
                continue

        return {"error": f"Tool '{tool_name}' not found or execution failed"}

    async def read_text_resource(self, server_name: str, uri: str) -> str:
        """Read a text resource from a connected MCP server."""
        session = self.mcp_sessions.get(server_name)
        if session is None:
            return ""
        result = await session.read_resource(uri)
        for content in getattr(result, "contents", []):
            text = getattr(content, "text", None)
            if text is not None:
                return str(text)
        return ""

    def _format_tool_result(self, result: Any) -> Any:
        """Format tool execution result"""
        if hasattr(result, 'content') and result.content:
            content = result.content[0]
            if hasattr(content, 'text'):
                try:
                    # Try to parse as JSON
                    import json
                    return json.loads(content.text)
                except:
                    # Return as string if not JSON
                    return content.text

        return result

    async def close(self):
        """Close all MCP connections"""
        try:
            if self._exit_stack:
                await self._exit_stack.aclose()
        except RuntimeError as e:
            # anyio 4.x may raise RuntimeError when cancel scopes are exited
            # from a different asyncio task during cascading cleanup.
            # This is benign — all resources are already shutting down.
            logger.debug(f"MCP cleanup note (non-critical): {e}")
        except Exception as e:
            logger.error(f"Error closing MCP connections: {e}")
        finally:
            self._exit_stack = None
            self.mcp_sessions.clear()
