"""Shared scope and provenance helpers for derived artifacts."""

from __future__ import annotations

import posixpath
from pathlib import PurePosixPath
from typing import Any

from lib import trace


def normalize_subtree_path(value: str) -> str:
    """Return a safe, normalized POSIX relative path.

    ``.`` denotes the complete snapshot.  Backslashes are accepted as input
    for convenience, but artifact paths are always emitted with ``/``.
    """

    if not isinstance(value, str):
        raise ValueError("subtree path must be a string")
    text = value.strip().replace("\\", "/")
    if not text or text == ".":
        return "."
    if text.startswith("/") or (len(text) >= 2 and text[1] == ":"):
        raise ValueError(f"subtree path must be relative: {value!r}")
    parts = [part for part in text.split("/") if part not in ("", ".")]
    if any(part == ".." for part in parts):
        raise ValueError(f"subtree path cannot contain '..': {value!r}")
    normalized = posixpath.normpath("/".join(parts))
    if normalized in ("", "."):
        return "."
    # PurePosixPath rejects some odd path forms and makes the intent explicit.
    return PurePosixPath(normalized).as_posix()


def path_in_subtree(path: str, subtree_path: str) -> bool:
    subtree = normalize_subtree_path(subtree_path)
    normalized = path.replace("\\", "/")
    return subtree == "." or normalized == subtree or normalized.startswith(subtree + "/")


def build_scope(root_path: str, subtree_path: str = ".") -> dict[str, str]:
    return {
        "root_path": str(root_path),
        "subtree_path": normalize_subtree_path(subtree_path),
        "path_format": "posix",
    }


def build_provenance(
    *,
    source_artifacts: list[dict[str, str]] | None = None,
    derived_from: str | None = None,
) -> dict[str, Any]:
    """Build optional provenance metadata without imposing a new artifact ID."""

    result: dict[str, Any] = {"source_artifacts": source_artifacts or []}
    if derived_from is not None:
        result["derived_from"] = derived_from
        result["derived_from_hash"] = trace.hash_json_artifact({"artifact": derived_from})
    return result
