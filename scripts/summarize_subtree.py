#!/usr/bin/env python3
"""Derive an immutable, schema-valid report for a snapshot subtree."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib import trace  # noqa: E402
from lib.metadata import (  # noqa: E402
    build_scope,
    compose_subtree_path,
    normalize_subtree_path,
    path_in_subtree,
    relative_to_scope,
    scope_equal,
    validate_snapshot_classification_lineage,
)
from lib.validate import validate_file, write_validated  # noqa: E402


def _select_nodes(snapshot: dict[str, Any], subtree_path: str) -> list[dict[str, Any]]:
    source_scope = snapshot.get("scope", {}).get("subtree_path", ".")
    requested = normalize_subtree_path(subtree_path)
    # Node paths in canonical subtree artifacts are relative to the artifact,
    # while older artifacts may retain the source scope prefix.
    # Current portable artifacts already contain paths relative to their
    # declared scope. Legacy artifacts retain the scope prefix. A canonical
    # root marker makes the distinction unambiguous for nested derivations.
    node_paths = [normalize_subtree_path(node["path"]) for node in snapshot["nodes"]]
    is_canonical = source_scope == "." or "." in node_paths
    canonical_nodes = [
        (node, path if is_canonical else relative_to_scope(path, source_scope))
        for node, path in zip(snapshot["nodes"], node_paths, strict=True)
    ]
    paths = {path for _, path in canonical_nodes}
    subtree = requested
    if subtree != "." and subtree not in paths:
        raise ValueError(f"subtree path not found in snapshot: {subtree}")
    return [
        {**node, "path": path} for node, path in canonical_nodes if path_in_subtree(path, subtree)
    ]


def build_subtree_snapshot(
    snapshot: dict[str, Any],
    subtree_path: str,
    *,
    source_artifact: str | None = None,
) -> dict[str, Any]:
    """Return only the selected node and its descendants.

    The input mapping is never modified, and node dictionaries are copied so
    callers cannot accidentally mutate the source artifact.
    """

    source_scope = snapshot.get("scope", {}).get("subtree_path", ".")
    requested = normalize_subtree_path(subtree_path)
    # Inputs are addressed relative to an already-scoped tree.  Also accept a
    # fully composed path for callers carrying paths from the original tree.
    selection_subtree = relative_to_scope(requested, source_scope)
    selected = _select_nodes(snapshot, selection_subtree)
    # Rebase selected nodes so the derived artifact is a canonical tree.
    rebased = []
    for node in selected:
        path = node["path"]
        if selection_subtree == ".":
            relative_path = path
        elif path == selection_subtree:
            relative_path = "."
        else:
            relative_path = path[len(selection_subtree) + 1 :]
        # ``path`` is local to the portable artifact.  Preserve a stable
        # source location independently so nested derivations and importers
        # never need to infer identity from a rebased path.
        source_path = node.get("source_path") or compose_subtree_path(source_scope, path)
        rebased.append({**node, "path": relative_path, "source_path": source_path})
    source_hash = trace.hash_json_artifact(snapshot)
    composed_scope = compose_subtree_path(source_scope, requested)
    result = {
        "schema_version": snapshot.get("schema_version", trace.SCHEMA_VERSION),
        "run_id": snapshot["run_id"],
        "root_path": snapshot["root_path"],
        "generated_at": trace.now_iso(),
        "scope": build_scope(snapshot["root_path"], composed_scope),
        "origin": build_scope(snapshot["root_path"], composed_scope),
        "provenance": {
            "source_scope": snapshot.get("scope", build_scope(snapshot["root_path"], ".")),
            "source_artifacts": [
                {
                    "artifact_type": "tree_snapshot",
                    "run_id": snapshot["run_id"],
                    "content_hash": source_hash,
                    **({"path": source_artifact} if source_artifact else {}),
                }
            ],
        },
        "nodes": rebased,
    }
    return result


def build_subtree_classification(
    classification: dict[str, Any],
    subtree_snapshot: dict[str, Any],
    *,
    source_artifact: str | None = None,
) -> dict[str, Any]:
    if classification["run_id"] != subtree_snapshot["run_id"]:
        raise ValueError("snapshot and classification run_id do not match")
    expected_scope = subtree_snapshot.get("scope")
    source_hashes = {
        artifact.get("content_hash")
        for artifact in subtree_snapshot.get("provenance", {}).get("source_artifacts", [])
        if artifact.get("artifact_type") == "tree_snapshot"
    }
    current_snapshot_hash = trace.hash_json_artifact(subtree_snapshot)
    if classification.get("input_hash") not in {current_snapshot_hash, *source_hashes}:
        raise ValueError("classification input_hash does not match snapshot lineage")
    if "scope" in classification and not scope_equal(classification.get("scope"), expected_scope):
        # A full-snapshot classification is a valid source for a derived
        # subtree, but a different scoped classification is not.
        source_scope = subtree_snapshot.get("provenance", {}).get("source_scope")
        if source_scope is None or not scope_equal(classification.get("scope"), source_scope):
            raise ValueError("snapshot and classification scope do not match")
    selected_ids = {node["node_id"] for node in subtree_snapshot["nodes"]}
    source_hash = trace.hash_json_artifact(classification)
    result = {
        "schema_version": classification.get("schema_version", trace.SCHEMA_VERSION),
        "run_id": classification["run_id"],
        "input_hash": trace.hash_json_artifact(subtree_snapshot),
        "iteration": classification.get("iteration", 1),
        "scope": subtree_snapshot["scope"],
        "origin": subtree_snapshot.get("origin", subtree_snapshot["scope"]),
        "provenance": {
            "source_artifacts": [
                {
                    "artifact_type": "classification",
                    "run_id": classification["run_id"],
                    "content_hash": source_hash,
                    **({"path": source_artifact} if source_artifact else {}),
                },
                {
                    "artifact_type": "tree_snapshot",
                    "run_id": subtree_snapshot["run_id"],
                    "content_hash": trace.hash_json_artifact(subtree_snapshot),
                },
            ]
        },
        "classifications": [
            dict(item)
            for item in classification["classifications"]
            if item["node_id"] in selected_ids
        ],
    }
    return result


def build_subtree_report(
    snapshot: dict[str, Any], classification: dict[str, Any]
) -> dict[str, Any]:
    validate_snapshot_classification_lineage(snapshot, classification)
    nodes = snapshot["nodes"]
    classes = classification["classifications"]
    availability = Counter(node.get("availability", "available") for node in nodes)
    categories = Counter(item["category"] for item in classes)
    return {
        "schema_version": trace.SCHEMA_VERSION,
        "report_type": "subtree",
        "run_id": snapshot["run_id"],
        "scope": snapshot["scope"],
        "origin": snapshot.get("origin", snapshot["scope"]),
        "provenance": {
            "source_artifacts": [
                {
                    "artifact_type": "tree_snapshot",
                    "run_id": snapshot["run_id"],
                    "content_hash": trace.hash_json_artifact(snapshot),
                },
                {
                    "artifact_type": "classification",
                    "run_id": classification["run_id"],
                    "content_hash": trace.hash_json_artifact(classification),
                },
            ]
        },
        "node_count": len(nodes),
        "file_count": sum(node["type"] == "file" for node in nodes),
        "directory_count": sum(node["type"] == "directory" for node in nodes),
        "classification_count": len(classes),
        "availability": dict(sorted(availability.items())),
        "categories": dict(sorted(categories.items())),
    }


def derive_subtree(
    snapshot: dict[str, Any],
    classification: dict[str, Any],
    subtree_path: str,
    *,
    snapshot_source: str | None = None,
    classification_source: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    validate_snapshot_classification_lineage(snapshot, classification)
    subtree_snapshot = build_subtree_snapshot(
        snapshot, subtree_path, source_artifact=snapshot_source
    )
    subtree_classification = build_subtree_classification(
        classification, subtree_snapshot, source_artifact=classification_source
    )
    return (
        subtree_snapshot,
        subtree_classification,
        build_subtree_report(subtree_snapshot, subtree_classification),
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="derive immutable snapshot/classification/report artifacts for a subtree"
    )
    parser.add_argument("snapshot_path")
    parser.add_argument("classification_path")
    parser.add_argument("subtree_path")
    parser.add_argument("--out", default=None, help="report output path")
    parser.add_argument("--report-out", default=None)
    parser.add_argument("--snapshot-out", default=None)
    parser.add_argument("--classification-out", default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    snapshot = validate_file(args.snapshot_path, "tree_snapshot")
    classification = validate_file(args.classification_path, "classification")
    subtree_snapshot, subtree_classification, report = derive_subtree(
        snapshot,
        classification,
        args.subtree_path,
        snapshot_source=args.snapshot_path,
        classification_source=args.classification_path,
    )

    run_dir = Path(args.snapshot_path).parent
    scope_path = subtree_snapshot["scope"]["subtree_path"]
    suffix = trace.hash_bytes(scope_path.encode("utf-8"))[len("sha256:") : len("sha256:") + 16]
    snapshot_out = args.snapshot_out or str(run_dir / f"tree_snapshot.subtree-{suffix}.json")
    classification_out = args.classification_out or str(
        run_dir / f"classification.subtree-{suffix}.json"
    )
    report_out = (
        args.report_out or args.out or str(run_dir / f"subtree_report.subtree-{suffix}.json")
    )

    output_paths = [Path(snapshot_out), Path(classification_out), Path(report_out)]
    source_paths = {Path(args.snapshot_path).resolve(), Path(args.classification_path).resolve()}
    normalized_outputs = [path.resolve() for path in output_paths]
    if len(set(normalized_outputs)) != len(normalized_outputs):
        raise ValueError("snapshot, classification, and report outputs must be distinct")
    if any(path in source_paths for path in normalized_outputs):
        raise ValueError("derived output must not overwrite a source artifact")
    if any(path.exists() for path in normalized_outputs):
        raise ValueError("derived output must not overwrite an existing artifact")

    if args.dry_run:
        print(json.dumps(report, indent=2))
        print(
            f"\n[dry-run] {report['node_count']} nodes selected; no artifacts written.",
            file=sys.stderr,
        )
        return

    write_validated(subtree_snapshot, "tree_snapshot", snapshot_out)
    write_validated(subtree_classification, "classification", classification_out)
    write_validated(report, "subtree_report", report_out)
    print(f"wrote {snapshot_out} ({len(subtree_snapshot['nodes'])} nodes)")
    print(
        f"wrote {classification_out} "
        f"({len(subtree_classification['classifications'])} classifications)"
    )
    print(f"wrote {report_out}")


if __name__ == "__main__":
    main()
