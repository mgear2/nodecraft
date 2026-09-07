#!/usr/bin/env python3
"""
I6 — Proposal Renderer (T3 in the runtime pipeline).

Pure deterministic transform: tree_snapshot.json + classification.json ->
proposal.json (including a human-readable, inline-annotated diagram).
Depends only on the schemas (I1), not on the Scanner or Classifier scripts
themselves — built and unit-tested against fixture JSON.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib import trace  # noqa: E402
from lib.validate import validate_file, write_validated  # noqa: E402

ACTIONABLE = {"move", "rename", "delete", "archive"}


def render_diagram(nodes_by_id: dict[str, dict], classifications: list[dict]) -> str:
    """Render an indented text tree with an inline comment per node showing
    its recommended action + rationale. This is deliberately plain text
    (not just JSON) so it doubles as the operator-editable artifact from
    FR3/A.5.2's capture-mechanism recommendation."""
    class_by_id = {c["node_id"]: c for c in classifications}
    lines: list[str] = []

    for node_id, node in sorted(nodes_by_id.items(), key=lambda kv: kv[1]["path"]):
        c = class_by_id.get(node_id)
        depth = node["path"].count("/")
        indent = "  " * depth
        name = Path(node["path"]).name
        marker = "/" if node["type"] == "directory" else ""

        if c is None:
            lines.append(f"{indent}{name}{marker}  # (unclassified)")
            continue

        action = c["recommended_action"]
        rationale = c["rationale"]
        if action in ACTIONABLE:
            target = f" -> {c['target_path']}" if c.get("target_path") else ""
            lines.append(f"{indent}{name}{marker}  # [{action.upper()}{target}] {rationale}")
        else:
            lines.append(f"{indent}{name}{marker}  # [{action}] {rationale}")

    return "\n".join(lines)


def build_changes(
    classifications: list[dict], nodes_by_id: dict[str, dict]
) -> list[dict[str, Any]]:
    changes: list[dict[str, Any]] = []
    for c in classifications:
        if c["recommended_action"] in ACTIONABLE:
            node = nodes_by_id.get(c["node_id"])
            if node is None:
                continue  # classification references a node not in this snapshot; skip defensively
            changes.append(
                {
                    "node_id": c["node_id"],
                    "action": c["recommended_action"],
                    "from_path": node["path"],
                    "to_path": c.get("target_path"),
                }
            )
    changes.sort(key=lambda ch: ch["from_path"])
    return changes


def render_proposal(snapshot: dict, classification: dict, iteration: int | None = None) -> dict:
    nodes_by_id = {n["node_id"]: n for n in snapshot["nodes"]}
    classifications = classification["classifications"]

    diagram = render_diagram(nodes_by_id, classifications)
    changes = build_changes(classifications, nodes_by_id)

    result = {
        "schema_version": trace.SCHEMA_VERSION,
        "proposal_id": trace.new_proposal_id(),
        "run_id": snapshot["run_id"],
        "iteration": iteration if iteration is not None else classification.get("iteration", 1),
        "based_on_input_hash": trace.hash_json_artifact(classification),
        "diagram": diagram,
        "changes": changes,
    }
    if "scope" in snapshot:
        result["scope"] = snapshot["scope"]
    result["provenance"] = {
        "source_artifacts": [
            {
                "artifact_type": "classification",
                "run_id": classification.get("run_id", snapshot["run_id"]),
                "content_hash": trace.hash_json_artifact(classification),
            }
        ]
    }
    return result


def main() -> None:
    parser = argparse.ArgumentParser(
        description="I6/T3: render a proposal from snapshot + classification"
    )
    parser.add_argument("snapshot_path")
    parser.add_argument("classification_path")
    parser.add_argument("--out", default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    snapshot = validate_file(args.snapshot_path, "tree_snapshot")
    classification = validate_file(args.classification_path, "classification")
    proposal = render_proposal(snapshot, classification)

    out_path = args.out or f"runs/{snapshot['run_id']}/proposal.v{proposal['iteration']}.json"

    if args.dry_run:
        print(proposal["diagram"])
        print(
            f"\n[dry-run] {len(proposal['changes'])} actionable changes; not written.",
            file=sys.stderr,
        )
    else:
        write_validated(proposal, "proposal", out_path)
        print(f"wrote {out_path} ({len(proposal['changes'])} actionable changes)")
        print()
        print(proposal["diagram"])


if __name__ == "__main__":
    main()
