#!/usr/bin/env python3
"""Implementation for the bird.run_agent module."""

import asyncio
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent))          # benchmarks/bird
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))      # project root -> evoontology package

from config import SEMANTIC_LAYER_DIR, ExperimentConfig


async def main():
    import argparse
    parser = argparse.ArgumentParser(description="BIRD ReAct Agent - single-question run")
    parser.add_argument("--config", required=True, help="Path to the YAML configuration file")
    parser.add_argument("--db-path", required=True, help="Path to the SQLite database file")
    parser.add_argument("--question", required=True, help="Natural-language question")
    parser.add_argument("--db-id", default="", help="Database ID used to locate the ontology layer")
    parser.add_argument("--llm-provider", default="", help="LLM provider override")
    parser.add_argument("--model", default="", help="Model name override")
    parser.add_argument("--api-key", default="", help="API key override")
    parser.add_argument("--base-url", default="", help="API base URL override")
    parser.add_argument("--temperature", type=float, default=-1.0,
                        help="Temperature override (default: 0)")
    parser.add_argument("--split", default="",
                        help="Split label recorded in the trajectory (e.g. train/test)")
    parser.add_argument("--semantic-version", default="",
                        help="Explicit semantic version to use (overrides the config)")
    args = parser.parse_args()


    config = ExperimentConfig.from_yaml(args.config)
    if args.semantic_version:
        config.semantic.version = args.semantic_version


    provider = args.llm_provider or config.agent.provider
    model = args.model or config.agent.model
    temperature = args.temperature if args.temperature >= 0 else config.agent.temperature


    from agent.llm_providers import create_llm_provider
    llm = create_llm_provider(
        provider,
        model=model,
        temperature=temperature,
        api_key=args.api_key or os.getenv(config.agent.api_key_env) or None,
        base_url=args.base_url or config.agent.base_url or None,
    )
    print(f"LLM: {llm.get_provider_name()}")


    db_id = args.db_id or Path(args.db_path).stem

    mcp_configs = []
    for s in config.mcp_servers:
        server_args = list(s.get("args", []))

        for i, val in enumerate(server_args):
            if val == "" or val is None:
                if i > 0 and server_args[i - 1] == "--db-path":
                    server_args[i] = args.db_path
                elif i > 0 and server_args[i - 1] == "--store":
                    store_path = config.semantic.store_path or str(
                        SEMANTIC_LAYER_DIR / db_id
                    )
                    server_args[i] = store_path
                elif i > 0 and server_args[i - 1] == "--db-id":
                    server_args[i] = db_id
                elif i > 0 and server_args[i - 1] == "--version":
                    server_args[i] = config.semantic.version
        if (str(s.get("module", "")).endswith(("semantic_mcp", "sqlite_mcp"))
                and config.semantic.version and "--version" not in server_args):
            server_args.extend(["--version", config.semantic.version])

        mcp_configs.append({
            "name": s["name"],
            "module": s.get("module", ""),
            "args": server_args,
            "description": s.get("description", ""),
        })


    semantic_manifest = ""
    if config.semantic.enabled:
        store_path = config.semantic.store_path or str(SEMANTIC_LAYER_DIR / db_id)
        try:
            from tceo.runtime import BIRDSemanticLayer
            layer = BIRDSemanticLayer(store_path, version=config.semantic.version)
            semantic_manifest = layer.manifest(db_id=db_id)
            print(f"Ontology layer loaded: {store_path}")
            print(f"  {layer.loader.stats()}")
        except FileNotFoundError:
            print(f"⚠ Ontology-layer directory does not exist: {store_path}")
            print("  Running as the baseline without semantic tools")
        except Exception as e:
            print(f"⚠ Failed to load the ontology layer: {e}")


    from tool_server.mcp_client import MCPClientManager
    mcp_client = MCPClientManager()
    ok = await mcp_client.connect_to_servers(mcp_configs)
    if not ok:
        print("❌ Could not connect to any MCP server")
        return


    from agent.data_agent import BIRDReActAgent
    agent = BIRDReActAgent(
        llm_provider=llm,
        mcp_client=mcp_client,
        max_turns=config.agent.max_turns,
        semantic_manifest=semantic_manifest,
    )

    try:

        session = await agent.start_session(
            question=args.question,
            db_id=db_id,
            db_path=args.db_path,
        )
        print(f"\nQuestion: {args.question}")
        print(f"Condition: {config.condition}")
        print(f"{'='*60}")

        await agent.run()

        print(f"\n{'='*60}")
        print(f"Completed: {session.completed}")
        print(f"Total turns: {session.total_turns}")
        print(f"Semantic tool calls: {agent.semantic_call_count}")
        print(f"Predicted SQL:\n{session.pred_sql}")


        if session.pred_sql:
            try:
                from tool_server.sqlite_mcp import SQLiteMCPServer
                sqlite = SQLiteMCPServer(args.db_path)
                result = sqlite._execute_query(session.pred_sql)
                print(f"\nExecution result: {json.dumps(result, ensure_ascii=False, default=str)[:300]}")
            except Exception as e:
                print(f"\nExecution failed: {e}")

        if config.semantic.enabled:
            from trajectory_recorder import record_trajectory

            store_path = config.semantic.store_path or str(SEMANTIC_LAYER_DIR / db_id)
            record_trajectory(
                store_path,
                db_id=db_id,
                question=args.question,
                split=args.split,
                messages=agent.export_trace().get("messages", []),
                final_answer=session.pred_sql,
                task_status="completed",
            )

    finally:
        await mcp_client.close()


if __name__ == "__main__":
    asyncio.run(main())
