"""Best-effort, metadata-only file availability detection.

Cloud placeholders on Windows are identified through GetFileAttributesW.  The
call reads filesystem metadata and never opens or recalls file content.
"""

from __future__ import annotations

import ctypes
import os
from pathlib import Path

AVAILABLE = "available"
CLOUD_ONLY = "cloud_only"
UNKNOWN = "unknown"

# Windows FILE_ATTRIBUTE_* values.  These flags are used by OneDrive and
# other cloud files providers for files whose content must be recalled.
FILE_ATTRIBUTE_OFFLINE = 0x00001000
FILE_ATTRIBUTE_RECALL_ON_OPEN = 0x00040000
FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x00400000
INVALID_FILE_ATTRIBUTES = 0xFFFFFFFF


def _windows_attributes(path: str | Path) -> int | None:
    if os.name != "nt":
        return None
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        get_attributes = kernel32.GetFileAttributesW
        get_attributes.argtypes = [ctypes.c_wchar_p]
        get_attributes.restype = ctypes.c_uint32
        attributes = get_attributes(str(path))
    except (AttributeError, OSError):
        return None
    if attributes == INVALID_FILE_ATTRIBUTES:
        return None
    return int(attributes)


def detect_availability(path: str | Path) -> str:
    """Return ``available``, ``cloud_only``, or ``unknown``.

    Non-Windows platforms intentionally return ``available`` so scanning
    behavior remains unchanged there.
    """

    if os.name != "nt":
        return AVAILABLE
    attributes = _windows_attributes(path)
    if attributes is None:
        return UNKNOWN
    if attributes & (
        FILE_ATTRIBUTE_OFFLINE
        | FILE_ATTRIBUTE_RECALL_ON_OPEN
        | FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS
    ):
        return CLOUD_ONLY
    return AVAILABLE


def is_cloud_only(path: str | Path) -> bool:
    return detect_availability(path) == CLOUD_ONLY
