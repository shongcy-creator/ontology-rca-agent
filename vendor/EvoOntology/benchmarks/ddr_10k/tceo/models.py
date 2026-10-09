"""Implementation for the ddr.tceo.models module."""

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


def _evidence_from(data: Dict[str, Any]) -> "_EvidenceCompat":
    """Build evidence from a dict or list ``evidence`` field.

    Sample data serializes ``evidence`` as a list of evidence ids (e.g.
    ``["evidence.term.finance.revenue"]``), whereas the canonical schema uses a
    dict. Accept both so term/mapping evidence associations survive loading.
    """
    evidence_raw = data.get("evidence", {})
    if isinstance(evidence_raw, dict):
        return _EvidenceCompat.from_dict(evidence_raw)
    if isinstance(evidence_raw, list):
        return _EvidenceCompat(source_refs=[str(e) for e in evidence_raw])
    return _EvidenceCompat()


def _mapping_evidence_refs(data: Dict[str, Any], ev: "_EvidenceCompat") -> List[str]:
    """Read mapping evidence refs from evidence_refs / validation.evidence / evidence."""
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
    source_family: str = "sqlite"
    column_names: List[str] = field(default_factory=list)
    table_pattern: str = ""

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SourcePattern":
        return cls(
            source_family=data.get("source_family", "sqlite"),
            column_names=list(data.get("column_names", [])),
            table_pattern=data.get("table_pattern", ""),
        )


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
    evidence_refs: List[str] = field(default_factory=list)


    mapping_type: str = "close_match"
    data_type: str = ""
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


        filters = mc.filters
        semantic_filter = ""
        if filters:
            fact_name = filters.get("fact_name", "")
            if isinstance(fact_name, list):
                semantic_filter = str(fact_name[0]) if fact_name else ""
            else:
                semantic_filter = str(fact_name)

        column = sp.column_names[0] if sp.column_names else data.get("column", "")

        return cls(
            id=data["id"],
            term_id=data.get("term_id", data.get("target_term", "")),
            database_source=data.get("database_source", sp.source_family),
            table=data.get("table", sp.table_pattern),
            column=column,
            semantic_filter=data.get("semantic_filter", semantic_filter),
            aggregation_semantics=data.get("aggregation_semantics", ""),
            grain=data.get("grain", ""),
            validation=data.get("validation", ""),
            evidence_refs=_mapping_evidence_refs(data, ev),
            mapping_type=data.get("mapping_type", "close_match"),
            data_type=data.get("data_type", mc.data_type),
            lifecycle_state=lc.state,
            lifecycle_introduced_in=lc.introduced_in,
            confidence_level=cf.level,
            confidence_reason=cf.reason,
            evidence_origin=ev.origin,
        )



Scope = _Scope
Lifecycle = _Lifecycle
Confidence = _Confidence
EvidenceCompat = _EvidenceCompat
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





# ============================================================

# ============================================================





