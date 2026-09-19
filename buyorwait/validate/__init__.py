"""Validation layer: schema, citations, coherence, and the combined gate.

These modules operate on `buyorwait.types` objects and plain row dicts only.
They never import ingest/, engine/ or perception/.
"""
from buyorwait.validate.citations import validate_citations
from buyorwait.validate.coherence import check_coherence
from buyorwait.validate.gate import gate
from buyorwait.validate.schema import validate_row

__all__ = ["validate_row", "validate_citations", "check_coherence", "gate"]
