"""Linux key vocabulary: X11 keysyms, the cmd→ctrl alias, the fn refusal.

Pure tables and a parser — no Display connection. Importable on every host so
the vocabulary is testable without an X server, matching windows.py.
"""

from __future__ import annotations

# X11 keysyms (keysymdef.h). Values are ABI-stable.

XK_BACKSPACE = 0xFF08
XK_TAB = 0xFF09
XK_RETURN = 0xFF0D
XK_ESCAPE = 0xFF1B
XK_DELETE = 0xFFFF
XK_HOME = 0xFF50
XK_LEFT = 0xFF51
XK_UP = 0xFF52
XK_RIGHT = 0xFF53
XK_DOWN = 0xFF54
XK_PAGE_UP = 0xFF55
XK_PAGE_DOWN = 0xFF56
XK_END = 0xFF57
XK_INSERT = 0xFF63
XK_F1 = 0xFFBE
XK_SHIFT_L = 0xFFE1
XK_CONTROL_L = 0xFFE3
XK_ALT_L = 0xFFE9
XK_SUPER_L = 0xFFEB
XK_SPACE = 0x0020

#: Modifier name -> keysym to hold.
#:
#: ``cmd``/``command`` map to Control, same decision as Windows: models carry a
#: mac-shaped vocabulary and ``cmd+c`` must copy. ``win``/``super`` is Super.
MODS: dict[str, int] = {
    "shift": XK_SHIFT_L,
    "ctrl": XK_CONTROL_L,
    "control": XK_CONTROL_L,
    "alt": XK_ALT_L,
    "option": XK_ALT_L,
    "cmd": XK_CONTROL_L,
    "command": XK_CONTROL_L,
    "win": XK_SUPER_L,
    "super": XK_SUPER_L,
}

UNSUPPORTED_MODS: dict[str, str] = {
    "fn": "the Fn modifier is macOS-only; X11 exposes no reliable keysym for it",
}

KEYCODES: dict[str, int] = {
    **{chr(c): 0x0061 + c - ord("a") for c in range(ord("a"), ord("z") + 1)},
    **{str(d): 0x0030 + d for d in range(10)},
    "return": XK_RETURN,
    "enter": XK_RETURN,
    "tab": XK_TAB,
    "space": XK_SPACE,
    "delete": XK_BACKSPACE,
    "backspace": XK_BACKSPACE,
    "forward_delete": XK_DELETE,
    "escape": XK_ESCAPE,
    "esc": XK_ESCAPE,
    "left": XK_LEFT,
    "up": XK_UP,
    "right": XK_RIGHT,
    "down": XK_DOWN,
    "home": XK_HOME,
    "end": XK_END,
    "pageup": XK_PAGE_UP,
    "pagedown": XK_PAGE_DOWN,
    "insert": XK_INSERT,
    "win": XK_SUPER_L,
    "super": XK_SUPER_L,
    "minus": 0x002D,
    "equal": 0x003D,
    "leftbracket": 0x005B,
    "rightbracket": 0x005D,
    "backslash": 0x005C,
    "semicolon": 0x003B,
    "quote": 0x0027,
    "comma": 0x002C,
    "period": 0x002E,
    "slash": 0x002F,
    "grave": 0x0060,
    **{f"f{n}": XK_F1 + n - 1 for n in range(1, 13)},
}

#: Characters that need Shift held on a US keymap when sent via XTest.
_SHIFT_CHARS = frozenset('~!@#$%^&*()_+{}|:"<>?')


def parse_key_combo(combo: str) -> tuple[tuple[int, ...], int]:
    """Parse ``"ctrl+shift+t"`` into ``(modifier_keysyms, base_keysym)``."""
    parts = [p.strip().lower() for p in combo.split("+") if p.strip()]
    if not parts:
        raise ValueError("empty key combo")

    *modifiers, base = parts
    mod_syms: list[int] = []
    for modifier in modifiers:
        if modifier in UNSUPPORTED_MODS:
            raise ValueError(f"unsupported modifier {modifier!r}: {UNSUPPORTED_MODS[modifier]}")
        if modifier not in MODS:
            raise ValueError(f"unknown modifier {modifier!r}; valid: {sorted(set(MODS))}")
        mod_syms.append(MODS[modifier])

    if base in UNSUPPORTED_MODS:
        raise ValueError(f"unsupported key {base!r}: {UNSUPPORTED_MODS[base]}")
    if base not in KEYCODES:
        raise ValueError(f"unknown key {base!r}")
    return tuple(mod_syms), KEYCODES[base]


def char_to_keysym(ch: str) -> tuple[int, bool]:
    """Map one character to ``(keysym, needs_shift)``.

    Latin-1 uses the codepoint as the keysym. Everything else uses the X11
    Unicode plane (``0x01000000 + codepoint``). Shift is a US-layout hint for
    XTest: the server does not rewrite the keymap.
    """
    if len(ch) != 1:
        raise ValueError(f"char_to_keysym expects one character, got {ch!r}")
    code = ord(ch)
    if ch == "\t":
        return XK_TAB, False
    if ch in "\n\r":
        return XK_RETURN, False
    if 0x20 <= code <= 0xFF:
        return code, ch.isupper() or ch in _SHIFT_CHARS
    return 0x01000000 + code, False
