# -*- coding: utf-8 -*-
from __future__ import annotations
from typing import Any, Optional, List
from pydantic import BaseModel, Field


class ChatMessage(BaseModel):
    role: str
    content: str = ""
    tool_calls: Optional[List[dict]] = None
    tool_call_id: Optional[str] = None
    name: Optional[str] = None
    incident_id: Optional[str] = None


class ChatRequest(BaseModel):
    messages: List[ChatMessage]
    session_id: str = "default"


class ChatResponse(BaseModel):
    message: ChatMessage
    session_id: str
    incident_id: Optional[str] = None


class RCAAlert(BaseModel):
    alertId: str = "USER-INPUT"
    severity: str = "P1"
    message: str = ""


class RCATopologyNode(BaseModel):
    id: str
    name: str = ""
    type: str = ""
    scope: str = ""
    relation_type: str = ""
    definition: str = ""


class RCATopologyEdge(BaseModel):
    id: str = ""
    source: str = ""
    target: str = ""
    relation_type: str = ""
    condition: str = ""


class RCAPathHop(BaseModel):
    from_: str = Field(default="", alias="from")
    to: str = ""
    relation: str = ""
    relation_type: str = ""
    condition: str = ""

    model_config = {"populate_by_name": True}


class OntoRef(BaseModel):
    """本体里的一个关联对象（归因对象 / 观测佐证）。"""
    id: str = ""
    label: str = ""
    condition: str = ""
    description: str = ""


class OntoConstraint(BaseModel):
    """以该根因术语为 target 的本体约束（判据口径）。"""
    id: str = ""
    description: str = ""
    severity: str = ""
    scope: str = ""
    constraint_type: str = ""
    trigger_keywords: List[str] = Field(default_factory=list)


class RCACandidate(BaseModel):
    """
    候选/最终根因。

    ⚠ 这里必须 `extra="allow"`：本体驱动的解释性字段（机制/归因对象/观测佐证/判据…）
    会随本体演进而增加。Pydantic 默认**静默丢弃未声明字段** —— 踩过的坑正是如此：
    引擎与数据库里都有完整的 17 个字段，但经这个模型序列化后只剩 5 个
    （category/entity_id/entity_name/confidence/reason），
    于是界面上「根因结论」又退回成一句"资源类 rc:tmp-disk"。
    显式声明的字段用于类型与文档，其余字段原样透传。
    """
    model_config = {"extra": "allow"}

    category: str = ""
    entity_id: str = ""
    entity_name: str = ""
    confidence: float = 0.0
    reason: str = ""
    # ── 结论的可解释字段（本体驱动）──
    definition: str = ""
    rationale: str = ""
    role: str = ""
    lifecycle: str = ""
    scope: str = ""
    relation_type: str = ""
    scoring_source: str = ""
    matched_keywords: List[str] = Field(default_factory=list)
    affected: List[OntoRef] = Field(default_factory=list)
    evidenced_by: List[OntoRef] = Field(default_factory=list)
    constraints: List[OntoConstraint] = Field(default_factory=list)
    description: str = ""


class RCAChainStep(BaseModel):
    step: int = 0
    type: str = ""
    description: str = ""
    keywords: Optional[List[str]] = None
    source: Optional[str] = None


class RCAResult(BaseModel):
    incident_id: str = ""
    alert: dict = Field(default_factory=dict)
    evidence: dict = Field(default_factory=dict)
    thresholds_triggered: List[dict] = Field(default_factory=list)
    topology: List[RCATopologyNode] = Field(default_factory=list)
    topology_edges: List[RCATopologyEdge] = Field(default_factory=list)
    root_cause_path: List[RCAPathHop] = Field(default_factory=list)
    candidates: List[RCACandidate] = Field(default_factory=list)
    root_cause: Optional[RCACandidate] = None
    confidence: float = 0.0
    rca_chain: List[RCAChainStep] = Field(default_factory=list)
    elapsed_ms: float = 0.0


class RCARequest(BaseModel):
    alert: RCAAlert
    session_id: str = "default"


class RCAResponse(BaseModel):
    result: RCAResult
    session_id: str


class TopologyRequest(BaseModel):
    app_name: str = "payment-app"
    session_id: str = "default"


class TopologyResponse(BaseModel):
    nodes: List[RCATopologyNode]
    session_id: str = "default"


class MetricQueryRequest(BaseModel):
    query: str
    session_id: str = "default"


class MetricQueryResponse(BaseModel):
    query: str
    result_type: str = "vector"
    series: List[dict] = Field(default_factory=list)
    session_id: str = "default"
