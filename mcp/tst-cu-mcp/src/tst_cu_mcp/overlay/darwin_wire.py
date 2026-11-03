"""The macOS overlay helper's line protocol, free of AppKit (TD-5226, TD-5227).

One command per stdin line, ``name`` then an optional argument (the rest of
the line). The reader thread queues each one and prints ``ok`` or ``err``:
at once for most commands, and only after the main thread has acted for
the grab pair. Kept apart from :mod:`darwin_helper` so it is testable on any
platform and the helper stays small.
"""

from __future__ import annotations

import queue
import sys
import threading

_SHOW, _HIDE, _TARGET, _GRAB_BEGIN, _GRAB_END, _QUIT = (
    "show",
    "hide",
    "target",
    "grab_begin",
    "grab_end",
    "quit",
)
_CURSOR_COMMANDS = frozenset({"cursor", "cursor_hide"})
#: Commands whose ack must wait for the main thread to have acted.
_MAIN_THREAD_ACKS = frozenset({_GRAB_BEGIN, _GRAB_END})
#: Reader-thread patience with the main thread before reporting failure.
_MAIN_ACK_TIMEOUT = 2.5

#: (name, argument, ack). The argument is the rest of the line, or "".
Command = tuple[str, str, threading.Event | None]
PanelKey = tuple[int, str, int, int, int, int]


def lit_keys(keys: list[PanelKey], target: int | None) -> list[PanelKey]:
    """The target display's panel, else the primary display's.

    NSScreen.screens() lists the primary (menu-bar) display first and the
    panel dict keeps that order, so the first key is the fallback.
    """
    chosen = [key for key in keys if key[0] == target]
    return chosen or keys[:1]


def _parse_display_id(arg: str) -> int | None:
    """``target``'s argument: a non-negative display id, else ``None``."""
    try:
        value = int(arg.strip())
    except ValueError:
        return None
    return value if value >= 0 else None


def _read_stdin(commands: queue.Queue[Command]) -> None:
    """Reader thread: enqueue lines, hand back acks.

    ``show``/``hide`` ack immediately — they are queued and the tick will
    act within ~33ms. The grab pair waits for the main thread's event, so
    the parent's capture ordering holds. EOF means the parent is gone:
    quit rather than haunt the screen.
    """
    try:
        for line in sys.stdin:
            command, _, arg = line.strip().partition(" ")
            if not command:
                continue
            if command in _MAIN_THREAD_ACKS:
                ack: threading.Event = threading.Event()
                commands.put((command, arg, ack))
                print("ok" if ack.wait(_MAIN_ACK_TIMEOUT) else "err", flush=True)
            elif command == _QUIT:
                commands.put((_QUIT, "", None))
                return
            else:
                commands.put((command, arg, None))
                print("ok", flush=True)
    except Exception:
        pass
    finally:
        commands.put((_QUIT, "", None))
