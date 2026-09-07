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
from lib.metadata import build_scope, normalize_subtree_path, path_in_subtree  # noqa: E402
from lib.validate import validate_file, write_validated  # noqa: E402


def _select_nodes(snapshot: dict[str, Any], subtree_path: str) -> list[dict[str, Any]]:
    subtree = normalize_subtree_path(subtree_path)
    paths = {node["path"].replace("\\", "/") for node in snapshot["nodes"]}
    if subtree != "." and subtree not in paths:
        raise ValueError(f"subtree path not found in snapshot: {subtree}")
    return [
        node
        for node in snapshot["nodes"]
        if path_in_subtree(node["path"], subtree)
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

    subtree = normalize_subtree_path(subtree_path)
    selected = _select_nodes(snapshot, subtree)
    source_hash = trace.hash_json_artifact(snapshot)
    result = {
        "schema_version": snapshot.get("schema_version", trace.SCHEMA_VERSION),
        "run_id": snapshot["run_id"],
        "root_path": snapshot["root_path"],
        "generated_at": trace.now_iso(),
        "scope": build_scope(snapshot["root_path"], subtree),
        "provenance": {
            "source_artifacts": [
                {
                    "artifact_type": "tree_snapshot",
                    "run_id": snapshot["run_id"],
                    "content_hash": source_hash,
                    **({"path": source_artifact} if source_artifact else {}),
                }
            ]
        },
        "nodes": [dict(node) for node in selected],
    }
    return result


def build_subtree_classification(
    classification: dict[str, Any],
    subtree_snapshot: dict[str, Any],
    *,
    source_artifact: str | None = None,
) -> dict[str, Any]:
    selected_ids = {node["node_id"] for node in subtree_snapshot["nodes"]}
    source_hash = trace.hash_json_artifact(classification)
    result = {
        "schema_version": classification.get("schema_version", trace.SCHEMA_VERSION),
        "run_id": classification["run_id"],
        "input_hash": trace.hash_json_artifact(subtree_snapshot),
        "iteration": classification.get("iteration", 1),
        "scope": subtree_snapshot["scope"],
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
    nodes = snapshot["nodes"]
    classes = classification["classifications"]
    availability = Counter(node.get("availability", "available") for node in nodes)
    categories = Counter(item["category"] for item in classes)
    return {
        "schema_version": trace.SCHEMA_VERSION,
        "report_type": "subtree",
        "run_id": snapshot["run_id"],
        "scope": snapshot["scope"],
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
    subtree = subtree_snapshot["scope"]["subtree_path"].replace("/", "_")
    suffix = "root" if subtree == "." else subtree
    snapshot_out = args.snapshot_out or str(run_dir / f"tree_snapshot.{suffix}.json")
    classification_out = args.classification_out or str(
        run_dir / f"classification.{suffix}.json"
    )
    report_out = args.report_out or args.out or str(run_dir / f"subtree_report.{suffix}.json")

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
