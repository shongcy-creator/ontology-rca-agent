"""Implementation for the insightbench.insightbench.tceo.binder module."""

import re
from typing import Any, Dict, List, Optional

from insightbench.tceo.models import (
    ColumnProfile,
    Mapping,
    TaskInventory,
    Term,
)


def _safe_id(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    return normalized or "column"


# ============================================================

# ============================================================

class TaskBinding:
    """Implementation of TaskBinding."""
    __slots__ = (
        "column", "term_id", "term_label", "mapping_id",
        "mapping_type", "confidence", "semantic_id",
    )

    def __init__(
        self,
        column: ColumnProfile,
        term_id: str,
        term_label: str,
        mapping_id: str = "",
        mapping_type: str = "",
        confidence: str = "task_inferred",
    ):
        self.column = column
        self.term_id = term_id
        self.term_label = term_label
        self.mapping_id = mapping_id
        self.mapping_type = mapping_type
        self.confidence = confidence
        self.semantic_id = f"binding.{column.source}.{_safe_id(column.name)}"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "semantic_id": self.semantic_id,
            "column_source": self.column.source,
            "column_name": self.column.name,
            "column_role": self.column.role,
            "column_dtype": self.column.dtype,
            "term_id": self.term_id,
            "term_label": self.term_label,
            "mapping_id": self.mapping_id,
            "mapping_type": self.mapping_type,
            "confidence": self.confidence,
        }


# ============================================================

# ============================================================

class DeterministicBinder:
    """Implementation of DeterministicBinder."""

    def __init__(
        self,
        mappings: Optional[Dict[str, Mapping]] = None,
        terms: Optional[Dict[str, Term]] = None,
    ):
        self._mappings = mappings or {}
        self._terms = terms or {}

    def bind(
        self,
        inventory: TaskInventory,
        domain: Optional[str] = None,
    ) -> List[TaskBinding]:
        """Bind the requested value."""
        bindings: List[TaskBinding] = []
        used_columns: set = set()


        for mapping in self._mappings.values():
            if mapping.lifecycle_state not in ("active", "validated"):
                continue

            if domain is not None:
                md = (mapping.scope or "").strip().lower()
                if md and md != "core" and md != domain.lower().strip():
                    continue
            matched_bindings = self._apply_mapping(mapping, inventory)
            for b in matched_bindings:
                key = (b.column.source, b.column.name)
                if key not in used_columns:
                    used_columns.add(key)
                    bindings.append(b)


        fallback_terms_map = self._build_fallback_term_map()
        for col in inventory.columns:
            key = (col.source, col.name)
            if key in used_columns:
                continue
            term_id = fallback_terms_map.get(col.role)
            if term_id and term_id in self._terms:
                t = self._terms[term_id]
                bindings.append(TaskBinding(
                    column=col,
                    term_id=term_id,
                    term_label=t.name,
                    mapping_type="role_inference",
                    confidence="task_inferred",
                ))
            else:

                bindings.append(TaskBinding(
                    column=col,
                    term_id="",
                    term_label=col.role if col.role != "unknown" else "unknown",
                    mapping_type="role_inference",
                    confidence="unknown",
                ))

        return bindings

    def _apply_mapping(self, mapping: Mapping, inventory: TaskInventory) -> List[TaskBinding]:
        """Apply mapping."""
        matches = []

        for col in inventory.columns:

            if mapping.table and mapping.table not in ("*", ""):
                if col.source != mapping.table:
                    continue


            col_name_lower = col.name.lower().strip()
            col_names = mapping.column_names if mapping.column_names else ([mapping.column] if mapping.column else [])
            if col_name_lower not in [n.lower().strip() for n in col_names]:
                continue


            if mapping.data_type:
                expected = mapping.data_type.lower()
                actual_dtype = col.dtype.lower()
                if "datetime" in expected:
                    if "datetime" not in actual_dtype and "date" not in actual_dtype:
                        continue
                elif "numeric" in expected:
                    if "int" not in actual_dtype and "float" not in actual_dtype and "number" not in actual_dtype:
                        continue

            if col.missing_rate < mapping.min_null_rate or col.missing_rate > mapping.max_null_rate:
                continue

            if col.unique_ratio < mapping.min_unique_ratio or col.unique_ratio > mapping.max_unique_ratio:
                continue


            term = self._terms.get(mapping.term_id) if self._terms else None
            matches.append(TaskBinding(
                column=col,
                term_id=mapping.term_id,
                term_label=term.name if term else mapping.term_id,
                mapping_id=mapping.id,
                mapping_type=mapping.mapping_type,
                confidence="validated",
            ))

        return matches

    def _build_fallback_term_map(self) -> Dict[str, str]:
        """Build fallback term map."""
        role_to_term = {}
        for term in self._terms.values():
            if term.type == "time":
                role_to_term["time"] = term.id
            elif term.type == "metric" and term.id.endswith("measure"):
                role_to_term["measure"] = term.id
            elif term.type == "dimension" and term.id.endswith("identifier"):
                role_to_term["identifier"] = term.id
            elif term.type == "dimension" and term.id.endswith("category"):
                role_to_term["dimension"] = term.id
        return role_to_term
