#!/usr/bin/env python3
"""Implementation for the bird.config module."""

import os
import yaml
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class MCPServerConfig:
    """Implementation of MCPServerConfig."""
    name: str
    module: str
    description: str = ""
    args: list = field(default_factory=list)


@dataclass
class AgentConfig:
    """Implementation of AgentConfig."""
    model: str = "gpt-4o"
    temperature: float = 0.0
    max_turns: int = 15
    provider: str = "openai"
    base_url: str = ""
    api_key_env: str = "BIRD_AGENT_API_KEY"


@dataclass
class SemanticConfig:
    """Implementation of SemanticConfig."""
    enabled: bool = False
    store_path: str = ""
    version: str = ""


@dataclass
class ExperimentConfig:
    """Implementation of ExperimentConfig."""
    condition: str = "baseline"                   # baseline | semantic
    agent: AgentConfig = field(default_factory=AgentConfig)
    semantic: SemanticConfig = field(default_factory=SemanticConfig)
    mcp_servers: list = field(default_factory=list)

    @classmethod
    def from_yaml(cls, path: str) -> "ExperimentConfig":
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        agent = AgentConfig(**data.get("agent", {}))
        semantic = SemanticConfig(**data.get("semantic", {}))
        mcp_servers = []
        for s in data.get("mcp_servers", []):
            mcp_servers.append({
                "name": s["name"],
                "module": s["module"],
                "description": s.get("description", ""),
                "args": s.get("args", []),
            })

        return cls(
            condition=data.get("condition", "baseline"),
            agent=agent,
            semantic=semantic,
            mcp_servers=mcp_servers,
        )



BIRD_DIR = Path(__file__).resolve().parent
DATA_DIR = BIRD_DIR / "data"
MINI_DEV_DATA = DATA_DIR / "mini_dev_data"
DB_DIR = MINI_DEV_DATA / "dev_databases"
SEMANTIC_LAYER_DIR = BIRD_DIR / ".evoontology"
RESULTS_DIR = BIRD_DIR / "results"
CONFIGS_DIR = BIRD_DIR / "configs"


DATASET_PATHS = {
    "minidev": {
        "questions": MINI_DEV_DATA / "minidev" / "mini_dev_sqlite.json",
        "gold_sql": MINI_DEV_DATA / "minidev" / "mini_dev_sqlite_gold.sql",
        "split_dir": DATA_DIR / "minidev" / "test",
    },
    "dev": {
        "questions": MINI_DEV_DATA / "dev" / "dev.json",
        "gold_sql": MINI_DEV_DATA / "dev" / "dev.sql",
        "split_dir": DATA_DIR / "dev" / "test",
    },
}

def get_dataset_config(dataset: str = "minidev") -> dict:
    """Return dataset config."""
    if dataset not in DATASET_PATHS:
        raise ValueError(f"Unknown dataset: {dataset}; available options: {list(DATASET_PATHS.keys())}")
    return DATASET_PATHS[dataset]
