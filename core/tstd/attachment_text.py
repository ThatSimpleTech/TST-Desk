"""What every document reader shares (TD-5131): the error and the capped sink.

Leaf, like ``attachments.py``: nothing from ``tstd`` is imported.
"""

from __future__ import annotations


class DocumentError(Exception):
    """A document that could not be turned into text, with its wire code."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


class TooLong(Exception):
    """The text passed the cap; extraction stops early."""


class Sink:
    """Collects lines and stops the moment their UTF-8 size passes the cap."""

    def __init__(self, cap: int) -> None:
        self._cap = cap
        self._size = 0
        self._parts: list[str] = []
        self.has_content = False

    def add(self, line: str, *, marker: bool = False) -> None:
        if self._parts:
            self._size += 1  # the newline joining it to the last line
        self._size += len(line.encode("utf-8"))
        if self._size > self._cap:
            raise TooLong
        self._parts.append(line)
        if not marker and line.strip():
            self.has_content = True

    def text(self) -> str:
        return "\n".join(self._parts)


def fmt(count: int) -> str:
    if count < 1000:
        return f"{count} bytes"
    if count < 1_000_000:
        return f"{count / 1000:.1f} KB"
    return f"{count / 1_000_000:.1f} MB"


def unreadable(name: str, what: str) -> DocumentError:
    return DocumentError(
        "attachment_unreadable",
        f"{name} couldn't be read as a {what}: it may be damaged or locked "
        "with a password. Re-save it, or attach it as text. Nothing was sent.",
    )
