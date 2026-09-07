#!/usr/bin/env python3
"""
I10 — Summary generator (T7 in the runtime pipeline).

Pure report generation from execution_log.json. Needs only that one
schema, so it was built independently of every other component.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib.validate import validate_file  # noqa: E402


def render_summary_md(log: dict) -> str:
    ops = log["operations"]
    by_status: dict[str, int] = {}
    for op in ops:
        by_status[op["status"]] = by_status.get(op["status"], 0) + 1

    lines = [
        f"# Run Summary — {log['run_id']}",
        "",
        f"- Proposal: `{log['proposal_id']}`",
        f"- Started: {log['started_at']}",
        f"- Completed: {log['completed_at']}",
        f"- Operations: {len(ops)} total "
        f"({', '.join(f'{v} {k}' for k, v in sorted(by_status.items()))})",
        f"- Undo script: `{log.get('undo_script_path') or 'n/a'}`",
        "",
        "## Operations",
        "",
        "| Action | From | To | Status | Error |",
        "|---|---|---|---|---|",
    ]
    for op in ops:
        lines.append(
            f"| {op['action']} | `{op['from_path']}` | `{op.get('to_path') or ''}` "
            f"| {op['status']} | {op.get('error') or ''} |"
        )
    return "\n".join(lines) + "\n"


def build_manifest(log: dict) -> dict:
    ops = log["operations"]
    by_status: dict[str, int] = {}
    for op in ops:
        by_status[op["status"]] = by_status.get(op["status"], 0) + 1
    return {
        "run_id": log["run_id"],
        "proposal_id": log["proposal_id"],
        "operation_count": len(ops),
        "by_status": by_status,
        "undo_script_path": log.get("undo_script_path"),
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="I10/T7: generate summary artifacts from an execution_log"
    )
    parser.add_argument("execution_log_path")
    parser.add_argument("--summary-out", default=None)
    parser.add_argument("--manifest-out", default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    log = validate_file(args.execution_log_path, "execution_log")
    summary_md = render_summary_md(log)
    manifest = build_manifest(log)

    if args.dry_run:
        print(summary_md)
        return

    summary_path = args.summary_out or f"runs/{log['run_id']}/summary.md"
    manifest_path = args.manifest_out or f"runs/{log['run_id']}/run_manifest.json"

    Path(summary_path).parent.mkdir(parents=True, exist_ok=True)
    with open(summary_path, "w", encoding="utf-8") as f:
        f.write(summary_md)
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)

    print(f"wrote {summary_path}")
    print(f"wrote {manifest_path}")


if __name__ == "__main__":
    main()
