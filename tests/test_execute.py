import subprocess
import sys
from pathlib import Path

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
    assert "mv" in undo


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

    undo_path = tmp_path / "undo.sh"
    undo_path.write_text(undo_script)
    undo_path.chmod(0o755)
    subprocess.run(["sh", str(undo_path)], check=True)

    assert (tmp_path / "keep.py").exists()
    assert not (tmp_path / "src" / "keep.py").exists()
