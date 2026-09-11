"""App identity and access policy for background computer-use.

Coordinate-driven tools aim at a point. Background tools aim at an *app*.
Matching and the allow/deny lists live here so they are testable without a
desktop, and so every tool that can see or drive an app goes through one
gate.

``allowed_apps`` empty means "all apps except denied". A non-empty list is
an allowlist: anything not on it is refused. Denied always wins.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class AppAccessError(RuntimeError):
    """Raised when a background action names an app the policy refuses."""


class AppNotFoundError(RuntimeError):
    """Raised when no running app matches the caller's needle."""


@dataclass(frozen=True)
class AppInfo:
    """One running application, as the OS reports it.

    ``bundle_id`` is the stable identifier (``com.apple.Safari``). ``name``
    is the localized display name. Either may be empty on a platform that
    does not expose it; matching tries both plus pid.
    """

    name: str
    pid: int
    bundle_id: str = ""
    hidden: bool = False
    active: bool = False
    windows: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "pid": self.pid,
            "bundle_id": self.bundle_id,
            "hidden": self.hidden,
            "active": self.active,
            "windows": list(self.windows),
        }

    def describe(self) -> str:
        if self.name and self.bundle_id:
            return f"{self.name} ({self.bundle_id}, pid {self.pid})"
        if self.name:
            return f"{self.name} (pid {self.pid})"
        if self.bundle_id:
            return f"{self.bundle_id} (pid {self.pid})"
        return f"pid {self.pid}"


def needle_matches(needle: str, app: AppInfo) -> bool:
    """True if *needle* identifies *app*.

    A digit-only needle is a pid. Otherwise a case-insensitive substring
    against the display name or bundle id. Substring so ``safari`` matches
    ``Safari`` and ``com.apple.Safari``. An empty needle matches nothing:
    that is almost certainly an unset variable, not "every app".
    """
    raw = needle.strip()
    if not raw:
        return False
    if raw.isdigit():
        return int(raw) == app.pid
    folded = raw.casefold()
    return folded in app.name.casefold() or folded in app.bundle_id.casefold()


def first_match(needle: str, apps: list[AppInfo]) -> AppInfo | None:
    """The first app *needle* identifies, preferring an exact name/bundle hit."""
    raw = needle.strip()
    if not raw:
        return None
    hits = [app for app in apps if needle_matches(raw, app)]
    if not hits:
        return None
    folded = raw.casefold()
    for app in hits:
        if app.bundle_id.casefold() == folded or app.name.casefold() == folded:
            return app
    return hits[0]


def is_denied(app: AppInfo, denied: tuple[str, ...]) -> bool:
    return any(needle_matches(item, app) for item in denied)


def is_allowed(app: AppInfo, allowed: tuple[str, ...]) -> bool:
    if not allowed:
        return True
    return any(needle_matches(item, app) for item in allowed)


def assert_access(
    app: AppInfo,
    *,
    allowed: tuple[str, ...] = (),
    denied: tuple[str, ...] = (),
) -> None:
    """Refuse a denied or not-allowlisted app. Does not actuate."""
    if is_denied(app, denied):
        raise AppAccessError(
            f"{app.describe()} is on the denied-apps list. The request was "
            "rejected. Remove it from denied apps in Settings to allow access. "
            "Actions in an allowed app may still affect it indirectly."
        )
    if not is_allowed(app, allowed):
        raise AppAccessError(
            f"{app.describe()} is not on the allowed-apps list. The request "
            "was rejected. Allowed apps is an allowlist while it is non-empty."
        )


def visible_apps(
    apps: list[AppInfo],
    *,
    allowed: tuple[str, ...] = (),
    denied: tuple[str, ...] = (),
) -> list[AppInfo]:
    """Running apps the model may see. Denied apps are omitted, not named."""
    return [app for app in apps if is_allowed(app, allowed) and not is_denied(app, denied)]
