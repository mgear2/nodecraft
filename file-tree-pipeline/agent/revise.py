#!/usr/bin/env python3
"""
I8 — Revise step (T5 in the runtime pipeline).

Invoked only when a rejected proposal's approval_decision.json carries
free-text `feedback`. Reuses classify.py's classify_all() rather than
duplicating logic — the only difference from a fresh classification pass is
that operator feedback is threaded into the backend call and the iteration
counter is bumped.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from agent.classify import HeuristicBackend, LLMBackend, classify_all  # noqa: E402
from lib.validate import validate_file, write_validated  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="I8/T5: revise classification using operator feedback")
    parser.add_argument("snapshot_path")
    parser.add_argument("approval_decision_path")
    parser.add_argument("--out", default=None)
    parser.add_argument("--backend", choices=["heuristic", "llm"], default="heuristic")
    parser.add_argument("--iteration", type=int, required=True, help="new iteration number")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    snapshot = validate_file(args.snapshot_path, "tree_snapshot")
    decision = validate_file(args.approval_decision_path, "approval_decision")

    if decision["decision"] != "reject" or not decision.get("feedback"):
        raise SystemExit(
            "revise.py should only run when approval_decision.decision == 'reject' "
            "and feedback is present. Orchestrator routing rule (see spec C.1) sends "
            "feedback-less rejections elsewhere."
        )

    backend = HeuristicBackend() if args.backend == "heuristic" else LLMBackend()
    result = classify_all(
        snapshot, backend, feedback=decision["feedback"], iteration=args.iteration
    )

    out_path = args.out or f"runs/{snapshot['run_id']}/classification.v{args.iteration}.json"
    if args.dry_run:
        print(json.dumps(result, indent=2))
    else:
        write_validated(result, "classification", out_path)
        print(f"wrote {out_path} (revised per feedback: {decision['feedback']!r})")


if __name__ == "__main__":
    main()
