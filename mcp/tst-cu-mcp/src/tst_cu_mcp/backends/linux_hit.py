"""Observe-only Linux hit-test: AT-SPI first, then the EWMH window.

OS handles stay inside the functions so this module imports on macOS and
Windows. A miss is an empty dict, not a pointer move.
"""

from __future__ import annotations

from typing import Any


def observe_at(x: float, y: float) -> dict[str, Any]:
    """Accessibility node at a global pixel, or the window that owns it."""
    atspi = _atspi_at(x, y)
    if atspi.get("role"):
        return atspi
    try:
        from tst_cu_mcp.backends import linux_x11

        return linux_x11.window_at_point(x, y)
    except RuntimeError:
        return {}


def _atspi_at(x: float, y: float) -> dict[str, Any]:
    """Walk AT-SPI desktops for the accessible at a screen point."""
    try:
        import gi

        gi.require_version("Atspi", "2.0")
        from gi.repository import Atspi
    except (ImportError, ValueError, AttributeError):
        return {}

    try:
        desktop = Atspi.get_desktop(0)
    except (TypeError, ValueError, OSError, RuntimeError):
        return {}
    if desktop is None:
        return {}

    try:
        count = int(desktop.get_child_count())
    except (TypeError, ValueError, OSError, RuntimeError):
        return {}

    for index in range(count):
        try:
            app = desktop.get_child_at_index(index)
        except (TypeError, ValueError, OSError, RuntimeError):
            continue
        if app is None:
            continue
        found = _atspi_in_tree(app, x, y)
        if found.get("role"):
            return found
    return {}


def _atspi_in_tree(acc: Any, x: float, y: float) -> dict[str, Any]:
    try:
        from gi.repository import Atspi
    except (ImportError, AttributeError):
        return {}
    try:
        component = acc.query_component()
        child = component.get_accessible_at_point(int(x), int(y), Atspi.CoordType.SCREEN)
    except (TypeError, ValueError, OSError, RuntimeError, AttributeError):
        return {}
    if child is None:
        return {}

    role = ""
    try:
        role = str(child.get_role_name() or "")
    except (TypeError, ValueError, OSError, RuntimeError, AttributeError):
        role = ""

    attributes: dict[str, str] = {}
    try:
        name = str(child.get_name() or "")
    except (TypeError, ValueError, OSError, RuntimeError, AttributeError):
        name = ""
    if name:
        attributes["name"] = name
    try:
        desc = str(child.get_description() or "")
    except (TypeError, ValueError, OSError, RuntimeError, AttributeError):
        desc = ""
    if desc:
        attributes["description"] = desc
    try:
        ident = str(child.get_accessible_id() or "")
    except (TypeError, ValueError, OSError, RuntimeError, AttributeError):
        ident = ""
    if ident:
        attributes["id"] = ident

    box = {"x": float(x), "y": float(y), "width": 1.0, "height": 1.0}
    try:
        extents = child.query_component().get_extents(Atspi.CoordType.SCREEN)
        box = {
            "x": float(extents.x),
            "y": float(extents.y),
            "width": float(extents.width),
            "height": float(extents.height),
        }
    except (TypeError, ValueError, OSError, RuntimeError, AttributeError):
        pass

    return {
        "xpath": None,
        "role": role or None,
        "attributes": attributes,
        "box": box,
        "styles": {},
    }
