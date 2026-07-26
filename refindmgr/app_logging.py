"""Private, rotating diagnostics for refindmgr.

Terminal output remains user-facing. This module records technical context in
plain text and never makes logging availability a requirement for boot work.
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import re
from pathlib import Path
from typing import Optional


LOGGER_NAME = "refindmgr"
MAX_BYTES = 2 * 1024 * 1024
BACKUP_COUNT = 3
_CONFIGURED_PATH: Optional[Path] = None


def default_log_path(*, root: Optional[bool] = None) -> Path:
    override = os.environ.get("REFINDMGR_LOG_FILE")
    if override:
        return Path(override).expanduser()
    is_root = (os.geteuid() == 0 if hasattr(os, "geteuid") else False) if root is None else root
    if is_root:
        return Path("/var/log/refindmgr/refindmgr.log")
    state = os.environ.get("XDG_STATE_HOME")
    base = Path(state).expanduser() if state else Path.home() / ".local/state"
    return base / "refindmgr/refindmgr.log"


class RedactingFilter(logging.Filter):
    _credential_url = re.compile(r"(https?://)[^/@\s]+@", re.IGNORECASE)
    _secret_query = re.compile(
        r"([?&](?:token|access_token|key|api_key|password)=)[^&\s]+",
        re.IGNORECASE,
    )
    _email = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)

    @classmethod
    def redact(cls, message: str) -> str:
        message = cls._credential_url.sub(r"\1***@", message)
        message = cls._secret_query.sub(r"\1***", message)
        # 'git@github.com:owner/repo' matches the address pattern but is a
        # clone source. Redacting it destroyed the diagnostic value of the
        # theme-source log lines for no privacy gain.
        message = cls._email.sub(
            lambda match: match.group(0) if match.group(0).lower().startswith("git@github.com")
            else "<email-redacted>",
            message,
        )
        try:
            home = str(Path.home())
        except RuntimeError:
            home = ""
        if home and home != "/":
            message = message.replace(home, "~")
        return message

    def filter(self, record: logging.LogRecord) -> bool:
        """Redact on the record itself, deliberately.

        Redacting only in RedactingFormatter would leave any handler an
        embedder attaches to this logger emitting the raw text, and this logger
        carries theme URLs and filesystem paths. Safety wins over arg fidelity
        here -- but the originals are preserved below so nothing is destroyed
        outright, which is what the previous version did.
        """
        if getattr(record, "refindmgr_redacted", False):
            return True
        record.refindmgr_raw_msg = record.msg
        record.refindmgr_raw_args = record.args
        record.msg = self.redact(record.getMessage())
        record.args = ()
        record.refindmgr_redacted = True
        return True


class RedactingFormatter(logging.Formatter):
    """Redact the fully formatted line, including exception tracebacks."""

    def format(self, record: logging.LogRecord) -> str:
        return RedactingFilter.redact(super().format(record))


class PrivateRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """Keep the current log private after creation and every rollover."""

    def _open(self):
        stream = super()._open()
        # A rollover onto a file we do not own raises EPERM here; losing the
        # stream over a permission tweak is worse than a slightly loose mode.
        try:
            os.chmod(self.baseFilename, 0o600)
        except OSError:
            pass
        return stream


def configure(path: Optional[Path] = None, *, level: int = logging.INFO) -> Optional[Path]:
    """Configure one rotating file handler; failures remain non-fatal."""
    global _CONFIGURED_PATH
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(level)
    logger.propagate = False
    if _CONFIGURED_PATH is not None:
        return _CONFIGURED_PATH
    destination = Path(path) if path is not None else default_log_path()
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        handler = PrivateRotatingFileHandler(
            destination,
            maxBytes=MAX_BYTES,
            backupCount=BACKUP_COUNT,
            encoding="utf-8",
            delay=True,
        )
        handler.setFormatter(RedactingFormatter(
            "%(asctime)s %(levelname)s %(name)s %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S%z",
        ))
        handler.addFilter(RedactingFilter())
        # Create the file BEFORE attaching the handler. Attaching first meant a
        # failing touch/chmod returned None (caller believes logging is off)
        # while the broken handler stayed on the logger, so every later log call
        # printed '--- Logging error ---' to stderr.
        destination.touch(mode=0o600, exist_ok=True)
        os.chmod(destination, 0o600)
        logger.addHandler(handler)
    except OSError:
        try:
            handler.close()
        except (OSError, NameError, UnboundLocalError):
            pass
        return None
    _CONFIGURED_PATH = destination
    return destination


def configured_path() -> Optional[Path]:
    return _CONFIGURED_PATH
