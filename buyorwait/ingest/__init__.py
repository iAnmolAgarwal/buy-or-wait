"""Ingest layer: dataset loading, FX conversion, lifecycle resolution, recurrence."""
from __future__ import annotations

from buyorwait.ingest import fx, lifecycle, loader, recurrence
from buyorwait.ingest.lifecycle import exclusion_reason, resolve_lifecycles
from buyorwait.ingest.loader import load_dataset
from buyorwait.ingest.recurrence import detect_streams, next_occurrences, salary_streams

__all__ = [
    "fx",
    "lifecycle",
    "loader",
    "recurrence",
    "load_dataset",
    "resolve_lifecycles",
    "exclusion_reason",
    "detect_streams",
    "next_occurrences",
    "salary_streams",
]
