"""Structured JSON logging with secrets redaction.

Logs are written as newline-delimited JSON objects to a rotating file.
A logging filter redacts known credential patterns before anything is written.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import re
import sys
from pathlib import Path
from typing import Any

# Patterns that must be redacted in log output (from tst-cua policy.py).
SECRET_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"(sk-[a-zA-Z0-9]{20,})"),  # OpenAI / OpenRouter key
    re.compile(r"(github_pat_[a-zA-Z0-9_]{36,})"),  # GitHub fine-grained PAT
    re.compile(r"(ghp_[a-zA-Z0-9]{36,})"),  # GitHub classic PAT
    re.compile(r"(AKIA[0-9A-Z]{16})"),  # AWS access key
    re.compile(r"(-----BEGIN\s+(RSA |EC |DSA |OPENSSH )?PRIVATE KEY)"),  # Private key
]


def user_data_dir() -> Path:
    """Return the platform-appropriate user data directory for TST Desk."""
    system = sys.platform
    if system == "darwin":
        return Path.home() / "Library" / "Application Support" / "com.thatsimpletech.tstdesk"
    if system == "win32":
        appdata = os.environ.get("APPDATA")
        if appdata:
            return Path(appdata) / "com.thatsimpletech.tstdesk"
        return Path.home() / "AppData" / "Roaming" / "com.thatsimpletech.tstdesk"
    # Linux / BSD
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return Path(xdg) / "tst-desk"
    return Path.home() / ".local" / "share" / "tst-desk"


class SecretsRedactionFilter(logging.Filter):
    """Redact known credential patterns from log records.

    Operates on the formatted message text so it catches secrets in any
    log field regardless of how the record was constructed.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if record.msg:
            for pattern in SECRET_PATTERNS:
                record.msg = pattern.sub("[REDACTED]", record.msg)
        # Also redact args that are strings
        if record.args:
            args = list(record.args)
            for i, arg in enumerate(args):
                if isinstance(arg, str):
                    for pattern in SECRET_PATTERNS:
                        arg = pattern.sub("[REDACTED]", arg)
                    args[i] = arg
            record.args = tuple(args)
        return True


class JSONFormatter(logging.Formatter):
    """Format log records as JSON lines."""

    def format(self, record: logging.LogRecord) -> str:
        obj: dict[str, Any] = {
            "ts": f"{record.created:.3f}",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info and record.exc_info[0]:
            obj["exception"] = self.formatException(record.exc_info)
        # Merge extra fields passed via extra={}
        for key, value in getattr(record, "extra_fields", {}).items():
            obj[key] = value
        return json.dumps(obj, default=str, ensure_ascii=False)


def setup_logging(
    *,
    level: str | int = "INFO",
    log_dir: Path | None = None,
    log_to_stdout: bool = True,
    max_bytes: int = 10 * 1024 * 1024,  # 10 MB
    backup_count: int = 5,
) -> logging.Logger:
    """Configure the root logger with JSON output and secrets redaction.

    Args:
        level: Log level (name or int).
        log_dir: Directory for rotating log files. Defaults to user data dir.
        log_to_stdout: Also emit JSON lines to stdout.
        max_bytes: Max size per log file before rotation.
        backup_count: Number of rotated log files to keep.

    Returns:
        The root logger, already configured.
    """
    if isinstance(level, str):
        level = getattr(logging, level.upper(), logging.INFO)

    log_dir = log_dir or (user_data_dir() / "logs")
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "tstd.log"

    root = logging.getLogger()
    root.setLevel(level)

    # Remove any pre-existing handlers (idempotent)
    root.handlers.clear()

    # Rotating file handler
    file_handler = logging.handlers.RotatingFileHandler(
        log_file,
        maxBytes=max_bytes,
        backupCount=backup_count,
    )
    file_handler.setFormatter(JSONFormatter())
    file_handler.addFilter(SecretsRedactionFilter())
    root.addHandler(file_handler)

    # Optional stdout handler
    if log_to_stdout:
        stdout_handler = logging.StreamHandler(sys.stdout)
        stdout_handler.setFormatter(JSONFormatter())
        stdout_handler.addFilter(SecretsRedactionFilter())
        root.addHandler(stdout_handler)

    return root


def get_logger(name: str) -> logging.Logger:
    """Get a logger with the given name, child of the root."""
    return logging.getLogger(name)
