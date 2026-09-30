"""Unfold and split a local iCalendar file into components (TD-3818).

Nested components (an alarm inside an event) are not events. Their
properties must not replace the event's own summary.
"""

from __future__ import annotations

Prop = tuple[dict[str, str], str]
Component = tuple[str, dict[str, Prop]]


def scan_components(text: str) -> list[Component]:
    """Top-level VEVENT maps, plus a marker when VCALENDAR was present."""
    return _components(_unfold(text))


def _unfold(text: str) -> list[str]:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    logical: list[str] = []
    for line in normalized.split("\n"):
        if line[:1] in " \t" and logical:
            logical[-1] += line[1:]
        elif line:
            logical.append(line)
    return logical


def _components(lines: list[str]) -> list[Component]:
    found: list[Component] = []
    depth = 0
    props: dict[str, Prop] = {}
    saw_calendar = False
    for line in lines:
        begin, name = _boundary(line)
        if begin is True:
            if depth == 0 and name == "VEVENT":
                depth = 1
                props = {}
            elif depth > 0:
                depth += 1
            elif name == "VCALENDAR":
                saw_calendar = True
            continue
        if begin is False:
            if depth == 0:
                continue
            depth -= 1
            if depth == 0 and name == "VEVENT":
                found.append(("VEVENT", props))
            continue
        if depth != 1:
            continue
        parsed = _split_prop(line)
        if parsed is not None:
            key, params, value = parsed
            props[key] = (params, value)
    # A BEGIN:VEVENT that never closed is not an event. Recording a
    # failed one makes a file with no completed event unreadable.
    # A file that already completed an event keeps that event.
    if depth > 0 and not any(kind == "VEVENT" for kind, _props in found):
        found.append(("VEVENT", {}))
    if saw_calendar:
        found.append(("VCALENDAR", {}))
    return found


def _boundary(line: str) -> tuple[bool | None, str]:
    upper = line.upper()
    if upper.startswith("BEGIN:"):
        return True, line.split(":", 1)[1].strip().upper()
    if upper.startswith("END:"):
        return False, line.split(":", 1)[1].strip().upper()
    return None, ""


def _split_prop(line: str) -> tuple[str, dict[str, str], str] | None:
    in_quote = False
    colon = -1
    for index, char in enumerate(line):
        if char == '"':
            in_quote = not in_quote
        elif char == ":" and not in_quote:
            colon = index
            break
    if colon < 0:
        return None
    name, params = _split_head(line[:colon])
    if not name:
        return None
    return name, params, line[colon + 1 :]


def _split_head(head: str) -> tuple[str, dict[str, str]]:
    parts = _split_semi(head)
    if not parts:
        return "", {}
    params: dict[str, str] = {}
    for part in parts[1:]:
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        params[key.strip().upper()] = value.strip().strip('"')
    return parts[0].strip().upper(), params


def _split_semi(text: str) -> list[str]:
    found: list[str] = []
    buf: list[str] = []
    in_quote = False
    for char in text:
        if char == '"':
            in_quote = not in_quote
            buf.append(char)
        elif char == ";" and not in_quote:
            found.append("".join(buf))
            buf = []
        else:
            buf.append(char)
    found.append("".join(buf))
    return found
