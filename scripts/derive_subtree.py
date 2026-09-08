#!/usr/bin/env python3
"""Create portable tree artifacts from a selected subtree.

This is the preferred entry point for derivation.  The legacy
``summarize_subtree`` module remains an import and CLI compatibility alias.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.summarize_subtree import (
    build_subtree_classification,
    build_subtree_report,
    build_subtree_snapshot,
    derive_subtree,
    main,
)

__all__ = [
    "build_subtree_classification",
    "build_subtree_report",
    "build_subtree_snapshot",
    "derive_subtree",
    "main",
]


if __name__ == "__main__":
    main()
