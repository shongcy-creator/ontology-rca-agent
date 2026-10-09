"""Load the immutable, versioned DDR semantic store."""

from pathlib import Path
from typing import Dict, Optional

from evoontology import SemanticStore

from .models import Constraint, Evidence, Mapping, Relation, Term

DEFAULT_STORE_DIR = Path(__file__).resolve().parents[1] / ".evoontology"


class VersionedSemanticStore:
    """In-memory view of the semantic version selected by ``active.json``."""

    def __init__(
        self,
        version: str,
        terms: Dict[str, Term],
        relations: Dict[str, Relation],
        mappings: Dict[str, Mapping],
        constraints: Dict[str, Constraint],
        evidence: Dict[str, Evidence],
        root_dir: str = "",
    ):
        self.version = version
        self.terms = terms
        self.relations = relations
        self.mappings = mappings
        self.constraints = constraints
        self.evidence = evidence
        self.root_dir = root_dir

    @classmethod
    def load(cls, path: Optional[str] = None, version: Optional[str] = None) -> "VersionedSemanticStore":
        """Load the version selected by ``active.json`` or given explicitly."""
        store_dir = Path(path) if path else DEFAULT_STORE_DIR
        version, raw_records = SemanticStore.load_records(str(store_dir), version=version)
        required = {
            "terms": Term,
            "relations": Relation,
            "mappings": Mapping,
            "constraints": Constraint,
            "evidence": Evidence,
        }
        loaded = {
            family: {item["id"]: model.from_dict(item) for item in raw_records[family]}
            for family, model in required.items()
        }
        return cls(version=version, root_dir=str(store_dir), **loaded)

    def get(self, semantic_id: str):
        """Return an object from any semantic record family."""
        return (
            self.terms.get(semantic_id)
            or self.relations.get(semantic_id)
            or self.mappings.get(semantic_id)
            or self.constraints.get(semantic_id)
            or self.evidence.get(semantic_id)
        )
