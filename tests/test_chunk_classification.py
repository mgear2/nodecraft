import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.classify import (  # noqa: E402
    HeuristicBackend,
    OllamaBackend,
    _extract_json,
    _normalize,
    _validate_shape,
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
