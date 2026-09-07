import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from lib.validate import validate_file  # noqa: E402
from scripts.scan import scan_tree_to_chunks  # noqa: E402


def test_streaming_scan_writes_bounded_chunks_and_manifest(tmp_path):
    root = tmp_path / "tree"
    (root / "src").mkdir(parents=True)
    for index in range(5):
        (root / "src" / f"{index}.py").write_text("print('x')\n")
    out = tmp_path / "chunks"

    manifest = scan_tree_to_chunks(
        str(root), "stream-run", out, hash_mode="none", max_nodes=2, max_bytes=10_000
    )

    assert len(manifest["chunks"]) > 1
    validate_file(out / "manifest.json", "chunk_manifest")
    for entry in manifest["chunks"]:
        chunk = json.loads((out / entry["path"]).read_text())
        assert chunk["chunk"]["node_count"] <= 2
        assert chunk["chunk"]["byte_count"] == entry["byte_count"]
        assert chunk["provenance"]["source_artifacts"][0]["artifact_type"] == "filesystem_scan"


def test_streaming_scan_marks_a_single_oversized_node(tmp_path):
    root = tmp_path / "tree"
    root.mkdir()
    (root / "large.txt").write_text("x" * 100)
    out = tmp_path / "chunks"

    manifest = scan_tree_to_chunks(
        str(root), "oversized-run", out, hash_mode="none", max_nodes=10, max_bytes=10
    )

    assert manifest["chunks"][0]["oversized"] is True
    assert manifest["chunks"][0]["node_count"] == 1


def test_streaming_scan_dry_run_does_not_write(tmp_path):
    root = tmp_path / "tree"
    root.mkdir()
    (root / "file.txt").write_text("content")
    out = tmp_path / "chunks"

    manifest = scan_tree_to_chunks(str(root), "dry-run", out, hash_mode="none", write=False)

    assert manifest["chunks"]
    assert not out.exists()
