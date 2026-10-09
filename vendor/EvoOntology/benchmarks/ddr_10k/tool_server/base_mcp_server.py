#!/usr/bin/env python3
"""
Base MCP Server with common functionality
Shared components for all MCP servers to eliminate code duplication
"""

import asyncio
import csv
import json
import logging
import os
import time
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

import mcp.server.stdio
import mcp.types as types
from mcp.server.lowlevel import NotificationOptions, Server
from mcp.server.models import InitializationOptions


def setup_logging(log_level: str = "INFO", log_file: str = None, server_name: str = "mcp"):
    """Setup enhanced logging configuration"""

    # If no log file specified, create one with timestamp
    if log_file is None:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        # Check for custom log directory from environment variable
        custom_log_dir = os.getenv('CUSTOM_LOG_DIR')
        if custom_log_dir:
            log_file = f"{custom_log_dir}/{server_name}_server_{timestamp}.log"
        else:
            log_file = f"./logs/{server_name}_server_{timestamp}.log"

    # Ensure log directory exists
    log_path = Path(log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    # Create formatter with more details
    formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - [%(funcName)s:%(lineno)d] - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

    # Setup root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, log_level.upper()))

    # Clear existing handlers
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)

    # Only use file handler - no console output
    file_handler = logging.FileHandler(log_file, mode='a', encoding='utf-8')
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)

    # Log session start separator
    parent_pid = os.getppid()
    root_logger.info("=" * 80)
    root_logger.info(f"{server_name.upper()} MCP SERVER SESSION STARTED - PID: {os.getpid()}, PPID: {parent_pid}")
    root_logger.info(f"Timestamp: {datetime.now().isoformat()}")
    root_logger.info(f"Log file: {log_file}")
    root_logger.info("=" * 80)

    return root_logger


class CSVLogger:
    """Simple CSV logger for MCP server operations"""

    def __init__(self, csv_file: str, extra_headers: List[str] = None):
        self.csv_file = csv_file
        self.csv_path = Path(csv_file)

        # Ensure directory exists
        self.csv_path.parent.mkdir(parents=True, exist_ok=True)

        # CSV headers
        self.headers = [
            'timestamp',
            'tool_name',
            'params',
            'result_content',
            'success'
        ]
        self.extra_headers = extra_headers or []
        self.headers.extend(self.extra_headers)

        # Initialize CSV file with headers if it doesn't exist
        if not self.csv_path.exists():
            with open(self.csv_file, 'w', newline='', encoding='utf-8') as f:
                writer = csv.writer(f)
                writer.writerow(self.headers)

    def log_tool_call(self, tool_name: str, params: Dict[str, Any], result_content: str, success: bool,
                      extra_values: Dict[str, Any] = None):
        """Log a tool call to CSV"""
        try:
            with open(self.csv_file, 'a', newline='', encoding='utf-8') as f:
                writer = csv.writer(f)
                row = [
                    datetime.now().isoformat(),
                    tool_name,
                    json.dumps(params, default=str, ensure_ascii=False),
                    result_content,
                    success
                ]
                # Append extra values in header order
                for h in self.extra_headers:
                    row.append((extra_values or {}).get(h, ""))
                writer.writerow(row)
        except Exception as e:
            # Fallback to basic logging if CSV fails
            print(f"CSV logging failed: {e}")


class BaseMCPServer(ABC):
    """Base class for MCP servers with common functionality"""

    def __init__(self, server_name: str, enable_detailed_logging: bool = True):
        self.server_name = server_name
        self.server = Server(server_name)
        self.enable_detailed_logging = enable_detailed_logging
        self.session_start_time = datetime.now()
        self.call_counter = 0
        self.client_stats = {
            "total_calls": 0,
            "tool_calls": {},
            "resource_reads": {},
            "errors": 0,
            "start_time": self.session_start_time.isoformat()
        }

        # Initialize logger - get the root logger that was set up by setup_logging
        self.logger = logging.getLogger()

        # Initialize CSV logger
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        # Check for custom log directory from environment variable
        custom_log_dir = os.getenv('CUSTOM_LOG_DIR')
        if custom_log_dir:
            csv_file = f"{custom_log_dir}/{server_name.replace('_', '-')}_calls_{timestamp}.csv"
        else:
            csv_file = f"./logs/{server_name.replace('_', '-')}_calls_{timestamp}.csv"

        # Extra CSV columns (subclasses can override)
        extra_csv_headers = self._get_extra_csv_headers()
        self.csv_logger = CSVLogger(csv_file, extra_headers=extra_csv_headers)

        self._setup_common_handlers()
        self._setup_specific_handlers()

    def _get_extra_csv_headers(self) -> List[str]:
        """Override in subclass to add extra CSV columns. Default: none."""
        return []

    def _log_client_call(self, call_type: str, details: Dict[str, Any] = None):
        """Log client call with details"""
        self.call_counter += 1
        self.client_stats["total_calls"] += 1

        if self.enable_detailed_logging:
            log_msg = f"📞 Client Call #{self.call_counter}: {call_type}"
            if details:
                log_msg += f" | Details: {json.dumps(details, default=str)}"

    def _log_tool_call(self, tool_name: str, arguments: Dict[str, Any], result_content: str = "", error: str = None):
        """Log tool call with execution details"""
        # Update statistics
        if tool_name not in self.client_stats["tool_calls"]:
            self.client_stats["tool_calls"][tool_name] = {"count": 0, "errors": 0}

        self.client_stats["tool_calls"][tool_name]["count"] += 1
        if error:
            self.client_stats["tool_calls"][tool_name]["errors"] += 1
            self.client_stats["errors"] += 1

        # Log to CSV
        success = error is None
        content_to_log = error if error else result_content
        extra_values = self._get_extra_csv_values()
        self.csv_logger.log_tool_call(tool_name, arguments, content_to_log, success, extra_values=extra_values)

    def _get_extra_csv_values(self) -> Dict[str, Any]:
        """Override in subclass to add extra CSV column values. Default: empty."""
        return {}

    def _log_resource_read(self, uri: str, success: bool = True, error: str = None):
        """Log resource read operation"""
        # Update statistics
        if uri not in self.client_stats["resource_reads"]:
            self.client_stats["resource_reads"][uri] = {"count": 0, "errors": 0}

        self.client_stats["resource_reads"][uri]["count"] += 1
        if error:
            self.client_stats["resource_reads"][uri]["errors"] += 1
            self.client_stats["errors"] += 1

        # Log details
        if self.enable_detailed_logging:
            status = "❌ ERROR" if error else "✅ SUCCESS"
            error_info = f" | Error: {error}" if error else ""

    def _setup_common_handlers(self):
        """Setup common MCP handlers"""

        @self.server.list_resources()
        async def handle_list_resources() -> List[types.Resource]:
            """List available resources"""
            self._log_client_call("list_resources")

            return self._get_specific_resources()

        @self.server.read_resource()
        async def handle_read_resource(uri: str) -> types.ReadResourceResult:
            """Read resource content"""
            start_time = time.time()

            try:
                content = await self._read_specific_resource(uri)

                return types.ReadResourceResult(
                    contents=[
                        types.TextContent(
                            type="text",
                            text=content
                        )
                    ]
                )

            except Exception as e:
                execution_time = time.time() - start_time
                error_msg = str(e)
                self._log_resource_read(uri, success=False, error=error_msg)
                raise

        @self.server.list_tools()
        async def handle_list_tools() -> List[types.Tool]:
            """List available tools"""
            self._log_client_call("list_tools")

            tools = self._get_specific_tools()

            return tools

        @self.server.call_tool()
        async def handle_call_tool(name: str, arguments: Dict[str, Any]) -> List[types.TextContent]:
            """Handle tool calls"""
            # Add detailed debug log

            try:
                result = await self._handle_specific_tool_call(name, arguments)

                # Prepare result text
                result_text = json.dumps(result, indent=2, ensure_ascii=False, default=str)

                # Log successful call
                self._log_tool_call(name, arguments, result_text)

                return [
                    types.TextContent(
                        type="text",
                        text=result_text
                    )
                ]

            except Exception as e:
                error_msg = str(e)
                self._log_tool_call(name, arguments, "", error_msg)

                return [
                    types.TextContent(
                        type="text",
                        text=json.dumps({"error": error_msg}, ensure_ascii=False)
                    )
                ]

    @abstractmethod
    def _setup_specific_handlers(self):
        """Setup server-specific handlers - implemented by subclasses"""
        pass

    @abstractmethod
    def _get_specific_resources(self) -> List[types.Resource]:
        """Get server-specific resources - implemented by subclasses"""
        pass

    @abstractmethod
    async def _read_specific_resource(self, uri: str) -> str:
        """Read server-specific resource - implemented by subclasses"""
        pass

    @abstractmethod
    def _get_specific_tools(self) -> List[types.Tool]:
        """Get server-specific tools - implemented by subclasses"""
        pass

    @abstractmethod
    async def _handle_specific_tool_call(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Handle server-specific tool call - implemented by subclasses"""
        pass

    async def run(self):
        """Run MCP server"""
        async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
            await self.server.run(
                read_stream,
                write_stream,
                InitializationOptions(
                    server_name=self.server_name,
                    server_version="1.0.0",
                    capabilities=self.server.get_capabilities(
                        notification_options=NotificationOptions(),
                        experimental_capabilities={}
                    )
                )
            )