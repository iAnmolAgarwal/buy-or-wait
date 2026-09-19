"""On-disk response cache for model calls.

`cache_key` mixes the request_id into the digest, so the same payload seen under
two different request_ids yields two different keys and can never cross-hit.
"""
from __future__ import annotations

import hashlib
import json
import os
from typing import Any, Optional


def canonical_json(payload: Any) -> str:
    """Deterministic JSON: sorted keys, no incidental whitespace."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str,
                      ensure_ascii=False)


def cache_key(request_id: str, payload: dict) -> str:
    """sha256 over request_id + canonical JSON of the payload."""
    h = hashlib.sha256()
    h.update(request_id.encode("utf-8"))
    h.update(b"\x00")
    h.update(canonical_json(payload).encode("utf-8"))
    return h.hexdigest()


class ResponseCache:
    """Stores one JSON document per key at ``<dir>/<key>.json``."""

    def __init__(self, dir: str) -> None:
        self.dir = dir
        self.hits = 0
        self.misses = 0

    def _path(self, key: str) -> str:
        return os.path.join(self.dir, f"{key}.json")

    def get(self, key: str) -> Optional[dict]:
        path = self._path(key)
        if not os.path.isfile(path):
            self.misses += 1
            return None
        with open(path, "r", encoding="utf-8") as fh:
            raw = fh.read()
        if not raw.strip():
            # A truncated/empty cache file is a miss, not an error.
            self.misses += 1
            return None
        value = json.loads(raw)
        if not isinstance(value, dict):
            self.misses += 1
            return None
        self.hits += 1
        return value

    def put(self, key: str, value: dict) -> None:
        os.makedirs(self.dir, exist_ok=True)
        tmp = self._path(key) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(canonical_json(value))
        os.replace(tmp, self._path(key))
