#!/usr/bin/env python3
"""Implementation for the bird.tceo.models module."""

from dataclasses import dataclass, field


@dataclass
class Term:
    """Implementation of Term."""
    # === Canonical fields ===
    id: str
    type: str                            # entity | metric | dimension | category | concept
    name: str
    definition: str
    scope: str = ""
    aliases: list = field(default_factory=list)
    evidence_refs: list = field(default_factory=list)


    tables: list = field(default_factory=list)
    grain: str = ""
    value_type: str = ""                 # nominal | ordinal | numeric | temporal
    confidence_level: str = "medium"     # high | medium | low

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "type": self.type,
            "name": self.name,
            "definition": self.definition,
            "scope": self.scope,
            "aliases": self.aliases,
            "evidence_refs": self.evidence_refs,
            "tables": self.tables,
            "grain": self.grain,
            "value_type": self.value_type,
            "confidence_level": self.confidence_level,
        }


@dataclass
class Mapping:
    """Implementation of Mapping."""
    # === Canonical fields ===
    id: str
    term_id: str
    table: str
    database_source: str = ""
    column: str = ""
    semantic_filter: str = ""
    aggregation_semantics: str = ""
    grain: str = ""
    validation: str = ""
    evidence_refs: list = field(default_factory=list)


    data_type: str = ""
    join_path: list = field(default_factory=list)
    confidence_level: str = "medium"

    def to_dict(self) -> dict:
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
            "evidence_refs": self.evidence_refs,
            "data_type": self.data_type,
            "join_path": self.join_path,
            "confidence_level": self.confidence_level,
        }


@dataclass
class Relation:
    """Implementation of Relation."""
    # === Canonical fields ===
    id: str
    source: str
    relation_type: str                   # association | hierarchy | composition | equivalence | derivation
    target: str
    connection_condition: str = ""
    description: str = ""
    evidence_refs: list = field(default_factory=list)


    confidence_level: str = "medium"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "source": self.source,
            "relation_type": self.relation_type,
            "target": self.target,
            "connection_condition": self.connection_condition,
            "description": self.description,
            "evidence_refs": self.evidence_refs,
            "confidence_level": self.confidence_level,
        }


@dataclass
class Constraint:
    """Implementation of Constraint."""
    # === Canonical fields ===
    id: str
    target: str = ""
    constraint_type: str = ""            # enum_semantics | data_quality | business_rule | unit | scope
    trigger_keywords: list = field(default_factory=list)
    severity: str = "warning"            # block | warning | info
    scope: str = ""
    confidence_level: str = "medium"     # high | medium | low
    description: str = ""
    evidence_refs: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "target": self.target,
            "constraint_type": self.constraint_type,
            "trigger_keywords": self.trigger_keywords,
            "severity": self.severity,
            "scope": self.scope,
            "confidence_level": self.confidence_level,
            "description": self.description,
            "evidence_refs": self.evidence_refs,
        }


@dataclass
class Evidence:
    """Implementation of Evidence."""
    # === Canonical fields ===
    id: str
    source: str = ""
    query: str = ""
    result: str = ""
    validation_method: str = ""
    timestamp: str = ""


    content: str = ""
    term_id: str = ""
    mapping_id: str = ""
    type: str = ""


    confidence_level: str = "medium"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "source": self.source,
            "query": self.query,
            "result": self.result,
            "validation_method": self.validation_method,
            "timestamp": self.timestamp,
            "content": self.content,
            "term_id": self.term_id,
            "mapping_id": self.mapping_id,
            "type": self.type,
            "confidence_level": self.confidence_level,
        }
