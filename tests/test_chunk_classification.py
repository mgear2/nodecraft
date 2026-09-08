import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.classify import (  # noqa: E402
    HeuristicBackend,
    OllamaBackend,
    _extract_json,
    _normalize,
    _validate_shape,
    classify_all,
    classify_manifest,
)
from lib.validate import validate_file  # noqa: E402
from scripts.render_proposal import render_manifest_proposal  # noqa: E402
from scripts.scan import scan_tree_to_chunks  # noqa: E402


def test_extract_json_strips_markdown_fences():
    text = '```json\n{"category": "docs", "confidence": 0.9}\n```'
    assert json.loads(_extract_json(text)) == {"category": "docs", "confidence": 0.9}


def test_extract_json_strips_prose():
    text = 'Here is the classification:\n{"category": "docs", "confidence": 0.9}\nHope that helps!'
    assert json.loads(_extract_json(text)) == {"category": "docs", "confidence": 0.9}


def test_extract_json_raises_when_no_object():
    import pytest

    with pytest.raises(ValueError, match="no JSON object"):
        _extract_json("no json here")


def test_normalize_maps_near_miss_enums():
    data = {
        "category": "source_code",
        "recommended_action": "delete_file",
        "confidence": "0.85",
    }
    _normalize(data)
    assert data["category"] == "source"
    assert data["recommended_action"] == "delete"
    assert data["confidence"] == 0.85


def test_normalize_leaves_valid_values_unchanged():
    data = {
        "category": "docs",
        "recommended_action": "keep",
        "confidence": 0.9,
    }
    _normalize(data)
    assert data == {
        "category": "docs",
        "recommended_action": "keep",
        "confidence": 0.9,
    }


def test_validate_shape_rejects_non_object_and_bad_confidence():
    import pytest

    with pytest.raises(ValueError, match="JSON object"):
        _validate_shape([])
    with pytest.raises(ValueError, match="between 0 and 1"):
        _validate_shape(
            {
                "purpose": "source",
                "category": "source",
                "recommended_action": "keep",
                "confidence": 1.1,
                "target_path": None,
                "rationale": "reason",
            }
        )


def test_ollama_backend_parses_json_response(monkeypatch):
    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps(
                {
                    "response": json.dumps(
                        {
                            "purpose": "documentation",
                            "category": "docs",
                            "recommended_action": "keep",
                            "target_path": None,
                            "confidence": 0.9,
                            "rationale": "markdown file",
                        }
                    )
                }
            ).encode()

    monkeypatch.setattr("agent.classify.urlopen", lambda request, timeout: _Response())
    result = OllamaBackend().classify_node({"path": "README.md", "type": "file"}, [])
    assert result["category"] == "docs"


def test_ollama_backend_handles_fenced_and_prose_wrapped_response(monkeypatch):
    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            payload = {
                "purpose": "source code",
                "category": "source_code",
                "recommended_action": "keep",
                "target_path": None,
                "confidence": "0.95",
                "rationale": "python file",
            }
            wrapped = f"Here you go:\n```json\n{json.dumps(payload)}\n```\nDone!"
            return json.dumps({"response": wrapped}).encode()

    monkeypatch.setattr("agent.classify.urlopen", lambda request, timeout: _Response())
    result = OllamaBackend().classify_node({"path": "main.py", "type": "file"}, [])
    assert result["category"] == "source"
    assert result["recommended_action"] == "keep"
    assert result["confidence"] == 0.95


def test_ollama_backend_parses_node_keyed_batch(monkeypatch):
    nodes = [
        {"node_id": "n1", "path": "README.md", "type": "file"},
        {"node_id": "n2", "path": "main.py", "type": "file"},
    ]

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            payload = [
                {
                    "node_id": "n2",
                    "purpose": "source code",
                    "category": "source",
                    "recommended_action": "keep",
                    "target_path": None,
                    "confidence": 0.9,
                    "rationale": "python file",
                },
                {
                    "node_id": "n1",
                    "purpose": "documentation",
                    "category": "docs",
                    "recommended_action": "keep",
                    "target_path": None,
                    "confidence": 0.9,
                    "rationale": "markdown file",
                },
            ]
            return json.dumps({"response": json.dumps(payload)}).encode()

    monkeypatch.setattr("agent.classify.urlopen", lambda request, timeout: _Response())
    result = OllamaBackend().classify_nodes(nodes, nodes)
    assert [item["node_id"] for item in result] == ["n1", "n2"]


def test_ollama_backend_coerces_singleton_object_response(monkeypatch):
    node = {"node_id": "n1", "path": "README.md", "type": "file"}

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            item = {
                "purpose": "documentation",
                "category": "docs",
                "recommended_action": "keep",
                "target_path": None,
                "confidence": 0.9,
                "rationale": "markdown file",
            }
            return json.dumps({"response": json.dumps(item)}).encode()

    monkeypatch.setattr("agent.classify.urlopen", lambda request, timeout: _Response())
    result = OllamaBackend().classify_nodes([node], [node])
    assert result[0]["node_id"] == "n1"
    assert result[0]["category"] == "docs"


def test_ollama_backend_retries_transport_errors(monkeypatch):
    node = {"node_id": "n1", "path": "README.md", "type": "file"}
    calls = 0

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            item = {
                "node_id": "n1",
                "purpose": "documentation",
                "category": "docs",
                "recommended_action": "keep",
                "target_path": None,
                "confidence": 0.9,
                "rationale": "markdown file",
            }
            return json.dumps({"response": json.dumps([item])}).encode()

    def _urlopen(request, timeout):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("temporary connection failure")
        return _Response()

    monkeypatch.setattr("agent.classify.urlopen", _urlopen)
    result = OllamaBackend(max_retries=1).classify_nodes([node], [node])
    assert result[0]["category"] == "docs"
    assert calls == 2


def test_batch_validation_rejects_duplicate_node_ids():
    from agent.classify import _validate_batch_shape

    node = {"node_id": "n1", "path": "a", "type": "file"}
    item = {
        "node_id": "n1",
        "purpose": "source",
        "category": "source",
        "recommended_action": "keep",
        "target_path": None,
        "confidence": 0.9,
        "rationale": "source",
    }
    with pytest.raises(ValueError, match="duplicates"):
        _validate_batch_shape([item, dict(item)], [node])


def test_batch_validation_rejects_missing_node_ids():
    from agent.classify import _validate_batch_shape

    item = {
        "node_id": "n1",
        "purpose": "source",
        "category": "source",
        "recommended_action": "keep",
        "target_path": None,
        "confidence": 0.9,
        "rationale": "source",
    }
    nodes = [
        {"node_id": "n1", "path": "a", "type": "file"},
        {"node_id": "n2", "path": "b", "type": "file"},
    ]
    with pytest.raises(ValueError, match="missing node_ids"):
        _validate_batch_shape([item], nodes)


def test_batch_validation_rejects_duplicate_input_node_ids():
    from agent.classify import _validate_batch_shape

    node = {"node_id": "n1", "path": "a", "type": "file"}
    item = {
        "node_id": "n1",
        "purpose": "source",
        "category": "source",
        "recommended_action": "keep",
        "target_path": None,
        "confidence": 0.9,
        "rationale": "source",
    }
    with pytest.raises(ValueError, match="input batch contains duplicate"):
        _validate_batch_shape([item], [node, dict(node)])


def test_classify_all_uses_configured_batches():
    class _BatchBackend:
        def __init__(self):
            self.batch_sizes = []

        def classify_nodes(self, nodes, all_nodes, feedback=None):
            del all_nodes, feedback
            self.batch_sizes.append(len(nodes))
            return [
                {
                    "node_id": node["node_id"],
                    "purpose": "test",
                    "category": "unknown",
                    "recommended_action": "keep",
                    "target_path": None,
                    "confidence": 0.5,
                    "rationale": "test",
                }
                for node in nodes
            ]

        def classify_node(self, node, all_nodes, feedback=None):
            raise AssertionError("batch method should be used")

    backend = _BatchBackend()
    snapshot = {
        "run_id": "batch-run",
        "nodes": [{"node_id": f"n{i}", "path": f"{i}.txt", "type": "file"} for i in range(5)],
    }
    result = classify_all(snapshot, backend, batch_size=2)
    assert backend.batch_sizes == [2, 2, 1]
    assert [item["node_id"] for item in result["classifications"]] == [f"n{i}" for i in range(5)]


def test_classify_all_rejects_unbounded_batch_size():
    snapshot = {
        "run_id": "batch-limit-run",
        "nodes": [{"node_id": "n1", "path": "1.txt", "type": "file"}],
    }
    with pytest.raises(ValueError, match="between 1 and 128"):
        classify_all(snapshot, HeuristicBackend(), batch_size=129)


def test_manifest_classification_is_per_chunk_and_resumable(tmp_path):
    root = tmp_path / "tree"
    root.mkdir()
    (root / "main.py").write_text("print('ok')\n")
    (root / "README.md").write_text("docs\n")
    chunks = tmp_path / "chunks"
    scan_tree_to_chunks(str(root), "chunk-run", chunks, hash_mode="none", max_nodes=1)

    outputs = classify_manifest(chunks / "manifest.json", backend=HeuristicBackend())

    manifest = validate_file(chunks / "manifest.json", "chunk_manifest")
    assert len(outputs) == len(manifest["chunks"]) > 1
    assert manifest["status"]["classification"] == "completed"
    assert manifest["status"]["completed_chunks"] == len(manifest["chunks"])
    for entry, output in zip(manifest["chunks"], outputs):
        assert entry["classification"]["status"] == "completed"
        assert Path(entry["classification"]["path"]) == output.relative_to(chunks)
        classification = validate_file(output, "classification")
        assert classification["provenance"]["source_artifacts"][0]["path"] == entry["path"]
        chunk = json.loads((chunks / entry["path"]).read_text())
        assert classification["input_hash"] != ""
        assert {item["node_id"] for item in classification["classifications"]} == {
            node["node_id"] for node in chunk["nodes"]
        }

    proposal = render_manifest_proposal(chunks / "manifest.json")
    assert proposal["run_id"] == "chunk-run"
    assert proposal["changes"] == []


def test_manifest_classification_resumes_from_batch_checkpoint(tmp_path):
    root = tmp_path / "tree"
    root.mkdir()
    for index in range(5):
        (root / f"file-{index}.txt").write_text(f"file {index}\n")
    chunks = tmp_path / "chunks"
    scan_tree_to_chunks(str(root), "checkpoint-run", chunks, hash_mode="none", max_nodes=10)

    class _InterruptingBackend(HeuristicBackend):
        def __init__(self, interrupt=True):
            self.calls = 0
            self.interrupt = interrupt

        def classify_nodes(self, nodes, all_nodes, feedback=None):
            self.calls += 1
            if self.interrupt and self.calls == 2:
                raise KeyboardInterrupt()
            return super().classify_nodes(nodes, all_nodes, feedback)

    manifest_path = chunks / "manifest.json"
    with pytest.raises(KeyboardInterrupt):
        classify_manifest(
            manifest_path,
            backend=_InterruptingBackend(interrupt=True),
            batch_size=2,
        )

    manifest = validate_file(manifest_path, "chunk_manifest")
    partials = list((chunks / "classifications").glob("*.classification.v1.partial.json"))
    assert len(partials) == 1
    partial = partials[0]
    assert manifest["status"]["classification"] == "failed"
    assert partial.exists()
    partial_artifact = validate_file(partial, "classification")
    assert len(partial_artifact["classifications"]) == 2

    outputs = classify_manifest(
        manifest_path, backend=_InterruptingBackend(interrupt=False), batch_size=2
    )

    assert len(outputs) == 1
    assert not partial.exists()
    manifest = validate_file(manifest_path, "chunk_manifest")
    assert manifest["status"]["classification"] == "completed"
    classification = validate_file(outputs[0], "classification")
    assert len(classification["classifications"]) == 5


def test_manifest_checkpoint_rejects_changed_context(tmp_path):
    root = tmp_path / "tree"
    root.mkdir()
    for index in range(3):
        (root / f"file-{index}.txt").write_text(f"file {index}\n")
    chunks = tmp_path / "chunks"
    scan_tree_to_chunks(str(root), "context-run", chunks, hash_mode="none", max_nodes=10)

    class _InterruptingBackend(HeuristicBackend):
        def __init__(self, interrupt=True):
            self.calls = 0
            self.interrupt = interrupt

        def classify_nodes(self, nodes, all_nodes, feedback=None):
            self.calls += 1
            if feedback == "first context" and self.interrupt:
                if self.calls == 1:
                    return super().classify_nodes(nodes, all_nodes, feedback)
                raise KeyboardInterrupt()
            return super().classify_nodes(nodes, all_nodes, feedback)

    manifest_path = chunks / "manifest.json"
    with pytest.raises(KeyboardInterrupt):
        classify_manifest(
            manifest_path,
            backend=_InterruptingBackend(interrupt=True),
            feedback="first context",
            batch_size=2,
        )

    classify_manifest(
        manifest_path,
        backend=HeuristicBackend(),
        feedback="different context",
        batch_size=2,
    )

    manifest = validate_file(manifest_path, "chunk_manifest")
    assert manifest["status"]["classification"] == "completed"

    classify_manifest(
        manifest_path,
        backend=_InterruptingBackend(interrupt=False),
        feedback="first context",
        batch_size=2,
    )


def test_manifest_adopts_orphan_final_and_cleans_partial(tmp_path):
    root = tmp_path / "tree"
    root.mkdir()
    (root / "file.txt").write_text("file\n")
    chunks = tmp_path / "chunks"
    scan_tree_to_chunks(str(root), "orphan-run", chunks, hash_mode="none", max_nodes=10)
    manifest_path = chunks / "manifest.json"

    class _ControlledBackend(HeuristicBackend):
        def __init__(self, fail=False):
            self.fail = fail

        def classify_nodes(self, nodes, all_nodes, feedback=None):
            if self.fail:
                raise AssertionError("orphan final should be adopted")
            return super().classify_nodes(nodes, all_nodes, feedback)

    outputs = classify_manifest(manifest_path, backend=_ControlledBackend())
    manifest = validate_file(manifest_path, "chunk_manifest")
    entry = manifest["chunks"][0]
    entry.pop("classification")
    validate_file(chunks / "manifest.json", "chunk_manifest")
    from lib.validate import write_validated

    write_validated(manifest, "chunk_manifest", manifest_path)
    partial = outputs[0].with_name(outputs[0].name.replace(".json", ".partial.json"))
    partial.write_text(outputs[0].read_text())

    recovered = classify_manifest(manifest_path, backend=_ControlledBackend(fail=True))

    assert recovered == outputs
    assert not partial.exists()
    manifest = validate_file(manifest_path, "chunk_manifest")
    assert manifest["chunks"][0]["classification"]["status"] == "completed"


def test_manifest_classification_preserves_duplicate_context_across_chunks(tmp_path):
    root = tmp_path / "tree"
    root.mkdir()
    (root / "one.bin").write_bytes(b"same")
    (root / "two.bin").write_bytes(b"same")
    chunks = tmp_path / "chunks"
    scan_tree_to_chunks(str(root), "duplicate-run", chunks, hash_mode="full", max_nodes=1)

    classify_manifest(chunks / "manifest.json", backend=HeuristicBackend())

    manifest = validate_file(chunks / "manifest.json", "chunk_manifest")
    classifications = []
    for entry in manifest["chunks"]:
        chunk = json.loads((chunks / entry["path"]).read_text())
        classification = validate_file(chunks / entry["classification"]["path"], "classification")
        classifications.extend(
            (node["path"], item["category"])
            for node in chunk["nodes"]
            for item in classification["classifications"]
            if item["node_id"] == node["node_id"]
        )

    assert sorted(category for _, category in classifications) == ["duplicate", "unknown"]
