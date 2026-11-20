"""What ``show_artifact`` puts in the Artifacts pane (TD-5254).

Two doors, the two the store has always had. ``path`` points at a text
file in the workspace: no copy is made, so the pane shows the file as it
is now. ``content`` is text the model hands over, kept in the session's
own data dir under a name the daemon makes; the model's ``name`` is a
label that picks the preview, never a place on disk.

Text only. A file that is not UTF-8, too big, a folder or not a regular
file is refused, and the refusal points at ``send_file``, which is the
tool for handing over a file as it is.
"""

from __future__ import annotations

import stat
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from .artifacts import ArtifactError, ArtifactRecord, ArtifactStore
from .autonomy.classifier import Boundary
from .sent_files import mime_for, safe_file_name
from .tools.boundary import PathGuard, RefusalError

if TYPE_CHECKING:
    from .config import SessionConfig

_OCTET = "application/octet-stream"
_NOT_TEXT = "show_artifact shows text. To hand over a file as it is, use send_file."


class ShownArtifacts:
    """Validates and records one shown artifact."""

    def __init__(self, store: ArtifactStore, config: Callable[[], SessionConfig]) -> None:
        self._store = store
        self._config = config
        self._guard: Callable[[], PathGuard | None] | None = None

    def bind_guard(self, guard: Callable[[], PathGuard | None]) -> None:
        """Re-check a path with the dispatcher's own guard, read at each call."""
        self._guard = guard

    def show(
        self,
        session_id: str,
        workspace: Path,
        *,
        title: str,
        path: str | None = None,
        content: str | None = None,
        name: str | None = None,
        caption: str | None = None,
        tool_call_id: str = "",
    ) -> ArtifactRecord:
        """Record the artifact. ArtifactError names what is wrong with the ask."""
        config = self._config()
        limit = config.artifact_max_bytes
        if (path is None) == (content is None):
            raise ArtifactError("bad_request", "Give path or content, not both and not neither.")
        if content is not None:
            if not name or not name.strip():
                raise ArtifactError(
                    "bad_request", "Give name with content, such as summary.md or chart.html."
                )
            data = content.encode("utf-8", errors="strict")
            if len(data) > limit:
                raise ArtifactError("artifact_too_large", _too_large(len(data), limit))
            shown_name = safe_file_name(name)
            return self._store.record(
                session_id,
                title,
                _text_mime(shown_name),
                workspace,
                content=data,
                name=shown_name,
                caption=caption or None,
                tool_call_id=tool_call_id or None,
                upsert=True,
                max_count=config.artifact_max_count,
                max_session_bytes=config.send_file_session_max_bytes,
            )
        assert path is not None
        checked = self._checked(path, workspace)
        self._require_text(checked, limit)
        return self._store.record(
            session_id,
            title,
            _text_mime(checked.name),
            workspace,
            path=str(checked),
            name=safe_file_name(checked.name),
            caption=caption or None,
            tool_call_id=tool_call_id or None,
            upsert=True,
            max_count=config.artifact_max_count,
        )

    def _checked(self, raw: str, workspace: Path) -> Path:
        guard = self._guard() if self._guard is not None else None
        if guard is None:
            guard = PathGuard(Boundary(workspace_root=workspace))
        try:
            return guard.check_read(raw)
        except RefusalError as exc:
            raise ArtifactError(exc.code, exc.reason) from exc

    @staticmethod
    def _require_text(path: Path, limit: int) -> None:
        try:
            info = path.lstat()
        except OSError:
            raise ArtifactError("artifact_not_found", f"No such file: {path.name}") from None
        if stat.S_ISDIR(info.st_mode):
            raise ArtifactError("not_a_file", f"{path.name} is a folder. {_NOT_TEXT}")
        if not stat.S_ISREG(info.st_mode):
            raise ArtifactError("not_a_file", f"{path.name} is not a regular file. {_NOT_TEXT}")
        if info.st_size > limit:
            raise ArtifactError("artifact_too_large", _too_large(info.st_size, limit))
        try:
            with path.open("rb") as handle:
                data = handle.read(limit + 1)
        except OSError as exc:
            raise ArtifactError("artifact_not_found", f"Could not read {path.name}: {exc}") from exc
        if len(data) > limit:
            raise ArtifactError("artifact_too_large", _too_large(len(data), limit))
        try:
            data.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            raise ArtifactError("not_text", f"{path.name} is not text. {_NOT_TEXT}") from None


def _text_mime(name: str) -> str:
    """The table's type for *name*; text that the table does not know is plain text."""
    mime = mime_for(name)
    return "text/plain" if mime == _OCTET else mime


def _too_large(size: int, limit: int) -> str:
    return (
        f"That is {size} bytes; an artifact holds at most {limit}. "
        "Show a shorter piece, or use send_file for the whole file."
    )
