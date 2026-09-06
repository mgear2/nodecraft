import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.validate import validate  # noqa: E402
from scripts.render_proposal import render_proposal  # noqa: E402

# Deliberately hand-built fixture inputs (not I5's live output) to prove I6
# was built and is testable independently of the Classifier script.
FIXTURE_SNAPSHOT = {
    "schema_version": "1.0",
    "run_id": "r1",
    "root_path": "/tmp/x",
    "generated_at": "2026-01-01T00:00:00+00:00",
    "nodes": [
        {"node_id": "a", "path": "keep.py", "type": "file", "size_bytes": 1,
         "mtime": None, "ctime": None, "extension": ".py", "content_type": None,
         "content_hash": None, "symlink_target": None, "permissions": None},
        {"node_id": "b", "path": "old.bak", "type": "file", "size_bytes": 1,
         "mtime": None, "ctime": None, "extension": ".bak", "content_type": None,
         "content_hash": None, "symlink_target": None, "permissions": None},
    ],
}

FIXTURE_CLASSIFICATION = {
    "schema_version": "1.0",
    "run_id": "r1",
    "input_hash": "sha256:deadbeef",
    "iteration": 1,
    "classifications": [
        {"node_id": "a", "purpose": "source", "category": "source",
         "recommended_action": "keep", "target_path": None, "confidence": 0.9,
         "rationale": "python source"},
        {"node_id": "b", "purpose": "backup", "category": "cache",
         "recommended_action": "archive", "target_path": "_archive/old.bak",
         "confidence": 0.7, "rationale": "backup suffix"},
    ],
}


def test_proposal_is_schema_valid():
    proposal = render_proposal(FIXTURE_SNAPSHOT, FIXTURE_CLASSIFICATION)
    validate(proposal, "proposal")


def test_proposal_only_includes_actionable_changes():
    proposal = render_proposal(FIXTURE_SNAPSHOT, FIXTURE_CLASSIFICATION)
    actions = {c["node_id"]: c["action"] for c in proposal["changes"]}
    assert "a" not in actions  # 'keep' is not actionable
    assert actions["b"] == "archive"


def test_proposal_diagram_contains_rationale():
    proposal = render_proposal(FIXTURE_SNAPSHOT, FIXTURE_CLASSIFICATION)
    assert "backup suffix" in proposal["diagram"]
    assert "old.bak" in proposal["diagram"]


def test_proposal_is_deterministic_given_same_iteration():
    p1 = render_proposal(FIXTURE_SNAPSHOT, FIXTURE_CLASSIFICATION, iteration=1)
    p2 = render_proposal(FIXTURE_SNAPSHOT, FIXTURE_CLASSIFICATION, iteration=1)
    # proposal_id is intentionally unique per render; diagram/changes must match
    assert p1["diagram"] == p2["diagram"]
    assert p1["changes"] == p2["changes"]
