"""Implementation for the insightbench.insightbench.tceo.models module."""

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

# ============================================================

# ============================================================

@dataclass(frozen=True)
class _Scope:
    benchmark: str = ""
    domain: str = ""
    dataset_family: Optional[str] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "_Scope":
        return cls(
            benchmark=data.get("benchmark", ""),
            domain=data.get("domain", ""),
            dataset_family=data.get("dataset_family"),
        )


@dataclass(frozen=True)
class _EvidenceCompat:
    origin: str = ""
    source_refs: List[str] = field(default_factory=list)
    supporting_episode_ids: List[str] = field(default_factory=list)
    counterexample_episode_ids: List[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "_EvidenceCompat":
        return cls(
            origin=data.get("origin", ""),
            source_refs=list(data.get("source_refs", [])),
            supporting_episode_ids=list(data.get("supporting_episode_ids", [])),
            counterexample_episode_ids=list(data.get("counterexample_episode_ids", [])),
        )


def _evidence_from(data: Dict[str, Any]) -> _EvidenceCompat:
    evidence_raw = data.get("evidence", {})
    if isinstance(evidence_raw, dict):
        return _EvidenceCompat.from_dict(evidence_raw)
    if isinstance(evidence_raw, list):
        return _EvidenceCompat(source_refs=[str(e) for e in evidence_raw])
    return _EvidenceCompat()


def _mapping_evidence_refs(data: Dict[str, Any], ev: _EvidenceCompat) -> List[str]:
    refs = list(data.get("evidence_refs", []))
    if refs:
        return refs
    validation_raw = data.get("validation", {})
    if isinstance(validation_raw, dict):
        val_evidence = validation_raw.get("evidence", [])
        if isinstance(val_evidence, list):
            return [str(e) for e in val_evidence]
    return list(ev.source_refs)


@dataclass(frozen=True)
class _Lifecycle:
    state: str = "active"
    introduced_in: str = ""
    supersedes: Optional[str] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "_Lifecycle":
        return cls(
            state=data.get("state", "active"),
            introduced_in=data.get("introduced_in", ""),
            supersedes=data.get("supersedes"),
        )


@dataclass(frozen=True)
class _Confidence:
    level: str = "medium"
    reason: str = ""

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "_Confidence":
        return cls(
            level=data.get("level", "medium"),
            reason=data.get("reason", ""),
        )


# ============================================================

# ============================================================

@dataclass(frozen=True)
class Term:
    """Implementation of Term."""
    # === Canonical fields ===
    id: str
    name: str = ""
    type: str = ""
    definition: str = ""
    scope: str = ""
    aliases: List[str] = field(default_factory=list)
    evidence_refs: List[str] = field(default_factory=list)


    value_type: str = ""
    grain: str = ""
    module_id: str = ""
    lifecycle_state: str = "active"
    lifecycle_introduced_in: str = ""
    lifecycle_supersedes: Optional[str] = None
    confidence_level: str = "medium"
    confidence_reason: str = ""
    evidence_origin: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "type": self.type,
            "definition": self.definition,
            "scope": self.scope,
            "aliases": list(self.aliases),
            "evidence_refs": list(self.evidence_refs),
            "value_type": self.value_type,
            "grain": self.grain,
            "module_id": self.module_id,
            "lifecycle_state": self.lifecycle_state,
            "lifecycle_introduced_in": self.lifecycle_introduced_in,
            "lifecycle_supersedes": self.lifecycle_supersedes,
            "confidence_level": self.confidence_level,
            "confidence_reason": self.confidence_reason,
            "evidence_origin": self.evidence_origin,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Term":
        scope_raw = data.get("scope", {})
        if isinstance(scope_raw, dict):
            s = _Scope.from_dict(scope_raw)
            scope_str = s.domain or s.benchmark or ""
        else:
            scope_str = str(scope_raw) if scope_raw else ""

        lifecycle_raw = data.get("lifecycle", {})
        lc = _Lifecycle.from_dict(lifecycle_raw) if isinstance(lifecycle_raw, dict) else _Lifecycle()

        confidence_raw = data.get("confidence", {})
        cf = _Confidence.from_dict(confidence_raw) if isinstance(confidence_raw, dict) else _Confidence()

        ev = _evidence_from(data)

        return cls(
            id=data["id"],
            name=data.get("name", data.get("label", "")),
            type=data.get("type", data.get("kind", "")),
            definition=data.get("definition", ""),
            scope=scope_str,
            aliases=list(data.get("aliases", [])),
            evidence_refs=list(ev.source_refs),
            value_type=data.get("value_type", ""),
            grain=data.get("grain", ""),
            module_id=data.get("module_id", ""),
            lifecycle_state=lc.state,
            lifecycle_introduced_in=lc.introduced_in,
            lifecycle_supersedes=lc.supersedes,
            confidence_level=cf.level,
            confidence_reason=cf.reason,
            evidence_origin=ev.origin,
        )


RELATION_TYPES = frozenset({
    "association", "hierarchy", "composition", "equivalence", "derivation",
})


_RELATION_TYPE_MAP = {
    "is_a": "hierarchy",
    "part_of": "composition",
    "derived_from": "derivation",
    "joinable_with": "association",
    "measured_by": "association",
}


@dataclass(frozen=True)
class Relation:
    """Implementation of Relation."""
    # === Canonical fields ===
    id: str
    source: str = ""
    relation_type: str = ""
    target: str = ""
    connection_condition: str = ""
    description: str = ""
    scope: str = ""
    evidence_refs: List[str] = field(default_factory=list)


    module_id: str = ""
    lifecycle_state: str = "active"
    lifecycle_introduced_in: str = ""
    confidence_level: str = "medium"
    confidence_reason: str = ""
    evidence_origin: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "source": self.source,
            "relation_type": self.relation_type,
            "target": self.target,
            "connection_condition": self.connection_condition,
            "description": self.description,
            "scope": self.scope,
            "evidence_refs": list(self.evidence_refs),
            "module_id": self.module_id,
            "lifecycle_state": self.lifecycle_state,
            "lifecycle_introduced_in": self.lifecycle_introduced_in,
            "confidence_level": self.confidence_level,
            "confidence_reason": self.confidence_reason,
            "evidence_origin": self.evidence_origin,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Relation":
        scope_raw = data.get("scope", {})
        if isinstance(scope_raw, dict):
            s = _Scope.from_dict(scope_raw)
            scope_str = s.domain or s.benchmark or ""
        else:
            scope_str = str(scope_raw) if scope_raw else ""

        lifecycle_raw = data.get("lifecycle", {})
        lc = _Lifecycle.from_dict(lifecycle_raw) if isinstance(lifecycle_raw, dict) else _Lifecycle()
        confidence_raw = data.get("confidence", {})
        cf = _Confidence.from_dict(confidence_raw) if isinstance(confidence_raw, dict) else _Confidence()
        ev = _evidence_from(data)

        raw_type = data.get("type", data.get("relation_type", ""))
        relation_type = _RELATION_TYPE_MAP.get(raw_type, raw_type)

        return cls(
            id=data["id"],
            source=data.get("source", data.get("from_term", "")),
            relation_type=relation_type,
            target=data.get("target", data.get("to_term", "")),
            connection_condition=data.get("connection_condition", data.get("join_meaning", "")),
            description=data.get("description", ""),
            scope=scope_str,
            evidence_refs=list(ev.source_refs),
            module_id=data.get("module_id", ""),
            lifecycle_state=lc.state,
            lifecycle_introduced_in=lc.introduced_in,
            confidence_level=cf.level,
            confidence_reason=cf.reason,
            evidence_origin=ev.origin,
        )


# ============================================================

# ============================================================

@dataclass(frozen=True)
class MatchCondition:
    """Implementation of MatchCondition."""
    data_type: str = ""
    min_null_rate: float = 0.0
    max_null_rate: float = 1.0
    min_unique_ratio: float = 0.0
    max_unique_ratio: float = 1.0
    filters: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MatchCondition":
        return cls(
            data_type=data.get("data_type", ""),
            min_null_rate=data.get("min_null_rate", 0.0),
            max_null_rate=data.get("max_null_rate", 1.0),
            min_unique_ratio=data.get("min_unique_ratio", 0.0),
            max_unique_ratio=data.get("max_unique_ratio", 1.0),
            filters=dict(data.get("filters", {})),
        )


@dataclass(frozen=True)
class SourcePattern:
    """Implementation of SourcePattern."""
    source_family: str = "insight_csv"
    column_names: List[str] = field(default_factory=list)
    table_pattern: str = ""

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SourcePattern":
        return cls(
            source_family=data.get("source_family", "insight_csv"),
            column_names=list(data.get("column_names", [])),
            table_pattern=data.get("table_pattern", ""),
        )


MAPPING_TYPES = frozenset({"exact_match", "close_match", "component", "transformation", "described_by"})


@dataclass(frozen=True)
class Mapping:
    """Implementation of Mapping."""
    # === Canonical fields ===
    id: str
    term_id: str = ""
    database_source: str = ""
    table: str = ""
    column: str = ""
    semantic_filter: str = ""
    aggregation_semantics: str = ""
    grain: str = ""
    validation: str = ""
    scope: str = ""
    evidence_refs: List[str] = field(default_factory=list)


    mapping_type: str = "close_match"
    data_type: str = ""
    column_names: List[str] = field(default_factory=list)
    min_null_rate: float = 0.0
    max_null_rate: float = 1.0
    min_unique_ratio: float = 0.0
    max_unique_ratio: float = 1.0
    lifecycle_state: str = "active"
    lifecycle_introduced_in: str = ""
    confidence_level: str = "medium"
    confidence_reason: str = ""
    evidence_origin: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "term_id": self.term_id,
            "database_source": self.database_source,
            "table": self.table,
            "column": self.column,
            "semantic_filter": self.semantic_filter,
            "aggregation_semantics": self.aggregation_semantics,
            "grain": self.grain,
            "validation": self.validation,
            "evidence_refs": list(self.evidence_refs),
            "scope": self.scope,
            "mapping_type": self.mapping_type,
            "data_type": self.data_type,
            "lifecycle_state": self.lifecycle_state,
            "lifecycle_introduced_in": self.lifecycle_introduced_in,
            "confidence_level": self.confidence_level,
            "confidence_reason": self.confidence_reason,
            "evidence_origin": self.evidence_origin,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Mapping":
        source_raw = data.get("source_pattern", {})
        sp = SourcePattern.from_dict(source_raw) if isinstance(source_raw, dict) else SourcePattern()
        match_raw = data.get("match_conditions", {})
        mc = MatchCondition.from_dict(match_raw) if isinstance(match_raw, dict) else MatchCondition()

        lifecycle_raw = data.get("lifecycle", {})
        lc = _Lifecycle.from_dict(lifecycle_raw) if isinstance(lifecycle_raw, dict) else _Lifecycle()
        confidence_raw = data.get("confidence", {})
        cf = _Confidence.from_dict(confidence_raw) if isinstance(confidence_raw, dict) else _Confidence()
        ev = _evidence_from(data)
        scope_raw = data.get("scope", "")
        if isinstance(scope_raw, dict):
            scope_obj = _Scope.from_dict(scope_raw)
            scope_str = scope_obj.domain or scope_obj.benchmark or ""
        else:
            scope_str = str(scope_raw) if scope_raw else ""

        column = sp.column_names[0] if sp.column_names else data.get("column", "")

        return cls(
            id=data["id"],
            term_id=data.get("term_id", data.get("target_term", "")),
            database_source=data.get("database_source", sp.source_family),
            table=data.get("table", sp.table_pattern),
            column=column,
            semantic_filter=data.get("semantic_filter", ""),
            aggregation_semantics=data.get("aggregation_semantics", ""),
            grain=data.get("grain", ""),
            validation=data.get("validation", ""),
            scope=scope_str,
            evidence_refs=_mapping_evidence_refs(data, ev),
            mapping_type=data.get("mapping_type", "close_match"),
            data_type=data.get("data_type", mc.data_type),
            column_names=list(data.get("column_names", sp.column_names)),
            min_null_rate=data.get("min_null_rate", mc.min_null_rate),
            max_null_rate=data.get("max_null_rate", mc.max_null_rate),
            min_unique_ratio=data.get("min_unique_ratio", mc.min_unique_ratio),
            max_unique_ratio=data.get("max_unique_ratio", mc.max_unique_ratio),
            lifecycle_state=lc.state,
            lifecycle_introduced_in=lc.introduced_in,
            confidence_level=cf.level,
            confidence_reason=cf.reason,
            evidence_origin=ev.origin,
        )



SemanticMapping = Mapping


# ============================================================
# Constraint
# ============================================================

CONSTRAINT_TYPES = frozenset({"enum_semantics", "data_quality", "business_rule", "unit", "scope"})


_CONSTRAINT_TYPE_MAP = {
    "derivation": "business_rule",
    "join": "business_rule",
    "grain": "scope",
    "validity": "data_quality",
}

SEVERITY_LEVELS = frozenset({"info", "warn", "block"})


@dataclass(frozen=True)
class Constraint:
    """Implementation of Constraint."""
    # === Canonical fields ===
    id: str
    target: str = ""
    constraint_type: str = ""
    trigger_keywords: List[str] = field(default_factory=list)
    severity: str = "warn"                  # info|warn|block
    scope: str = ""
    confidence_level: str = "medium"
    description: str = ""
    evidence_refs: List[str] = field(default_factory=list)


    module_id: str = ""
    expression: str = ""
    input_terms: List[str] = field(default_factory=list)
    requirements: List[str] = field(default_factory=list)
    lifecycle_state: str = "active"
    lifecycle_introduced_in: str = ""
    confidence_reason: str = ""
    evidence_origin: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "target": self.target,
            "constraint_type": self.constraint_type,
            "trigger_keywords": list(self.trigger_keywords),
            "severity": self.severity,
            "scope": self.scope,
            "confidence_level": self.confidence_level,
            "description": self.description,
            "evidence_refs": list(self.evidence_refs),
            "module_id": self.module_id,
            "expression": self.expression,
            "input_terms": list(self.input_terms),
            "requirements": list(self.requirements),
            "lifecycle_state": self.lifecycle_state,
            "lifecycle_introduced_in": self.lifecycle_introduced_in,
            "confidence_reason": self.confidence_reason,
            "evidence_origin": self.evidence_origin,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Constraint":
        scope_raw = data.get("scope", {})
        if isinstance(scope_raw, dict):
            s = _Scope.from_dict(scope_raw)
            scope_str = s.domain or s.benchmark or ""
        else:
            scope_str = str(scope_raw) if scope_raw else ""

        lifecycle_raw = data.get("lifecycle", {})
        lc = _Lifecycle.from_dict(lifecycle_raw) if isinstance(lifecycle_raw, dict) else _Lifecycle()
        confidence_raw = data.get("confidence", {})
        cf = _Confidence.from_dict(confidence_raw) if isinstance(confidence_raw, dict) else _Confidence()
        ev = _evidence_from(data)

        raw_type = data.get("type", data.get("constraint_type", ""))
        constraint_type = _CONSTRAINT_TYPE_MAP.get(raw_type, raw_type)


        desc = data.get("description", "")
        if not desc:
            definition = data.get("definition", "")
            check_message = data.get("check_message", "")
            if definition and check_message:
                desc = f"{definition}. {check_message}"
            else:
                desc = definition or check_message or ""

        return cls(
            id=data["id"],
            target=data.get("target", data.get("output_term", data.get("term_id", ""))),
            constraint_type=constraint_type,
            trigger_keywords=list(data.get("trigger_keywords", [])),
            severity=data.get("severity", "warn"),
            scope=scope_str,
            confidence_level=cf.level,
            description=desc,
            evidence_refs=list(data.get("evidence_refs", ev.source_refs)),
            module_id=data.get("module_id", ""),
            expression=data.get("expression", ""),
            input_terms=list(data.get("input_terms", [])),
            requirements=list(data.get("requirements", [])),
            lifecycle_state=lc.state,
            lifecycle_introduced_in=lc.introduced_in,
            confidence_reason=cf.reason,
            evidence_origin=ev.origin,
        )


# ============================================================

# ============================================================

@dataclass(frozen=True)
class Evidence:
    """Implementation of Evidence."""
    id: str
    source: str = ""
    query: str = ""
    result: str = ""
    validation_method: str = ""
    timestamp: str = ""

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Evidence":
        return cls(
            id=data["id"],
            source=data.get("source", ""),
            query=data.get("query", ""),
            result=data.get("result", ""),
            validation_method=data.get("validation_method", ""),
            timestamp=data.get("timestamp", ""),
        )


# ============================================================

# ============================================================

Scope = _Scope
Lifecycle = _Lifecycle
Confidence = _Confidence
EvidenceCompat = _EvidenceCompat


# ============================================================
# ============================================================







# ============================================================

# ============================================================

@dataclass(frozen=True)
class ColumnProfile:
    """Implementation of ColumnProfile."""
    source: str
    name: str
    dtype: str
    role: str
    row_count: int
    missing_rate: float
    unique_count: int
    unique_ratio: float
    sample_values: List[str] = field(default_factory=list)
    numeric_min: Optional[float] = None
    numeric_max: Optional[float] = None
    numeric_mean: Optional[float] = None
    numeric_p25: Optional[float] = None
    numeric_p50: Optional[float] = None
    numeric_p75: Optional[float] = None
    time_min: Optional[str] = None
    time_max: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        d = {
            "source": self.source, "name": self.name,
            "dtype": self.dtype, "role": self.role,
            "row_count": self.row_count, "missing_rate": self.missing_rate,
            "unique_count": self.unique_count, "unique_ratio": self.unique_ratio,
        }
        if self.sample_values:
            d["sample_values"] = list(self.sample_values)
        numeric = {}
        for k in ("min", "max", "mean", "p25", "p50", "p75"):
            v = getattr(self, f"numeric_{k}")
            if v is not None:
                numeric[k] = v
        if numeric:
            d["numeric_summary"] = numeric
        if self.time_min or self.time_max:
            d["time_range"] = {}
            if self.time_min:
                d["time_range"]["min"] = self.time_min
            if self.time_max:
                d["time_range"]["max"] = self.time_max
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ColumnProfile":
        ns = data.get("numeric_summary", {}) or {}
        tr = data.get("time_range", {}) or {}
        return cls(
            source=data["source"], name=data["name"],
            dtype=data.get("dtype", ""), role=data.get("role", "unknown"),
            row_count=data.get("row_count", 0),
            missing_rate=data.get("missing_rate", 0.0),
            unique_count=data.get("unique_count", 0),
            unique_ratio=data.get("unique_ratio", 0.0),
            sample_values=list(data.get("sample_values", [])),
            numeric_min=ns.get("min"), numeric_max=ns.get("max"),
            numeric_mean=ns.get("mean"), numeric_p25=ns.get("p25"),
            numeric_p50=ns.get("p50"), numeric_p75=ns.get("p75"),
            time_min=tr.get("min"), time_max=tr.get("max"),
        )


@dataclass(frozen=True)
class JoinCandidate:
    """Implementation of JoinCandidate."""
    left_source: str
    left_column: str
    right_source: str
    right_column: str
    coverage: float
    expected_cardinality: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "JoinCandidate":
        return cls(
            left_source=data["left_source"],
            left_column=data["left_column"],
            right_source=data.get("right_source", ""),
            right_column=data.get("right_column", ""),
            coverage=data.get("coverage", 0.0),
            expected_cardinality=data.get("expected_cardinality", "many_to_one"),
        )


@dataclass(frozen=True)
class TaskInventory:
    """Implementation of TaskInventory."""
    columns: List[ColumnProfile] = field(default_factory=list)
    joins: List[JoinCandidate] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "columns": [c.to_dict() for c in self.columns],
            "joins": [j.to_dict() for j in self.joins],
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TaskInventory":
        return cls(
            columns=[ColumnProfile.from_dict(c) for c in data.get("columns", [])],
            joins=[JoinCandidate.from_dict(j) for j in data.get("joins", [])],
        )












# ============================================================

# ============================================================

