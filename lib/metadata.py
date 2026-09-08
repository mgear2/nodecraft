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


def compose_subtree_path(parent: str, child: str) -> str:
    """Compose a requested path with an artifact's existing scope.

    Subtree artifacts are trees in their own right, so callers address a
    descendant relative to the artifact.  Accepting the already-composed
    form as well keeps the helper convenient for callers passing paths from a
    full snapshot.
    """

    base = normalize_subtree_path(parent)
    requested = normalize_subtree_path(child)
    if base == ".":
        return requested
    if requested == base or requested.startswith(base + "/"):
        return requested
    return normalize_subtree_path(posixpath.join(base, requested))


def relative_to_scope(path: str, scope_path: str) -> str:
    """Return a node path relative to ``scope_path``."""

    normalized = normalize_subtree_path(path)
    scope = normalize_subtree_path(scope_path)
    if scope == ".":
        return normalized
    if normalized == scope:
        return "."
    if normalized.startswith(scope + "/"):
        return normalized[len(scope) + 1 :]
    return normalized


def localize_path(path: str, scope_path: str) -> str:
    """Convert a source-relative path into an artifact-local path.

    Unlike :func:`relative_to_scope`, this rejects paths outside the selected
    scope instead of returning them unchanged.  That distinction matters for
    proposal targets: silently retaining an outside path would make a
    portable subtree proposal ambiguous.
    """

    normalized = normalize_subtree_path(path)
    scope = normalize_subtree_path(scope_path)
    if scope == ".":
        return normalized
    if normalized != scope and not normalized.startswith(scope + "/"):
        raise ValueError(f"path is outside selected scope: {path!r}")
    return relative_to_scope(normalized, scope)


def scope_equal(left: dict[str, Any] | None, right: dict[str, Any] | None) -> bool:
    """Compare scopes using canonical paths and roots."""

    if left is None or right is None:
        return left is right
    return (
        str(left.get("root_path")) == str(right.get("root_path"))
        and normalize_subtree_path(str(left.get("subtree_path", ".")))
        == normalize_subtree_path(str(right.get("subtree_path", ".")))
        and left.get("path_format", "posix") == right.get("path_format", "posix")
    )


def validate_snapshot_classification_lineage(
    snapshot: dict[str, Any],
    classification: dict[str, Any],
    *,
    require_input_hash: bool = True,
) -> None:
    """Reject classifications that were not produced for ``snapshot``."""

    if snapshot.get("run_id") != classification.get("run_id"):
        raise ValueError("snapshot and classification run_id do not match")
    if require_input_hash and classification.get("input_hash") != trace.hash_json_artifact(
        snapshot
    ):
        raise ValueError("classification input_hash does not match snapshot")
    if "scope" in snapshot or "scope" in classification:
        if not scope_equal(snapshot.get("scope"), classification.get("scope")):
            raise ValueError("snapshot and classification scope do not match")


def path_in_subtree(path: str, subtree_path: str) -> bool:
    subtree = normalize_subtree_path(subtree_path)
    normalized = path.replace("\\", "/")
    return subtree == "." or normalized == subtree or normalized.startswith(subtree + "/")


def build_scope(root_path: str, subtree_path: str = ".") -> dict[str, str]:
    """Describe an artifact's source location.

    ``root_path`` and ``subtree_path`` are provenance.  They are deliberately
    not an execution target: a consumer must explicitly mount an artifact
    before applying its artifact-relative paths to a filesystem.
    """

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
