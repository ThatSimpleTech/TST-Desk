"""Logging setup with a redaction chokepoint.

Two invariants: (1) logs go to **stderr** only — stdout is the MCP protocol
channel and must stay clean; (2) long base64 runs (screenshot image data) are
scrubbed so image bytes never land in a log. Typed text is never logged in the
first place (see ``input_control.type_text``); this filter is defense in depth.
"""

from __future__ import annotations

import logging
import re
import sys

LOGGER_NAME = "tst_cu_mcp"
_BASE64_RUN = re.compile(r"[A-Za-z0-9+/]{120,}={0,2}")


def redact(text: str) -> str:
    """Replace long base64 runs (e.g. image data) with a short placeholder."""
    return _BASE64_RUN.sub(lambda m: f"[redacted {len(m.group(0))} base64 chars]", text)


class RedactionFilter(logging.Filter):
    """Format each record and scrub sensitive blobs before it reaches a handler."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage())
        record.args = None
        return True


def configure_logging(level: int = logging.WARNING) -> None:
    """Attach a stderr handler with redaction to the package logger."""
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(level)
    handler = logging.StreamHandler(sys.stderr)
    handler.addFilter(RedactionFilter())
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logger.handlers.clear()
    logger.addHandler(handler)
    logger.propagate = False
