"""
I3 — Run/trace utility.

Provides run_id generation, timestamps, and input hashing so every artifact
can carry enough provenance to be traced back to exactly what produced it.
Deliberately has zero dependency on the schemas (I1) or validator (I2) so it
can be built/tested fully in parallel with them.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "1.0"


def new_run_id() -> str:
    return str(uuid.uuid4())


def new_proposal_id() -> str:
    return str(uuid.uuid4())


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def hash_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def hash_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


def hash_json_artifact(artifact: dict[str, Any]) -> str:
    """Canonical hash of a JSON artifact (used to populate input_hash /
    based_on_input_hash fields on downstream artifacts).

    Sorts keys and uses a fixed separator so the same logical content
    always hashes the same way regardless of dict ordering.
    """
    canonical = json.dumps(artifact, sort_keys=True, separators=(",", ":"))
    return hash_bytes(canonical.encode("utf-8"))


def hash_json_file(path: str | Path) -> str:
    with open(path, encoding="utf-8") as f:
        artifact = json.load(f)
    return hash_json_artifact(artifact)


def run_dir(base_runs_dir: str | Path, run_id: str) -> Path:
    d = Path(base_runs_dir) / run_id
    d.mkdir(parents=True, exist_ok=True)
    return d
