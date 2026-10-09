#!/usr/bin/env python3
"""Implementation for the bird.tceo.binder module."""

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .models import Mapping, Term


@dataclass
class TaskBinding:
    """Implementation of TaskBinding."""
    table: str
    column: str
    term_id: str
    term_name: str
    mapping_id: str = ""
    confidence: str = "medium"  # validated | medium | low


class DeterministicBinder:
    """Implementation of DeterministicBinder."""

    def __init__(self,
                 mappings: Dict[str, Mapping],
                 terms: Dict[str, Term]):
        self._mappings = mappings
        self._terms = terms

    # ------------------------------------------------------------------
    # bind
    # ------------------------------------------------------------------

    def bind(self, schema: Dict[str, List[dict]]) -> List[TaskBinding]:
        """Bind the requested value."""
        bindings: List[TaskBinding] = []
        bound: set = set()


        for m in self._mappings.values():
            term = self._terms.get(m.term_id)
            if term is None:
                continue
            table = m.table.lower()
            col = m.column.lower()
            if table not in schema:
                continue
            for col_info in schema[table]:
                if col_info["name"].lower() == col:
                    bindings.append(TaskBinding(
                        table=m.table,
                        column=col_info["name"],
                        term_id=m.term_id,
                        term_name=term.name,
                        mapping_id=m.id,
                        confidence="validated",
                    ))
                    bound.add((m.table.lower(), col_info["name"].lower()))
                    break


        for table_name, columns in schema.items():
            for col_info in columns:
                col_name = col_info["name"]
                col_type = (col_info.get("type") or "").upper()
                key = (table_name.lower(), col_name.lower())
                if key in bound:
                    continue

                inferred = self._infer_role(col_name, col_type)
                if inferred:
                    bindings.append(TaskBinding(
                        table=table_name,
                        column=col_name,
                        term_id=inferred.id,
                        term_name=inferred.name,
                        mapping_id="",
                        confidence="low",
                    ))

        self._cached_bindings = bindings
        return bindings

    # ------------------------------------------------------------------
    # role inference
    # ------------------------------------------------------------------


    _ROLE_PATTERNS = {
        "identifier": ["id", "_id", "key", "_key", "uuid", "code"],
        "temporal": ["date", "time", "year", "month", "day",
                      "birthday", "created", "updated", "timestamp"],
        "metric": ["value", "amount", "price", "count", "score",
                    "rating", "points", "sum", "total", "avg",
                    "weight", "height", "speed", "rate", "number"],
        "nominal": ["name", "type", "status", "category", "label",
                     "description", "text", "color", "sex", "gender"],
    }

    def _infer_role(self, col_name: str, col_type: str) -> Optional[Term]:
        """Infer role."""
        col_lower = col_name.lower().replace("_", " ")


        for pat in self._ROLE_PATTERNS["temporal"]:
            if pat in col_lower:
                return self._find_term_by_hint("time", "temporal", "date")


        for pat in self._ROLE_PATTERNS["identifier"]:
            if col_lower.endswith(pat) or col_lower.startswith(pat):
                return self._find_term_by_hint("identifier", "id", "entity")


        if col_type in ("INTEGER", "REAL", "FLOAT", "DOUBLE", "NUMERIC",
                         "NUMBER", "DECIMAL"):
            for pat in self._ROLE_PATTERNS["metric"]:
                if pat in col_lower:
                    return self._find_term_by_hint("metric", "measure", "score")


        if col_type in ("TEXT", "VARCHAR", "CHAR", "NVARCHAR"):
            for pat in self._ROLE_PATTERNS["nominal"]:
                if pat in col_lower:
                    return self._find_term_by_hint("dimension", "entity",
                                                    "category")

        return None

    def _find_term_by_hint(self, *hints: str) -> Optional[Term]:
        """Find term by hint."""
        for hint in hints:
            for term in self._terms.values():
                if term.type == hint:
                    return term
                if hint in term.id.lower():
                    return term
                if hint in term.name.lower():
                    return term
        return None

    # ------------------------------------------------------------------

    # ------------------------------------------------------------------

    def get_bindings_for_term(self, term_id: str) -> List[TaskBinding]:
        """Return bindings for term."""
        return [b for b in getattr(self, '_cached_bindings', [])
                if b.term_id == term_id]

    def get_term_for_column(self, table: str, column: str) -> Optional[str]:
        """Return term for column."""
        for b in getattr(self, '_cached_bindings', []):
            if b.table.lower() == table.lower() and \
               b.column.lower() == column.lower():
                return b.term_id
        return None
