import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from lib import trace  # noqa: E402
from scripts.summarize_subtree import (  # noqa: E402
    build_subtree_classification,
    build_subtree_snapshot,
)


def _artifacts():
    snapshot = {
        "schema_version": "1.0",
        "run_id": "run-1",
        "root_path": "C:/tree",
        "generated_at": "2026-01-01T00:00:00+00:00",
        "scope": {"root_path": "C:/tree", "subtree_path": ".", "path_format": "posix"},
        "nodes": [
            {"node_id": "dir", "path": "src", "type": "directory"},
            {"node_id": "file", "path": "src/main.py", "type": "file"},
        ],
    }
    classification = {
        "schema_version": "1.0",
        "run_id": "run-1",
        "input_hash": trace.hash_json_artifact(snapshot),
        "scope": snapshot["scope"],
        "classifications": [
            {
                "node_id": "dir",
                "category": "unknown",
                "recommended_action": "keep",
                "confidence": 0.5,
                "rationale": "directory",
            },
            {
                "node_id": "file",
                "category": "source",
                "recommended_action": "keep",
                "confidence": 0.9,
                "rationale": "source",
            },
        ],
    }
    return snapshot, classification


def test_subtree_is_rebased_and_composes_scope():
    snapshot, classification = _artifacts()
    derived = build_subtree_snapshot(snapshot, "src")

    assert derived["scope"]["subtree_path"] == "src"
    assert [node["path"] for node in derived["nodes"]] == [".", "main.py"]
    assert build_subtree_classification(classification, derived)["input_hash"] == (
        trace.hash_json_artifact(derived)
    )

    nested = build_subtree_snapshot(derived, "main.py")
    assert nested["scope"]["subtree_path"] == "src/main.py"
    assert nested["nodes"][0]["path"] == "."


def test_subtree_rejects_lineage_mismatch():
    snapshot, classification = _artifacts()
    classification["input_hash"] = "sha256:wrong"
    derived = build_subtree_snapshot(snapshot, "src")
    with pytest.raises(ValueError, match="input_hash"):
        build_subtree_classification(classification, derived)
