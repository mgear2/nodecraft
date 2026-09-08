from lib.availability import UNKNOWN
from scripts.scan import build_snapshot, parse_size


def test_parse_size_supports_documented_units():
    assert parse_size("100MB") == 100 * 1024 * 1024
    assert parse_size("2 KB") == 2 * 1024
    assert parse_size("512") == 512


def test_conditional_hashing_skips_large_files(tmp_path):
    small = tmp_path / "small.txt"
    large = tmp_path / "large.bin"
    small.write_text("small")
    large.write_bytes(b"x" * 10)

    snapshot = build_snapshot(str(tmp_path), "r1", hash_mode="conditional", max_hash_size=4)
    nodes = {node["path"]: node for node in snapshot["nodes"]}
    assert nodes["small.txt"]["content_hash"] is None
    assert nodes["large.bin"]["content_hash"] is None


def test_cache_reuses_hash_and_none_mode_skips_hashes(tmp_path):
    path = tmp_path / "file.txt"
    path.write_text("content")
    cache_path = tmp_path / "cache.json"

    first = build_snapshot(str(tmp_path), "r1", cache_path=cache_path)
    assert first["nodes"][0]["content_hash"]
    assert cache_path.exists()

    path.write_text("changed")
    second = build_snapshot(str(tmp_path), "r2", hash_mode="none", cache_path=cache_path)
    node = next(node for node in second["nodes"] if node["path"] == "file.txt")
    assert node["content_hash"] is None


def test_cache_path_is_excluded_from_scan(tmp_path):
    cache_path = tmp_path / ".nodecraft" / "hash-cache.json"
    (tmp_path / "file.txt").write_text("content")
    build_snapshot(str(tmp_path), "r1", cache_path=cache_path)
    snapshot = build_snapshot(str(tmp_path), "r2", cache_path=cache_path)
    assert all(node["path"] != ".nodecraft/hash-cache.json" for node in snapshot["nodes"])


def test_unknown_availability_is_not_hashed(tmp_path, monkeypatch):
    path = tmp_path / "cloud-placeholder"
    path.write_text("content")
    monkeypatch.setattr("scripts.scan.detect_availability", lambda _: UNKNOWN)

    snapshot = build_snapshot(str(tmp_path), "r1")

    assert snapshot["nodes"][0]["availability"] == UNKNOWN
    assert snapshot["nodes"][0]["content_hash"] is None
