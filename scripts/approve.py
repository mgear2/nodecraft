#!/usr/bin/env python3
"""
I7 — Approval capture (T4 in the runtime pipeline).

Presents the proposal's diagram (from I6) to the operator and captures an
approval_decision.json. Supports two modes:

  - Interactive: prints the diagram, prompts for approve/reject + feedback.
  - Non-interactive (--decision/--feedback flags): for scripted/automated
    use and for the I18 end-to-end test, which cannot sit at a TTY prompt.

This is the first task that depends on I6's actual output format (the
rendered diagram text), not just the proposal schema.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib import trace  # noqa: E402
from lib.validate import validate_file, write_validated  # noqa: E402


def capture_decision_interactive(proposal: dict, decided_by: str) -> dict:
    print(proposal["diagram"])
    print()
    print(f"{len(proposal['changes'])} changes proposed (iteration {proposal['iteration']}).")
    answer = input("Approve? [y/N]: ").strip().lower()

    if answer == "y":
        decision = "approve"
        feedback = None
    else:
        decision = "reject"
        feedback = input("Feedback for revision: ").strip()

    return build_decision(proposal, decision, feedback, decided_by)


def build_decision(proposal: dict, decision: str, feedback: str | None, decided_by: str) -> dict:
    if decision == "reject" and not feedback:
        raise ValueError("feedback is required when decision == 'reject' (per schema)")
    return {
        "schema_version": trace.SCHEMA_VERSION,
        "proposal_id": proposal["proposal_id"],
        "proposal_content_hash": trace.hash_json_artifact(proposal),
        "change_count": len(proposal["changes"]),
        "decision": decision,
        "feedback": feedback,
        "decided_at": trace.now_iso(),
        "decided_by": decided_by,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="I7/T4: capture an approval decision for a proposal"
    )
    parser.add_argument("proposal_path")
    parser.add_argument("--out", default=None)
    parser.add_argument("--decided-by", default="operator")
    parser.add_argument(
        "--decision",
        choices=["approve", "reject"],
        default=None,
        help="non-interactive mode: skip the prompt",
    )
    parser.add_argument("--feedback", default=None)
    args = parser.parse_args()

    proposal = validate_file(args.proposal_path, "proposal")

    if args.decision:
        decision = build_decision(proposal, args.decision, args.feedback, args.decided_by)
    else:
        decision = capture_decision_interactive(proposal, args.decided_by)

    out_path = (
        args.out or f"runs/{proposal['run_id']}/approval_decision.v{proposal['iteration']}.json"
    )
    write_validated(decision, "approval_decision", out_path)
    print(f"wrote {out_path}: decision={decision['decision']}")


if __name__ == "__main__":
    main()
