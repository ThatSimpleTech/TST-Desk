"""Read-only access to the Grok CLI's home directory.

TST Desk never writes ``~/.grok/auth.json`` or a parallel session store.
This module lists what the CLI already persisted so the window can resume
a TUI session, show MCP/skills, and open ``grok --resume`` in a terminal.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from .desktop.factory import OVERLAY_ENV
from .logging import get_logger

log = get_logger("tstd.grok_home")

_LOCAL_URL = re.compile(
    r"https?://(?:127\.0\.0\.1|localhost|\[::1\])(?::\d+)?(?:/[^\s)\"']*)?",
    re.IGNORECASE,
)

_MEDIA_SUFFIX = {
    ".png": "image",
    ".jpg": "image",
    ".jpeg": "image",
    ".gif": "image",
    ".webp": "image",
    ".svg": "image",
    ".mp4": "video",
    ".webm": "video",
    ".mov": "video",
    ".pdf": "pdf",
    ".html": "html",
    ".htm": "html",
}


def grok_home() -> Path:
    override = os.environ.get("GROK_HOME", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".grok"


def list_grok_sessions(limit: int = 40) -> list[dict[str, str]]:
    """Newest-first summaries from ``~/.grok/sessions``."""
    root = grok_home() / "sessions"
    if not root.is_dir():
        return []
    found: list[tuple[float, dict[str, str]]] = []
    try:
        group_dirs = list(root.iterdir())
    except OSError:
        return []
    for group in group_dirs:
        if not group.is_dir():
            continue
        try:
            children = list(group.iterdir())
        except OSError:
            continue
        for child in children:
            summary_path = child / "summary.json"
            if not summary_path.is_file():
                continue
            row = _read_summary(summary_path, child)
            if row is None:
                continue
            found.append((row[0], row[1]))
    found.sort(key=lambda item: item[0], reverse=True)
    return [item[1] for item in found[:limit]]


def _read_summary(path: Path, session_dir: Path) -> tuple[float, dict[str, str]] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    info = data.get("info") if isinstance(data.get("info"), dict) else {}
    session_id = str(
        data.get("session_id") or info.get("session_id") or session_dir.name  # type: ignore[union-attr]
    )
    title = str(
        data.get("generated_title")
        or data.get("title")
        or data.get("session_summary")
        or session_id[:8]
    )
    cwd = str(info.get("cwd") or data.get("cwd") or "")  # type: ignore[union-attr]
    updated = data.get("updated_at") or data.get("created_at") or 0
    try:
        stamp = float(updated)
    except (TypeError, ValueError):
        stamp = path.stat().st_mtime
    return stamp, {
        "id": session_id,
        "title": title[:200],
        "cwd": cwd,
        "updated_at": str(updated),
    }


def list_grok_extensions() -> list[dict[str, str]]:
    """MCP servers, skills, and plugins visible on disk. Never secrets."""
    items: list[dict[str, str]] = []
    home = grok_home()
    config_path = home / "config.toml"
    if config_path.is_file():
        items.extend(_mcp_from_toml(config_path))
    for scope, folder in (
        ("user", home / "skills"),
        ("bundled", home / "bundled" / "skills"),
    ):
        if not folder.is_dir():
            continue
        try:
            names = sorted(p.name for p in folder.iterdir() if (p / "SKILL.md").is_file())
        except OSError:
            names = []
        for name in names:
            items.append({"kind": "skill", "name": name, "detail": scope})
    plugins = home / "plugins"
    if plugins.is_dir():
        try:
            for child in sorted(plugins.iterdir()):
                if child.is_dir():
                    items.append({"kind": "plugin", "name": child.name, "detail": "user"})
        except OSError:
            pass
    return items


def acp_mcp_servers(
    cu_command: str | list[str] = "",
    *,
    search_from: str = "",
) -> list[dict[str, Any]]:
    """MCP servers to send on ACP ``session/new``.

    Merges ``~/.grok`` stdio servers with TST Desk's computer-use MCP when
    it is configured or found on disk. A packaged sidecar cannot see the
    git checkout via ``__file__``, so we also search the workspace and
    ``~/Documents/TST-Desk``.
    """
    servers = mcp_servers_for_acp()
    names = {str(item.get("name")) for item in servers}
    computer = computer_use_mcp(cu_command, search_from=search_from)
    if computer is not None and computer["name"] not in names:
        servers.append(computer)
    return servers


def mcp_servers_for_acp() -> list[dict[str, Any]]:
    """Stdio MCP servers from ``~/.grok/config.toml``. Never logs env values."""
    path = grok_home() / "config.toml"
    if not path.is_file():
        return []
    try:
        import tomllib
    except ImportError:  # pragma: no cover
        return []
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return []
    raw = data.get("mcp_servers")
    if not isinstance(raw, dict):
        return []
    servers: list[dict[str, Any]] = []
    for name, spec in raw.items():
        if not isinstance(spec, dict) or spec.get("enabled", True) is False:
            continue
        command = str(spec.get("command") or "").strip()
        if not command:
            continue
        args = spec.get("args") or []
        item: dict[str, Any] = {
            "name": str(name),
            "command": command,
            "args": [str(a) for a in args] if isinstance(args, list) else [],
        }
        env = spec.get("env")
        if isinstance(env, dict):
            # ACP wants [{name, value}], not a TOML table.
            item["env"] = [{"name": str(k), "value": str(v)} for k, v in env.items()]
        servers.append(item)
    return servers


def computer_use_mcp(
    cu_command: str | list[str] = "",
    *,
    search_from: str = "",
) -> dict[str, Any] | None:
    """ACP stdio server for ``tst-cu-mcp`` when a binary is available."""
    argv: list[str] = []
    if isinstance(cu_command, list):
        argv = [str(part) for part in cu_command if str(part).strip()]
    elif isinstance(cu_command, str) and cu_command.strip():
        import shlex

        argv = shlex.split(cu_command)
    # A configured command is also the daemon's own driver, which paints
    # the real-display ring from cu_session tags; this child must not.
    configured = bool(argv)
    if not argv:
        override = os.environ.get("TST_CU_MCP", "").strip()
        which = override or shutil.which("tst-cu-mcp")
        if which:
            argv = [which]
        else:
            found = _checkout_cu_mcp(search_from)
            if found is None:
                return None
            argv = [str(found)]
    binary = Path(argv[0]).expanduser()
    if not binary.is_file() or not os.access(binary, os.X_OK):
        return None
    return {
        "name": "computer-use",
        "command": str(binary.resolve()),
        "args": argv[1:],
        "env": _host_env(argv, paint=not configured),
    }


def _host_env(argv: list[str], *, paint: bool = True) -> list[dict[str, str]]:
    env: list[dict[str, str]] = []
    from .cu_host import SOCK_ENV

    sock = os.environ.get(SOCK_ENV, "").strip()
    if sock:
        env.append({"name": SOCK_ENV, "value": sock})
    if argv and Path(argv[0]).name.startswith("tstd") and "--cu-mcp" in argv[1:]:
        env.append({"name": "TST_CU_MCP_HOST", "value": "1"})
    if not paint:
        # Nothing tells this child when a turn ends, so a ring it lit would
        # linger; the daemon's driver paints and closes it instead.
        env.append({"name": OVERLAY_ENV, "value": "0"})
    return env


def _checkout_cu_mcp(search_from: str = "") -> Path | None:
    """Find ``mcp/tst-cu-mcp`` even when this module is inside a sidecar."""
    suffix = Path("mcp") / "tst-cu-mcp" / ".venv" / "bin" / "tst-cu-mcp"
    if sys.platform == "win32":
        suffix = Path("mcp") / "tst-cu-mcp" / ".venv" / "Scripts" / "tst-cu-mcp.exe"
    roots: list[Path] = []
    here = Path(__file__).resolve()
    roots.extend(here.parents[i] for i in range(1, min(6, len(here.parents))))
    if search_from.strip():
        start = Path(search_from.strip()).expanduser()
        roots.append(start)
        roots.extend(start.parents[:4])
    roots.append(Path.cwd())
    roots.extend(Path.cwd().parents[:4])
    roots.append(Path.home() / "Documents" / "TST-Desk")
    roots.append(Path.home() / "TST-Desk")
    seen: set[Path] = set()
    for root in roots:
        try:
            resolved = root.resolve()
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        candidate = resolved / suffix
        if candidate.is_file():
            return candidate
        nested = resolved / "TST-Desk" / suffix
        if nested.is_file():
            return nested
    return None


def _mcp_from_toml(path: Path) -> list[dict[str, str]]:
    try:
        import tomllib
    except ImportError:  # pragma: no cover
        return []
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return []
    servers = data.get("mcp_servers")
    if not isinstance(servers, dict):
        return []
    items: list[dict[str, str]] = []
    for name, spec in servers.items():
        if not isinstance(spec, dict):
            continue
        command = str(spec.get("command") or "")
        enabled = spec.get("enabled", True)
        detail = command if enabled else f"disabled · {command}"
        items.append({"kind": "mcp", "name": str(name), "detail": detail[:200]})
    return items


def open_in_terminal(resume_id: str, cwd: str) -> None:
    """Open the real Grok TUI in the user's terminal. Never a network bind."""
    resume = resume_id.strip()
    if not resume or any(ch < " " or ch == "\x7f" for ch in resume):
        raise ValueError("unusable session id")
    work = cwd.strip() or str(Path.home())
    if sys.platform == "darwin":
        script = f"cd {_quote(work)} && grok --resume {_quote(resume)}"
        subprocess.Popen(
            ["osascript", "-e", f'tell application "Terminal" to do script {_quote(script)}'],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return
    term = os.environ.get("TERMINAL") or "x-terminal-emulator"
    subprocess.Popen(
        [term, "-e", "grok", "--resume", resume],
        cwd=work,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _quote(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


def sniff_local_url(text: str) -> str | None:
    """First loopback http(s) URL in *text*, or None."""
    match = _LOCAL_URL.search(text)
    return match.group(0) if match else None


def media_kind(path: str) -> str | None:
    suffix = Path(path).suffix.lower()
    return _MEDIA_SUFFIX.get(suffix)


def resolve_workspace_file(workspace: str, raw: str) -> Path | None:
    """Return an existing file under *workspace* (or the absolute path if inside)."""
    candidate = Path(raw)
    root = Path(workspace).resolve()
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        resolved = candidate.resolve()
        resolved.relative_to(root)
    except (OSError, ValueError):
        return None
    if resolved.is_file():
        return resolved
    return None
