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

    def set_global_duplicate_map(self, duplicate_map: dict[str, list[str]]) -> None:
        """Supply duplicate context collected across all streamed chunks."""
        self._hash_counts = duplicate_map

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
        if "availability" in node:
            result["availability"] = node["availability"]
        classifications.append({"node_id": node["node_id"], **result})
        items.update()
    items.finish()

    result = {
        "schema_version": trace.SCHEMA_VERSION,
        "run_id": snapshot["run_id"],
        "input_hash": trace.hash_json_artifact(snapshot),
        "iteration": iteration,
        "classifications": classifications,
    }
    if "scope" in snapshot:
        result["scope"] = snapshot["scope"]
    result["provenance"] = {
        "source_artifacts": [
            {
                "artifact_type": "tree_snapshot",
                "run_id": snapshot["run_id"],
                "content_hash": trace.hash_json_artifact(snapshot),
            }
        ]
    }
    return result


def classify_manifest(
    manifest_path: str | Path,
    *,
    backend: ClassifierBackend,
    iteration: int = 1,
    feedback: str | None = None,
    output_dir: str | Path | None = None,
    progress: Progress | None = None,
) -> list[Path]:
    """Classify each manifest chunk independently.

    Only one bounded chunk and its classification are resident at a time.  The
    manifest is atomically updated after each chunk, so an interrupted pass
    leaves an accurate resumable status and never requires a full snapshot.
    """

    manifest_file = Path(manifest_path)
    manifest = validate_file(manifest_file, "chunk_manifest")
    chunk_root = manifest_file.parent
    destination = Path(output_dir) if output_dir else chunk_root / "classifications"
    destination.mkdir(parents=True, exist_ok=True)
    entries = manifest["chunks"]
    status = manifest.setdefault("status", {})
    status.update(
        {
            "classification": "in_progress",
            "total_chunks": len(entries),
            "completed_chunks": 0,
            "failed_chunks": 0,
            "updated_at": trace.now_iso(),
        }
    )
    status.pop("error", None)
    write_validated(manifest, "chunk_manifest", manifest_file)

    reporter = progress or Progress(quiet=True)
    items = reporter.items("classifying chunks", len(entries))
    outputs: list[Path] = []
    try:
        duplicate_map: dict[str, list[str]] = {}
        for entry in entries:
            chunk = validate_file(chunk_root / entry["path"], "tree_snapshot")
            for node in chunk["nodes"]:
                content_hash = node.get("content_hash")
                if content_hash:
                    duplicate_map.setdefault(content_hash, []).append(node["node_id"])
        set_global_duplicate_map = getattr(backend, "set_global_duplicate_map", None)
        if callable(set_global_duplicate_map):
            set_global_duplicate_map(duplicate_map)

        for entry in entries:
            chunk_path = chunk_root / entry["path"]
            chunk = validate_file(chunk_path, "tree_snapshot")
            result = classify_all(
                chunk,
                backend,
                feedback=feedback,
                iteration=iteration,
                progress=Progress(quiet=True),
            )
            result["provenance"]["source_artifacts"][0]["path"] = entry["path"]
            result["provenance"]["source_artifacts"][0]["chunk_id"] = entry["chunk_id"]
            output = destination / f"{Path(entry['path']).stem}.classification.v{iteration}.json"
            write_validated(result, "classification", output)
            try:
                relative_output = output.relative_to(chunk_root).as_posix()
            except ValueError:
                # A caller may intentionally place classifications outside
                # the chunk directory; retain an explicit provenance path.
                relative_output = str(output.resolve())
            entry["classification"] = {
                "status": "completed",
                "path": relative_output,
                "content_hash": trace.hash_json_artifact(result),
                "iteration": iteration,
            }
            status["completed_chunks"] += 1
            status["updated_at"] = trace.now_iso()
            write_validated(manifest, "chunk_manifest", manifest_file)
            outputs.append(output)
            items.update()
    except Exception as error:
        status["classification"] = "failed"
        status["failed_chunks"] = len(entries) - status["completed_chunks"]
        status["error"] = str(error)
        status["updated_at"] = trace.now_iso()
        write_validated(manifest, "chunk_manifest", manifest_file)
        raise
    finally:
        items.finish()

    status["classification"] = "completed"
    status["updated_at"] = trace.now_iso()
    write_validated(manifest, "chunk_manifest", manifest_file)
    return outputs


classify_chunks = classify_manifest


def main() -> None:
    parser = argparse.ArgumentParser(description="I5/T2: classify nodes in a tree_snapshot")
    parser.add_argument("snapshot_path", nargs="?")
    parser.add_argument("--out", default=None)
    parser.add_argument("--backend", choices=["heuristic", "llm"], default="heuristic")
    parser.add_argument(
        "--feedback", default=None, help="operator feedback for a revision pass (T5)"
    )
    parser.add_argument("--iteration", type=int, default=1)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--quiet", action="store_true", help="suppress progress output")
    parser.add_argument(
        "--manifest",
        default=None,
        help="classify each chunk referenced by a chunk manifest instead of one snapshot",
    )
    args = parser.parse_args()

    if args.manifest:
        backend = HeuristicBackend() if args.backend == "heuristic" else LLMBackend()
        outputs = classify_manifest(
            args.manifest,
            backend=backend,
            iteration=args.iteration,
            feedback=args.feedback,
            progress=Progress(args.quiet),
        )
        print(f"classified {len(outputs)} chunks")
        return

    if not args.snapshot_path:
        parser.error("snapshot_path is required unless --manifest is provided")

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
