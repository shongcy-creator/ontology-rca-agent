#!/usr/bin/env python3
"""Implementation for the bird.tool_server.sqlite_mcp module."""

import argparse
import asyncio
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import mcp.types as types
from mcp.server import stdio
from mcp.server.lowlevel import NotificationOptions, Server
from mcp.server.models import InitializationOptions

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))  # project root -> evoontology package


class SQLiteMCPServer:
    """Implementation of SQLiteMCPServer."""

    def __init__(self, db_path: str, semantic_store: str = "", semantic_version: str = ""):
        self.db_path = Path(db_path)
        if not self.db_path.exists():
            raise FileNotFoundError(f"Database file does not exist: {db_path}")
        self.db_name = self.db_path.stem
        self.server = Server("sqlite-mcp")
        self._semantic_layer: Optional[Any] = None
        if semantic_store and Path(semantic_store).exists():
            try:
                from tceo.runtime import BIRDSemanticLayer
                self._semantic_layer = BIRDSemanticLayer(semantic_store, version=semantic_version)
            except Exception:
                pass
        self._register_handlers()

    def _get_conn(self) -> sqlite3.Connection:
        """Return conn."""
        conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _register_handlers(self):
        @self.server.list_tools()
        async def handle_list_tools() -> List[types.Tool]:
            return [
                types.Tool(
                    name="get_database_info",
                    description="Return an overview of all database tables, column counts, and row counts. "
                                "Call this at the start of exploration to discover the available tables.",
                    inputSchema={
                        "type": "object",
                        "properties": {},
                        "required": [],
                    },
                ),
                types.Tool(
                    name="describe_table",
                    description="Describe one table, including columns, types, primary keys, and foreign-key references "
                                "(via PRAGMA foreign_key_list), plus three sample rows. "
                                "Call this before using a table.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "table_name": {
                                "type": "string",
                                "description": "Name of the table to describe",
                            },
                        },
                        "required": ["table_name"],
                    },
                ),
                types.Tool(
                    name="execute_query",
                    description="Execute a read-only SELECT or PRAGMA query. "
                                "At most 100 rows are returned for validation and data retrieval.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "query": {
                                "type": "string",
                                "description": "SQL query (SELECT or PRAGMA only)",
                            },
                            "limit": {
                                "type": "integer",
                                "description": "Maximum returned rows (default: 20; maximum: 100)",
                                "default": 20,
                                "minimum": 1,
                                "maximum": 100,
                            },
                        },
                        "required": ["query"],
                    },
                ),
            ]

        @self.server.call_tool()
        async def handle_call_tool(name: str, arguments: Dict[str, Any]) -> List[types.TextContent]:
            try:
                if name == "get_database_info":
                    result = self._get_database_info()
                elif name == "describe_table":
                    result = self._describe_table(arguments["table_name"])
                elif name == "execute_query":
                    result = self._execute_query(
                        arguments["query"],
                        arguments.get("limit", 20),
                    )
                else:
                    raise ValueError(f"Unknown tool: {name}")

                return [types.TextContent(
                    type="text",
                    text=json.dumps(result, ensure_ascii=False, default=str),
                )]
            except Exception as e:
                return [types.TextContent(
                    type="text",
                    text=json.dumps({"error": True, "message": str(e)}, ensure_ascii=False),
                )]

    # =========================================================================
    # Tool: get_database_info
    # =========================================================================

    def _get_database_info(self) -> dict:
        conn = self._get_conn()
        try:

            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
            table_names = [row[0] for row in cursor.fetchall()]

            tables = []
            schema_for_bind: Dict[str, list] = {}
            for tname in table_names:
                cols = conn.execute(
                    f"PRAGMA table_info('{tname}')"
                ).fetchall()
                col_count = len(cols)
                row_count = conn.execute(
                    f"SELECT COUNT(*) FROM \"{tname}\""
                ).fetchone()[0]
                tables.append({
                    "name": tname,
                    "columns_count": col_count,
                    "row_count": row_count,
                })
                schema_for_bind[tname] = [
                    {"name": c[1], "type": c[2]} for c in cols
                ]


            if self._semantic_layer:
                try:
                    self._semantic_layer.bind_schema(schema_for_bind)
                except Exception:
                    pass

            return {
                "db_name": self.db_name,
                "table_count": len(tables),
                "tables": tables,
            }
        finally:
            conn.close()

    # =========================================================================
    # Tool: describe_table
    # =========================================================================

    def _describe_table(self, table_name: str) -> dict:
        conn = self._get_conn()
        try:

            exists = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (table_name,)
            ).fetchone()
            if not exists:
                return {"error": True, "message": f"Table '{table_name}' does not exist"}


            cols = conn.execute(f"PRAGMA table_info('{table_name}')").fetchall()


            fk_list = conn.execute(
                f"PRAGMA foreign_key_list('{table_name}')"
            ).fetchall()
            fk_map = {}  # from_col -> "ref_table.ref_col"
            for fk in fk_list:
                fk_map[fk[3]] = f"{fk[2]}.{fk[4]}"

            columns = []
            for col in cols:
                cid, name, ctype, not_null, default, pk = col
                columns.append({
                    "name": name,
                    "type": ctype,
                    "nullable": not not_null,
                    "is_pk": bool(pk),
                    "is_fk": name in fk_map,
                    "fk_refs": fk_map.get(name),
                })


            try:
                sample = conn.execute(
                    f"SELECT * FROM \"{table_name}\" LIMIT 3"
                ).fetchall()
                col_names = [c["name"] for c in columns]
                sample_rows = [
                    {col_names[i]: row[i] for i in range(len(col_names))}
                    for row in sample
                ]
            except Exception:
                sample_rows = []


            row_count = conn.execute(
                f"SELECT COUNT(*) FROM \"{table_name}\""
            ).fetchone()[0]


            if self._semantic_layer:
                try:
                    columns = self._semantic_layer.enrich_schema(
                        table_name, columns,
                    )
                except Exception:
                    pass

            return {
                "table_name": table_name,
                "row_count": row_count,
                "columns": columns,
                "sample_rows": sample_rows,
            }
        finally:
            conn.close()

    # =========================================================================
    # Tool: execute_query
    # =========================================================================

    def _execute_query(self, query: str, limit: int = 20) -> dict:
        query_upper = query.strip().upper()

        if not (query_upper.startswith("SELECT") or query_upper.startswith("PRAGMA")):
            return {
                "error": True,
                "message": f"Only SELECT and PRAGMA queries are allowed; received: {query[:50]}",
            }


        if query_upper.startswith("SELECT") and "LIMIT" not in query_upper:
            query = query.rstrip(";") + f" LIMIT {limit}"

        limit = max(1, min(int(limit), 100))
        conn = self._get_conn()
        try:
            cursor = conn.execute(query)
            rows = cursor.fetchall()
            columns = [d[0] for d in cursor.description] if cursor.description else []

            return {
                "columns": columns,
                "rows": [list(row) for row in rows],
                "row_count": len(rows),
                "truncated": len(rows) >= limit if limit else False,
            }
        except Exception as e:
            return {"error": True, "message": str(e)}
        finally:
            conn.close()

    async def run(self):
        """Run the requested value."""
        async with stdio.stdio_server() as (read_stream, write_stream):
            await self.server.run(
                read_stream,
                write_stream,
                InitializationOptions(
                    server_name="sqlite-mcp",
                    server_version="1.0.0",
                    capabilities=self.server.get_capabilities(
                        notification_options=NotificationOptions(),
                        experimental_capabilities={},
                    ),
                ),
            )


async def main():
    parser = argparse.ArgumentParser(description="SQLite MCP Server")
    parser.add_argument("--db-path", required=True, help="Path to the SQLite database file")
    parser.add_argument("--semantic-store", default="",
                        help="Optional ontology-layer directory for column annotations")
    parser.add_argument("--version", default="",
                        help="Explicit semantic version to serve (default: active)")
    args = parser.parse_args()

    server = SQLiteMCPServer(args.db_path, args.semantic_store, args.version)
    await server.run()


if __name__ == "__main__":
    asyncio.run(main())
