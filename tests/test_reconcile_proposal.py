import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.classify import classify_manifest  # noqa: E402
from lib.validate import validate  # noqa: E402
from scripts.render_proposal import render_manifest_proposal  # noqa: E402
from scripts.scan import scan_tree_to_chunks  # noqa: E402


class _Backend:
    def __init__(self, classify):
        self._classify = classify

    def classify_node(self, node, all_nodes, feedback=None):
        action, target = self._classify(node["path"])
        return {
            "purpose": "test",
            "category": "unknown",
            "recommended_action": action,
            "target_path": target,
            "confidence": 1.0,
            "rationale": "test classification",
        }


def _manifest(tmp_path, files, classify):
    root = tmp_path / "tree"
    root.mkdir()
    for name in files:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("content")
    chunks = tmp_path / "chunks"
    scan_tree_to_chunks(str(root), "reconcile-run", chunks, hash_mode="none", max_nodes=1)
    classify_manifest(chunks / "manifest.json", backend=_Backend(classify))
    return chunks / "manifest.json"


def test_reconciled_proposal_keeps_each_chunk_provenance_and_validates(tmp_path):
    manifest = _manifest(
        tmp_path,
        ["one.txt"],
        lambda path: ("move", "archive/" + path),
    )

    proposal = render_manifest_proposal(manifest)

    validate(proposal, "proposal")
    assert proposal["provenance"]["source_artifacts"][0]["chunk_id"]
    assert proposal["provenance"]["source_artifacts"][0]["chunk_path"]
    assert proposal["changes"][0]["provenance"]["source_artifacts"][0]["chunk_id"]


def test_reconciliation_rejects_duplicate_destinations_across_chunks(tmp_path):
    manifest = _manifest(
        tmp_path,
        ["one.txt", "two.txt"],
        lambda path: ("move", "archive/same.txt"),
    )

    with pytest.raises(ValueError, match="duplicate operation destination"):
        render_manifest_proposal(manifest)


def test_reconciliation_rejects_incompatible_overlapping_sources(tmp_path):
    manifest = _manifest(
        tmp_path,
        ["folder/file.txt"],
        lambda path: ("delete", None) if path == "folder" else ("move", "elsewhere/file.txt"),
    )

    with pytest.raises(ValueError, match="overlapping source paths"):
        render_manifest_proposal(manifest)


def test_subtree_proposal_rejects_target_outside_local_scope(tmp_path):
    manifest = _manifest(
        tmp_path,
        ["src/old.bak"],
        lambda path: ("move", "archive/" + path),
    )

    with pytest.raises(ValueError, match="outside selected scope"):
        render_manifest_proposal(manifest, subtree_path="src")


def test_subtree_proposal_localizes_target_inside_scope(tmp_path):
    manifest = _manifest(
        tmp_path,
        ["src/old.bak"],
        lambda path: (
            ("move", "src/archive/old.bak") if path == "src/old.bak" else ("keep", None)
        ),
    )

    proposal = render_manifest_proposal(manifest, subtree_path="src")

    assert proposal["changes"][0]["from_path"] == "old.bak"
    assert proposal["changes"][0]["to_path"] == "archive/old.bak"


def test_subtree_proposal_rejects_missing_scope(tmp_path):
    manifest = _manifest(tmp_path, ["src/file.txt"], lambda path: ("keep", None))

    with pytest.raises(ValueError, match="subtree path not found"):
        render_manifest_proposal(manifest, subtree_path="missing")
