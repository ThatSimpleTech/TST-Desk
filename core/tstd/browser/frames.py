"""Persist a browser screenshot under the session dir and emit ``screen_frame``.

Path, not bytes, on the wire. The PNG and a text data-URL sidecar sit in
``sessions/<id>/screens/`` — the same wall ``read_text_file`` already
honours for artifacts (TD-1710).
"""

from __future__ import annotations

import asyncio
import base64
import uuid
from pathlib import Path
from typing import Any

from ..protocol import ScreenFrame
from .protocol import png_size

_SCREENS = "screens"


def persist_dir_of(session: object) -> Path | None:
    """Session persist directory if the daemon attached one."""
    raw = getattr(session, "persist_dir", None)
    if raw is None:
        return None
    return Path(raw)


def _write_pair(dest_dir: Path, png: bytes) -> str:
    dest_dir.mkdir(parents=True, exist_ok=True)
    stem = uuid.uuid4().hex
    png_name = f"{stem}.png"
    (dest_dir / png_name).write_bytes(png)
    dataurl = "data:image/png;base64," + base64.b64encode(png).decode("ascii")
    (dest_dir / f"{stem}.dataurl").write_text(dataurl, encoding="utf-8")
    return f"{_SCREENS}/{png_name}"


async def persist_screen_frame(
    session: object,
    png: bytes,
    *,
    tool_call_id: str | None = None,
) -> dict[str, Any]:
    """Write the PNG, emit ``screen_frame``, return path and size for the tool."""
    width, height = png_size(png)
    persist = persist_dir_of(session)
    path = ""
    if persist is not None:
        path = await asyncio.to_thread(_write_pair, persist / _SCREENS, png)
        log = getattr(session, "event_log", None)
        add = getattr(log, "add", None) if log is not None else None
        session_id = getattr(session, "id", None)
        if add is not None and isinstance(session_id, str):
            await add(
                ScreenFrame(
                    session_id=session_id,
                    path=path,
                    mime="image/png",
                    width=width,
                    height=height,
                    tool_call_id=tool_call_id,
                    seq=1,
                )
            )
    return {"path": path, "width": width, "height": height, "mime": "image/png"}
