"""The denied apps list holds for the pointer, keyboard and screenshot tools (CU-02).

The Settings pane promised that any request to access a denied app is
rejected, but only the accessibility-tree tools looked at the list. TST
Desk's own engine drives the desktop through desktop_move, _click, _type,
_scroll and _screenshot, and none of them checked it.

Each of those now reads the window in front first. When it belongs to a
denied app (the name in its process or its title), nothing happens and the
call is refused. If the window in front cannot be read while the list has
entries, the call is refused too: the list fails closed.

A pointer action aimed at a denied app's window behind the front one is not
caught here: the OS does not say whose window is under a point without
acting. The pane says what is enforced.
"""

from __future__ import annotations

from typing import Any

from ..cu_policy import load_cu_policy
from ..tools.results import HandlerRefusal
from .settle import window_identity


async def refuse_if_denied(driver: Any) -> None:
    """Raise ``HandlerRefusal`` when a denied app is in front, or that cannot be told."""
    denied = [name.casefold() for name in load_cu_policy().denied_apps if name.strip()]
    if not denied:
        return
    try:
        window = await driver.foreground_window()
    except Exception as exc:
        raise HandlerRefusal(
            "denied_app_unknown",
            "The app in front could not be read, and the denied apps list is not empty, "
            "so nothing was done.",
        ) from exc
    ident = window_identity(window if isinstance(window, dict) else {})
    app, title = ident["app"], ident["title"]
    for name in denied:
        if name in app.casefold() or name in title.casefold():
            shown = app or title
            raise HandlerRefusal(
                "denied_app",
                f"{shown} is on the denied apps list in Settings, so the agent may not "
                "click, type, scroll or take a screenshot while it is in front. Tell the "
                "person; do not try another way.",
            )
