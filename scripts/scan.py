#!/usr/bin/env python3
"""Filesystem scanning.

The normal scan path is :func:`scan_tree_to_chunks`.  It walks the tree in
depth-first order and writes bounded tree artifacts as it goes; no complete
snapshot is constructed in memory.  ``build_snapshot`` remains a small
in-process helper for code that needs to inspect a fixture, but is not used by
the CLI or orchestrator.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import stat
import sys
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib import trace  # noqa: E402
from lib.availability import AVAILABLE, CLOUD_ONLY, UNKNOWN, detect_availability  # noqa: E402
from lib.hash_cache import HashCache  # noqa: E402
from lib.progress import Progress  # noqa: E402
from lib.validate import write_validated  # noqa: E402

DEFAULT_EXCLUDES = {".git", ".nodecraft", "node_modules", "__pycache__", ".venv"}
HASH_MODES = {"full", "conditional", "none"}
DEFAULT_MAX_HASH_SIZE = 100 * 1024 * 1024
DEFAULT_MAX_NODES = 10_000
DEFAULT_MAX_BYTES = 8 * 1024 * 1024


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


def _node_size(node: dict[str, Any]) -> int:
    """Return the byte budget cost of a node's canonical JSON representation."""

    return len(json.dumps(node, sort_keys=True, separators=(",", ":")).encode("utf-8")) + 1


def _entry_node(
    entry: os.DirEntry[str],
    root: Path,
    *,
    hash_mode: str,
    max_hash_size: int,
    cache: HashCache | None,
    skip_cloud_only: bool,
) -> tuple[dict[str, Any] | None, bool]:
    """Build one node without retaining any scan-wide state.

    The boolean says whether the entry is a directory and should be descended
    into.  An entry that cannot be stat'ed is retained as an unknown file node
    so a transient permission error is visible to classification.
    """

    full_path = Path(entry.path)
    rel_path = full_path.relative_to(root).as_posix()
    try:
        st = entry.stat(follow_symlinks=False)
    except OSError:
        return (
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
                "availability": UNKNOWN,
            },
            False,
        )

    is_symlink = stat.S_ISLNK(st.st_mode)
    is_dir = stat.S_ISDIR(st.st_mode) and not is_symlink
    node_type = "symlink" if is_symlink else ("directory" if is_dir else "file")
    availability = detect_availability(full_path) if node_type == "file" else AVAILABLE
    if availability == CLOUD_ONLY and skip_cloud_only:
        return None, is_dir

    content_hash = None
    should_hash = (
        node_type == "file"
        and availability == AVAILABLE
        and (hash_mode == "full" or hash_mode == "conditional" and st.st_size <= max_hash_size)
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

    symlink_target = None
    if is_symlink:
        try:
            symlink_target = os.readlink(full_path)
        except OSError:
            pass

    birthtime = getattr(st, "st_birthtime", None)
    if birthtime is None:
        # Fallback for platforms that do not expose st_birthtime.
        # Accessed via getattr with a string to avoid the static
        # deprecation check on st_ctime.
        ctime_attr = "st_ctime"
        birthtime = getattr(st, ctime_attr)
    return (
        {
            "node_id": _node_id(rel_path),
            "path": rel_path,
            "type": node_type,
            "size_bytes": st.st_size if node_type == "file" else None,
            "mtime": datetime.fromtimestamp(st.st_mtime, tz=UTC).isoformat(timespec="seconds"),
            "ctime": datetime.fromtimestamp(birthtime, tz=UTC).isoformat(timespec="seconds"),
            "extension": full_path.suffix or None,
            "content_type": (
                mimetypes.guess_type(str(full_path))[0] if node_type == "file" else None
            ),
            "content_hash": content_hash,
            "symlink_target": symlink_target,
            "permissions": oct(stat.S_IMODE(st.st_mode)),
            "availability": availability,
        },
        is_dir,
    )


def _iter_nodes(
    root: Path,
    excludes: set[str],
    *,
    hash_mode: str,
    max_hash_size: int,
    cache: HashCache | None,
    progress: Progress,
    skip_cloud_only: bool,
) -> Iterator[dict[str, Any]]:
    """Yield nodes using a deterministic, bounded-memory DFS.

    Only one directory iterator per DFS frame is retained.  Crucially,
    yielded nodes are consumed by the chunk writer immediately; unlike
    ``os.walk`` this never materializes the complete tree (or a complete
    directory listing).
    """

    def entries(path: Path) -> Iterator[os.DirEntry[str]]:
        try:
            return os.scandir(path)
        except OSError:
            return iter(())

    items = progress.items("scanning", 0)
    stack: list[Iterator[os.DirEntry[str]]] = [iter(entries(root))]
    try:
        while stack:
            current = stack[-1]
            try:
                entry = next(current)
            except StopIteration:
                close = getattr(current, "close", None)
                if close:
                    close()
                stack.pop()
                continue
            if entry.name in excludes:
                continue
            node, descend = _entry_node(
                entry,
                root,
                hash_mode=hash_mode,
                max_hash_size=max_hash_size,
                cache=cache,
                skip_cloud_only=skip_cloud_only,
            )
            if node is None:
                try:
                    st = entry.stat(follow_symlinks=False)
                    items.update(
                        bytes_count=st.st_size, is_file=not stat.S_ISDIR(st.st_mode), skipped=True
                    )
                except OSError:
                    pass
            else:
                items.update(
                    bytes_count=node.get("size_bytes") or 0,
                    is_file=node["type"] == "file",
                )
                yield node
            if descend:
                stack.append(iter(entries(Path(entry.path))))
    finally:
        for current in stack:
            close = getattr(current, "close", None)
            if close:
                close()
        items.finish()


class _ChunkWriter:
    def __init__(
        self,
        root: Path,
        run_id: str,
        out_dir: Path,
        *,
        generated_at: str,
        max_nodes: int,
        max_bytes: int,
        write: bool,
    ) -> None:
        self.root = root
        self.run_id = run_id
        self.out_dir = out_dir
        self.generated_at = generated_at
        self.max_nodes = max_nodes
        self.max_bytes = max_bytes
        self.write = write
        self.nodes: list[dict[str, Any]] = []
        self.byte_count = 0
        self.index = 0
        self.chunks: list[dict[str, Any]] = []

    def add(self, node: dict[str, Any]) -> None:
        cost = _node_size(node)
        if self.nodes and (
            len(self.nodes) >= self.max_nodes or self.byte_count + cost > self.max_bytes
        ):
            self.flush()
        self.nodes.append(node)
        self.byte_count += cost
        if cost > self.max_bytes:
            self.flush()

    def flush(self) -> None:
        if not self.nodes:
            return
        self.index += 1
        first_path = self.nodes[0]["path"]
        last_path = self.nodes[-1]["path"]
        oversized = len(self.nodes) > self.max_nodes or self.byte_count > self.max_bytes
        seed = f"{self.run_id}:{self.index}:{first_path}:{last_path}".encode()
        chunk_id = trace.hash_bytes(seed)
        chunk = {
            "schema_version": trace.SCHEMA_VERSION,
            "run_id": self.run_id,
            "root_path": str(self.root),
            "generated_at": self.generated_at,
            "scope": {
                "root_path": str(self.root),
                "subtree_path": ".",
                "path_format": "posix",
            },
            "origin": {
                "root_path": str(self.root),
                "subtree_path": ".",
                "path_format": "posix",
            },
            "chunk": {
                "index": self.index,
                "chunk_id": chunk_id,
                "node_count": len(self.nodes),
                "byte_count": self.byte_count,
                "max_nodes": self.max_nodes,
                "max_bytes": self.max_bytes,
                "oversized": oversized,
                "first_path": first_path,
                "last_path": last_path,
            },
            "provenance": {
                "source_artifacts": [
                    {
                        "artifact_type": "filesystem_scan",
                        "run_id": self.run_id,
                        "content_hash": trace.hash_bytes(f"{self.root}:{self.run_id}".encode()),
                    }
                ]
            },
            "nodes": self.nodes,
        }
        filename = f"chunk-{self.index:06d}-{chunk_id[7:19]}.json"
        if self.write:
            write_validated(chunk, "tree_snapshot", self.out_dir / filename)
        self.chunks.append(
            {
                "index": self.index,
                "chunk_id": chunk_id,
                "path": filename,
                "node_count": len(self.nodes),
                "byte_count": self.byte_count,
                "content_hash": trace.hash_json_artifact(chunk),
                "first_path": first_path,
                "last_path": last_path,
                "oversized": oversized,
                "scopes": ["."],
            }
        )
        self.nodes = []
        self.byte_count = 0


def scan_tree_to_chunks(
    root_path: str,
    run_id: str,
    out_dir: str | Path,
    *,
    excludes: set[str] | None = None,
    progress: Progress | None = None,
    hash_mode: str = "full",
    max_hash_size: int = DEFAULT_MAX_HASH_SIZE,
    cache_path: str | Path | None = None,
    cache_enabled: bool = True,
    skip_cloud_only: bool = False,
    max_nodes: int = DEFAULT_MAX_NODES,
    max_bytes: int = DEFAULT_MAX_BYTES,
    write: bool = True,
) -> dict[str, Any]:
    """Scan ``root_path`` and atomically write independently classifiable chunks."""

    if hash_mode not in HASH_MODES:
        raise ValueError(f"hash_mode must be one of {sorted(HASH_MODES)}")
    if max_hash_size < 0:
        raise ValueError("max_hash_size must be non-negative")
    if max_nodes < 1 or max_bytes < 1:
        raise ValueError("max_nodes and max_bytes must be at least 1")
    root = Path(root_path).resolve()
    if not root.is_dir():
        raise ValueError(f"scan root does not exist or is not a directory: {root_path}")
    destination = Path(out_dir)
    if write:
        destination.mkdir(parents=True, exist_ok=True)
    excludes = excludes if excludes is not None else DEFAULT_EXCLUDES
    reporter = progress or Progress(quiet=True)
    cache = (
        None
        if hash_mode == "none" or not cache_enabled
        else HashCache(cache_path or root / ".nodecraft" / "hash-cache.json")
    )
    if cache:
        cache.load()
    generated_at = trace.now_iso()
    writer = _ChunkWriter(
        root,
        run_id,
        destination,
        generated_at=generated_at,
        max_nodes=max_nodes,
        max_bytes=max_bytes,
        write=write,
    )
    for node in _iter_nodes(
        root,
        excludes,
        hash_mode=hash_mode,
        max_hash_size=max_hash_size,
        cache=cache,
        progress=reporter,
        skip_cloud_only=skip_cloud_only,
    ):
        writer.add(node)
    writer.flush()
    if cache:
        try:
            cache.save()
        except OSError as error:
            print(f"[nodecraft] warning: could not save hash cache: {error}", file=sys.stderr)

    manifest: dict[str, Any] = {
        "schema_version": trace.SCHEMA_VERSION,
        "run_id": run_id,
        "root_path": str(root),
        "generated_at": generated_at,
        "source": {
            "artifact_type": "filesystem_scan",
            "source_id": trace.hash_bytes(f"{root}:{run_id}".encode()),
        },
        "provenance": {
            "source_artifacts": [
                {
                    "artifact_type": "filesystem_scan",
                    "run_id": run_id,
                    "content_hash": trace.hash_bytes(f"{root}:{run_id}".encode()),
                }
            ]
        },
        "chunking": {
            "strategy": "dfs",
            "max_nodes": max_nodes,
            "max_bytes": max_bytes,
        },
        "totals": {
            "node_count": sum(chunk["node_count"] for chunk in writer.chunks),
            "byte_count": sum(chunk["byte_count"] for chunk in writer.chunks),
        },
        "status": {
            "classification": "pending",
            "total_chunks": len(writer.chunks),
            "completed_chunks": 0,
            "failed_chunks": 0,
            "updated_at": trace.now_iso(),
        },
        "chunks": writer.chunks,
    }
    if write:
        write_validated(manifest, "chunk_manifest", destination / "manifest.json")
    return manifest


# Explicit name for callers that want to emphasize that this is the primary
# streaming path.
scan_to_chunks = scan_tree_to_chunks
scan_tree_streaming = scan_tree_to_chunks


def scan_tree(
    root_path: str,
    excludes: set[str] | None = None,
    progress: Progress | None = None,
    hash_mode: str = "full",
    max_hash_size: int = DEFAULT_MAX_HASH_SIZE,
    cache_path: str | Path | None = None,
    cache_enabled: bool = True,
    skip_cloud_only: bool = False,
) -> list[dict[str, Any]]:
    """In-memory fixture helper; production scans should use chunks."""

    root = Path(root_path).resolve()
    reporter = progress or Progress(quiet=True)
    cache = (
        None
        if hash_mode == "none" or not cache_enabled
        else HashCache(cache_path or root / ".nodecraft" / "hash-cache.json")
    )
    if cache:
        cache.load()
    nodes = list(
        _iter_nodes(
            root,
            excludes if excludes is not None else DEFAULT_EXCLUDES,
            hash_mode=hash_mode,
            max_hash_size=max_hash_size,
            cache=cache,
            progress=reporter,
            skip_cloud_only=skip_cloud_only,
        )
    )
    if cache:
        cache.save()
    nodes.sort(key=lambda node: node["path"])
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
    skip_cloud_only: bool = False,
) -> dict[str, Any]:
    """Build a snapshot for small callers; this is not the CLI scan path."""

    root = Path(root_path).resolve()
    return {
        "schema_version": trace.SCHEMA_VERSION,
        "run_id": run_id,
        "root_path": str(root),
        "generated_at": trace.now_iso(),
        "scope": {"root_path": str(root), "subtree_path": ".", "path_format": "posix"},
        "origin": {"root_path": str(root), "subtree_path": ".", "path_format": "posix"},
        "nodes": scan_tree(
            root_path,
            excludes,
            progress,
            hash_mode,
            max_hash_size,
            cache_path,
            cache_enabled,
            skip_cloud_only,
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="scan a directory into bounded tree chunks")
    parser.add_argument("root_path")
    parser.add_argument("--run-id", default=None, help="reuse an existing run_id")
    parser.add_argument("--out-dir", default=None, help="chunk directory")
    parser.add_argument("--exclude", action="append", default=[])
    parser.add_argument("--dry-run", action="store_true", help="print manifest, do not write")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--hash-mode", choices=sorted(HASH_MODES), default="full")
    parser.add_argument("--max-hash-size", type=parse_size, default=DEFAULT_MAX_HASH_SIZE)
    parser.add_argument("--cache-path", default=None)
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument("--max-nodes", type=int, default=DEFAULT_MAX_NODES)
    parser.add_argument("--max-bytes", type=parse_size, default=DEFAULT_MAX_BYTES)
    parser.add_argument("--skip-cloud-only", action="store_true")
    args = parser.parse_args()

    run_id = args.run_id or trace.new_run_id()
    destination = Path(args.out_dir or f"runs/{run_id}/chunks")
    manifest = scan_tree_to_chunks(
        args.root_path,
        run_id,
        destination,
        excludes=DEFAULT_EXCLUDES | set(args.exclude),
        progress=Progress(args.quiet),
        hash_mode=args.hash_mode,
        max_hash_size=args.max_hash_size,
        cache_path=args.cache_path,
        cache_enabled=not args.no_cache and not args.dry_run,
        skip_cloud_only=args.skip_cloud_only,
        max_nodes=args.max_nodes,
        max_bytes=args.max_bytes,
        write=not args.dry_run,
    )
    if args.dry_run:
        print(json.dumps(manifest, indent=2))
        return
    print(f"run_id={run_id}")
    print(f"wrote {destination / 'manifest.json'} ({len(manifest['chunks'])} chunks)")


if __name__ == "__main__":
    main()
