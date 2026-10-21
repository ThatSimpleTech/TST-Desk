"""Language servers for diagnostics and code navigation (TD-5139).

:mod:`.client` speaks LSP to one server over stdio, :mod:`.pool` keeps
the running ones, :mod:`.diagnostics` reports problems after a write, and
:mod:`tstd.tools.lsp` holds the definition, references and symbols tools.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from ..lsp_config import LspConfig, LspServerConfig
from .client import LspClient, LspError
from .pool import LspPool, LspStartFailed, default_pool

__all__ = [
    "LspClient",
    "LspError",
    "LspPool",
    "LspStartFailed",
    "Target",
    "default_pool",
    "lsp_config",
    "relative",
    "session_root",
    "target_for",
]


@dataclass(frozen=True)
class Target:
    """A file, the server for it, and where it sits in the workspace."""

    config: LspConfig
    server: LspServerConfig
    root: Path
    path: Path
    rel: str


def lsp_config(session: object) -> LspConfig | None:
    """The session's ``lsp`` block, or None when it names no server."""
    config = getattr(getattr(session, "config", None), "lsp", None)
    if isinstance(config, LspConfig) and config.servers:
        return config
    return None


async def session_root(session: object) -> Path | None:
    """The session's workspace, resolved (written paths are canonical)."""
    raw = getattr(session, "workspace_path", None)
    if not isinstance(raw, str) or not raw:
        return None
    return await asyncio.to_thread(Path(raw).resolve)


def relative(path: Path, root: Path) -> str | None:
    """*path* from *root*, or None when it is outside."""
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return None


async def target_for(session: object, path: Path) -> Target | None:
    """The configured server for *path* in *session*, or None.

    An autonomous session gets None: its commands run only in its
    container, and a language server has no container path yet, so it
    does not run at all rather than run on the host.
    """
    if getattr(session, "autonomy", False):
        return None
    config = lsp_config(session)
    root = await session_root(session)
    if config is None or root is None:
        return None
    resolved = await asyncio.to_thread(path.resolve)
    rel = relative(resolved, root)
    if rel is None:
        return None
    server = config.server_for(rel)
    if server is None:
        return None
    return Target(config=config, server=server, root=root, path=resolved, rel=rel)
