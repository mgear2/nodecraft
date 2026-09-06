"""
I18 — End-to-end integration test.

Exercises the full runtime pipeline (T1-T7) against a fresh copy of the
fixture tree, including one reject-with-feedback -> revise -> re-approve
cycle, which none of the per-component unit tests cover in isolation.
"""
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import trace  # noqa: E402
from orchestrator import run_pipeline  # noqa: E402

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "sample_tree"


def _copy_fixture_tree(tmp_path: Path) -> Path:
    dest = tmp_path / "tree"
    shutil.copytree(FIXTURE_ROOT, dest)
    return dest


def test_e2e_approve_immediately(tmp_path):
    tree = _copy_fixture_tree(tmp_path)
    runs_dir = tmp_path / "runs"

    def approve_first_time(proposal):
        return {
            "schema_version": trace.SCHEMA_VERSION,
            "proposal_id": proposal["proposal_id"],
            "decision": "approve",
            "feedback": None,
            "decided_at": trace.now_iso(),
            "decided_by": "test",
        }

    result = run_pipeline(str(tree), approve_first_time, runs_dir=str(runs_dir))

    assert result["iterations"] == 1
    assert result["final_decision"] == "approve"
    # build/ and build/output.o should have been deleted (archived to .trash)
    assert not (tree / "build").exists()
    assert (tree / ".trash" / result["run_id"] / "build").exists()
    # notes.txt.bak should have been archived
    assert (tree / "_archive" / "notes.txt.bak").exists()
    # source file untouched
    assert (tree / "src" / "main.py").exists()

    log = json.loads(Path(result["artifacts"]["execution_log"]).read_text())
    assert all(op["status"] in ("success", "skipped") for op in log["operations"])

    summary = Path(result["artifacts"]["summary"]).read_text()
    assert "Run Summary" in summary


def test_e2e_reject_then_approve_cycle(tmp_path):
    """Covers the T4 -> T5 -> T3 revision loop end-to-end: first proposal is
    rejected with feedback, forcing a re-classification pass, then the
    second proposal is approved."""
    tree = _copy_fixture_tree(tmp_path)
    runs_dir = tmp_path / "runs"
    calls = {"count": 0}

    def reject_once_then_approve(proposal):
        calls["count"] += 1
        if calls["count"] == 1:
            return {
                "schema_version": trace.SCHEMA_VERSION,
                "proposal_id": proposal["proposal_id"],
                "decision": "reject",
                "feedback": "do not delete build artifacts, just flag them for review instead",
                "decided_at": trace.now_iso(),
                "decided_by": "test",
            }
        return {
            "schema_version": trace.SCHEMA_VERSION,
            "proposal_id": proposal["proposal_id"],
            "decision": "approve",
            "feedback": None,
            "decided_at": trace.now_iso(),
            "decided_by": "test",
        }

    result = run_pipeline(str(tree), reject_once_then_approve, runs_dir=str(runs_dir))

    assert calls["count"] == 2
    assert result["iterations"] == 2
    assert result["final_decision"] == "approve"

    # both iterations' artifacts should be on disk (append-only history,
    # per spec B.3 -- not overwritten)
    rdir = Path(runs_dir) / result["run_id"]
    assert (rdir / "proposal.v1.json").exists()
    assert (rdir / "proposal.v2.json").exists()
    assert (rdir / "approval_decision.v1.json").exists()
    assert (rdir / "approval_decision.v2.json").exists()

    v1_decision = json.loads((rdir / "approval_decision.v1.json").read_text())
    assert v1_decision["decision"] == "reject"
    v2_decision = json.loads((rdir / "approval_decision.v2.json").read_text())
    assert v2_decision["decision"] == "approve"


def test_e2e_max_iterations_exceeded(tmp_path):
    tree = _copy_fixture_tree(tmp_path)
    runs_dir = tmp_path / "runs"

    def always_reject(proposal):
        return {
            "schema_version": trace.SCHEMA_VERSION,
            "proposal_id": proposal["proposal_id"],
            "decision": "reject",
            "feedback": "still not right",
            "decided_at": trace.now_iso(),
            "decided_by": "test",
        }

    from orchestrator import MaxIterationsExceeded
    try:
        run_pipeline(str(tree), always_reject, runs_dir=str(runs_dir), max_iterations=2)
        assert False, "should have raised MaxIterationsExceeded"
    except MaxIterationsExceeded:
        pass
