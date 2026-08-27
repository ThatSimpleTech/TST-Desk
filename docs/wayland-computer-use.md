# Wayland computer-use — TD-4901 assessment

Re-assessment of native Wayland desktop computer-use for TST Desk (TD-2002,
TD-4901). **X11 is unchanged** — this document is the plan for a future size-13
epic, not current product behaviour. On Wayland today, `health` reports
`supported: false`, `session_type: "wayland"`, `backend: null`.

Prime directives still apply: loopback-only daemon, no telemetry, consent must
not hang waiting for a prompt that will never appear.

---

## 1. Capture — ScreenCast portal + PipeWire

### Feasible where

| Compositor / DE | Portal backend | ScreenCast | User picker |
|---|---|---|---|
| GNOME | `xdg-desktop-portal-gnome` | Yes | GNOME dialog (monitor / window) |
| KDE Plasma | `xdg-desktop-portal-kde` | Yes | KDE dialog |
| Sway / Hyprland (wlroots) | `xdg-desktop-portal-wlr` | Yes | Often `slurp` / wofi; may default to first output |
| Others | varies | Maybe | Maybe |

### Consent flow (PipeWire path)

1. Client calls `org.freedesktop.portal.ScreenCast` **`CreateSession`**.
2. **`SelectSources`** names monitor / window / virtual output; may request a
   restore token (`persist_mode`) when portal ≥ 1.21 and the DE supports it.
3. **`Start`** raises the user-visible picker (or wlr equivalent).
4. Portal returns PipeWire node ids; client opens **`OpenPipeWireRemote`** and
   reads frames from the stream.

Scope is **what the user picked**, not the full desktop. Cursor may be a
separate stream. This is async D-Bus + PipeWire — not an X11 `ImageGrab`.

### Recommendation (capture sub-epic)

Implement as **TD-4901a** (capture only): portal ScreenCast → PipeWire frames →
existing vision pipeline. Do not claim `supported: true` until a headless CI /
clean-guest test exists on at least GNOME and one wlroots compositor.

---

## 2. Input — RemoteDesktop portal + libei

### Feasible where

| Backend | RemoteDesktop | libei / EIS |
|---|---|---|
| GNOME | Yes | Yes (`ConnectToEIS`, portal ≥ 1.17) |
| KDE | Yes | Yes |
| `xdg-desktop-portal-wlr` | **Historically no** | No unless a newer generic backend is installed |

Consent is a **second** grant from viewing — “control this session”, not merely
“share screen”. Persistence exists since portal 1.21 (DE support varies).

Xwayland 23.2+ can translate XTEST into portal+libei for **XWayland clients
only** — not native Wayland apps. That is why XWayland `DISPLAY` must never
flip `health` to supported.

### Recommendation (input sub-epic)

Implement as **TD-4901b** (input only), **after** capture proves consent UX.
Dependencies: D-Bus, libei (or ctypes), portal RemoteDesktop session lifecycle.
Refuse on compositors with ScreenCast but no RemoteDesktop (common on wlroots).

---

## 3. `foreground_window` — explicit strategy

**Decision for TD-4901:** do **not** degrade `expect_window` on Wayland.

| Strategy | Use on Wayland | `expect_window` |
|---|---|---|
| AT-SPI focused accessible | Hint / Design-mode only (TD-3406) | **Refuse** — not compositor foreground |
| `wlr-foreign-toplevel-management` | Sway / some wlroots | Partial list; no GNOME |
| `ext-foreign-toplevel-list-v1` | Lists titles; **no focus bit** | **Refuse** |
| GNOME Shell D-Bus / extension | GNOME-only | Out of scope for v0.4 neutral backend |
| Portal restore token + picked window | Capture scope only | Match picked source, not global FG |

On Wayland, **`expect_window` stays a hard refusal** until a chosen strategy
actually identifies the same surface the user granted in the portal picker.
Listing toplevels without focus is not enough.

X11 path keeps EWMH `_NET_ACTIVE_WINDOW` — unchanged.

---

## 4. Health and permissions copy

Flip `supported: true` only when **both** chosen capture and input strategies
pass on the session's compositor (or when the product deliberately ships
capture-only Wayland with input still refused — must be explicit in
`permissions_linux` copy).

Until then, keep TD-2002 behaviour and extend `WAYLAND_LIMIT` to link here.

---

## 5. Suggested split (size 13)

```
TD-4901a — Wayland capture (ScreenCast + PipeWire)
TD-4901b — Wayland input (RemoteDesktop + libei)
TD-4901c — foreground_window policy + tests (no silent degrade)
```

Do not start from “drive XWayland only” — that violates spec §9 and TD-2002.

See TD-2002 and TD-4901 entries in `DECISIONS.md`.
