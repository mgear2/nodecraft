#!/usr/bin/env python3
"""
I9 — Executor (T6 in the runtime pipeline).

The ONLY script in the pipeline permitted to perform destructive filesystem
operations, and only after a persisted `approve` decision referencing the
exact proposal_id it is executing (FR6, "Safety" NFR). Generates an undo
script per operation for reversibility (NFR "Reversibility").

Deletes default to archiving into a `.trash/<run_id>/` location rather than
permanent removal, per A.4's reversibility requirement, unless
--permanent-delete is explicitly passed.
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib import trace  # noqa: E402
from lib.validate import validate_file, write_validated  # noqa: E402


def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def execute_operation(root: Path, change: dict, trash_dir: Path, permanent_delete: bool) -> dict:
    from_abs = root / change["from_path"]
    action = change["action"]
    op = {
        "node_id": change["node_id"],
        "action": action,
        "from_path": change["from_path"],
        "to_path": change.get("to_path"),
        "status": "success",
        "error": None,
    }

    try:
        if not from_abs.exists() and not from_abs.is_symlink():
            op["status"] = "skipped"
            op["error"] = "source path no longer exists"
            return op

        if action in ("move", "rename"):
            to_abs = root / change["to_path"]
            _ensure_parent(to_abs)
            shutil.move(str(from_abs), str(to_abs))

        elif action == "archive":
            to_abs = root / change["to_path"] if change.get("to_path") else trash_dir / change["from_path"]
            _ensure_parent(to_abs)
            shutil.move(str(from_abs), str(to_abs))
            op["to_path"] = str(to_abs.relative_to(root)) if to_abs.is_relative_to(root) else str(to_abs)

        elif action == "delete":
            if permanent_delete:
                if from_abs.is_dir() and not from_abs.is_symlink():
                    shutil.rmtree(from_abs)
                else:
                    from_abs.unlink()
                op["to_path"] = None
            else:
                to_abs = trash_dir / change["from_path"]
                _ensure_parent(to_abs)
                shutil.move(str(from_abs), str(to_abs))
                op["to_path"] = str(to_abs)

    except OSError as e:
        op["status"] = "failed"
        op["error"] = str(e)

    return op


def generate_undo_script(operations: list[dict], root: Path) -> str:
    """Best-effort inverse: moves/renames/archives are reversible by moving
    back; permanent deletes are not reversible and are called out."""
    lines = ["#!/bin/sh", "set -e", "# Auto-generated undo script. Review before running."]
    for op in reversed(operations):
        if op["status"] != "success":
            continue
        if op["action"] in ("move", "rename", "archive") and op.get("to_path"):
            to_abs = root / op["to_path"] if not Path(op["to_path"]).is_absolute() else Path(op["to_path"])
            from_abs = root / op["from_path"]
            lines.append(f'mkdir -p "{from_abs.parent}"')
            lines.append(f'mv "{to_abs}" "{from_abs}"')
        elif op["action"] == "delete" and op.get("to_path"):
            to_abs = Path(op["to_path"])
            from_abs = root / op["from_path"]
            lines.append(f'mkdir -p "{from_abs.parent}"')
            lines.append(f'mv "{to_abs}" "{from_abs}"')
        elif op["action"] == "delete" and not op.get("to_path"):
            lines.append(f'# IRREVERSIBLE: {op["from_path"]} was permanently deleted')
    return "\n".join(lines) + "\n"


def execute_proposal(
    root_path: str,
    proposal: dict,
    approval: dict,
    permanent_delete: bool = False,
) -> tuple[dict, str]:
    if approval["decision"] != "approve":
        raise ValueError("Executor refuses to run: approval_decision.decision != 'approve'")
    if approval["proposal_id"] != proposal["proposal_id"]:
        raise ValueError(
            "Executor refuses to run: approval_decision.proposal_id does not match "
            "the proposal being executed (safety check per NFR 'Safety')"
        )

    root = Path(root_path).resolve()
    trash_dir = root / ".trash" / proposal["run_id"]
    started = trace.now_iso()

    operations = [
        execute_operation(root, change, trash_dir, permanent_delete)
        for change in proposal["changes"]
    ]

    log = {
        "schema_version": trace.SCHEMA_VERSION,
        "run_id": proposal["run_id"],
        "proposal_id": proposal["proposal_id"],
        "started_at": started,
        "completed_at": trace.now_iso(),
        "operations": operations,
        "undo_script_path": None,  # filled in by caller once written to disk
    }
    undo_script = generate_undo_script(operations, root)
    return log, undo_script


def main() -> None:
    parser = argparse.ArgumentParser(description="I9/T6: execute an approved proposal")
    parser.add_argument("root_path")
    parser.add_argument("proposal_path")
    parser.add_argument("approval_decision_path")
    parser.add_argument("--out", default=None, help="execution_log.json output path")
    parser.add_argument("--undo-out", default=None, help="undo.sh output path")
    parser.add_argument("--permanent-delete", action="store_true",
                         help="skip trash archival; irreversibly delete (default: archive to .trash/)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    proposal = validate_file(args.proposal_path, "proposal")
    approval = validate_file(args.approval_decision_path, "approval_decision")

    if args.dry_run:
        print(f"[dry-run] would execute {len(proposal['changes'])} operations:")
        for c in proposal["changes"]:
            print(f"  {c['action']}: {c['from_path']} -> {c.get('to_path')}")
        return

    log, undo_script = execute_proposal(args.root_path, proposal, approval, args.permanent_delete)

    out_path = args.out or f"runs/{proposal['run_id']}/execution_log.json"
    undo_path = args.undo_out or f"runs/{proposal['run_id']}/undo.sh"

    Path(undo_path).parent.mkdir(parents=True, exist_ok=True)
    with open(undo_path, "w", encoding="utf-8") as f:
        f.write(undo_script)
    Path(undo_path).chmod(0o755)

    log["undo_script_path"] = undo_path
    write_validated(log, "execution_log", out_path)

    failed = sum(1 for op in log["operations"] if op["status"] == "failed")
    print(f"wrote {out_path}; {len(log['operations'])} ops, {failed} failed")
    print(f"wrote {undo_path}")


if __name__ == "__main__":
    main()
