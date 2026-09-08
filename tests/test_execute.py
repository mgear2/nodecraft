import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import trace  # noqa: E402
from lib.validate import validate  # noqa: E402
from scripts.execute import execute_proposal  # noqa: E402


def _make_tree(tmp_path: Path):
    (tmp_path / "a.bak").write_text("hello")
    (tmp_path / "keep.py").write_text("print(1)")


def test_execute_moves_and_archives(tmp_path):
    _make_tree(tmp_path)
    proposal = {
        "schema_version": "1.0",
        "proposal_id": "p1",
        "run_id": "r1",
        "iteration": 1,
        "based_on_input_hash": "sha256:x",
        "diagram": "n/a",
        "changes": [
            {"node_id": "n1", "action": "archive", "from_path": "a.bak", "to_path": None},
        ],
    }
    approval = {
        "schema_version": "1.0",
        "proposal_id": "p1",
        "decision": "approve",
        "feedback": None,
        "decided_at": trace.now_iso(),
        "decided_by": "test",
    }

    log, undo = execute_proposal(str(tmp_path), proposal, approval)
    validate(log, "execution_log")

    assert log["operations"][0]["status"] == "success"
    assert not (tmp_path / "a.bak").exists()
    assert (tmp_path / ".trash" / "r1" / "a.bak").exists()
    assert "shutil.move" in undo


def test_execute_refuses_without_approval(tmp_path):
    _make_tree(tmp_path)
    proposal = {
        "schema_version": "1.0",
        "proposal_id": "p1",
        "run_id": "r1",
        "iteration": 1,
        "based_on_input_hash": "x",
        "diagram": "n/a",
        "changes": [],
    }
    bad_approval = {
        "schema_version": "1.0",
        "proposal_id": "p1",
        "decision": "reject",
        "feedback": "no",
        "decided_at": trace.now_iso(),
        "decided_by": "test",
    }
    try:
        execute_proposal(str(tmp_path), proposal, bad_approval)
        assert False, "should have raised"
    except ValueError:
        pass


def test_execute_refuses_mismatched_proposal_id(tmp_path):
    _make_tree(tmp_path)
    proposal = {
        "schema_version": "1.0",
        "proposal_id": "p1",
        "run_id": "r1",
        "iteration": 1,
        "based_on_input_hash": "x",
        "diagram": "n/a",
        "changes": [],
    }
    approval = {
        "schema_version": "1.0",
        "proposal_id": "DIFFERENT",
        "decision": "approve",
        "feedback": None,
        "decided_at": trace.now_iso(),
        "decided_by": "test",
    }
    try:
        execute_proposal(str(tmp_path), proposal, approval)
        assert False, "should have raised"
    except ValueError:
        pass


def test_execute_rejects_paths_outside_root(tmp_path):
    _make_tree(tmp_path)
    proposal = {
        "schema_version": "1.0",
        "proposal_id": "p1",
        "run_id": "r1",
        "iteration": 1,
        "based_on_input_hash": "x",
        "diagram": "n/a",
        "changes": [
            {"node_id": "n1", "action": "delete", "from_path": "../outside.txt", "to_path": None}
        ],
    }
    approval = {
        "proposal_id": "p1",
        "decision": "approve",
    }
    try:
        execute_proposal(str(tmp_path), proposal, approval)
        assert False, "should have raised"
    except ValueError as error:
        assert "contained within root" in str(error)


def test_execute_rejects_move_without_target(tmp_path):
    _make_tree(tmp_path)
    proposal = {
        "schema_version": "1.0",
        "proposal_id": "p1",
        "run_id": "r1",
        "iteration": 1,
        "based_on_input_hash": "x",
        "diagram": "n/a",
        "changes": [{"node_id": "n1", "action": "move", "from_path": "keep.py", "to_path": None}],
    }
    approval = {"proposal_id": "p1", "decision": "approve"}
    try:
        execute_proposal(str(tmp_path), proposal, approval)
        assert False, "should have raised"
    except ValueError as error:
        assert "requires a to_path" in str(error)


def test_undo_script_actually_reverses_move(tmp_path):
    """This is the correctness check called for by C.1's I14 note:
    verify the undo script actually reverses the operation, not just that
    it was generated."""
    _make_tree(tmp_path)
    proposal = {
        "schema_version": "1.0",
        "proposal_id": "p1",
        "run_id": "r1",
        "iteration": 1,
        "based_on_input_hash": "x",
        "diagram": "n/a",
        "changes": [
            {"node_id": "n1", "action": "move", "from_path": "keep.py", "to_path": "src/keep.py"}
        ],
    }
    approval = {
        "schema_version": "1.0",
        "proposal_id": "p1",
        "decision": "approve",
        "feedback": None,
        "decided_at": trace.now_iso(),
        "decided_by": "test",
    }

    log, undo_script = execute_proposal(str(tmp_path), proposal, approval)
    assert (tmp_path / "src" / "keep.py").exists()
    assert not (tmp_path / "keep.py").exists()

    undo_path = tmp_path / "undo.py"
    undo_path.write_text(undo_script)
    undo_path.chmod(0o755)
    subprocess.run([sys.executable, str(undo_path)], check=True)

    assert (tmp_path / "keep.py").exists()
    assert not (tmp_path / "src" / "keep.py").exists()


def test_portable_subtree_executes_at_explicit_mount_and_preserves_origin(tmp_path):
    scoped = tmp_path / "scoped"
    scoped.mkdir()
    (scoped / "old.bak").write_text("hello")
    proposal = {
        "schema_version": "1.0",
        "proposal_id": "p1",
        "run_id": "r1",
        "iteration": 1,
        "based_on_input_hash": "x",
        "diagram": "n/a",
        "scope": {
            "root_path": str(tmp_path),
            "subtree_path": "scoped",
            "path_format": "posix",
        },
        "changes": [{"node_id": "n1", "action": "delete", "from_path": "old.bak", "to_path": None}],
    }
    approval = {"proposal_id": "p1", "decision": "approve"}

    log, undo_script = execute_proposal(str(scoped), proposal, approval)

    assert (scoped / ".trash" / "r1" / "old.bak").exists()
    assert not (tmp_path / ".trash" / "r1").exists()
    assert log["mount_path"] == str(scoped.resolve())
    undo_path = tmp_path / "undo.py"
    undo_path.write_text(undo_script)
    subprocess.run([sys.executable, str(undo_path)], check=True)
    assert (scoped / "old.bak").exists()


def test_execute_does_not_treat_origin_as_a_filesystem_target(tmp_path):
    mount = tmp_path / "mount"
    mount.mkdir()
    (mount / "old.bak").write_text("hello")
    proposal = {
        "schema_version": "1.0",
        "proposal_id": "p1",
        "run_id": "r1",
        "iteration": 1,
        "based_on_input_hash": "x",
        "diagram": "n/a",
        "scope": {"root_path": "C:/source", "subtree_path": "src", "path_format": "posix"},
        "origin": {"root_path": "C:/source", "subtree_path": "src", "path_format": "posix"},
        "changes": [{"node_id": "n1", "action": "delete", "from_path": "old.bak", "to_path": None}],
    }

    execute_proposal(str(mount), proposal, {"proposal_id": "p1", "decision": "approve"})

    assert not (mount / "old.bak").exists()
    assert (mount / ".trash" / "r1" / "old.bak").exists()


def test_execute_rejects_duplicate_destinations(tmp_path):
    _make_tree(tmp_path)
    proposal = {
        "schema_version": "1.0",
        "proposal_id": "p1",
        "run_id": "r1",
        "iteration": 1,
        "based_on_input_hash": "x",
        "diagram": "n/a",
        "changes": [
            {"node_id": "n1", "action": "move", "from_path": "keep.py", "to_path": "x.py"},
            {"node_id": "n2", "action": "move", "from_path": "a.bak", "to_path": "x.py"},
        ],
    }
    with pytest.raises(ValueError, match="duplicate"):
        execute_proposal(str(tmp_path), proposal, {"proposal_id": "p1", "decision": "approve"})


def test_execute_rejects_stale_proposal_content_hash(tmp_path):
    _make_tree(tmp_path)
    proposal = {
        "schema_version": "1.0",
        "proposal_id": "p1",
        "run_id": "r1",
        "iteration": 1,
        "based_on_input_hash": "x",
        "diagram": "n/a",
        "changes": [],
    }
    with pytest.raises(ValueError, match="content_hash"):
        execute_proposal(
            str(tmp_path),
            proposal,
            {
                "proposal_id": "p1",
                "decision": "approve",
                "proposal_content_hash": "stale",
            },
        )
