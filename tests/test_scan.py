import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.validate import validate  # noqa: E402
from scripts.scan import build_snapshot  # noqa: E402

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "sample_tree"


def test_scan_produces_valid_schema():
    snapshot = build_snapshot(str(FIXTURE_ROOT), run_id="unit-test-run")
    validate(snapshot, "tree_snapshot")  # raises on failure


def test_scan_excludes_git_and_pycache():
    snapshot = build_snapshot(str(FIXTURE_ROOT), run_id="unit-test-run")
    paths = [n["path"] for n in snapshot["nodes"]]
    assert not any(".git" in p for p in paths)
    assert not any("__pycache__" in p for p in paths)


def test_scan_finds_known_files():
    snapshot = build_snapshot(str(FIXTURE_ROOT), run_id="unit-test-run")
    paths = {n["path"] for n in snapshot["nodes"]}
    assert "src/main.py" in paths
    assert "docs/README.md" in paths
    assert "notes.txt.bak" in paths


def test_scan_is_deterministic_ordering():
    s1 = build_snapshot(str(FIXTURE_ROOT), run_id="r1")
    s2 = build_snapshot(str(FIXTURE_ROOT), run_id="r1")
    assert [n["path"] for n in s1["nodes"]] == [n["path"] for n in s2["nodes"]]


def test_duplicate_files_have_matching_hash():
    snapshot = build_snapshot(str(FIXTURE_ROOT), run_id="unit-test-run")
    by_path = {n["path"]: n for n in snapshot["nodes"]}
    assert by_path["src/main.py"]["content_hash"] == by_path["src/main_copy.py"]["content_hash"]
