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
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib import trace  # noqa: E402
from lib.validate import validate_file, write_validated  # noqa: E402


def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def _safe_path(mount: Path, value: str, *, allow_missing: bool = True) -> Path:
    """Resolve an artifact-relative path inside its explicit mount."""

    candidate = Path(value)
    if candidate.is_absolute() or not value or any(part == ".." for part in candidate.parts):
        raise ValueError(f"path must be relative and contained within root: {value!r}")
    mount_root = mount.resolve()
    resolved = (mount_root / candidate).resolve(strict=not allow_missing)
    if resolved != mount_root and mount_root not in resolved.parents:
        raise ValueError(f"path escapes artifact mount: {value!r}")
    return mount_root / candidate


def _validate_change(mount: Path, change: dict) -> None:
    if Path(change["from_path"]).as_posix() == ".":
        raise ValueError("operations on the mounted root are not supported")
    _safe_path(mount, change["from_path"], allow_missing=True)
    if change["action"] in ("move", "rename") and not change.get("to_path"):
        raise ValueError(f"{change['action']} requires a to_path")
    if change.get("to_path"):
        _safe_path(mount, change["to_path"], allow_missing=True)


def _trash_path(mount: Path, run_id: str, from_path: str) -> Path:
    """Resolve an implicit archive/delete destination through mount containment."""
    return _safe_path(mount, str(Path(".trash") / run_id / from_path))


def execute_operation(
    mount: Path,
    change: dict,
    run_id: str,
    permanent_delete: bool,
) -> dict:
    action = change["action"]
    op = {
        "node_id": change["node_id"],
        "action": action,
        "from_path": change["from_path"],
        "to_path": change.get("to_path"),
        "status": "success",
        "error": None,
    }
    provenance = change.get("provenance", {}).get("source_artifacts", [])
    if provenance and provenance[0].get("chunk_id"):
        op["chunk_id"] = provenance[0]["chunk_id"]

    try:
        _validate_change(mount, change)
        from_abs = _safe_path(mount, change["from_path"])
        if not from_abs.exists() and not from_abs.is_symlink():
            op["status"] = "skipped"
            op["error"] = "source path no longer exists"
            return op

        if action in ("move", "rename"):
            to_abs = _safe_path(mount, change["to_path"])
            _ensure_parent(to_abs)
            shutil.move(str(from_abs), str(to_abs))

        elif action == "archive":
            to_abs = (
                _safe_path(mount, change["to_path"])
                if change.get("to_path")
                else _trash_path(mount, run_id, change["from_path"])
            )
            _ensure_parent(to_abs)
            shutil.move(str(from_abs), str(to_abs))
            op["to_path"] = (
                str(to_abs.relative_to(mount)) if to_abs.is_relative_to(mount) else str(to_abs)
            )

        elif action == "delete":
            if permanent_delete:
                if from_abs.is_dir() and not from_abs.is_symlink():
                    shutil.rmtree(from_abs)
                else:
                    from_abs.unlink()
                op["to_path"] = None
            else:
                to_abs = _trash_path(mount, run_id, change["from_path"])
                _ensure_parent(to_abs)
                shutil.move(str(from_abs), str(to_abs))
                op["to_path"] = str(to_abs.relative_to(mount))

    except (OSError, ValueError, TypeError) as e:
        op["status"] = "failed"
        op["error"] = str(e)

    return op


def generate_undo_script(operations: list[dict], mount: Path) -> str:
    """Best-effort inverse: moves/renames/archives are reversible by moving
    back; permanent deletes are not reversible and are called out."""
    undo_operations = [
        op
        for op in reversed(operations)
        if op["status"] == "success"
        and (
            op["action"] in ("move", "rename", "archive")
            and op.get("to_path")
            or op["action"] == "delete"
            and op.get("to_path")
        )
    ]
    irreversible = [
        op["from_path"]
        for op in operations
        if op["status"] == "success" and op["action"] == "delete" and not op.get("to_path")
    ]
    return f"""#!/usr/bin/env python3
\"\"\"Auto-generated undo script. Review before running.\"\"\"

import shutil
import json
from pathlib import Path

MOUNT = Path({str(mount)!r})
OPERATIONS = json.loads({json.dumps(json.dumps(undo_operations))})
IRREVERSIBLE = json.loads({json.dumps(json.dumps(irreversible))})


def resolve(path: str) -> Path:
    candidate = Path(path)
    if candidate.is_absolute() or not path or any(part == ".." for part in candidate.parts):
        raise ValueError(f"undo path is not relative: {{path!r}}")
    mount_root = MOUNT.resolve()
    resolved = (mount_root / candidate).resolve()
    if resolved != mount_root and mount_root not in resolved.parents:
        raise ValueError(f"undo path escapes artifact mount: {{path!r}}")
    return mount_root / candidate


for operation in OPERATIONS:
    source = resolve(operation["to_path"])
    destination = resolve(operation["from_path"])
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(destination))

for path in IRREVERSIBLE:
    print(f"WARNING: {{path}} was permanently deleted and cannot be restored.")
"""


def execute_proposal(
    mount_path: str,
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
    expected_hash = approval.get("proposal_content_hash")
    if expected_hash != trace.hash_json_artifact(proposal):
        raise ValueError(
            "Executor refuses to run: approval_decision.proposal_content_hash "
            "does not match the proposal"
        )

    # ``mount_path`` is explicit authority to execute.  Proposal scope and
    # origin are provenance only; they must never redirect filesystem writes.
    mount = Path(mount_path).resolve()
    if not mount.is_dir():
        raise ValueError(f"artifact mount must be an existing directory: {mount}")
    for change in proposal["changes"]:
        _validate_change(mount, change)
    source_paths = {
        _safe_path(mount, change["from_path"]).resolve() for change in proposal["changes"]
    }
    seen_destinations: set[str] = set()
    destination_paths: list[tuple[Path, str]] = []
    for change in proposal["changes"]:
        destination = change.get("to_path")
        if (change["action"] == "archive" and not destination) or (
            change["action"] == "delete" and not permanent_delete
        ):
            destination_path = _trash_path(mount, proposal["run_id"], change["from_path"])
            destination = str(destination_path.relative_to(mount))
        if destination:
            resolved = _safe_path(mount, destination).resolve()
            key = str(resolved).casefold()
            if key in seen_destinations:
                raise ValueError(f"duplicate operation destination: {destination!r}")
            seen_destinations.add(key)
            if resolved in source_paths:
                raise ValueError(f"refusing to overwrite an operation source: {destination!r}")
            if resolved.exists():
                raise ValueError(f"refusing to overwrite existing destination: {destination!r}")
            for previous, previous_value in destination_paths:
                if (
                    resolved == previous
                    or resolved in previous.parents
                    or previous in resolved.parents
                ):
                    raise ValueError(
                        "overlapping operation destinations: "
                        f"{previous_value!r} and {destination!r}"
                    )
            destination_paths.append((resolved, destination))
    started = trace.now_iso()

    operations = [
        execute_operation(mount, change, proposal["run_id"], permanent_delete)
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
    if "scope" in proposal:
        log["scope"] = proposal["scope"]
    if "origin" in proposal:
        log["origin"] = proposal["origin"]
    log["mount_path"] = str(mount)
    undo_script = generate_undo_script(operations, mount)
    return log, undo_script


def main() -> None:
    parser = argparse.ArgumentParser(description="I9/T6: execute an approved proposal")
    parser.add_argument("mount_path", help="filesystem directory mounted at this artifact's root")
    parser.add_argument("proposal_path")
    parser.add_argument("approval_decision_path")
    parser.add_argument("--out", default=None, help="execution_log.json output path")
    parser.add_argument("--undo-out", default=None, help="undo.py output path")
    parser.add_argument(
        "--permanent-delete",
        action="store_true",
        help="irreversibly remove delete operations; archives always remain reversible",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    proposal = validate_file(args.proposal_path, "proposal")
    approval = validate_file(args.approval_decision_path, "approval_decision")

    if args.dry_run:
        print(f"[dry-run] would execute {len(proposal['changes'])} operations:")
        for c in proposal["changes"]:
            print(f"  {c['action']}: {c['from_path']} -> {c.get('to_path')}")
        return

    log, undo_script = execute_proposal(args.mount_path, proposal, approval, args.permanent_delete)

    out_path = args.out or f"runs/{proposal['run_id']}/execution_log.json"
    undo_path = args.undo_out or f"runs/{proposal['run_id']}/undo.py"

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
