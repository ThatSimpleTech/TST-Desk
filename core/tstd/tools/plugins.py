"""In-process tool plugins via Python entry points (TD-4601).

Third-party packages expose ``register(registry, dispatcher)`` on the
``tstd.tools`` group. Loading is fail-closed on license: only a short
permissive allowlist is accepted; GPL/AGPL/LGPL/Proprietary/empty/unknown
are logged and skipped, never registered. A broken plugin is the same —
logged, skipped, builtins still work. Name collisions with already
registered tools (builtins) are skipped, not replaced.
"""

from __future__ import annotations

import inspect
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from email.message import Message
from importlib.metadata import PackageNotFoundError, entry_points, metadata
from typing import Any, Protocol
from weakref import WeakKeyDictionary

from ..logging import get_logger
from .dispatch import ToolDispatcher
from .registry import Tool, ToolRegistry

log = get_logger("tstd.tools.plugins")

ENTRY_POINT_GROUP = "tstd.tools"
"""Installable plugins declare ``register`` callables in this group."""

PERMISSIVE_LICENSES: frozenset[str] = frozenset(
    {
        "MIT",
        "Apache-2.0",
        "BSD-2-Clause",
        "BSD-3-Clause",
        "ISC",
        "Unlicense",
        "0BSD",
        "CC0-1.0",
    }
)
_PERMISSIVE_FOLD = frozenset(item.casefold() for item in PERMISSIVE_LICENSES)

# Simple ``License`` field spellings mapped onto the SPDX allowlist.
_SIMPLE_ALIASES: dict[str, str] = {
    "mit": "mit",
    "mit license": "mit",
    "apache-2.0": "apache-2.0",
    "apache 2.0": "apache-2.0",
    "apache license 2.0": "apache-2.0",
    "bsd-2-clause": "bsd-2-clause",
    "bsd 2-clause": "bsd-2-clause",
    "bsd-3-clause": "bsd-3-clause",
    "bsd 3-clause": "bsd-3-clause",
    "isc": "isc",
    "unlicense": "unlicense",
    "0bsd": "0bsd",
    "cc0-1.0": "cc0-1.0",
    "cc0": "cc0-1.0",
}

_IDENT = re.compile(r"[A-Za-z0-9.+-]+")
_HAS_OP = re.compile(r"\b(and|or|with)\b", re.IGNORECASE)

Handler = Callable[..., Awaitable[str]]
_PENDING_HANDLERS: WeakKeyDictionary[ToolRegistry, dict[str, Handler]] = WeakKeyDictionary()


class PluginEntry(Protocol):
    """Subset of ``importlib.metadata.EntryPoint`` tests can fake."""

    @property
    def name(self) -> str: ...

    @property
    def dist(self) -> object | None: ...

    def load(self) -> object: ...


@dataclass(frozen=True)
class PluginSkip:
    """One plugin or tool that was not registered."""

    name: str
    dist: str
    reason: str
    detail: str = ""


@dataclass(frozen=True)
class PluginLoadResult:
    """What ``load_plugins`` actually registered and refused."""

    loaded: tuple[str, ...]
    skipped: tuple[PluginSkip, ...]


class _HandlerBuffer:
    """Stash handlers when ``create_registry`` runs before a dispatcher exists."""

    def __init__(self) -> None:
        self.handlers: dict[str, Handler] = {}

    def register_handler(self, name: str, handler: Handler) -> None:
        self.handlers[name] = handler


def license_is_permissive(text: str) -> bool:
    """Whether *text* is only allowlisted SPDX identifiers (TD-4601).

    A bare id matches case-insensitively (plus a few ``License:`` aliases).
    SPDX expressions pass when every identifier is allowlisted and they are
    joined only by ``AND`` / ``OR``. ``WITH`` exceptions, GPL/AGPL/LGPL,
    Proprietary, empty, and anything else fail closed.
    """
    raw = " ".join(text.split())
    if not raw:
        return False
    if _HAS_OP.search(raw) is not None or "(" in raw or ")" in raw:
        return _spdx_only_permissive(raw)
    folded = raw.casefold()
    if folded in _SIMPLE_ALIASES:
        return True
    return folded in _PERMISSIVE_FOLD


def iter_plugin_entries() -> list[PluginEntry]:
    """Installed ``tstd.tools`` entry points. Tests monkeypatch this."""
    return list(entry_points(group=ENTRY_POINT_GROUP))


def load_plugins(
    registry: ToolRegistry,
    dispatcher: ToolDispatcher | None = None,
    *,
    entries: Sequence[PluginEntry] | None = None,
) -> PluginLoadResult:
    """Discover, license-check, and register in-process tool plugins.

    When *dispatcher* is omitted, handlers are captured and later bound
    with :func:`bind_plugin_handlers` (``create_registry`` runs before a
    session dispatcher exists). *entries* injects fakes so tests do not
    need an installed wheel.
    """
    loaded: list[str] = []
    skipped: list[PluginSkip] = []
    buffer = _handler_buffer(registry)
    sink = dispatcher if dispatcher is not None else _HandlerBuffer()

    for entry in entries if entries is not None else iter_plugin_entries():
        _load_one(entry, registry, sink, buffer, loaded, skipped)

    if isinstance(sink, _HandlerBuffer):
        buffer.update(sink.handlers)
    return PluginLoadResult(loaded=tuple(loaded), skipped=tuple(skipped))


def bind_plugin_handlers(registry: ToolRegistry, dispatcher: ToolDispatcher) -> None:
    """Attach handlers captured during :func:`load_plugins` to *dispatcher*.

    Only names whose provenance is ``plugin:`` are bound, so a plugin cannot
    replace a builtin handler. Called from the daemon next to MCP attach.
    """
    buffer = _PENDING_HANDLERS.get(registry)
    if not buffer:
        return
    for name, handler in list(buffer.items()):
        tool = registry.get(name)
        if tool is None or tool.provenance is None:
            continue
        if not tool.provenance.startswith("plugin:"):
            continue
        dispatcher.register_handler(name, handler)


def _handler_buffer(registry: ToolRegistry) -> dict[str, Handler]:
    buf = _PENDING_HANDLERS.get(registry)
    if buf is None:
        buf = {}
        _PENDING_HANDLERS[registry] = buf
    return buf


def _load_one(
    entry: PluginEntry,
    registry: ToolRegistry,
    sink: ToolDispatcher | _HandlerBuffer,
    buffer: dict[str, Handler],
    loaded: list[str],
    skipped: list[PluginSkip],
) -> None:
    dist_obj = getattr(entry, "dist", None)
    dist_name = _dist_name(dist_obj, entry.name)
    license_text = _license_text(dist_obj)
    if not license_is_permissive(license_text):
        skip = PluginSkip(
            name=entry.name,
            dist=dist_name,
            reason="plugin_license",
            detail=license_text or "(empty)",
        )
        skipped.append(skip)
        log.warning(
            "refusing non-permissive tool plugin",
            extra={
                "extra_fields": {
                    "reason": "plugin_license",
                    "license_class": "non_permissive",
                    "plugin": dist_name,
                    "entry": entry.name,
                    "license": skip.detail,
                }
            },
        )
        return

    view = _RegistryView(registry, dist_name)
    guarded = _DispatcherView(sink, view, buffer, dist_name)
    try:
        loaded_obj = entry.load()
        if not callable(loaded_obj):
            raise TypeError(f"entry point {entry.name!r} did not return a callable")
        result = loaded_obj(view, guarded)
        if inspect.isawaitable(result):
            raise TypeError("register() must be synchronous")
    except Exception as exc:
        skipped.append(
            PluginSkip(
                name=entry.name,
                dist=dist_name,
                reason="broken",
                detail=str(exc) or exc.__class__.__name__,
            )
        )
        log.warning(
            "skipping broken tool plugin",
            extra={
                "extra_fields": {
                    "reason": "broken",
                    "plugin": dist_name,
                    "entry": entry.name,
                    "error": str(exc) or exc.__class__.__name__,
                }
            },
            exc_info=True,
        )
        return
    loaded.extend(view.accepted)
    skipped.extend(view.skips)


def _dist_name(dist: object | None, fallback: str) -> str:
    if dist is not None:
        raw = getattr(dist, "name", None)
        if isinstance(raw, str) and raw:
            return raw
    return fallback


def _license_text(dist: object | None) -> str:
    if dist is None:
        return ""
    meta = _distribution_metadata(dist)
    expr = str(meta.get("License-Expression") or "").strip()
    if expr:
        return expr
    return str(meta.get("License") or "").strip()


def _distribution_metadata(dist: object) -> Any:
    name = getattr(dist, "name", None)
    if isinstance(name, str) and name:
        try:
            return metadata(name)
        except PackageNotFoundError:
            pass
    meta = getattr(dist, "metadata", None)
    if isinstance(meta, Message):
        return meta
    return Message()


def _spdx_only_permissive(raw: str) -> bool:
    if re.search(r"\bwith\b", raw, re.IGNORECASE) is not None:
        return False
    idents = [match.group(0).casefold() for match in _IDENT.finditer(raw)]
    idents = [ident for ident in idents if ident not in {"and", "or", "with"}]
    if not idents:
        return False
    return all(ident in _PERMISSIVE_FOLD for ident in idents)


class _RegistryView:
    """``Tool.register`` that will not replace an existing name."""

    def __init__(self, inner: ToolRegistry, dist: str) -> None:
        self._inner = inner
        self._dist = dist
        self.accepted: list[str] = []
        self.skips: list[PluginSkip] = []

    def register(self, tool: Tool) -> None:
        if self._inner.get(tool.name) is not None:
            self.skips.append(
                PluginSkip(
                    name=tool.name,
                    dist=self._dist,
                    reason="name_collision",
                    detail="builtin or already registered",
                )
            )
            log.warning(
                "plugin tool name collides with an already-registered tool; skipped",
                extra={
                    "extra_fields": {
                        "reason": "name_collision",
                        "plugin": self._dist,
                        "tool": tool.name,
                    }
                },
            )
            return
        stamped = tool
        if tool.provenance is None or not tool.provenance.startswith("plugin:"):
            stamped = replace(tool, provenance=f"plugin:{self._dist}")
        self._inner.register(stamped)
        self.accepted.append(stamped.name)

    def get(self, name: str) -> Tool | None:
        return self._inner.get(name)

    def require(self, name: str) -> Tool:
        return self._inner.require(name)

    def list_tools(self) -> list[Tool]:
        return self._inner.list_tools()

    @property
    def count(self) -> int:
        return self._inner.count


class _DispatcherView:
    """``register_handler`` that only binds tools this plugin actually added."""

    def __init__(
        self,
        inner: ToolDispatcher | _HandlerBuffer,
        view: _RegistryView,
        buffer: dict[str, Handler],
        dist: str,
    ) -> None:
        self._inner = inner
        self._view = view
        self._buffer = buffer
        self._dist = dist

    def register_handler(self, name: str, handler: Handler) -> None:
        if name not in self._view.accepted:
            log.warning(
                "plugin handler skipped because the tool was not registered",
                extra={
                    "extra_fields": {
                        "reason": "name_collision",
                        "plugin": self._dist,
                        "tool": name,
                    }
                },
            )
            return
        self._buffer[name] = handler
        self._inner.register_handler(name, handler)
