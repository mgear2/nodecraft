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
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib import trace  # noqa: E402
from lib.progress import Progress  # noqa: E402
from lib.validate import validate_file, write_validated  # noqa: E402

VALID_CATEGORIES = {"source", "config", "docs", "cache", "duplicate", "build_artifact", "unknown"}
VALID_ACTIONS = {"keep", "move", "rename", "delete", "archive", "review"}
DEFAULT_BATCH_SIZE = 32
MAX_BATCH_SIZE = 128
OLLAMA_BATCH_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "required": [
            "node_id",
            "purpose",
            "category",
            "recommended_action",
            "target_path",
            "confidence",
            "rationale",
        ],
        "properties": {
            "node_id": {"type": "string"},
            "purpose": {"type": "string"},
            "category": {"type": "string", "enum": sorted(VALID_CATEGORIES)},
            "recommended_action": {"type": "string", "enum": sorted(VALID_ACTIONS)},
            "target_path": {"type": ["string", "null"]},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "rationale": {"type": "string"},
        },
    },
}

# Common near-miss values produced by small local models (qwen, llama, etc.)
# mapped to the schema-valid enums.  Kept at module level so both LLMBackend
# and OllamaBackend can normalize responses before shape validation.
CATEGORY_ALIASES = {
    "source_code": "source",
    "sourcecode": "source",
    "code": "source",
    "config_file": "config",
    "configuration": "config",
    "documentation": "docs",
    "doc": "docs",
    "readme": "docs",
    "cache_file": "cache",
    "cached": "cache",
    "build": "build_artifact",
    "build_output": "build_artifact",
    "build_artifact": "build_artifact",
    "duplicate_file": "duplicate",
    "duplicated": "duplicate",
    "other": "unknown",
    "misc": "unknown",
}
ACTION_ALIASES = {
    "retain": "keep",
    "preserve": "keep",
    "move_to": "move",
    "relocate": "move",
    "delete_file": "delete",
    "remove": "delete",
    "archive_file": "archive",
    "flag": "review",
    "inspect": "review",
}


def _extract_json(text: str) -> str:
    """Extract the first JSON object or array from a model response.

    Small local models frequently wrap JSON in markdown code fences or
    prepend/append prose. This finds the first decodable object or array and
    returns its exact JSON substring.
    """
    decoder = json.JSONDecoder()
    candidates = [position for position, char in enumerate(text) if char in "[{]"]
    for position in candidates:
        try:
            _, end = decoder.raw_decode(text[position:])
        except json.JSONDecodeError:
            continue
        return text[position : position + end]
    raise ValueError(f"no JSON object found (or array) in response: {text[:200]!r}")


def _normalize(data: Any) -> dict:
    """Normalize common near-miss values from small local models."""
    if not isinstance(data, dict):
        raise ValueError("classifier response must be a JSON object")
    category = data.get("category")
    if isinstance(category, str):
        normalized = CATEGORY_ALIASES.get(category.strip().lower())
        if normalized:
            data["category"] = normalized
    action = data.get("recommended_action")
    if isinstance(action, str):
        normalized = ACTION_ALIASES.get(action.strip().lower())
        if normalized:
            data["recommended_action"] = normalized
    confidence = data.get("confidence")
    if isinstance(confidence, str):
        try:
            data["confidence"] = float(confidence)
        except ValueError:
            pass
    return data


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

    def classify_nodes(
        self, nodes: list[dict], all_nodes: list[dict], feedback: str | None = None
    ) -> list[dict]:
        return [
            {"node_id": node["node_id"], **self.classify_node(node, all_nodes, feedback)}
            for node in nodes
        ]

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
                data = json.loads(_extract_json(text))
                _normalize(data)
                _validate_shape(data)
                return data
            except (json.JSONDecodeError, ValueError) as e:
                last_error = e
                prompt = (
                    prompt + f"\n\nYour previous response was invalid ({e}). "
                    "Return ONLY a single valid JSON object, no prose, no markdown fences."
                )
        raise RuntimeError(f"LLM classifier failed schema validation after retries: {last_error}")

    def classify_nodes(
        self, nodes: list[dict], all_nodes: list[dict], feedback: str | None = None
    ) -> list[dict]:
        del all_nodes
        import anthropic  # imported lazily so HeuristicBackend has no hard dependency

        client = anthropic.Anthropic()
        prompt = self._build_batch_prompt(nodes, feedback)
        last_error = None
        for attempt in range(self.max_retries + 1):
            resp = client.messages.create(
                model=self.model,
                max_tokens=max(1200, len(nodes) * 160),
                messages=[{"role": "user", "content": prompt}],
            )
            text = "".join(b.text for b in resp.content if b.type == "text")
            try:
                data = json.loads(_extract_json(text))
                return _validate_batch_shape(data, nodes)
            except (json.JSONDecodeError, ValueError) as error:
                last_error = error
                prompt += (
                    f"\n\nYour previous response was invalid ({error}). "
                    "Return ONLY the JSON array with every requested node_id."
                )
        raise RuntimeError(f"LLM batch classifier failed after retries: {last_error}")

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

    @staticmethod
    def _build_batch_prompt(nodes: list[dict], feedback: str | None) -> str:
        base = (
            "Classify every filesystem node below. Respond with ONLY a JSON array "
            "containing exactly one object per node. Each object must include the "
            "input node_id plus purpose, category, recommended_action, target_path, "
            "confidence, and rationale. Preserve every node_id exactly. "
            f"Categories: {sorted(VALID_CATEGORIES)}. Actions: {sorted(VALID_ACTIONS)}.\n\n"
            f"Nodes: {json.dumps(nodes)}"
        )
        if feedback:
            base += f"\n\nOperator feedback to incorporate: {feedback}"
        return base


class OllamaBackend:
    """Local Ollama backend using its HTTP generate API.

    Ollama runs independently (for example in Docker Desktop/WSL) and is
    contacted without adding a Python client dependency to the project.
    """

    def __init__(
        self,
        model: str = "llama3.1",
        base_url: str = "http://localhost:11434",
        max_retries: int = 2,
        timeout: float = 120.0,
    ):
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.max_retries = max_retries
        self.timeout = timeout

    def classify_node(self, node: dict, all_nodes: list[dict], feedback: str | None = None) -> dict:
        del all_nodes
        prompt = LLMBackend._build_prompt(node, feedback)
        last_error: Exception | None = None
        for _ in range(self.max_retries + 1):
            payload = json.dumps(
                {
                    "model": self.model,
                    "prompt": prompt,
                    "stream": False,
                    "format": "json",
                }
            ).encode("utf-8")
            try:
                request = Request(
                    f"{self.base_url}/api/generate",
                    data=payload,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urlopen(request, timeout=self.timeout) as response:
                    response_data = json.loads(response.read().decode("utf-8"))
                data = json.loads(_extract_json(response_data["response"]))
                _normalize(data)
                _validate_shape(data)
                return data
            except (KeyError, json.JSONDecodeError, TypeError, ValueError, OSError) as error:
                last_error = error
                prompt += f"\n\nYour previous response was invalid ({error}). Return only JSON."
        raise RuntimeError(
            f"Ollama classifier failed schema validation after retries: {last_error}"
        )

    def classify_nodes(
        self, nodes: list[dict], all_nodes: list[dict], feedback: str | None = None
    ) -> list[dict]:
        del all_nodes
        prompt = LLMBackend._build_batch_prompt(nodes, feedback)
        last_error: Exception | None = None
        for _ in range(self.max_retries + 1):
            payload = json.dumps(
                {
                    "model": self.model,
                    "prompt": prompt,
                    "stream": False,
                    "format": OLLAMA_BATCH_SCHEMA,
                    "options": {"num_predict": max(1200, len(nodes) * 160)},
                }
            ).encode("utf-8")
            try:
                request = Request(
                    f"{self.base_url}/api/generate",
                    data=payload,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urlopen(request, timeout=self.timeout) as response:
                    response_data = json.loads(response.read().decode("utf-8"))
                data = json.loads(_extract_json(response_data["response"]))
                data = _coerce_ollama_batch(data, nodes)
                return _validate_batch_shape(data, nodes)
            except (KeyError, json.JSONDecodeError, TypeError, ValueError, OSError) as error:
                last_error = error
                prompt += (
                    f"\n\nYour previous response was invalid ({error}). "
                    "Return only the requested JSON array."
                )
        raise RuntimeError(f"Ollama batch classifier failed after retries: {last_error}")


def build_backend(
    name: str,
    *,
    model: str | None = None,
    ollama_url: str = "http://localhost:11434",
) -> ClassifierBackend:
    if name == "heuristic":
        return HeuristicBackend()
    if name == "llm":
        return LLMBackend(model=model or "claude-sonnet-4-6")
    if name == "ollama":
        return OllamaBackend(model=model or "llama3.1", base_url=ollama_url)
    raise ValueError(f"unknown classifier backend: {name}")


def _validate_shape(data: Any) -> None:
    if not isinstance(data, dict):
        raise ValueError("classifier response must be a JSON object")
    if not isinstance(data.get("purpose"), str):
        raise ValueError("purpose must be a string")
    if data.get("category") not in VALID_CATEGORIES:
        raise ValueError(f"invalid category: {data.get('category')}")
    if data.get("recommended_action") not in VALID_ACTIONS:
        raise ValueError(f"invalid recommended_action: {data.get('recommended_action')}")
    confidence = data.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise ValueError("confidence must be numeric")
    if not 0 <= confidence <= 1:
        raise ValueError("confidence must be between 0 and 1")
    if data.get("target_path") is not None and not isinstance(data.get("target_path"), str):
        raise ValueError("target_path must be a string or null")
    if not isinstance(data.get("rationale"), str):
        raise ValueError("rationale must be a string")


def _validate_batch_shape(data: Any, nodes: list[dict]) -> list[dict]:
    if not isinstance(data, list):
        raise ValueError("classifier batch response must be a JSON array")
    expected_ids = [node["node_id"] for node in nodes]
    expected = set(expected_ids)
    if len(expected_ids) != len(expected):
        raise ValueError("input batch contains duplicate node_ids")
    by_id: dict[str, dict] = {}
    for item in data:
        if not isinstance(item, dict):
            raise ValueError("each batch classification must be an object")
        node_id = item.get("node_id")
        if not isinstance(node_id, str) or node_id not in expected:
            raise ValueError(f"batch response contains unexpected node_id: {node_id!r}")
        if node_id in by_id:
            raise ValueError(f"batch response duplicates node_id: {node_id!r}")
        normalized = _normalize(item)
        _validate_shape(normalized)
        by_id[node_id] = normalized
    missing = expected - by_id.keys()
    if missing:
        raise ValueError(f"batch response is missing node_ids: {sorted(missing)!r}")
    return [by_id[node_id] for node_id in expected_ids]


def _coerce_ollama_batch(data: Any, nodes: list[dict]) -> Any:
    """Accept a singleton object from models that ignore array formatting."""

    if isinstance(data, dict) and len(nodes) == 1:
        data = dict(data)
        data.setdefault("node_id", nodes[0]["node_id"])
        return [data]
    return data


def _classify_batch(
    backend: ClassifierBackend,
    nodes: list[dict],
    all_nodes: list[dict],
    feedback: str | None,
) -> list[dict]:
    batch_method = getattr(backend, "classify_nodes", None)
    if callable(batch_method):
        results = batch_method(nodes, all_nodes, feedback)
    else:
        results = [
            {"node_id": node["node_id"], **backend.classify_node(node, all_nodes, feedback)}
            for node in nodes
        ]
    if len(results) != len(nodes):
        raise ValueError("classifier batch returned the wrong number of results")
    return _validate_batch_shape(results, nodes)


def _classification_artifact(snapshot: dict, iteration: int, classifications: list[dict]) -> dict:
    result = {
        "schema_version": trace.SCHEMA_VERSION,
        "run_id": snapshot["run_id"],
        "input_hash": trace.hash_json_artifact(snapshot),
        "iteration": iteration,
        "classifications": classifications,
    }
    if "scope" in snapshot:
        result["scope"] = snapshot["scope"]
    if "origin" in snapshot:
        result["origin"] = snapshot["origin"]
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


def _classification_context_hash(
    backend: ClassifierBackend,
    feedback: str | None,
    iteration: int,
    batch_size: int,
) -> str:
    """Identify the inputs that must remain stable when resuming a checkpoint."""

    backend_type = type(backend)
    context = {
        "backend": f"{backend_type.__module__}.{backend_type.__qualname__}",
        "model": getattr(backend, "model", None),
        "base_url": getattr(backend, "base_url", None),
        "feedback": feedback,
        "iteration": iteration,
        "batch_size": batch_size,
    }
    return trace.hash_json_artifact(context)


def _set_classification_provenance(artifact: dict, entry: dict, context_hash: str) -> None:
    source = artifact["provenance"]["source_artifacts"][0]
    source["path"] = entry["path"]
    source["chunk_id"] = entry["chunk_id"]
    artifact["provenance"]["classification_context_hash"] = context_hash


def classify_all(
    snapshot: dict,
    backend: ClassifierBackend,
    feedback: str | None = None,
    iteration: int = 1,
    progress: Progress | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    initial_classifications: list[dict] | None = None,
    on_batch: Callable[[list[dict]], None] | None = None,
) -> dict:
    if not 1 <= batch_size <= MAX_BATCH_SIZE:
        raise ValueError(f"batch_size must be between 1 and {MAX_BATCH_SIZE}")
    nodes = snapshot["nodes"]
    node_by_id = {node["node_id"]: node for node in nodes}
    if len(node_by_id) != len(nodes):
        raise ValueError("snapshot contains duplicate node_ids")
    initial = initial_classifications or []
    initial_ids = [item.get("node_id") for item in initial]
    if len(initial_ids) != len(set(initial_ids)):
        raise ValueError("initial classifications contain duplicate node_ids")
    if any(node_id not in node_by_id for node_id in initial_ids):
        raise ValueError("initial classifications contain unknown node_ids")
    classifications_by_id = {item["node_id"]: item for item in initial}
    classifications = [
        classifications_by_id[node["node_id"]]
        for node in nodes
        if node["node_id"] in classifications_by_id
    ]
    remaining_nodes = [node for node in nodes if node["node_id"] not in classifications_by_id]
    items = (progress or Progress(quiet=True)).items("classifying", len(nodes))
    for _ in initial:
        items.update()
    for start in range(0, len(remaining_nodes), batch_size):
        batch = remaining_nodes[start : start + batch_size]
        results = _classify_batch(backend, batch, nodes, feedback)
        for node, result in zip(batch, results, strict=True):
            result = {key: value for key, value in result.items() if key != "node_id"}
            if "availability" in node:
                result["availability"] = node["availability"]
            classifications.append({"node_id": node["node_id"], **result})
            items.update()
        if on_batch:
            on_batch(list(classifications))
    items.finish()
    return _classification_artifact(snapshot, iteration, classifications)


def classify_manifest(
    manifest_path: str | Path,
    *,
    backend: ClassifierBackend,
    iteration: int = 1,
    feedback: str | None = None,
    output_dir: str | Path | None = None,
    progress: Progress | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> list[Path]:
    """Classify each manifest chunk independently.

    Only one bounded chunk and its classification are resident at a time.  The
    manifest is atomically updated after each chunk, so an interrupted pass
    leaves an accurate resumable status and never requires a full snapshot.
    """

    manifest_file = Path(manifest_path)
    if not 1 <= batch_size <= MAX_BATCH_SIZE:
        raise ValueError(f"batch_size must be between 1 and {MAX_BATCH_SIZE}")
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
            output = destination / f"{Path(entry['path']).stem}.classification.v{iteration}.json"
            partial = (
                destination / f"{Path(entry['path']).stem}.classification.v{iteration}.partial.json"
            )
            context_hash = _classification_context_hash(backend, feedback, iteration, batch_size)

            existing = entry.get("classification")
            if (
                existing
                and existing.get("status") == "completed"
                and existing.get("iteration") == iteration
            ):
                existing_output = chunk_root / existing["path"]
                if existing_output.exists() and trace.hash_json_artifact(
                    validate_file(existing_output, "classification")
                ) == existing.get("content_hash"):
                    outputs.append(existing_output)
                    partial.unlink(missing_ok=True)
                    status["completed_chunks"] += 1
                    status["updated_at"] = trace.now_iso()
                    write_validated(manifest, "chunk_manifest", manifest_file)
                    items.update()
                    continue

            # A process may have written the final artifact but stopped before
            # committing the manifest. Adopt it if its lineage and context are
            # complete; this closes the finalization crash window.
            if output.exists():
                orphan = validate_file(output, "classification")
                source = orphan.get("provenance", {}).get("source_artifacts", [{}])[0]
                if (
                    orphan.get("run_id") == chunk["run_id"]
                    and orphan.get("input_hash") == trace.hash_json_artifact(chunk)
                    and orphan.get("iteration") == iteration
                    and orphan.get("provenance", {}).get("classification_context_hash")
                    == context_hash
                    and source.get("path") == entry["path"]
                    and source.get("chunk_id") == entry["chunk_id"]
                ):
                    partial.unlink(missing_ok=True)
                    entry["classification"] = {
                        "status": "completed",
                        "path": output.relative_to(chunk_root).as_posix()
                        if output.is_relative_to(chunk_root)
                        else str(output.resolve()),
                        "content_hash": trace.hash_json_artifact(orphan),
                        "iteration": iteration,
                    }
                    status["completed_chunks"] += 1
                    status["updated_at"] = trace.now_iso()
                    write_validated(manifest, "chunk_manifest", manifest_file)
                    outputs.append(output)
                    items.update()
                    continue

            initial_classifications: list[dict] = []
            if partial.exists():
                partial_artifact = validate_file(partial, "classification")
                lineage_matches = not (
                    partial_artifact.get("run_id") != chunk["run_id"]
                    or partial_artifact.get("input_hash") != trace.hash_json_artifact(chunk)
                    or partial_artifact.get("iteration") != iteration
                    or partial_artifact.get("provenance", {}).get("classification_context_hash")
                    != context_hash
                )
                if lineage_matches:
                    initial_classifications = partial_artifact["classifications"]
                else:
                    # A checkpoint from another context cannot be safely
                    # combined with this pass. It is disposable progress,
                    # so restart this chunk from its canonical snapshot.
                    partial.unlink(missing_ok=True)

            def checkpoint(classifications: list[dict]) -> None:
                partial_artifact = _classification_artifact(chunk, iteration, classifications)
                _set_classification_provenance(partial_artifact, entry, context_hash)
                write_validated(partial_artifact, "classification", partial)

            result = classify_all(
                chunk,
                backend,
                feedback=feedback,
                iteration=iteration,
                progress=Progress(quiet=True),
                batch_size=batch_size,
                initial_classifications=initial_classifications,
                on_batch=checkpoint,
            )
            _set_classification_provenance(result, entry, context_hash)
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
            partial.unlink(missing_ok=True)
            outputs.append(output)
            items.update()
    except BaseException as error:
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
    parser.add_argument("--backend", choices=["heuristic", "llm", "ollama"], default="heuristic")
    parser.add_argument("--model", default=None)
    parser.add_argument("--ollama-url", default="http://localhost:11434")
    parser.add_argument(
        "--feedback", default=None, help="operator feedback for a revision pass (T5)"
    )
    parser.add_argument("--iteration", type=int, default=1)
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--quiet", action="store_true", help="suppress progress output")
    parser.add_argument(
        "--manifest",
        default=None,
        help="classify each chunk referenced by a chunk manifest instead of one snapshot",
    )
    args = parser.parse_args()

    if args.manifest:
        backend = build_backend(args.backend, model=args.model, ollama_url=args.ollama_url)
        outputs = classify_manifest(
            args.manifest,
            backend=backend,
            iteration=args.iteration,
            feedback=args.feedback,
            progress=Progress(args.quiet),
            batch_size=args.batch_size,
        )
        print(f"classified {len(outputs)} chunks")
        return

    if not args.snapshot_path:
        parser.error("snapshot_path is required unless --manifest is provided")

    snapshot = validate_file(args.snapshot_path, "tree_snapshot")
    backend = build_backend(args.backend, model=args.model, ollama_url=args.ollama_url)
    result = classify_all(
        snapshot,
        backend,
        feedback=args.feedback,
        iteration=args.iteration,
        progress=Progress(args.quiet),
        batch_size=args.batch_size,
    )

    out_path = args.out or f"runs/{snapshot['run_id']}/classification.v{args.iteration}.json"
    if args.dry_run:
        print(json.dumps(result, indent=2))
    else:
        write_validated(result, "classification", out_path)
        print(f"wrote {out_path} ({len(result['classifications'])} classifications)")


if __name__ == "__main__":
    main()
