"""Session artifact records (TD-3201).

Artifacts are things the model made for the user — a doc, a preview —
that persist with the session. They are not the Files pane (writes this
session) and not attachments (inbound).

On disk: ``{data_dir}/sessions/{id}/artifacts.json`` plus optional bytes
at ``{data_dir}/sessions/{id}/artifacts/{artifact_id}``. A record may
instead point at a workspace-relative path. Both doors go through the
workspace wall (or the session persist dir); escape and symlink-out are
refused.
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from .autonomy.classifier import Boundary, canonical_path
from .logging import get_logger, redact_secrets
from .protocol import ArtifactEntry
from .session_persist import SessionPersist
from .tools.boundary import PathGuard, RefusalError, windows_unsafe_reason

log = get_logger("tstd.artifacts")

_META = "artifacts.json"
_BYTES_DIR = "artifacts"


class ArtifactError(Exception):
    """A refused or missing artifact. ``code`` is the wire error code."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


class ArtifactRecord(BaseModel):
    """One persisted artifact. ``location`` is on disk only, not on the wire."""

    id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    mime: str = Field(min_length=1)
    path: str = Field(min_length=1)
    title: str = Field(min_length=1)
    location: Literal["workspace", "session"] = "workspace"


def to_entry(record: ArtifactRecord) -> ArtifactEntry:
    """Wire list/open fields. Title and path go through the redaction chokepoint."""
    return ArtifactEntry(
        artifact_id=record.id,
        title=redact_secrets(record.title),
        mime=record.mime,
        path=redact_secrets(record.path),
    )


def _safe_session_id(session_id: str) -> str:
    """Refuse a session id that would leave ``sessions/<id>/``."""
    if not session_id or Path(session_id).name != session_id:
        raise ArtifactError("session_not_found", f"Session {session_id!r} not found")
    return session_id


class ArtifactStore:
    """Read and write one session's artifact index and optional bytes."""

    def __init__(self, persist: SessionPersist) -> None:
        self._persist = persist
        self._lock = threading.Lock()

    def record(
        self,
        session_id: str,
        title: str,
        mime: str,
        workspace: Path,
        *,
        path: str | None = None,
        content: bytes | None = None,
    ) -> ArtifactRecord:
        """Store an artifact under the workspace wall or the session persist dir.

        ``content`` writes bytes to ``artifacts/<id>`` in the session dir.
        ``path`` registers a workspace path (relative) or an absolute path
        already inside the session persist dir. Pass exactly one.
        """
        session_id = _safe_session_id(session_id)
        if (path is None) == (content is None):
            raise ArtifactError("bad_request", "pass path or content, not both")
        if not title.strip():
            raise ArtifactError("bad_request", "title must not be empty")
        if not mime.strip():
            raise ArtifactError("bad_request", "mime must not be empty")

        self._persist.prepare(session_id)
        persist_dir = self._persist.dir_for(session_id)
        artifact_id = uuid.uuid4().hex

        if content is not None:
            dest = persist_dir / _BYTES_DIR / artifact_id
            self._write_bytes(dest, persist_dir, content)
            stored_path = f"{_BYTES_DIR}/{artifact_id}"
            location: Literal["workspace", "session"] = "session"
        else:
            assert path is not None
            location, stored_path = self._classify_path(path, workspace, persist_dir)

        record = ArtifactRecord(
            id=artifact_id,
            session_id=session_id,
            mime=mime.strip(),
            path=stored_path,
            title=title.strip(),
            location=location,
        )
        with self._lock:
            records = self._load(session_id)
            records.append(record)
            self._save(session_id, records)
        return record

    def list_records(self, session_id: str, workspace: Path) -> list[ArtifactRecord]:
        """Records still inside the wall. Escaped paths are omitted, not rewritten."""
        session_id = _safe_session_id(session_id)
        persist_dir = self._persist.dir_for(session_id)
        with self._lock:
            records = self._load(session_id)
        kept: list[ArtifactRecord] = []
        for record in records:
            try:
                self._resolve_stored(record, workspace, persist_dir)
            except ArtifactError:
                continue
            kept.append(record)
        return kept

    def get(self, session_id: str, artifact_id: str, workspace: Path) -> ArtifactRecord:
        """Look up one record. Unknown id and wall escape are typed errors."""
        session_id = _safe_session_id(session_id)
        persist_dir = self._persist.dir_for(session_id)
        with self._lock:
            records = self._load(session_id)
        for record in records:
            if record.id != artifact_id:
                continue
            self._resolve_stored(record, workspace, persist_dir)
            return record
        raise ArtifactError(
            "artifact_not_found",
            f"No artifact {artifact_id!r} in session {session_id!r}",
        )

    def _classify_path(
        self, raw: str, workspace: Path, persist_dir: Path
    ) -> tuple[Literal["workspace", "session"], str]:
        persist_root = canonical_path(persist_dir)
        candidate = Path(raw)
        if candidate.is_absolute():
            unsafe = windows_unsafe_reason(raw)
            if unsafe:
                raise ArtifactError("windows_unsafe", unsafe)
            target = canonical_path(candidate)
            if target == persist_root or persist_root in target.parents:
                if target == persist_root:
                    raise ArtifactError(
                        "outside_workspace",
                        "cannot register the session data dir root",
                    )
                return "session", target.relative_to(persist_root).as_posix()
            return "workspace", self._workspace_relative(raw, workspace)
        return "workspace", self._workspace_relative(raw, workspace)

    def _workspace_relative(self, raw: str, workspace: Path) -> str:
        try:
            checked = PathGuard(Boundary(workspace_root=workspace)).check_read(raw)
        except RefusalError as exc:
            raise ArtifactError(exc.code, exc.reason) from exc
        root = canonical_path(workspace)
        if checked == root:
            raise ArtifactError("outside_workspace", "cannot register the workspace root")
        return checked.relative_to(root).as_posix()

    def _resolve_stored(self, record: ArtifactRecord, workspace: Path, persist_dir: Path) -> Path:
        if record.location == "session":
            persist_root = canonical_path(persist_dir)
            target = canonical_path(persist_root / record.path)
            if target != persist_root and persist_root not in target.parents:
                raise ArtifactError(
                    "outside_workspace",
                    f"path outside the session data dir: {record.path}",
                )
            return target
        try:
            return PathGuard(Boundary(workspace_root=workspace)).check_read(record.path)
        except RefusalError as exc:
            raise ArtifactError(exc.code, exc.reason) from exc

    def _load(self, session_id: str) -> list[ArtifactRecord]:
        path = self._persist.dir_for(session_id) / _META
        if not path.is_file():
            return []
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning(
                "artifacts index unreadable",
                extra={"extra_fields": {"path": str(path), "error": str(exc)}},
            )
            return []
        if not isinstance(raw, list):
            return []
        out: list[ArtifactRecord] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            try:
                out.append(ArtifactRecord.model_validate(item))
            except ValidationError:
                continue
        return out

    def _save(self, session_id: str, records: list[ArtifactRecord]) -> None:
        path = self._persist.dir_for(session_id) / _META
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = [r.model_dump() for r in records]
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            tmp.chmod(0o600)
            os.replace(tmp, path)
            path.chmod(0o600)
        except OSError as exc:
            log.warning(
                "failed to write artifacts index",
                extra={"extra_fields": {"path": str(path), "error": str(exc)}},
            )
            if tmp.exists():
                tmp.unlink(missing_ok=True)
            raise ArtifactError(
                "artifact_store_failed", f"Could not persist artifact: {exc}"
            ) from exc

    @staticmethod
    def _write_bytes(dest: Path, persist_dir: Path, content: bytes) -> None:
        persist_root = canonical_path(persist_dir)
        dest.parent.mkdir(parents=True, exist_ok=True)
        # The id is hex; this is belt-and-braces if that ever changes.
        resolved = canonical_path(dest)
        if resolved != persist_root and persist_root not in resolved.parents:
            raise ArtifactError(
                "outside_workspace",
                f"path outside the session data dir: {dest}",
            )
        tmp = dest.with_name(f".{dest.name}.{os.getpid()}.tmp")
        try:
            tmp.write_bytes(content)
            tmp.chmod(0o600)
            os.replace(tmp, dest)
            dest.chmod(0o600)
        except OSError as exc:
            if tmp.exists():
                tmp.unlink(missing_ok=True)
            raise ArtifactError(
                "artifact_store_failed", f"Could not persist artifact: {exc}"
            ) from exc
