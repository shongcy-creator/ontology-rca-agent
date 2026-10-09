#!/usr/bin/env python3
"""Implementation for the bird.tceo.loader module."""

from pathlib import Path
from typing import Dict

from evoontology import SemanticStore

from .models import Constraint, Evidence, Mapping, Relation, Term


class SemanticLayerLoader:
    """Implementation of SemanticLayerLoader."""

    def __init__(self, store_path: str, version: str = ""):
        self.store_path = Path(store_path)
        self._requested_version = str(version or "")
        self.terms: Dict[str, Term] = {}
        self.mappings: Dict[str, Mapping] = {}
        self.relations: Dict[str, Relation] = {}
        self.constraints: Dict[str, Constraint] = {}
        self.evidence: Dict[str, Evidence] = {}
        self.version: str = "unknown"
        self._records: Dict[str, list] = {}
        self._loaded = False

    @classmethod
    def load(cls, store_path: str, version: str = "") -> "SemanticLayerLoader":
        instance = cls(store_path, version=version)
        instance._load_all()
        return instance

    def _load_all(self):
        if not self.store_path.exists():
            raise FileNotFoundError(f"Ontology-layer directory does not exist: {self.store_path}")

        self.version, self._records = SemanticStore.load_records(
            str(self.store_path), version=self._requested_version or None
        )

        self._load_terms()
        self._load_mappings()
        self._load_relations()
        self._load_constraints()
        self._load_evidence()
        self._load_manifest()
        self._loaded = True

    def _read_json(self, filename: str) -> list:
        family = filename.removesuffix(".json")
        return list(self._records.get(family, []))

    def _load_terms(self):
        for item in self._read_json("terms.json"):
            term = Term(
                id=item["id"],
                type=item.get("type", "entity"),
                name=item.get("name", ""),
                definition=item.get("definition", ""),
                scope=item.get("scope", ""),
                aliases=item.get("aliases", []),
                evidence_refs=item.get("evidence_refs", item.get("evidence", [])),
                tables=item.get("tables", []),
                grain=item.get("grain", ""),
                value_type=item.get("value_type", ""),
                confidence_level=item.get("confidence", item.get("confidence_level", "medium")),
            )
            self.terms[term.id] = term

    def _load_mappings(self):
        for item in self._read_json("mappings.json"):
            validation = item.get("validation", "")
            if isinstance(validation, dict):
                evidence_refs = item.get("evidence_refs", validation.get("evidence", []))
                validation = validation.get("description", "")
            else:
                evidence_refs = item.get("evidence_refs", item.get("evidence", []))
            mapping = Mapping(
                id=item["id"],
                term_id=item.get("term_id", ""),
                database_source=item.get("database_source", ""),
                table=item.get("table", ""),
                column=item.get("column", ""),
                semantic_filter=item.get("semantic_filter", item.get("filter", "")),
                aggregation_semantics=item.get("aggregation_semantics", item.get("aggregation", "")),
                grain=item.get("grain", ""),
                validation=validation,
                evidence_refs=evidence_refs,
                data_type=item.get("data_type", ""),
                join_path=item.get("join_path", []),
                confidence_level=item.get("confidence", item.get("confidence_level", "medium")),
            )
            self.mappings[mapping.id] = mapping

    def _load_relations(self):
        for item in self._read_json("relations.json"):
            relation = Relation(
                id=item["id"],
                source=item.get("source", item.get("from_term", "")),
                relation_type=item.get("relation_type", item.get("type", "association")),
                target=item.get("target", item.get("to_term", "")),
                connection_condition=item.get("connection_condition", item.get("join_condition", "")),
                description=item.get("description", ""),
                evidence_refs=item.get("evidence_refs", item.get("evidence", [])),
                confidence_level=item.get("confidence", item.get("confidence_level", "medium")),
            )
            self.relations[relation.id] = relation

    def _load_constraints(self):
        for item in self._read_json("constraints.json"):
            constraint = Constraint(
                id=item["id"],
                target=item.get("target", item.get("term_id", "")),
                constraint_type=item.get("constraint_type", item.get("type", "")),
                trigger_keywords=item.get("trigger_keywords", []),
                severity=item.get("severity", "warning"),
                scope=item.get("scope", ""),
                confidence_level=item.get("confidence", item.get("confidence_level", "medium")),
                description=item.get("description", ""),
                evidence_refs=item.get("evidence_refs", item.get("evidence", [])),
            )
            self.constraints[constraint.id] = constraint

    def _load_evidence(self):
        for item in self._read_json("evidence.json"):
            evidence = Evidence(
                id=item["id"],
                source=item.get("source", ""),
                query=item.get("query", ""),
                result=item.get("result", ""),
                validation_method=item.get("validation_method", ""),
                timestamp=item.get("timestamp", ""),
                content=item.get("content", ""),
                term_id=item.get("term_id", ""),
                mapping_id=item.get("mapping_id", ""),
                type=item.get("type", ""),
                confidence_level=item.get("confidence", item.get("confidence_level", "medium")),
            )
            self.evidence[evidence.id] = evidence

    def _load_manifest(self):
        manifest_path = self.store_path / "manifest.md"
        if not manifest_path.exists():
            return
        content = manifest_path.read_text(encoding="utf-8")
        for line in content.split("\n"):
            if line.startswith("Ontology layer version:"):
                self.version = line.split(":", 1)[1].strip()
                break

    def get(self, semantic_id: str):
        for store in [self.terms, self.mappings, self.relations, self.constraints, self.evidence]:
            if semantic_id in store:
                return store[semantic_id]
        return None

    def stats(self) -> dict:
        return {
            "terms": len(self.terms),
            "mappings": len(self.mappings),
            "relations": len(self.relations),
            "constraints": len(self.constraints),
            "evidence": len(self.evidence),
            "version": self.version,
        }
