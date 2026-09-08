#!/usr/bin/env python3
"""
I17 — CLI entrypoint.

Thin dispatcher over the orchestrator and individual scripts, so the tool
is installable/runnable as a single `filetree` command with subcommands
mirroring the pipeline stages, or `filetree run` for the full orchestrated
pipeline.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        print(
            "Usage: filetree "
            "<run|scan|classify|classify-chunks|render|approve|execute|"
            "summarize|derive-subtree|summarize-subtree> [args...]"
        )
        sys.exit(1)

    command = sys.argv[1]
    rest = sys.argv[2:]
    sys.argv = [f"filetree {command}"] + rest

    if command == "run":
        import orchestrator

        orchestrator.main()
    elif command == "scan":
        import scripts.scan as scan

        scan.main()
    elif command == "classify":
        import agent.classify as classify

        classify.main()
    elif command == "classify-chunks":
        import agent.classify as classify

        if rest == ["--help"] or rest == ["-h"]:
            sys.argv = ["filetree classify", "--help"]
            classify.main()
            return
        if not rest:
            print("Usage: filetree classify-chunks MANIFEST_PATH [options]")
            sys.exit(1)
        sys.argv = ["filetree classify", "--manifest", rest[0]] + rest[1:]
        classify.main()
    elif command == "revise":
        import agent.revise as revise

        revise.main()
    elif command == "render":
        import scripts.render_proposal as render_proposal

        render_proposal.main()
    elif command == "approve":
        import scripts.approve as approve

        approve.main()
    elif command == "execute":
        import scripts.execute as execute

        execute.main()
    elif command == "summarize":
        import scripts.summarize as summarize

        summarize.main()
    elif command in {"derive-subtree", "summarize-subtree"}:
        import scripts.derive_subtree as derive_subtree

        derive_subtree.main()
    else:
        print(f"Unknown command: {command}")
        sys.exit(1)


if __name__ == "__main__":
    main()
