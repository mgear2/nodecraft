#!/usr/bin/env python3
"""
I5 — Classifier (T2 in the runtime pipeline).

Agent-assisted step: the only step in the pipeline that requires judgment
about purpose/intent. Built against a pluggable `ClassifierBackend`
interface so it can be developed and unit-tested against golden fixtures
without a live LLM call, then swapped to a real backend with no change to
the surrounding script. This mirrors the DAG note that I5 only depends on
schemas (I1), not on I4's actual output.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Protocol

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib import trace  # noqa: E402
from lib.progress import Progress  # noqa: E402
from lib.validate import validate_file, write_validated  # noqa: E402

VALID_CATEGORIES = {"source", "config", "docs", "cache", "duplicate", "build_artifact", "unknown"}
VALID_ACTIONS = {"keep", "move", "rename", "delete", "archive", "review"}


class ClassifierBackend(Protocol):
    def classify_node(self, node: dict, all_nodes: list[dict], feedback: str | None = None) -> dict:
        """Return a dict with purpose/category/recommended_action/target_path/
        confidence/rationale for a single node."""
        ...


class HeuristicBackend:
    """Deterministic rule-based backend. Used as the default so the whole
    pipeline can be built, tested, and demoed end-to-end without any network
    access or API key. A real LLM backend (see LLMBackend below) implements
    the same interface and can be swapped in via --backend llm."""

    BUILD_EXTS = {".o", ".pyc", ".class", ".obj"}
    CACHE_DIRS = {"__pycache__", ".cache", "build", "dist", "target"}
    BACKUP_PATTERN = re.compile(r"\.(bak|old|orig)$")
    LOG_PATTERN = re.compile(r"\.log(\.\d{4})?$")

    def __init__(self):
        self._hash_counts: dict[str, list[str]] | None = None

    def _duplicate_map(self, all_nodes: list[dict]) -> dict[str, list[str]]:
        if self._hash_counts is None:
            m: dict[str, list[str]] = {}
            for n in all_nodes:
                h = n.get("content_hash")
                if h:
                    m.setdefault(h, []).append(n["node_id"])
            self._hash_counts = m
        return self._hash_counts

    def classify_node(self, node: dict, all_nodes: list[dict], feedback: str | None = None) -> dict:
        path = node["path"]
        ext = (node.get("extension") or "").lower()
        parts = Path(path).parts

        if node["type"] == "directory":
            if any(p in self.CACHE_DIRS for p in parts):
                return self._result(
                    "build/cache directory",
                    "cache",
                    "delete",
                    None,
                    0.7,
                    "Directory name matches known cache/build output pattern",
                )
            return self._result(
                "directory", "unknown", "keep", None, 0.5, "No strong signal; default to keep"
            )

        if any(p in self.CACHE_DIRS for p in parts) or ext in self.BUILD_EXTS:
            return self._result(
                "build artifact",
                "build_artifact",
                "delete",
                None,
                0.85,
                "Extension/location matches known build-output pattern",
            )

        if self.BACKUP_PATTERN.search(path):
            return self._result(
                "backup file",
                "cache",
                "archive",
                f"_archive/{path}",
                0.75,
                "Filename suffix indicates a manual backup copy",
            )

        if self.LOG_PATTERN.search(path):
            return self._result(
                "old log file",
                "cache",
                "archive",
                f"_archive/{path}",
                0.7,
                "Dated/old log file; likely safe to archive",
            )

        dup_map = self._duplicate_map(all_nodes)
        h = node.get("content_hash")
        if h and len(dup_map.get(h, [])) > 1 and dup_map[h][0] != node["node_id"]:
            return self._result(
                "duplicate content",
                "duplicate",
                "review",
                None,
                0.6,
                "Content hash matches another file; flagged for human review before deletion",
            )

        if ext in {".py", ".js", ".ts", ".go", ".rs", ".java", ".c", ".cpp"}:
            return self._result(
                "source code", "source", "keep", None, 0.9, "Recognized source-code extension"
            )

        if ext in {".md", ".rst", ".txt"} or "docs" in parts:
            return self._result(
                "documentation",
                "docs",
                "keep",
                None,
                0.8,
                "Recognized documentation location/extension",
            )

        if ext in {".yml", ".yaml", ".toml", ".ini", ".json", ".cfg"}:
            return self._result(
                "configuration", "config", "keep", None, 0.75, "Recognized config file extension"
            )

        return self._result(
            "unclassified",
            "unknown",
            "review",
            None,
            0.4,
            "No rule matched confidently; flagged for human review",
        )

    @staticmethod
    def _result(purpose, category, action, target, confidence, rationale) -> dict:
        return {
            "purpose": purpose,
            "category": category,
            "recommended_action": action,
            "target_path": target,
            "confidence": confidence,
            "rationale": rationale,
        }


class LLMBackend:
    """Real backend: calls the Anthropic API, constrained to schema-valid
    JSON output, with retry-on-invalid-output per FR2/A.4. Kept separate
    from HeuristicBackend so swapping backends is a one-line CLI flag, not
    a code change to classify.py itself."""

    def __init__(self, model: str = "claude-sonnet-4-6", max_retries: int = 2):
        self.model = model
        self.max_retries = max_retries

    def classify_node(self, node: dict, all_nodes: list[dict], feedback: str | None = None) -> dict:
        import anthropic  # imported lazily so HeuristicBackend has no hard dependency

        client = anthropic.Anthropic()
        prompt = self._build_prompt(node, feedback)

        last_error = None
        for attempt in range(self.max_retries + 1):
            resp = client.messages.create(
                model=self.model,
                max_tokens=300,
                messages=[{"role": "user", "content": prompt}],
            )
            text = "".join(b.text for b in resp.content if b.type == "text")
            try:
                data = json.loads(text)
                _validate_shape(data)
                return data
            except (json.JSONDecodeError, ValueError) as e:
                last_error = e
                prompt = (
                    prompt + f"\n\nYour previous response was invalid ({e}). "
                    "Return ONLY a single valid JSON object, no prose, no markdown fences."
                )
        raise RuntimeError(f"LLM classifier failed schema validation after retries: {last_error}")

    @staticmethod
    def _build_prompt(node: dict, feedback: str | None) -> str:
        base = (
            "Classify this filesystem node. Respond with ONLY a JSON object "
            "with keys: purpose (string), category (one of "
            f"{sorted(VALID_CATEGORIES)}), recommended_action (one of "
            f"{sorted(VALID_ACTIONS)}), target_path (string or null), "
            "confidence (0-1 float), rationale (short string).\n\n"
            f"Node: {json.dumps(node)}"
        )
        if feedback:
            base += f"\n\nOperator feedback to incorporate: {feedback}"
        return base


def _validate_shape(data: dict) -> None:
    if data.get("category") not in VALID_CATEGORIES:
        raise ValueError(f"invalid category: {data.get('category')}")
    if data.get("recommended_action") not in VALID_ACTIONS:
        raise ValueError(f"invalid recommended_action: {data.get('recommended_action')}")
    if not isinstance(data.get("confidence"), (int, float)):
        raise ValueError("confidence must be numeric")


def classify_all(
    snapshot: dict,
    backend: ClassifierBackend,
    feedback: str | None = None,
    iteration: int = 1,
    progress: Progress | None = None,
) -> dict:
    nodes = snapshot["nodes"]
    classifications = []
    items = (progress or Progress(quiet=True)).items("classifying", len(nodes))
    for node in nodes:
        result = backend.classify_node(node, nodes, feedback)
        classifications.append({"node_id": node["node_id"], **result})
        items.update()
    items.finish()

    return {
        "schema_version": trace.SCHEMA_VERSION,
        "run_id": snapshot["run_id"],
        "input_hash": trace.hash_json_artifact(snapshot),
        "iteration": iteration,
        "classifications": classifications,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="I5/T2: classify nodes in a tree_snapshot")
    parser.add_argument("snapshot_path")
    parser.add_argument("--out", default=None)
    parser.add_argument("--backend", choices=["heuristic", "llm"], default="heuristic")
    parser.add_argument(
        "--feedback", default=None, help="operator feedback for a revision pass (T5)"
    )
    parser.add_argument("--iteration", type=int, default=1)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--quiet", action="store_true", help="suppress progress output")
    args = parser.parse_args()

    snapshot = validate_file(args.snapshot_path, "tree_snapshot")
    backend: ClassifierBackend = HeuristicBackend() if args.backend == "heuristic" else LLMBackend()
    result = classify_all(
        snapshot, backend, feedback=args.feedback, iteration=args.iteration,
        progress=Progress(args.quiet),
    )

    out_path = args.out or f"runs/{snapshot['run_id']}/classification.v{args.iteration}.json"
    if args.dry_run:
        print(json.dumps(result, indent=2))
    else:
        write_validated(result, "classification", out_path)
        print(f"wrote {out_path} ({len(result['classifications'])} classifications)")


if __name__ == "__main__":
    main()
