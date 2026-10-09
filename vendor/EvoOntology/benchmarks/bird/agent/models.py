#!/usr/bin/env python3
"""Implementation for the bird.agent.models module."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional


@dataclass
class AgentMessage:
    """Implementation of AgentMessage."""
    role: str                               # agent | environment | system
    content: str
    timestamp: str = ""
    tool_call: Optional[Dict[str, Any]] = None
    tool_result: Optional[Any] = None

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.now().isoformat()


@dataclass
class AgentSession:
    """Implementation of AgentSession."""
    session_id: str
    task: str
    db_id: str
    db_path: str
    start_time: datetime = field(default_factory=datetime.now)
    messages: List[AgentMessage] = field(default_factory=list)
    available_tools: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    completed: bool = False
    pred_sql: str = ""
    total_turns: int = 0


@dataclass
class EvalResult:
    """Implementation of EvalResult."""
    question_id: int
    db_id: str
    question: str
    difficulty: str
    condition: str                          # baseline | semantic
    pred_sql: str
    gold_sql: str
    ex: bool                                # Execution Accuracy
    turns: int
    ves: float = 0.0
    error: str = ""
    semantic_tool_calls: int = 0
