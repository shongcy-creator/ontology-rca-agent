#!/usr/bin/env python3
"""Implementation for the bird.tceo.runtime module."""

import re
from typing import Any, Dict, List, Optional

from .binder import DeterministicBinder, TaskBinding
from .loader import SemanticLayerLoader
from .models import Term, Mapping, Relation, Constraint


def _tokenize(text: str) -> set:
    """Implement tokenize."""
    return set(re.findall(r"\w+", str(text).lower(), flags=re.UNICODE))


class BIRDSemanticLayer:
    """Implementation of BIRDSemanticLayer."""

    MAX_BROWSE = 8
    MAX_RESOLVE = 5

    def __init__(self, store_path: str, min_browse_score: int = 0,
                 max_browse_results: int = 0, use_basic_browse: bool = False,
                 version: str = ""):
        self.loader = SemanticLayerLoader.load(store_path, version=version)
        self._binder = DeterministicBinder(
            mappings=self.loader.mappings,
            terms=self.loader.terms,
        )
        self._bindings: List[TaskBinding] = []
        self._resolved_cache: Dict[str, str] = {}
        self.min_browse_score = min_browse_score
        self.max_browse_results = max_browse_results or self.MAX_BROWSE
        self.use_basic_browse = use_basic_browse

    @property
    def version(self) -> str:
        return self.loader.version

    # =========================================================================
    # Manifest
    # =========================================================================

    def manifest(self, db_id: str = "") -> str:
        """Implement manifest."""
        stats = self.loader.stats()
        db_label = f"{db_id}" if db_id else "unknown"
        lines = [
            f"Ontology layer: {db_label} "
            f"({stats['terms']} concepts, {stats['mappings']} mappings, "
            f"{stats['constraints']} constraints)",
            "",
            "The ontology layer provides workload-specific semantic "
            "grounding beyond raw schema information.",
            "",
            "Use semantic tools when:",
            "- the question contains ambiguous business concepts, "
            "metrics, categories, or domain terms;",
            "- a concept may map to multiple tables or columns;",
            "- definitions, value meanings, or constraints are not "
            "clear from schema alone.",
            "",
            "Recommended workflow:",
            "1. browse_semantics — discover relevant concepts.",
            "2. resolve_semantics — retrieve mappings, relationships, "
            "and constraints for those concepts.",
            "3. Verify with describe_table / execute_query.",
            "",
            "Skip when tables and columns are already explicit in "
            "the question, or browse returns irrelevant results.",
            "",
            "Tools:",
            "- browse_semantics(query): search concepts by natural "
            "language.",
            "- resolve_semantics(concept_id): get definitions, "
            "mappings, relationships, and constraints.",
            "",
            "Semantic information should guide, not replace, schema "
            "verification. Always confirm with the database.",
        ]
        return "\n".join(lines)

    # =========================================================================

    # =========================================================================

    def enrich_schema(self,
                      table_name: str,
                      columns: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Implement enrich schema."""
        if not self._bindings:
            return columns

        enriched = []
        for col in columns:
            col_copy = dict(col)
            binding = self._find_binding(table_name, col.get("name", ""))
            if binding and binding.confidence != "low":
                col_copy["_semantic"] = {
                    "term_id": binding.term_id,
                    "term_name": binding.term_name,
                    "confidence": binding.confidence,
                }
            enriched.append(col_copy)
        return enriched

    def bind_schema(self, schema: Dict[str, List[dict]]):
        """Bind schema."""
        self._bindings = self._binder.bind(schema)

    def _find_binding(self, table: str, column: str) -> Optional[TaskBinding]:
        """Find binding."""
        table_l = table.lower()
        col_l = column.lower()
        for b in self._bindings:
            if b.table.lower() == table_l and b.column.lower() == col_l:
                return b
        return None

    # =========================================================================
    # browse_semantics
    # =========================================================================

    def browse(self, query: str = "", kind: str = "all",
               limit: int = 5) -> Dict[str, Any]:
        """Browse the requested value."""
        query = str(query or "").strip().lower()
        kind = str(kind).lower()
        if kind not in ("entity", "metric", "dimension", "category", "concept", "all"):
            kind = "all"

        limit = max(1, min(int(limit or 5), self.max_browse_results))


        items = []
        if kind in ("entity", "metric", "dimension", "category", "concept"):
            for term in self.loader.terms.values():
                if term.type == kind:
                    items.append(self._browse_term_view(term))
        else:
            for term in self.loader.terms.values():
                items.append(self._browse_term_view(term))

        catalog_total = len(items)

        if not query:

            for item in items:
                item.pop("_search_text", None)
                item.pop("_search_name", None)
                item.pop("_search_aliases", None)
                item.pop("_search_def", None)
                item.pop("_search_tables", None)
            return {
                "status": "ok",
                "results": items[:limit],
                "catalog_total": catalog_total,
                "version": self.version,
                "note": f"Showing first {min(limit, len(items))} of {catalog_total} terms. "
                        f"Provide a specific query for targeted results.",
            }


        query_tokens = _tokenize(query)
        ranked = []
        for item in items:
            search_text = item.pop("_search_text", "").lower()
            search_tokens = _tokenize(search_text)


            name_text = item.pop("_search_name", "").lower()
            aliases_text = item.pop("_search_aliases", "").lower()
            def_text = item.pop("_search_def", "").lower()
            tables_text = item.pop("_search_tables", "").lower()

            name_tokens = _tokenize(name_text)
            aliases_tokens = _tokenize(aliases_text)
            def_tokens = _tokenize(def_text)
            tables_tokens = _tokenize(tables_text)

            overlap = sorted(query_tokens & search_tokens)
            name_hits = query_tokens & name_tokens
            aliases_hits = query_tokens & aliases_tokens
            def_hits = query_tokens & def_tokens
            tables_hits = query_tokens & tables_tokens


            substring_hits = []
            for qt in query_tokens:
                if len(qt) < 3:
                    continue
                for st in search_tokens:
                    if len(st) >= 3 and qt != st and (qt in st or st in qt):
                        substring_hits.append(qt)
                        break

            phrase_match = query in search_text

            if not overlap and not phrase_match and not substring_hits:
                continue

            if self.use_basic_browse:
                # Basic: equal-weight scoring (C8 algorithm)
                score = len(overlap) + len(substring_hits)
                score += 1 if phrase_match else 0
            else:
                # Weighted score: name > aliases > definition/tables > substring
                score = 0
                score += 5 if phrase_match else 0
                score += len(name_hits) * 3
                score += len(aliases_hits) * 2
                score += len(def_hits) * 1
                score += len(tables_hits) * 1
                score += len(substring_hits) * 1

            all_hits = sorted(set(list(overlap) + substring_hits))
            item["match_tokens"] = all_hits
            item["relevance_score"] = score
            ranked.append((score, item))

        ranked.sort(key=lambda p: (-p[0], p[1]["semantic_id"]))


        filtered_count = 0
        if self.min_browse_score > 0:
            filtered = [(s, item) for s, item in ranked if s >= self.min_browse_score]
            filtered_count = len(ranked) - len(filtered)
            ranked = filtered

        note = ""
        if filtered_count > 0:
            note = (f"{filtered_count} low-relevance result(s) filtered "
                    f"(score < {self.min_browse_score}). "
                    f"Try more specific keywords if needed.")

        return {
            "status": "ok",
            "results": [item for _, item in ranked[:limit]],
            "matched_total": len(ranked),
            "catalog_total": catalog_total,
            "version": self.version,
            "note": note or None,
        }

    def _browse_term_view(self, term: Term) -> dict:

        bound_cols = [
            f"{b.table}.{b.column}"
            for b in self._bindings
            if b.term_id == term.id
        ]
        view = {
            "semantic_id": term.id,
            "type": term.type,
            "name": term.name,
            "definition": term.definition,
            "tables": term.tables,
            "grain": term.grain,
            "_search_text": " ".join([
                term.name, term.definition, " ".join(term.aliases),
                " ".join(term.tables), term.type,
            ]),
            "_search_name": term.name,
            "_search_aliases": " ".join(term.aliases),
            "_search_def": term.definition,
            "_search_tables": " ".join(term.tables),
        }
        if bound_cols:
            view["columns"] = bound_cols
        return view

    # =========================================================================
    # resolve_semantics
    # =========================================================================

    def resolve(self, mentions: Optional[List[str]] = None,
                context: str = "") -> Dict[str, Any]:
        """Resolve the requested value."""
        mentions = [str(m).strip().lower() for m in (mentions or []) if str(m).strip()]
        accepted = mentions[:self.MAX_RESOLVE]
        skipped = mentions[self.MAX_RESOLVE:]

        results = []
        for mention in accepted:

            item = self.loader.get(mention)
            if item and isinstance(item, Term):
                results.append(self._build_resolve_result(mention, item, "id_lookup"))
                continue


            term_from_binding = self._resolve_via_binding(mention)
            if term_from_binding:
                t, rationale = term_from_binding
                results.append(self._build_resolve_result(mention, t, rationale))
                continue


            candidates = self._match_term(mention)
            if not candidates:
                results.append({
                    "query": mention,
                    "status": "unavailable",
                    "match_rationale": f"No term matching '{mention}'",
                })
            elif len(candidates) == 1:
                term, rationale = candidates[0]
                results.append(self._build_resolve_result(mention, term, rationale))
            else:

                results.append({
                    "query": mention,
                    "status": "ambiguous",
                    "match_rationale": f"{len(candidates)} terms match '{mention}'",
                    "candidates": [
                        self._build_resolve_result(mention, t, r)
                        for t, r in candidates
                    ],
                })

        return {
            "status": "ok",
            "results": results,
            "requested_count": len(mentions),
            "processed_count": len(accepted),
            "skipped": skipped,
            "version": self.version,
        }

    def _resolve_via_binding(self, mention: str) -> Optional[tuple]:
        """Resolve via binding."""
        mention_l = mention.lower()
        for b in self._bindings:
            full = f"{b.table}.{b.column}".lower()
            if mention_l == full or mention_l in full:
                term = self.loader.terms.get(b.term_id)
                if term:
                    return (term, f"column binding: {full}")
            if mention_l == b.column.lower():
                term = self.loader.terms.get(b.term_id)
                if term:
                    return (term, f"column binding: {b.column} → {term.name}")
        return None

    def _match_term(self, mention: str) -> List[tuple]:
        """Implement match term."""
        mention_lower = mention.lower().replace("_", " ")

        exact_label = []
        exact_alias = []
        substring = []

        for term in self.loader.terms.values():
            label_lower = term.name.lower()
            if mention_lower == label_lower:
                exact_label.append((term, f"exact name match: '{term.name}'"))
                continue
            if any(mention_lower == a.lower() for a in term.aliases):
                exact_alias.append((term, f"exact alias match for '{term.name}'"))
                continue
            if mention_lower in label_lower or any(
                mention_lower in a.lower() for a in term.aliases
            ):
                substring.append((term, f"substring match: '{term.name}'"))

        if exact_label:
            return exact_label
        if exact_alias:
            return exact_alias
        if substring:
            return substring

        # Token-by-token fallback for multi-word mentions
        tokens = [t for t in mention_lower.split() if len(t) > 2]
        if len(tokens) <= 1:
            return []

        def _token_in_term(term, token: str) -> bool:
            """Implement token in term."""
            if token in term.name.lower():
                return True
            if any(token in a.lower() for a in term.aliases):
                return True
            if token in term.definition.lower():
                return True
            return False

        def _token_in_term_constraints(term, token: str) -> bool:
            """Implement token in term constraints."""
            for c in self.loader.constraints.values():
                if c.target == term.id:
                    c_text = (c.description + " " +
                              " ".join(c.trigger_keywords)).lower()
                    if token in c_text:
                        return True
            return False

        token_matches = {}
        for token in tokens:
            for term in self.loader.terms.values():
                tid = term.id
                if tid in token_matches:
                    continue
                if _token_in_term(term, token):
                    token_matches[tid] = (term, token)
                elif _token_in_term_constraints(term, token):
                    token_matches[tid] = (term, token)

        return [(t, f"token '{tok}' matched term '{t.name}'")
                for t, tok in token_matches.values()]

    def _build_resolve_result(self, query: str, term: Term,
                              rationale: str) -> dict:
        """Build resolve result."""
        result = {
            "query": query,
            "semantic_id": term.id,
            "name": term.name,
            "type": term.type,
            "definition": term.definition,
            "status": "supported",
            "match_rationale": rationale,
            "mappings": [],
            "relations": [],
            "constraints": [],
            "evidence": [],
        }


        for m in self.loader.mappings.values():
            if m.term_id == term.id:
                result["mappings"].append({
                    "mapping_id": m.id,
                    "table": m.table,
                    "column": m.column,
                    "data_type": m.data_type,
                    "semantic_filter": m.semantic_filter,
                    "aggregation_semantics": m.aggregation_semantics,
                    "grain": m.grain,
                    "join_path": m.join_path,
                    "evidence_refs": m.evidence_refs,
                })


        for r in self.loader.relations.values():
            if r.source == term.id or r.target == term.id:
                result["relations"].append({
                    "relation_id": r.id,
                    "relation_type": r.relation_type,
                    "source": r.source,
                    "target": r.target,
                    "connection_condition": r.connection_condition,
                    "description": r.description,
                })


        for c in self.loader.constraints.values():
            if c.target == term.id:
                result["constraints"].append({
                    "constraint_id": c.id,
                    "constraint_type": c.constraint_type,
                    "description": c.description,
                    "severity": c.severity,
                    "evidence_refs": c.evidence_refs,
                })


        for ev_ref in term.evidence_refs:
            if ev_ref in self.loader.evidence:
                e = self.loader.evidence[ev_ref]
                result["evidence"].append({
                    "evidence_id": e.id,
                    "type": e.type,
                    "source": e.source,
                    "content": e.content,
                    "query": e.query,
                    "result": e.result,
                })


        bound_cols = [
            f"{b.table}.{b.column}"
            for b in self._bindings
            if b.term_id == term.id
        ]
        if bound_cols:
            result["bound_columns"] = bound_cols


        if not result["mappings"]:
            result["status"] = "partial"

        return result

    def execute(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Execute the requested value."""
        if name == "browse_semantics":
            return self.browse(
                query=arguments.get("query", ""),
                kind=arguments.get("kind", "all"),
                limit=arguments.get("limit", 5),
            )
        if name == "resolve_semantics":
            return self.resolve(
                mentions=arguments.get("mentions", []),
                context=arguments.get("context", ""),
            )
        raise ValueError(f"Unknown semantic tool: {name}")
