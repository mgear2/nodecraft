#!/usr/bin/env python3
"""
I4 — Scanner (T1 in the runtime pipeline).

Deterministic, pure filesystem walk. Depends only on the tree_snapshot
schema (I1) and the shared validate/trace utilities (I2/I3) — not on any
other script in the pipeline.
"""

from __future__ import annotations

import argparse
import mimetypes
import os
import stat
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib import trace  # noqa: E402
from lib.hash_cache import HashCache  # noqa: E402
from lib.progress import Progress  # noqa: E402
from lib.validate import write_validated  # noqa: E402

DEFAULT_EXCLUDES = {".git", ".nodecraft", "node_modules", "__pycache__", ".venv"}
HASH_MODES = {"full", "conditional", "none"}
DEFAULT_MAX_HASH_SIZE = 100 * 1024 * 1024


def parse_size(value: str) -> int:
    units = {"b": 1, "kb": 1024, "mb": 1024**2, "gb": 1024**3}
    text = value.strip().lower()
    for suffix, multiplier in sorted(units.items(), key=lambda item: len(item[0]), reverse=True):
        if text.endswith(suffix):
            number = text[: -len(suffix)].strip()
            if not number:
                raise ValueError(f"invalid size: {value!r}")
            return int(float(number) * multiplier)
    return int(text)


def _node_id(path: str) -> str:
    return trace.hash_bytes(path.encode("utf-8"))


def _classify_type(entry: os.DirEntry) -> str:
    if entry.is_symlink():
        return "symlink"
    if entry.is_dir(follow_symlinks=False):
        return "directory"
    return "file"


def scan_tree(
    root_path: str,
    excludes: set[str] | None = None,
    progress: Progress | None = None,
    hash_mode: str = "full",
    max_hash_size: int = DEFAULT_MAX_HASH_SIZE,
    cache_path: str | Path | None = None,
    cache_enabled: bool = True,
) -> list[dict]:
    """Pure function: filesystem -> list of node dicts. No I/O side effects
    beyond reading. Excludes are matched against directory *names* at any
    depth (simple, predictable semantics — not glob patterns)."""
    excludes = excludes if excludes is not None else DEFAULT_EXCLUDES
    if hash_mode not in HASH_MODES:
        raise ValueError(f"hash_mode must be one of {sorted(HASH_MODES)}")
    if max_hash_size < 0:
        raise ValueError("max_hash_size must be non-negative")
    root = Path(root_path).resolve()
    nodes: list[dict] = []
    reporter = progress or Progress(quiet=True)
    progress_items = reporter.items("scanning", 0)
    cache = None if hash_mode == "none" or not cache_enabled else HashCache(
        cache_path or root / ".nodecraft" / "hash-cache.json"
    )
    if cache:
        cache.load()

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in excludes]

        for name in list(dirnames) + filenames:
            full_path = Path(dirpath) / name
            progress_items.total += 1
            rel_path = full_path.relative_to(root).as_posix()
            try:
                st = os.lstat(full_path)
            except OSError:
                # Unreadable node: still record it as `unknown`-ish rather
                # than silently dropping it, so the operator sees it.
                nodes.append(
                    {
                        "node_id": _node_id(rel_path),
                        "path": rel_path,
                        "type": "file",
                        "size_bytes": None,
                        "mtime": None,
                        "ctime": None,
                        "extension": full_path.suffix or None,
                        "content_type": None,
                        "content_hash": None,
                        "symlink_target": None,
                        "permissions": None,
                    }
                )
                continue

            is_symlink = stat.S_ISLNK(st.st_mode)
            is_dir = full_path.is_dir() and not is_symlink
            node_type = "symlink" if is_symlink else ("directory" if is_dir else "file")

            content_hash = None
            should_hash = (
                node_type == "file"
                and (
                    hash_mode == "full"
                    or hash_mode == "conditional" and st.st_size <= max_hash_size
                )
            )
            if should_hash and cache:
                content_hash = cache.get(rel_path, st)
            if should_hash and content_hash is None:
                try:
                    content_hash = trace.hash_file(full_path)
                    if cache:
                        cache.put(rel_path, st, content_hash)
                except OSError:
                    content_hash = None

            content_type = None
            if node_type == "file":
                content_type, _ = mimetypes.guess_type(str(full_path))

            symlink_target = None
            if is_symlink:
                try:
                    symlink_target = os.readlink(full_path)
                except OSError:
                    symlink_target = None

            nodes.append(
                {
                    "node_id": _node_id(rel_path),
                    "path": rel_path,
                    "type": node_type,
                    "size_bytes": st.st_size if node_type == "file" else None,
                    "mtime": datetime.fromtimestamp(st.st_mtime, tz=UTC).isoformat(
                        timespec="seconds"
                    ),
                    "ctime": datetime.fromtimestamp(
                        getattr(st, "st_birthtime", getattr(st, "st_ctime")), tz=UTC
                    ).isoformat(
                        timespec="seconds"
                    ),
                    "extension": full_path.suffix or None,
                    "content_type": content_type,
                    "content_hash": content_hash,
                    "symlink_target": symlink_target,
                    "permissions": oct(stat.S_IMODE(st.st_mode)),
                }
            )
            progress_items.update(
                bytes_count=st.st_size if node_type == "file" else 0,
                is_file=node_type == "file",
            )

    progress_items.finish()
    if cache:
        try:
            cache.save()
        except OSError as error:
            print(f"[nodecraft] warning: could not save hash cache: {error}", file=sys.stderr)
    nodes.sort(key=lambda n: n["path"])
    return nodes


def build_snapshot(
    root_path: str,
    run_id: str,
    excludes: set[str] | None = None,
    progress: Progress | None = None,
    hash_mode: str = "full",
    max_hash_size: int = DEFAULT_MAX_HASH_SIZE,
    cache_path: str | Path | None = None,
    cache_enabled: bool = True,
) -> dict:
    return {
        "schema_version": trace.SCHEMA_VERSION,
        "run_id": run_id,
        "root_path": str(Path(root_path).resolve()),
        "generated_at": trace.now_iso(),
        "nodes": scan_tree(
            root_path, excludes, progress, hash_mode, max_hash_size, cache_path, cache_enabled
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="I4/T1: scan a directory tree")
    parser.add_argument("root_path")
    parser.add_argument("--run-id", default=None, help="reuse an existing run_id")
    parser.add_argument(
        "--out", default=None, help="output path (default: runs/<run_id>/tree_snapshot.json)"
    )
    parser.add_argument(
        "--exclude", action="append", default=[], help="directory name to exclude (repeatable)"
    )
    parser.add_argument("--dry-run", action="store_true", help="print result, do not write file")
    parser.add_argument("--quiet", action="store_true", help="suppress progress output")
    parser.add_argument("--hash-mode", choices=sorted(HASH_MODES), default="full")
    parser.add_argument("--max-hash-size", type=parse_size, default=DEFAULT_MAX_HASH_SIZE)
    parser.add_argument("--cache-path", default=None)
    parser.add_argument("--no-cache", action="store_true")
    args = parser.parse_args()

    run_id = args.run_id or trace.new_run_id()
    excludes = DEFAULT_EXCLUDES | set(args.exclude)
    snapshot = build_snapshot(
        args.root_path, run_id, excludes, Progress(args.quiet),
        args.hash_mode, args.max_hash_size, args.cache_path,
        cache_enabled=not args.no_cache,
    )

    out_path = args.out or f"runs/{run_id}/tree_snapshot.json"

    if args.dry_run:
        import json

        print(json.dumps(snapshot, indent=2))
        print(f"\n[dry-run] {len(snapshot['nodes'])} nodes scanned; not written.", file=sys.stderr)
    else:
        write_validated(snapshot, "tree_snapshot", out_path)
        print(f"run_id={run_id}")
        print(f"wrote {out_path} ({len(snapshot['nodes'])} nodes)")


if __name__ == "__main__":
    main()
