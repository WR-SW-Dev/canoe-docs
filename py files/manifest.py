#!/usr/bin/env python3
"""
manifest.py -- Idempotency record, keyed on the Canoe document id.

A local JSON file mapping each Canoe document id to what was done with it. The sync
consults it before doing any work and records an entry only after a document has been
successfully uploaded, so a rerun on the same day skips everything already present and
never duplicates a document in the library. A crash mid-run leaves earlier successes
recorded and simply resumes the rest next time.
"""

from __future__ import annotations

import json
import os
import tempfile


class Manifest:
    def __init__(self, path: str):
        self.path = path
        self._data: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        if os.path.exists(self.path):
            try:
                with open(self.path) as f:
                    self._data = json.load(f)
            except (OSError, ValueError):
                self._data = {}

    def has(self, doc_id: str) -> bool:
        return doc_id in self._data

    def record(self, doc_id: str, dest_path: str, uploaded_at: str, size: int) -> None:
        self._data[doc_id] = {
            "dest_path": dest_path,
            "uploaded_at": uploaded_at,
            "size": size,
        }
        self._save()

    def count(self) -> int:
        return len(self._data)

    def used_paths(self) -> set:
        """Destination paths already claimed by a document (for collision-safe naming)."""
        return {e.get("dest_path") for e in self._data.values() if e.get("dest_path")}

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        # Atomic write so a crash never corrupts the manifest.
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(self.path) or ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(self._data, f, indent=2, sort_keys=True)
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
