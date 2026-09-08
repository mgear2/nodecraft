from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

CACHE_VERSION = 1


class HashCache:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.entries: dict[str, dict] = {}

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if (
                isinstance(data, dict)
                and data.get("version") == CACHE_VERSION
                and isinstance(data.get("entries"), dict)
            ):
                self.entries = data["entries"]
        except (OSError, json.JSONDecodeError):
            self.entries = {}

    def get(self, path: str, stat_result: os.stat_result) -> str | None:
        entry = self.entries.get(path)
        if not entry:
            return None
        identity = (entry.get("size"), entry.get("mtime_ns"), entry.get("file_id"))
        current = (stat_result.st_size, stat_result.st_mtime_ns, getattr(stat_result, "st_ino", 0))
        return entry.get("hash") if identity == current else None

    def put(self, path: str, stat_result: os.stat_result, content_hash: str) -> None:
        self.entries[path] = {
            "size": stat_result.st_size,
            "mtime_ns": stat_result.st_mtime_ns,
            "file_id": getattr(stat_result, "st_ino", 0),
            "hash": content_hash,
        }

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"version": CACHE_VERSION, "entries": self.entries}, indent=2)
        fd, temp_name = tempfile.mkstemp(prefix=".hash-cache-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as file:
                file.write(payload)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temp_name, self.path)
        except BaseException:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
            raise
