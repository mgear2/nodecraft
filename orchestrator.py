#!/usr/bin/env python3
"""
I16 — Orchestrator.

Sequences T1-T7 from the runtime pipeline (spec section B), enforcing:
  - schema validation between every step (fail-fast, per NFR "Schema-
    validated I/O")
  - the hard approval gate before any destructive action (FR4)
  - the T4 -> T5 -> T3 revision loop, bounded by max_iterations (FR5),
    modeled as new node instances per iteration rather than a literal
    cycle (spec B.3) so the run stays a resumable, append-only DAG
  - routing: T5 (agent) only runs when approval_decision has feedback,
    per spec's original (simpler) reject/feedback model

This is the one task that needed every other component's real interface
finalized (I4, I5/I8, I6, I7, I9, I10) rather than just a schema, since it
calls their functions directly.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from agent.classify import DEFAULT_BATCH_SIZE, build_backend, classify_manifest  # noqa: E402
from lib import trace  # noqa: E402
from lib.metadata import normalize_subtree_path  # noqa: E402
from lib.progress import Progress  # noqa: E402
from lib.validate import write_validated  # noqa: E402
from scripts.execute import execute_proposal  # noqa: E402
from scripts.render_proposal import render_manifest_proposal  # noqa: E402
from scripts.scan import (  # noqa: E402
    DEFAULT_MAX_BYTES,
    DEFAULT_MAX_HASH_SIZE,
    DEFAULT_MAX_NODES,
    scan_tree_to_chunks,
)
from scripts.summarize import build_manifest, render_summary_md  # noqa: E402


class MaxIterationsExceeded(Exception):
    pass


def run_pipeline(
    root_path: str,
    approval_callback,
    backend_name: str = "heuristic",
    model: str | None = None,
    ollama_url: str = "http://localhost:11434",
    max_iterations: int = 5,
    runs_dir: str = "runs",
    permanent_delete: bool = False,
    progress: Progress | None = None,
    hash_mode: str = "full",
    max_hash_size: int = DEFAULT_MAX_HASH_SIZE,
    cache_path: str | None = None,
    cache_enabled: bool = True,
    skip_cloud_only: bool = False,
    subtree_path: str = ".",
    max_nodes: int = DEFAULT_MAX_NODES,
    max_bytes: int = DEFAULT_MAX_BYTES,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> dict:
    """Runs T1 -> T2 -> (T3 -> T4 -> [T5 -> T3]*)+ -> T6 -> T7.

    `approval_callback(proposal: dict) -> approval_decision: dict` is
    injected so this function has no I/O dependency on a terminal/CLI —
    the same orchestrator drives both interactive use (scripts/approve.py)
    and the I18 end-to-end test (a scripted callback).

    Returns a dict summarizing the run: run_id, iterations, final proposal,
    execution log, and paths to all written artifacts.
    """
    backend = build_backend(backend_name, model=model, ollama_url=ollama_url)
    progress = progress or Progress(quiet=True)

    run_id = trace.new_run_id()
    rdir = trace.run_dir(runs_dir, run_id)
    artifacts: dict[str, str] = {}

    # T1: scan directly to independently classifiable chunks.  Chunks remain
    # on disk and are consumed one at a time by T2; no giant snapshot is built.
    progress.stage("scanning")
    chunk_dir = rdir / "chunks"
    scan_tree_to_chunks(
        root_path,
        run_id,
        chunk_dir,
        progress=progress,
        hash_mode=hash_mode,
        max_hash_size=max_hash_size,
        cache_path=cache_path,
        cache_enabled=cache_enabled,
        skip_cloud_only=skip_cloud_only,
        max_nodes=max_nodes,
        max_bytes=max_bytes,
    )
    artifacts["chunk_manifest"] = str(chunk_dir / "manifest.json")

    # T2
    progress.stage("classifying")
    classify_manifest(
        chunk_dir / "manifest.json",
        backend=backend,
        iteration=1,
        progress=progress,
        batch_size=batch_size,
    )
    artifacts["classification.v1.chunks"] = str(chunk_dir / "classifications")

    iteration = 1
    proposal = None
    approval = None

    while True:
        if iteration > max_iterations:
            raise MaxIterationsExceeded(
                f"Exceeded max_iterations={max_iterations} without approval "
                f"(run_id={run_id}). See runs/{run_id}/ for full history."
            )

        # T3
        proposal = render_manifest_proposal(
            chunk_dir / "manifest.json",
            iteration=iteration,
            subtree_path=subtree_path,
        )
        progress.stage(f"rendering proposal (iteration {iteration})")
        proposal_path = rdir / f"proposal.v{iteration}.json"
        write_validated(proposal, "proposal", proposal_path)
        artifacts[f"proposal.v{iteration}"] = str(proposal_path)

        # T4 (human gate — via injected callback)
        approval = approval_callback(proposal)
        approval.setdefault("proposal_content_hash", trace.hash_json_artifact(proposal))
        approval.setdefault("change_count", len(proposal["changes"]))
        progress.stage("waiting for approval")
        approval_path = rdir / f"approval_decision.v{iteration}.json"
        write_validated(approval, "approval_decision", approval_path)
        artifacts[f"approval_decision.v{iteration}"] = str(approval_path)

        if approval["decision"] == "approve":
            break

        # reject -> T5, new iteration (new node instance, not a back-edge;
        # see spec B.3)
        iteration += 1
        classify_manifest(
            chunk_dir / "manifest.json",
            backend=backend,
            feedback=approval.get("feedback"),
            iteration=iteration,
            progress=progress,
            batch_size=batch_size,
        )
        artifacts[f"classification.v{iteration}.chunks"] = str(chunk_dir / "classifications")

    # T6
    progress.stage("executing approved changes")
    mount_path = Path(root_path).resolve() / normalize_subtree_path(subtree_path)
    log, undo_script = execute_proposal(str(mount_path), proposal, approval, permanent_delete)
    undo_path = rdir / "undo.py"
    undo_path.write_text(undo_script)
    undo_path.chmod(0o755)
    log["undo_script_path"] = str(undo_path)
    log["proposal_content_hash"] = trace.hash_json_artifact(proposal)
    log["change_count"] = len(proposal["changes"])
    log_path = rdir / "execution_log.json"
    write_validated(log, "execution_log", log_path)
    artifacts["execution_log"] = str(log_path)
    artifacts["undo_script"] = str(undo_path)

    # T7
    progress.stage("writing summary")
    summary_md = render_summary_md(log)
    manifest = build_manifest(log)
    summary_path = rdir / "summary.md"
    manifest_path = rdir / "run_manifest.json"
    summary_path.write_text(summary_md)
    manifest_path.write_text(json.dumps(manifest, indent=2))
    artifacts["summary"] = str(summary_path)
    artifacts["run_manifest"] = str(manifest_path)

    return {
        "run_id": run_id,
        "iterations": iteration,
        "final_decision": approval["decision"],
        "artifacts": artifacts,
    }


def interactive_approval_callback(proposal: dict) -> dict:
    from scripts.approve import capture_decision_interactive

    return capture_decision_interactive(proposal, decided_by="operator")


def main() -> None:
    parser = argparse.ArgumentParser(description="I16: run the full file-tree pipeline")
    parser.add_argument("root_path")
    parser.add_argument("--backend", choices=["heuristic", "llm", "ollama"], default="heuristic")
    parser.add_argument("--model", default=None)
    parser.add_argument("--ollama-url", default="http://localhost:11434")
    parser.add_argument("--max-iterations", type=int, default=5)
    parser.add_argument("--runs-dir", default="runs")
    parser.add_argument("--permanent-delete", action="store_true")
    parser.add_argument("--quiet", action="store_true", help="suppress progress output")
    parser.add_argument("--hash-mode", choices=["full", "conditional", "none"], default="full")
    from scripts.scan import parse_size

    parser.add_argument("--max-hash-size", type=parse_size, default=DEFAULT_MAX_HASH_SIZE)
    parser.add_argument("--max-bytes", type=parse_size, default=DEFAULT_MAX_BYTES)
    parser.add_argument("--cache-path", default=None)
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument(
        "--skip-cloud-only",
        action="store_true",
        help="omit Windows cloud-only placeholder files",
    )
    parser.add_argument(
        "--subtree",
        default=".",
        help="run the pipeline as a canonical tree rooted at this relative subtree",
    )
    parser.add_argument("--max-nodes", type=int, default=DEFAULT_MAX_NODES)
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument(
        "--auto-approve",
        action="store_true",
        help="skip the interactive prompt and approve iteration 1 "
        "(useful for scripted/CI runs; use with care)",
    )
    args = parser.parse_args()

    if args.auto_approve:

        def callback(proposal):
            return {
                "schema_version": trace.SCHEMA_VERSION,
                "proposal_id": proposal["proposal_id"],
                "decision": "approve",
                "feedback": None,
                "decided_at": trace.now_iso(),
                "decided_by": "auto-approve-flag",
            }
    else:
        callback = interactive_approval_callback

    result = run_pipeline(
        args.root_path,
        callback,
        backend_name=args.backend,
        model=args.model,
        ollama_url=args.ollama_url,
        max_iterations=args.max_iterations,
        runs_dir=args.runs_dir,
        permanent_delete=args.permanent_delete,
        progress=Progress(args.quiet),
        hash_mode=args.hash_mode,
        max_hash_size=args.max_hash_size,
        cache_path=args.cache_path,
        cache_enabled=not args.no_cache,
        skip_cloud_only=args.skip_cloud_only,
        subtree_path=args.subtree,
        max_nodes=args.max_nodes,
        max_bytes=args.max_bytes,
        batch_size=args.batch_size,
    )

    print(f"\nrun_id={result['run_id']}")
    print(f"iterations={result['iterations']}")
    print(f"final_decision={result['final_decision']}")
    for name, path in result["artifacts"].items():
        print(f"  {name}: {path}")


if __name__ == "__main__":
    main()
