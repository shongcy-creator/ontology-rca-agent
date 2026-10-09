"""Load an immutable, versioned InsightBench semantic store."""

from pathlib import Path
from typing import Dict, Optional

from evoontology import SemanticStore
from insightbench.tceo.models import Constraint, Evidence, Mapping, Relation, Term

_DEFAULT_STORE_DIR = Path(__file__).resolve().parents[2] / ".evoontology"
DEFAULT_STORE_PATH = str(_DEFAULT_STORE_DIR)


class VersionedSemanticStore:
    """In-memory view of the version selected by ``active.json``."""

    def __init__(
        self,
        version: str,
        terms: Dict[str, Term],
        relations: Dict[str, Relation],
        mappings: Dict[str, Mapping],
        constraints: Dict[str, Constraint],
        evidence: Dict[str, Evidence],
    ):
        self.version = version
        self._terms = terms
        self._relations = relations
        self._mappings = mappings
        self._constraints = constraints
        self._evidence = evidence

    @property
    def terms(self) -> Dict[str, Term]:
        return dict(self._terms)

    @property
    def relations(self) -> Dict[str, Relation]:
        return dict(self._relations)

    @property
    def mappings(self) -> Dict[str, Mapping]:
        return dict(self._mappings)

    @property
    def constraints(self) -> Dict[str, Constraint]:
        return dict(self._constraints)

    @property
    def evidence(self) -> Dict[str, Evidence]:
        return dict(self._evidence)

    def get(self, semantic_id: str):
        """Return an object from any semantic record family."""
        return (
            self._terms.get(semantic_id)
            or self._relations.get(semantic_id)
            or self._mappings.get(semantic_id)
            or self._constraints.get(semantic_id)
            or self._evidence.get(semantic_id)
        )

    @classmethod
    def load(cls, path: Optional[str] = None, version: Optional[str] = None) -> "VersionedSemanticStore":
        """Load the version selected by ``active.json`` or given explicitly."""
        store_dir = Path(path) if path else _DEFAULT_STORE_DIR
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
        return cls(version=version, **loaded)
