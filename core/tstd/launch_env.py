"""Give the user's own environment back to everything the daemon starts (PKG-02).

The Linux AppImage's launcher exports its own packaging: PYTHONHOME and
PYTHONPATH pointing into the image, LD_LIBRARY_PATH and PATH with the image's
folders first, GTK and GStreamer paths, GDK_BACKEND=x11. PyInstaller then
puts its unpack folder at the front of LD_LIBRARY_PATH. The host passed all
of it to the daemon, so every command the agent ran inherited it: the
system python3 died ("No module named encodings"), and git and curl loaded
the image's libraries instead of the system's.

``restore_user_environment`` runs once, first thing in the daemon, and
cleans its own ``os.environ``, so the shell tool, hooks, LSP servers, git
helpers, MCP servers and xdg-open all start from what the user had. The app
itself is already loaded and keeps working.
"""

from __future__ import annotations

import os
import sys
from collections.abc import MutableMapping

#: Lists of paths: only the entries inside the image or the unpack folder go.
PATH_LISTS: tuple[str, ...] = (
    "PATH",
    "LD_LIBRARY_PATH",
    "XDG_DATA_DIRS",
    "PYTHONPATH",
    "PYTHONHOME",
    "PERLLIB",
    "PERL5LIB",
    "QT_PLUGIN_PATH",
    "GTK_PATH",
    "GTK_EXE_PREFIX",
    "GTK_DATA_PREFIX",
    "GTK_IM_MODULE_FILE",
    "GDK_PIXBUF_MODULE_FILE",
    "GIO_EXTRA_MODULES",
    "GSETTINGS_SCHEMA_DIR",
    "GI_TYPELIB_PATH",
    "GST_PLUGIN_PATH",
    "GST_PLUGIN_SYSTEM_PATH",
    "GST_PLUGIN_SYSTEM_PATH_1_0",
    "GST_PLUGIN_SCANNER",
    "GST_PLUGIN_SCANNER_1_0",
)
#: Set by the AppImage's GTK hook with no path in them: dropped under an AppImage.
APPIMAGE_ONLY: tuple[str, ...] = ("GDK_BACKEND", "GTK_THEME")


def _bundle_roots(env: MutableMapping[str, str]) -> list[str]:
    roots = [env.get("APPDIR", ""), getattr(sys, "_MEIPASS", "")]
    return [os.path.normpath(r) for r in roots if r]


def _inside(entry: str, roots: list[str]) -> bool:
    path = os.path.normpath(entry)
    return any(path == root or path.startswith(root + os.sep) for root in roots)


def restore_user_environment(env: MutableMapping[str, str] | None = None) -> list[str]:
    """Clean *env* (``os.environ`` by default); the names changed, for the log."""
    target: MutableMapping[str, str] = os.environ if env is None else env
    roots = _bundle_roots(target)
    changed: list[str] = []
    # PyInstaller keeps what LD_LIBRARY_PATH was before it added its folder.
    original = target.pop("LD_LIBRARY_PATH_ORIG", None)
    if original is not None:
        target["LD_LIBRARY_PATH"] = original
        changed.append("LD_LIBRARY_PATH")
    if not roots:
        return changed
    for name in PATH_LISTS:
        value = target.get(name)
        if value is None:
            continue
        kept = [e for e in value.split(os.pathsep) if e and not _inside(e, roots)]
        joined = os.pathsep.join(kept)
        if joined == value:
            continue
        if kept:
            target[name] = joined
        else:
            del target[name]
        if name not in changed:
            changed.append(name)
    if target.get("APPDIR"):
        for name in APPIMAGE_ONLY:
            if target.pop(name, None) is not None:
                changed.append(name)
    return changed
