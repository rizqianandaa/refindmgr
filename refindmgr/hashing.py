"""One SHA-256 helper for the whole project.

Five near-identical implementations used to live in cli, os_inventory,
boot_diagnostics, firmware_compat and system.  Only one of them swallowed
``OSError``, so call sites that looked identical had different failure
semantics -- an unreadable ESP file raised in one module and returned ``None``
in another.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Optional, Union

CHUNK_BYTES = 1024 * 1024


def sha256_file(path: Union[str, Path]) -> str:
    """Hash a file, propagating OSError to the caller."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_file_or_none(path: Union[str, Path]) -> Optional[str]:
    """Hash a file, returning None when it cannot be read."""
    try:
        return sha256_file(path)
    except OSError:
        return None


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
