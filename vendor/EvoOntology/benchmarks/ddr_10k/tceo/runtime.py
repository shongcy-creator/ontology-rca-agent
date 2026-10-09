"""Implementation for the ddr.tceo.runtime module."""

import re
from typing import Any, Dict, List, Optional

from .store import VersionedSemanticStore

_ACTIVE_STATES = {"validated", "active"}


def _tokens(text: str) -> set:
    """Implement tokens."""
    return set(re.findall(r"\w+", str(text).lower(), flags=re.UNICODE))


class DDRSemanticLayer:
    """Implementation of DDRSemanticLayer."""

    def __init__(self, store: VersionedSemanticStore):
        self.store = store
        self._resolved_mentions: Dict[str, str] = {}

    @classmethod
    def load(cls, store_path: Optional[str] = None, version: Optional[str] = None) -> "DDRSemanticLayer":
        return cls(VersionedSemanticStore.load(store_path, version=version))

    @property
    def version(self) -> str:
        return self.store.version

    def manifest(self, exposed_tools: Optional[List[str]] = None,
                 task: Optional[str] = None,
                 include_fact_names: bool = True) -> str:
        active_constraints = [c for c in self.store.constraints.values() if _is_active(c)]
        constraint_summary = {}
        for c in active_constraints:
            constraint_summary[c.severity] = constraint_summary.get(c.severity, 0) + 1
        summary_parts = [f"{sev}={count}" for sev, count in sorted(constraint_summary.items())]

        # Build pre-resolved key metrics table
        key_metrics = self._extract_key_metrics()
        metrics_lines = []
        if key_metrics:
            if task:
                key_metrics = self._filter_concepts_by_task(task, key_metrics)
            if include_fact_names:
                metrics_lines = [
                    "",
                    "## Financial Concept Reference",
                    "These are available concepts mapped to database fact_names.",
                    "",
                ]
                for term_key, fact_names in sorted(key_metrics.items()):
                    display_name = term_key.replace("_", " ").title()
                    for fn in fact_names:
                        metrics_lines.append(f"- {display_name} → fact_name = '{fn}'")
            else:
                metrics_lines = [
                    "",
                    "## Financial Concept Reference",
                    "Available analysis concepts. Use resolve_semantics to get exact fact_names",
                    "and ready-to-run SQL queries for any concept below.",
                    "",
                ]
                for term_key in sorted(key_metrics.keys()):
                    display_name = term_key.replace("_", " ").title()
                    metrics_lines.append(f"- {display_name}")
                metrics_lines.append(
                    f"\n({len(key_metrics)} concepts available. "
                    f"Call resolve_semantics with relevant mentions to get SQL-ready mappings.)"
                )

        exposed = set(exposed_tools or ["browse_semantics", "resolve_semantics"])
        tool_lines = []
        if "browse_semantics" in exposed:
            tool_lines.append(
                "- **browse_semantics**(query, kind, limit) — find up to 6 catalog items relevant to a specific need."
            )
        if "resolve_semantics" in exposed:
            tool_lines.append(
                "- **resolve_semantics**(mentions) — resolve up to 5 concepts to exact fact_names "
                "WITH ready-to-run SQL queries. Always use this before writing financial SQL."
            )

        lines = [
            f"Ontology layer version: {self.version}",
            f"Active constraints: {len(active_constraints)} ({', '.join(summary_parts)})",
            *metrics_lines,
            "",
            "## Semantic MCP Tools",
            f"Semantic Tools ({len(tool_lines)} available)",
            "",
            "These tools are bounded navigation aids. Use them proactively to improve your analytical accuracy and completeness.",
            "",
            *tool_lines,
            "",
            "### When to use semantic tools",
            "- **IMPORTANT**: Call resolve_semantics FIRST before querying financial data. It gives you exact fact_names AND ready-to-run SQL.",
            "- Pass a list of concept mentions (e.g., ['revenue', 'net income', 'assets']) to resolve_semantics.",
            "- Each resolved result includes a sample_query you can execute directly via sqlite-mcp.",
            "- Use browse_semantics only when you need to discover what concepts are available.",
            "- Resolved mappings are guidance, not final answers — always validate against the actual database.",
        ]
        if "resolve_semantics" in exposed:
            lines.extend([
                "- coverage_status=supported means a SQL-ready mapping exists; execute the sample_query to get started.",
                "- If resolve returns unresolved, inspect the database directly with native data tools.",
            ])
        return "\n".join(lines)

    def _extract_key_metrics(self) -> dict:
        """Extract key metric fact_names from active mappings."""
        metrics: dict = {}
        for m in self.store.mappings.values():
            if not _is_active(m):
                continue
            fact_name = m.semantic_filter
            if not fact_name:
                continue
            term_part = m.term_id.split(".")[-1] if m.term_id else "unknown"
            if term_part not in metrics:
                metrics[term_part] = []
            if fact_name not in metrics[term_part]:
                metrics[term_part].append(fact_name)
        return metrics

    def _filter_concepts_by_task(self, task: str, key_metrics: dict) -> dict:
        """Filter key_metrics to task-relevant concepts, capped at MAX_CONCEPTS.

        Always includes core concepts first, then adds task-relevant ones
        by token overlap score. Total concepts capped to prevent shotgunning.
        """
        CORE_CONCEPTS = {
            "revenue", "net_income", "operating_income", "gross_profit",
            "operating_expenses", "total_assets", "total_liabilities",
            "stockholders_equity", "operating_cash_flow", "capital_expenditure",
        }
        MAX_CONCEPTS = 15
        task_tokens = _tokens(task)

        # Score all non-core concepts by task relevance
        scored_extras = []
        for term_key, fact_names in key_metrics.items():
            if term_key in CORE_CONCEPTS:
                continue
            term = self.store.terms.get(f"term.finance.{term_key}")
            search_text = term_key
            if term:
                aliases_text = " ".join(term.aliases or [])
                def_text = term.definition or ""
                search_text = f"{term_key} {term.name} {aliases_text} {def_text}"
            score = len(task_tokens & _tokens(search_text))
            scored_extras.append((term_key, fact_names, score))
        scored_extras.sort(key=lambda x: -x[2])

        # Start with core concepts
        filtered = {k: v for k, v in key_metrics.items() if k in CORE_CONCEPTS}
        # Fill up to MAX_CONCEPTS with best-matching extras
        for term_key, fact_names, score in scored_extras:
            if len(filtered) >= MAX_CONCEPTS:
                break
            filtered[term_key] = fact_names

        return filtered

    # ---- browse_semantics --------------------------------------------------

    _MAX_BROWSE_ITEMS = 6

    def browse(self, query: str = "", kind: str = "term",
               scope: Optional[Dict] = None, limit: int = 6) -> Dict[str, Any]:
        """Return a small, deterministic set of query-relevant catalog items."""
        query = str(query or "").strip().lower()
        kind = str(kind).lower()
        if kind not in ("term", "mapping", "relation", "constraint", "all"):
            kind = "term"
        limit = max(1, min(int(limit or self._MAX_BROWSE_ITEMS), self._MAX_BROWSE_ITEMS))

        items = []
        if kind in ("term", "all"):
            items.extend(
                self._browse_view(item, "term")
                for item in self.store.terms.values() if _is_active(item)
            )
        if kind in ("relation", "all"):
            items.extend(
                self._browse_view(item, "relation")
                for item in self.store.relations.values() if _is_active(item)
            )
        if kind in ("mapping", "all"):
            items.extend(
                self._browse_view(item, "mapping")
                for item in self.store.mappings.values() if _is_active(item)
            )
        if kind in ("constraint", "all"):
            items.extend(
                self._browse_view(item, "constraint")
                for item in self.store.constraints.values() if _is_active(item)
            )
        if scope:
            items = [item for item in items if _matches_scope_str(item.get("scope", ""), scope)]

        catalog_total = len(items)
        if not query:
            return {
                "status": "needs_query",
                "items": [],
                "catalog_total": catalog_total,
                "version": self.version,
                "note": "Provide a specific query. Full-catalog enumeration is disabled.",
            }

        query_tokens = _tokens(query)
        ranked = []
        for item in items:
            search_text = item.pop("_search_text", "")
            overlap = sorted(query_tokens & _tokens(search_text))
            phrase_match = query in search_text.lower()
            if not overlap and not phrase_match:
                continue
            score = (2 if phrase_match else 0) + len(overlap)
            item["match_rationale"] = {
                "phrase_match": phrase_match,
                "overlap_tokens": overlap,
            }
            ranked.append((score, item))
        ranked.sort(key=lambda pair: (-pair[0], pair[1]["id"]))

        return {
            "status": "ok",
            "items": [item for _, item in ranked[:limit]],
            "matched_total": len(ranked),
            "catalog_total": catalog_total,
            "version": self.version,
            "note": "Only query-relevant items are returned; this is not an analysis checklist.",
        }

    @staticmethod
    def _browse_view(item: Any, kind: str) -> dict:
        name = getattr(item, "name", "") or ""
        if not name and kind == "mapping":
            name = f"{getattr(item, 'term_id', '')} -> {getattr(item, 'table', '')}"
        scope_val = getattr(item, "scope", "")
        return {
            "id": item.id,
            "name": name,
            "type": getattr(item, "type", kind),
            "scope": scope_val,
            "availability": getattr(item, "lifecycle_state", "active"),
            "_search_text": " ".join([
                name,
                str(getattr(item, "definition", "") or ""),
                " ".join(getattr(item, "aliases", []) or []),
                str(getattr(item, "term_id", "") or ""),
            ]),
        }

    # ---- resolve_semantics -------------------------------------------------

    def resolve(self, mentions: Optional[List[str]] = None,
                context: Optional[str] = None) -> Dict[str, Any]:
        """Resolve the requested value."""
        mentions = [str(m).strip().lower() for m in (mentions or []) if str(m).strip()]
        requested = [("mention", value) for value in mentions]
        accepted = requested[:5]
        skipped = requested[5:]
        mentions = [value for _kind, value in accepted]

        results = []

        # Resolve by mentions
        for mention in mentions:
            prev_status = self._resolved_mentions.get(mention)
            if prev_status == "unresolved":
                results.append({
                    "query": mention,
                    "query_type": "mention",
                    "status": "unresolved",
                    "match_rationale": f"Already resolved '{mention}' — unresolved. Explore the database directly.",
                    "candidates": [],
                })
                continue

            candidates = self._match_mention(mention)
            if not candidates:
                self._resolved_mentions[mention] = "unresolved"
                results.append({
                    "query": mention,
                    "query_type": "mention",
                    "status": "unresolved",
                    "match_rationale": f"No active term matching '{mention}'. Inspect the database directly.",
                    "candidates": [],
                })
            elif len(candidates) == 1:
                item, rationale = candidates[0]
                self._resolved_mentions[mention] = "resolved"
                results.append(self._build_resolve_result(mention, item, rationale, "resolved"))
            else:
                candidate_results = []
                for item, rationale in candidates:
                    candidate_results.append(self._build_resolve_result(mention, item, rationale, "ambiguous"))
                self._resolved_mentions[mention] = "ambiguous"
                results.append({
                    "query": mention,
                    "query_type": "mention",
                    "status": "ambiguous",
                    "match_rationale": f"{len(candidates)} terms match '{mention}'",
                    "candidates": candidate_results,
                })

        for result in results:
            if result.get("status") == "unresolved":
                result["coverage_status"] = "unavailable"
            elif result.get("status") == "ambiguous":
                result["coverage_status"] = "partial"
            elif result.get("mappings") or result.get("mapping"):
                result["coverage_status"] = "supported"
            else:
                result["coverage_status"] = "partial"

        return {
            "status": "partial" if skipped else "ok",
            "results": results,
            "requested_count": len(requested),
            "processed_count": len(accepted),
            "skipped": [{"query_type": kind_, "query": value} for kind_, value in skipped],
            "version": self.version,
            "note": (
                "Resolve is limited to 5 relevant concepts. Do not expand skipped items into a generic checklist."
                if skipped else "Use these mappings only for the current analysis need."
            ),
        }

    def _match_mention(self, mention: str) -> List[tuple]:
        """Match a mention string against active terms by name/alias."""
        mention_lower = mention.lower()
        exact_name: List[tuple] = []
        exact_alias: List[tuple] = []
        substring: List[tuple] = []

        for term in self.store.terms.values():
            if not _is_active(term):
                continue
            name_lower = term.name.lower()
            if mention_lower == name_lower:
                exact_name.append((term, f"exact name match: '{term.name}'"))
                continue
            if any(mention_lower == a.lower() for a in (term.aliases or [])):
                exact_alias.append((term, f"exact alias match: term '{term.name}'"))
                continue
            if mention_lower in name_lower or any(mention_lower in a.lower() for a in (term.aliases or [])):
                substring.append((term, f"substring match: term '{term.name}'"))

        if exact_name:
            return exact_name
        if exact_alias:
            return exact_alias
        return substring

    def _build_resolve_result(self, query: str, item: Any, rationale: str,
                              status: str) -> dict:
        """Build a single resolve result from a matched term."""
        term_id = item.id if hasattr(item, "id") else ""
        result: Dict[str, Any] = {
            "query": query,
            "query_type": "semantic_id" if "id_lookup" in rationale else "mention",
            "status": status,
            "match_rationale": rationale,
            "term": self._term_summary(item),
            "guidance": (
                "This is a navigation aid, not a complete analysis target. "
                "The binding shows where this concept lives in the database. "
                "Use SELECT DISTINCT fact_name FROM <table> to discover the full range of question-relevant data. "
                "Do not compute only this metric; explore related fact_names to address the specific question."
            ),
        }

        # Attach related mappings
        related_mappings = []
        for m in self.store.mappings.values():
            if not _is_active(m):
                continue
            if m.term_id == term_id:
                mapping_entry = {
                    "id": m.id,
                    "term_id": m.term_id,
                    "table": m.table,
                    "column": m.column,
                    "semantic_filter": m.semantic_filter,
                    "mapping_type": m.mapping_type,
                    "evidence_refs": list(m.evidence_refs),
                }
                related_mappings.append(mapping_entry)
        if related_mappings:
            result["mappings"] = related_mappings

        # Attach related relations
        related_relations = []
        for r in self.store.relations.values():
            if not _is_active(r):
                continue
            if r.source == term_id or r.target == term_id:
                related_relations.append({
                    "id": r.id,
                    "relation_type": r.relation_type,
                    "source": r.source,
                    "target": r.target,
                    "connection_condition": r.connection_condition or "",
                    "evidence_refs": list(r.evidence_refs),
                })
        if related_relations:
            result["relations"] = related_relations

        # Attach related constraints
        related_constraints = []
        for c in self.store.constraints.values():
            if not _is_active(c):
                continue
            input_terms = list(c.input_terms) if c.input_terms else []
            if term_id in input_terms or c.target == term_id:
                related_constraints.append({
                    "id": c.id,
                    "severity": c.severity,
                    "description": c.description or "",
                    "evidence_refs": list(c.evidence_refs),
                })
        if related_constraints:
            result["constraints"] = related_constraints

        return result

    @staticmethod
    def _term_summary(term: Any) -> dict:
        return {
            "id": term.id,
            "type": term.type,
            "name": term.name,
            "aliases": list(getattr(term, "aliases", [])),
            "definition": term.definition,
            "value_type": getattr(term, "value_type", ""),
            "grain": getattr(term, "grain", ""),
            "evidence_refs": list(getattr(term, "evidence_refs", [])),
        }

    def execute(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        if name == "browse_semantics":
            return self.browse(
                query=arguments.get("query", ""),
                kind=arguments.get("kind", "term"),
                scope=arguments.get("scope"),
                limit=arguments.get("limit", 6),
            )
        if name == "resolve_semantics":
            return self.resolve(
                mentions=arguments.get("mentions"),
                context=arguments.get("context"),
            )
        raise ValueError(f"Unknown semantic tool: {name}")


def _is_active(item: Any) -> bool:
    return item.lifecycle_state in _ACTIVE_STATES


def _matches_scope_str(item_scope: str, filter_scope: dict) -> bool:
    """Match item scope string against filter dict (for backward compat with scope param)."""
    for k, v in filter_scope.items():
        if k not in item_scope.lower():
            return False
    return True
