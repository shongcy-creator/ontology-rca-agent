"""Implementation for the insightbench.insightbench.tceo.retriever module."""

import json
import re
import time
from typing import Any, Dict, List, Optional

import pandas as pd

from insightbench.tceo.adapter import InsightAdapter
from insightbench.tceo.binder import DeterministicBinder, TaskBinding
from insightbench.tceo.models import TaskInventory
from insightbench.tceo.session_manifest import build_session_manifest
from insightbench.tceo.store import VersionedSemanticStore


def _tokens(text: str) -> set:
    return set(re.findall(r"[a-z0-9_]+", str(text).lower()))




class InsightSemanticLayer:
    """Implementation of InsightSemanticLayer."""

    def __init__(
        self,
        store: VersionedSemanticStore,
        inventory: TaskInventory,
        trace_events: Optional[List[dict]] = None,
        domain: Optional[str] = None,
    ):
        self.store = store
        self.inventory = inventory
        self.trace_events = trace_events if trace_events is not None else []
        self._domain = domain

        # Per-session browse history (for active dedup filtering)
        self._browse_history: Dict[str, set] = {}  # normalized_query → {item_ids}

        # Per-session resolve history (for dedup)
        self._resolve_history: Dict[str, set] = {}  # mention → {resolved_term_ids}


        binder = DeterministicBinder(
            mappings=store.mappings,
            terms=store.terms,
        )
        self._task_bindings: List[TaskBinding] = binder.bind(inventory, domain=domain)

    @classmethod
    def from_tables(
        cls,
        table: pd.DataFrame,
        table_user: Optional[pd.DataFrame] = None,
        store_path: Optional[str] = None,
        trace_events: Optional[List[dict]] = None,
        domain: Optional[str] = None,
        version: Optional[str] = None,
    ) -> "InsightSemanticLayer":
        inventory = InsightAdapter().build(table, table_user)
        return cls(
            store=VersionedSemanticStore.load(store_path, version=version),
            inventory=inventory,
            trace_events=trace_events,
            domain=domain,
        )

    @property
    def version(self) -> str:
        return self.store.version

    def manifest(self) -> str:
        return build_session_manifest(
            inventory=self.inventory,
            version=self.version,
            store=self.store,
            task_bindings=self._task_bindings,
        )

    def _scope_applies(self, scope: Any) -> bool:
        """Implement scope applies."""
        if self._domain is None:
            return True
        if isinstance(scope, str):
            normalized = scope.strip().lower()
            return normalized in ("", "core", self._domain.strip().lower())
        if hasattr(scope, "domain"):
            normalized = str(scope.domain or "").strip().lower()
            return normalized in ("", "core", self._domain.strip().lower())
        if isinstance(scope, dict):
            normalized = str(scope.get("domain", "")).strip().lower()
            return normalized in ("", "core", self._domain.strip().lower())
        return True

    # ============================================================

    # ============================================================

    _MAX_BROWSE_ITEMS = 4

    def browse(self, query: str = "", kind: str = "term",
               scope: Optional[Dict] = None, limit: int = 4) -> Dict[str, Any]:
        """Browse the requested value."""
        query = str(query or "").strip().lower()
        kind = str(kind).lower()
        if kind not in ("term", "mapping", "relation", "constraint", "all"):
            kind = "term"
        limit = max(1, min(int(limit or self._MAX_BROWSE_ITEMS), self._MAX_BROWSE_ITEMS))

        items = []
        if kind in ("term", "all"):
            for item in self.store.terms.values():
                if not self._scope_applies(item.scope):
                    continue
                if item.lifecycle_state not in ("active", "validated"):
                    continue
                items.append({
                    "id": item.id, "label": item.name, "kind": "term",
                    "scope": {"domain": item.scope} if item.scope else {},
                    "availability": item.lifecycle_state,
                    "_search_text": " ".join([item.name, item.definition, *item.aliases]),
                })
        if kind in ("relation", "all"):
            for item in self.store.relations.values():
                if not self._scope_applies(item.scope):
                    continue
                if item.lifecycle_state not in ("active", "validated"):
                    continue
                items.append({
                    "id": item.id,
                    "label": f"{item.source} -> {item.target}",
                    "kind": "relation",
                    "scope": {"domain": item.scope} if item.scope else {},
                    "availability": item.lifecycle_state,
                    "_search_text": " ".join([
                        item.relation_type, item.source, item.target, item.connection_condition,
                    ]),
                })
        if kind in ("mapping", "all"):
            for item in self.store.mappings.values():
                if not self._scope_applies(item.scope):
                    continue
                if item.lifecycle_state not in ("active", "validated"):
                    continue
                col_names = item.column_names if item.column_names else ([item.column] if item.column else [])
                items.append({
                    "id": item.id,
                    "label": f"{item.term_id} -> {col_names}",
                    "kind": "mapping",
                    "scope": {"domain": item.scope} if item.scope else {},
                    "availability": item.lifecycle_state,
                    "_search_text": " ".join([
                        item.term_id, *col_names,
                    ]),
                })
        if kind in ("constraint", "all"):
            for item in self.store.constraints.values():
                if not self._scope_applies(item.scope):
                    continue
                if item.lifecycle_state not in ("active", "validated"):
                    continue
                desc = item.description or ""
                items.append({
                    "id": item.id, "label": item.id or desc[:80], "kind": "constraint",
                    "scope": {"domain": item.scope} if item.scope else {},
                    "availability": item.lifecycle_state,
                    "_search_text": " ".join([
                        item.id, desc, *item.trigger_keywords,
                    ]),
                })

        # Scope filter
        if scope:
            items = [it for it in items if all(
                it.get("scope", {}).get(k) == v for k, v in scope.items()
            )]

        catalog_total = len(items)
        if not query:
            return {
                "status": "needs_query", "items": [], "catalog_total": catalog_total,
                "version": self.version,
                "note": "Provide a specific query. Full-catalog enumeration is disabled.",
            }

        query_tokens = _tokens(query)
        ranked = []
        for item in items:
            search_text = " ".join([
                item.get("id", ""), item.get("label", ""),
                item.pop("_search_text", ""),
            ]).lower()
            overlap = sorted(query_tokens & _tokens(search_text))
            phrase_match = query in search_text
            if not overlap and not phrase_match:
                continue
            item["match_rationale"] = {
                "phrase_match": phrase_match,
                "overlap_tokens": overlap,
            }
            ranked.append(((2 if phrase_match else 0) + len(overlap), item))
        ranked.sort(key=lambda pair: (-pair[0], pair[1]["id"]))

        # Active session dedup: exclude items already returned in previous browses
        all_seen_ids: set = set()
        for prev_ids in self._browse_history.values():
            all_seen_ids |= prev_ids

        fresh_items = []
        seen_now: set = set()
        for _, item in ranked:
            if item["id"] in all_seen_ids or item["id"] in seen_now:
                continue
            fresh_items.append(item)
            seen_now.add(item["id"])
            if len(fresh_items) >= limit:
                break

        skipped = len(ranked) - len(fresh_items)

        # Enrich term items with column bindings from Binder
        for item in fresh_items:
            if item.get("kind") == "term":
                item_id = item.get("id", "")
                bound_cols = [
                    tb.column.name for tb in self._task_bindings
                    if tb.term_id == item_id
                ]
                if bound_cols:
                    item["columns"] = bound_cols

        norm_q = " ".join(sorted(_tokens(query))) if query else ""
        if norm_q:
            self._browse_history[norm_q] = frozenset(item["id"] for item in fresh_items)

        dedup_note = ""
        if skipped > 0:
            dedup_note = (
                f"{skipped} item(s) already seen in earlier browses were excluded. "
                f"Use resolve() for specific column bindings instead of re-browsing."
            )

        return {
            "status": "ok",
            "items": fresh_items,
            "matched_total": len(ranked), "catalog_total": catalog_total,
            "version": self.version,
            "note": "Only query-relevant items are returned; this is not an analysis checklist.",
            **({"dedup_warning": dedup_note} if dedup_note else {}),
        }

    # ============================================================

    # ============================================================

    def resolve(self, mentions: Optional[List[str]] = None,
                context: Optional[str] = None) -> Dict[str, Any]:
        """Resolve the requested value."""
        mentions = [str(m).strip().lower() for m in (mentions or []) if str(m).strip()]

        results = []

        # Fallback: when called without mentions, return the full
        # column-to-concept binding table from Binder. This turns an
        # otherwise-wasted empty resolve round into a useful discovery step.
        if not mentions:
            bindings_map: Dict[str, list] = {}
            for tb in self._task_bindings:
                if tb.term_id:
                    bindings_map.setdefault(tb.term_id, []).append(tb.column.name)
            if bindings_map:
                results.append({
                    "query": "(empty — returning column bindings)",
                    "query_type": "fallback",
                    "status": "resolved",
                    "match_rationale": "No specific mentions provided. These are the known column-to-concept bindings for your dataset.",
                    "column_bindings": [
                        {"term_id": tid, "columns": cols}
                        for tid, cols in sorted(bindings_map.items())
                    ],
                })
            else:
                results.append({
                    "query": "(empty)", "query_type": "fallback",
                    "status": "unresolved",
                    "match_rationale": "No specific mentions and no column bindings available.",
                })
            return {"status": "ok", "results": results, "version": self.version}

        # Per-call dedup: track which term_ids already appeared in this resolve call
        seen_term_ids: set = set()

        # Resolve by mentions
        for mention in mentions:
            candidates = []
            mention_lower = mention.lower()

            # Initialize session resolve history for this mention if needed
            if mention not in self._resolve_history:
                self._resolve_history[mention] = set()

            # Column name shortcut: check if mention matches a bound column directly
            column_handled = False
            for tb in self._task_bindings:
                if tb.column.name.lower().strip() == mention_lower and tb.term_id:
                    t = self.store.get(tb.term_id)
                    if t is not None:
                        column_handled = True
                        # Internal dedup: skip if same term_id already in this call's results
                        if t.id in seen_term_ids:
                            # Session dedup: still record the mention→term mapping
                            self._resolve_history[mention].add(t.id)
                            break
                        # Session dedup: skip if already resolved this session
                        if t.id in self._resolve_history[mention]:
                            break
                        seen_term_ids.add(t.id)
                        self._resolve_history[mention].add(t.id)
                        results.append(self._build_resolve_result(
                            mention, t,
                            f"column '{tb.column.name}' is bound to term '{t.name}'",
                            "resolved"
                        ))
                    break
            else:
                # Not a column name match — proceed with normal term search
                pass
            if any(r.get("query") == mention for r in results) or column_handled:
                continue  # already resolved via column shortcut (or dedup-handled)

            for t in self.store.terms.values():
                if not self._scope_applies(t.scope):
                    continue
                if t.lifecycle_state not in ("active", "validated"):
                    continue
                if mention_lower == t.name.lower():
                    candidates.append((t, f"exact name match: '{t.name}'"))
                elif any(mention_lower == a.lower() for a in (t.aliases or [])):
                    candidates.append((t, f"exact alias match: term '{t.name}'"))
                elif mention_lower in t.name.lower() or any(
                    mention_lower in a.lower() for a in (t.aliases or [])
                ):
                    candidates.append((t, f"substring match: term '{t.name}'"))

            if not candidates:
                results.append({
                    "query": mention, "query_type": "mention",
                    "status": "unresolved",
                    "match_rationale": f"No active term matching '{mention}'",
                    "candidates": [],
                })
            elif len(candidates) == 1:
                t = candidates[0][0]
                rationale = candidates[0][1]
                # Internal dedup: skip if same term_id already in this call
                if t.id in seen_term_ids:
                    self._resolve_history[mention].add(t.id)
                    continue
                # Session dedup: skip if already resolved this session
                if t.id in self._resolve_history[mention]:
                    continue
                seen_term_ids.add(t.id)
                self._resolve_history[mention].add(t.id)
                results.append(self._build_resolve_result(
                    mention, t, rationale, "resolved"
                ))
            else:
                candidate_results = []
                for item, rationale in candidates:
                    candidate_results.append(self._build_resolve_result(
                        mention, item, rationale, "ambiguous"
                    ))
                results.append({
                    "query": mention, "query_type": "mention",
                    "status": "ambiguous",
                    "match_rationale": f"{len(candidates)} terms match '{mention}'",
                    "candidates": candidate_results,
                })

        return {"status": "ok", "results": results, "version": self.version}

    def _build_resolve_result(self, query: str, item: Any, rationale: str,
                              status: str) -> dict:
        """Build resolve result."""
        result = {
            "query": query,
            "query_type": "semantic_id" if "id_lookup" in rationale else "mention",
            "status": status,
            "match_rationale": rationale,
            "term": self._term_summary(item),
        }

        # Related mappings
        related_mappings = []
        for m in self.store.mappings.values():
            if not self._scope_applies(m.scope):
                continue
            if m.lifecycle_state not in ("active", "validated"):
                continue
            if m.term_id == getattr(item, "id", ""):
                col_names = m.column_names if m.column_names else ([m.column] if m.column else [])
                related_mappings.append({
                    "id": m.id, "term_id": m.term_id,
                    "table": m.table,
                    "columns": col_names,
                    "semantic_filter": m.semantic_filter,
                    "mapping_type": m.mapping_type,
                    "evidence_refs": list(m.evidence_refs),
                })
        if related_mappings:
            result["mappings"] = related_mappings

        # Related relations
        term_id = getattr(item, "id", "")
        related_relations = []
        for r in self.store.relations.values():
            if not self._scope_applies(r.scope):
                continue
            if r.lifecycle_state not in ("active", "validated"):
                continue
            if r.source == term_id or r.target == term_id:
                related_relations.append({
                    "id": r.id, "relation_type": r.relation_type,
                    "source": r.source, "target": r.target,
                    "connection_condition": r.connection_condition or "",
                    "evidence_refs": list(r.evidence_refs),
                })
        if related_relations:
            result["relations"] = related_relations

        # Related constraints
        related_constraints = []
        for c in self.store.constraints.values():
            if not self._scope_applies(c.scope):
                continue
            if c.lifecycle_state not in ("active", "validated"):
                continue
            if term_id in (getattr(c, "input_terms", []) or []) or c.target == term_id:
                related_constraints.append({
                    "id": c.id,
                    "severity": c.severity,
                    "description": c.description or "",
                    "evidence_refs": list(c.evidence_refs),
                })
        if related_constraints:
            result["constraints"] = related_constraints

        # Task bindings: current-task physical column bindings for this term
        task_bindings = []
        for tb in self._task_bindings:
            if tb.term_id == term_id:
                task_bindings.append(tb.to_dict())
        if task_bindings:
            result["task_bindings"] = task_bindings

        return result

    @staticmethod
    def _term_summary(term: Any) -> dict:
        return {
            "id": term.id if hasattr(term, "id") else term.get("semantic_id", ""),
            "type": term.type if hasattr(term, "type") else term.get("kind", ""),
            "name": term.name if hasattr(term, "name") else term.get("label", ""),
            "aliases": list(getattr(term, "aliases", []) or []),
            "definition": getattr(term, "definition", "") or "",
            "value_type": getattr(term, "value_type", "") or "",
            "grain": getattr(term, "grain", "") or "",
            "evidence_refs": list(getattr(term, "evidence_refs", []) or []),
        }

    def execute_tool(
        self, name: str, arguments: Dict[str, Any], stage: str
    ) -> Dict[str, Any]:
        started = time.perf_counter()
        if name == "browse_semantics":
            result = self.browse(
                query=arguments.get("query", ""),
                kind=arguments.get("kind", "term"),
                scope=arguments.get("scope"),
                limit=arguments.get("limit", 6),
            )
            result_ids = [item["id"] for item in result.get("items", [])]
        elif name == "resolve_semantics":
            result = self.resolve(
                mentions=arguments.get("mentions"),
                context=arguments.get("context"),
            )
            result_ids = [
                r.get("term", {}).get("id", "")
                for res in result.get("results", [])
                for r in ([res] if res.get("status") == "resolved" else res.get("candidates", []))
            ]
        else:
            raise ValueError(f"Unknown semantic tool: {name}")

        self.trace_events.append({
            "type": name,
            "stage": stage,
            "semantic_version": self.version,
            "arguments": arguments,
            "result_ids": result_ids,
            "status": result["status"],
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
        })
        return result

    @property
    def available_tool_names(self) -> List[str]:
        """Implement available tool names."""
        return [s["function"]["name"] for s in self.tool_schemas()]

    @staticmethod
    def tool_schemas() -> List[dict]:
        """Implement tool schemas."""
        return [
            {
                "type": "function",
                "function": {
                    "name": "browse_semantics",
                    "description": (
                        "Find up to 4 semantic objects relevant to a specific analysis need. "
                        "Use this only when column bindings in the manifest are insufficient. "
                        "Full-catalog enumeration is disabled. "
                        "Re-browsing similar queries wastes rounds — check dedup_warning."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {
                                "type": "string",
                                "description": "Specific concept or analysis need",
                            },
                            "kind": {
                                "type": "string",
                                "description": "Filter by kind: term, mapping, relation, constraint, or all",
                                "enum": ["term", "mapping", "relation", "constraint", "all"],
                            },
                            "scope": {
                                "type": "object",
                                "description": "Optional scope filter",
                            },
                            "limit": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": 4,
                            },
                        },
                        "required": ["query"],
                        "additionalProperties": False,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "resolve_semantics",
                    "description": (
                        "Map concept mentions (like 'revenue', 'customer age') or "
                        "column names to their semantic definitions, physical column "
                        "bindings, and constraints. "
                        "Use this BEFORE writing code to confirm: "
                        "(1) which column represents a concept, "
                        "(2) what the column values mean, "
                        "(3) any constraints or caveats. "
                        "Call with empty arguments to get the full column-to-concept "
                        "binding table for the current dataset."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "mentions": {
                                "type": "array",
                                "items": {"type": "string"},
                                "maxItems": 5,
                                "description": "Concepts or column names to resolve, e.g. ['revenue', 'rating']",
                            },
                            "context": {
                                "type": "string",
                                "description": "Optional task context to disambiguate mentions",
                            },
                        },
                        "additionalProperties": False,
                    },
                },
            },
        ]

    def reset_session(self) -> None:
        """Reset per-question session state (resolve/browse history).

        Called at the start of each new question to prevent cross-question
        session dedup from silently disabling the ontology layer for follow-up
        questions that need to re-resolve the same columns.
        """
        self._resolve_history.clear()
        self._browse_history.clear()

    def trace(self) -> Dict[str, Any]:
        return {
            "semantic_version": self.version,
            "semantic_events": list(self.trace_events),
        }

    def execute_json(self, name: str, arguments_json: str, stage: str) -> str:
        arguments = json.loads(arguments_json or "{}")
        return json.dumps(
            self.execute_tool(name, arguments, stage), ensure_ascii=False
        )
