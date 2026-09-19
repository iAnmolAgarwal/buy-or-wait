"""Shared test helpers.

The repository ships without `dataset/` in some checkouts (it is excluded from
the deliverable zip), so every test that reads it must skip rather than error at
collection. Import `DATASET_AVAILABLE` / `requires_dataset` from here.
"""
from __future__ import annotations

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

DATASET_DIR = os.path.join(ROOT, "dataset")
DATASET_AVAILABLE = os.path.isfile(os.path.join(DATASET_DIR, "requests.csv"))
MEDIA_AVAILABLE = DATASET_AVAILABLE and os.path.isdir(
    os.path.join(DATASET_DIR, "media", "images"))

requires_dataset = pytest.mark.skipif(
    not DATASET_AVAILABLE, reason="dataset/ is not present in this checkout")
requires_media = pytest.mark.skipif(
    not MEDIA_AVAILABLE, reason="dataset/media/images is not present in this checkout")


def skip_without_dataset() -> None:
    """Call at module import time, before a module-level load_dataset()."""
    if not DATASET_AVAILABLE:
        pytest.skip("dataset/ is not present in this checkout",
                    allow_module_level=True)
