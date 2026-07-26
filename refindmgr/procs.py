"""One place to run an external command safely.

``boot_diagnostics`` and ``boot_recovery`` each carried a byte-identical
``_run`` differing only in the exception type they raised, which meant the
missing-``timeout`` defect had to be fixed twice.  Both now delegate here.

The timeout is not optional.  These wrappers drive ``efibootmgr``, ``lsblk``,
``mount`` and ``umount``; without it, a damaged FAT partition or a broken
``efivarfs`` hangs the process forever, including inside the ``finally`` block
that is supposed to unmount.
"""
from __future__ import annotations

import subprocess
from typing import Callable, Sequence, Type

RunFn = Callable[..., subprocess.CompletedProcess]

DEFAULT_TIMEOUT_SECONDS = 20


def run_command(
    command: Sequence[str],
    error_type: Type[Exception],
    run_fn: RunFn = subprocess.run,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess:
    """Run ``command`` and convert failures into ``error_type``."""
    try:
        return run_fn(list(command), capture_output=True, text=True, check=False, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise error_type(
            f"Tidak dapat menjalankan {command[0]}: batas waktu {timeout} detik terlampaui"
        ) from exc
    except OSError as exc:
        raise error_type(f"Tidak dapat menjalankan {command[0]}: {exc}") from exc
