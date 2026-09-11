# tst-cu-mcp

A local **computer-use MCP server**. It gives an MCP-capable model *eyes*
(screenshots) and *hands* (mouse, keyboard, scroll) on your machine, over the MCP
**stdio** transport. It never opens a network socket.

Built for the "what's on my screen?" / "click that thing in the corner" loop:
the model takes a screenshot, reasons about the pixels, and acts.

- **Vision-first.** `screenshot` returns a real PNG plus the coordinate metadata
  the model needs to point accurately (works on any display, Retina- and
  DPI-aware).
- **Whole-desktop, autonomous.** No per-action approval prompt. A **kill-switch**
  is the safety net instead (see [Safety](#safety)).
- **Standalone.** Works with any MCP client — Kiro, Claude Desktop, Goose. It does
  not modify any other project.

**macOS, Windows, and Linux (X11).** Each is first-class; the platform-specific
code lives in `backends/` behind one interface. A Wayland session reports
unsupported (`health.session_type`). Portal support was assessed (TD-2002)
and is not in the current milestones.
See [Platform notes](#platform-notes).

## Install

Requires [`uv`](https://docs.astral.sh/uv/). Python 3.12 is provisioned by uv.

```sh
cd mcp/tst-cu-mcp
uv sync
```

This creates the launch command your MCP client will use:

- macOS/Linux: `.venv/bin/tst-cu-mcp`
- Windows: `.venv\Scripts\tst-cu-mcp.exe`

Confirm it starts (Ctrl-C to exit; it waits for a client on stdin):

```sh
uv run tst-cu-mcp
```

## Platform notes

### macOS — grant permissions (required)

macOS gates screen capture and input synthesis behind two permissions, and it
grants them to **the app that launches the server** — TST Desk, Kiro, Claude
Desktop, Goose, or your terminal — **not** to the server itself.

1. **Screen Recording** — System Settings > Privacy & Security > Screen Recording
2. **Accessibility** — System Settings > Privacy & Security > Accessibility

Enable your **host app** in both, then **fully quit and reopen it** (Cmd+Q, not
just closing the window). macOS pins each grant to the host's code signature,
so reconnecting the server or clicking Allow on the in-app prompt again will
not pick it up. `check_permissions` with `request=true` raises each prompt at
most once.

**Settings shows the host ON but capture or input still fails?** The grant
belongs to an older build of the host (an ad-hoc signature changes on every
rebuild). `check_permissions` reports this as `stale_grant_suspected`; no
amount of `request=true` repairs it. Reset the rows and allow again:

- In TST Desk: Settings → Computer use → **Reset grants & re-request**, then
  relaunch. TST Desk signs its builds with a stable identity
  (`shell/scripts/install-macos.sh`), so grants survive later rebuilds.
- Any other host: `tccutil reset ScreenCapture <host bundle id>` and
  `tccutil reset Accessibility <host bundle id>`, then relaunch the host.

### Windows — no permission gate, two real limits

Windows has no equivalent of macOS TCC, so there is nothing to grant and
`check_permissions` reports both as not required. Two things do bite, and both
fail **silently** rather than with an error, so the tool reports them up front:

- **UIPI (integrity levels).** A non-elevated process cannot send input to an
  elevated window. `SendInput` succeeds and the target simply ignores it, so a
  click into an admin window looks like it worked and did nothing. If the model
  needs to drive an elevated app, the host app must run elevated too.
- **The secure desktop.** UAC consent prompts, the lock screen, and
  Ctrl+Alt+Del run on a separate desktop that cannot be captured or driven at
  all. Screenshots of it come back black.

Call **`check_permissions`** on either platform at any time; it reports what is
granted, what is missing or unavailable, and the fix steps.

### Linux — X11 only, no permission gate

An X11 session is required. `health` reports `supported: true` and
`session_type: "x11"` there. A Wayland session reports `supported: false` —
Wayland does not let an unprivileged client capture the screen or inject
global input, and an XWayland `DISPLAY` is not a substitute (it would only
drive X11 clients). Portal-based Wayland support was assessed in TD-2002
and is not in the current milestones.

X11 itself has no grant dialog. `check_permissions` says so, and names a
missing XTEST extension if input synthesis cannot work.

## Connect it to your client

Replace the path with your checkout. On Windows use the `.exe` under
`.venv\Scripts\`; on macOS use `.venv/bin/`.

**Kiro** — `~/.kiro/settings/mcp.json`:

```json
{
  "mcpServers": {
    "tst-cu-mcp": {
      "command": "C:\\path\\to\\TST-Desk\\mcp\\tst-cu-mcp\\.venv\\Scripts\\tst-cu-mcp.exe",
      "args": [],
      "disabled": false
    }
  }
}
```

**Claude Desktop** — `~/Library/Application Support/Claude/claude_desktop_config.json`
(macOS) or `%APPDATA%\Claude\claude_desktop_config.json` (Windows):

```json
{
  "mcpServers": {
    "tst-cu-mcp": {
      "command": "/ABSOLUTE/PATH/TO/tst-cu-mcp/.venv/bin/tst-cu-mcp"
    }
  }
}
```

**Goose** — merge under `extensions:` in `~/.config/goose/config.yaml`:

```yaml
extensions:
  tst-cu-mcp:
    name: tst-cu-mcp
    type: stdio
    cmd: /ABSOLUTE/PATH/TO/tst-cu-mcp/.venv/bin/tst-cu-mcp
    args: []
    enabled: true
    timeout: 300
```

The absolute in-venv path is used deliberately: it self-contains its Python and
needs nothing on the host app's `PATH`.

## Tools

| Tool | Purpose |
|------|---------|
| `health` | Liveness, active backend, and whether this platform is supported. |
| `check_permissions` | Report capture/input permission status per platform, with fix steps. |
| `get_screen_info` | List displays: bounds, scale, which is main. |
| `get_foreground_window` | Which window is in front: title, process, pid, bounds. |
| `get_cursor_position` | Where the pointer is now. |
| `screenshot` | Capture a display (or a `region` of it) as PNG + coordinate metadata. |
| `wait` | Sleep (max 30s) to let the UI settle. |
| `wait_for_window` | Poll until a named window is in front, or time out. |
| `list_apps` | Running user-facing apps (name, pid, bundle id). Denied apps omitted. |
| `ui_snapshot` | Accessibility tree for an app. Does not take the pointer or raise the app. |
| `ui_action` | `press` / `set_value` / `focus` / `raise` / `show_menu` on an element id from `ui_snapshot`. |
| `launch_app` | Open an app by name or bundle id without Spotlight. |
| `hide_other_apps` | Full control: hide other regular apps so only the target stays visible. |
| `unhide_apps` | Restore apps hidden by `hide_other_apps`. |
| `move_mouse` | Move the cursor to a point. |
| `click` | Click; `button` = left/right, `count` >= 2 for double/triple. |
| `type_text` | Type a Unicode string at the current focus (never logged). |
| `press_keys` | Press a combo like `cmd+c`, `ctrl+shift+t`, `return`, `escape`. |
| `scroll` | Scroll by lines, optionally over a target point. |

Prefer **background** tools (`list_apps` → `ui_snapshot` → `ui_action`) so the
agent drives an allowed app through its accessibility tree while you keep using
the computer. Screenshot / click / type take **full control** of the pointer and
keyboard; use them when the tree cannot reach the control.

The reads — `health`, `check_permissions`, `get_screen_info`,
`get_foreground_window`, `get_cursor_position`, `screenshot`, `list_apps`,
`ui_snapshot` — keep working while the kill-switch is engaged. Stopping the hands
should not blind the eyes.

### Aiming safely: `expect_window`

Every input tool takes an optional `expect_window`. Pass part of the intended
window's title or process name and the action is refused, before anything is
sent, unless that window is in front:

```
click(x=700, y=361, ..., expect_window="Google Chrome")
```

Use it. A coordinate aims at a *point*, not at a *thing*, and if the UI moved
between your screenshot and your click, the point now means something else. The
refusal names what it found instead, so it is recoverable:

```
expected the foreground window to match 'Google Chrome', but it is
'windows.md - TST-Desk - Kiro' (Kiro.exe). Nothing was sent. Take a fresh
screenshot: the UI has moved since the one this call was aimed at, or the
window has not finished taking focus.
```

The guard cannot catch everything. A window can be in front and still not be
ready for keystrokes — see the timing notes below.

### Coordinate model

`screenshot` returns an image plus `captured_region_points` and `image_px`. The
model gives click/move coordinates in **the returned image's pixel space**
(`coordinate_space="image"`, passing `image_width`, `image_height`, and
`region`); the server maps them proportionally to the global coordinate space —
correct regardless of Retina scaling, Windows per-monitor DPI, or downscaling.
Global coordinates can be sent directly with `coordinate_space="points"`.

The mapping is proportional over the captured region, which is what makes it
scale-agnostic: it never needs to know the backing scale factor.

### Key combos

Modifiers: `shift`, `ctrl`/`control`, `alt`/`option`, `cmd`/`command`, plus
`win`/`super` on Windows. `fn` is macOS-only.

**On Windows, `cmd` is accepted as an alias for Ctrl.** Models have a
mac-shaped shortcut vocabulary and will reach for `cmd+c`; treating it as Ctrl
makes copy/paste work instead of silently failing. Use `win`/`super` when you
actually mean the Windows key. `fn` is refused with an explicit error rather
than dropped.

`win`/`super` can also be pressed on its own (`press_keys("win")` opens Start),
since on Windows that is the normal way to launch something. `ctrl+escape` does
the same thing.

### Driving a window that just appeared

Two timing traps, both learned the hard way rather than reasoned about:

- **Keystrokes sent immediately after a window opens can be dropped.** The Start
  menu is the worst offender: it takes focus, then re-homes focus onto its own
  search field, and anything typed in between is discarded. `type_text` will
  report every character delivered, because `SendInput` genuinely delivered them
  — the receiving window was not listening yet. `expect_window` does not help:
  the window *is* in front. Screenshot and confirm the caret before typing
  anything that matters.
- **Coordinates go stale fast.** A click is aimed at a point, not at a thing. If
  the UI moved or a menu closed between the screenshot and the click, the click
  lands on whatever is at that point now — which may be a different application
  entirely. `expect_window` catches the common case where focus changed. It
  cannot catch a menu that moved *within* the same window, so take a fresh
  screenshot immediately before clicking, not one step earlier.

The recommended sequence after launching something:

```
wait_for_window("Claude")     # returns as soon as it is in front
screenshot()                  # fresh coordinates
click(..., expect_window="Claude")
```

Neither trap is a bug in this server, and the second is only partly fixable
inside it. They are the cost of driving a UI by coordinates, and the reason
`screenshot` is cheap.

## Approvals

Most MCP clients prompt before each tool call. For this server that is actively
counterproductive: approving a `screenshot` changes the screen *before* the
capture happens, and approving a `click` or `type_text` gives the client window
focus immediately before an input event aimed somewhere else. **The gate corrupts
the thing it is gating.**

So per-action approval is the wrong shape here. Consent belongs at the session
level, and the kill-switch below is the mechanism — it is a file, so engaging it
never steals focus or moves your pointer.

Recommended setup: auto-approve every tool in your client, and gate the session
with the stop-file instead. For Kiro, in `~/.kiro/settings/mcp.json`:

```json
"autoApprove": [
  "health", "check_permissions", "get_screen_info", "get_foreground_window",
  "get_cursor_position", "screenshot", "wait", "wait_for_window",
  "list_apps", "ui_snapshot", "ui_action", "launch_app",
  "hide_other_apps", "unhide_apps",
  "move_mouse", "scroll", "click", "type_text", "press_keys"
]
```

If you want a middle setting, list only the first eight. That removes most
prompts while leaving anything that can actually *act* behind a gate — at the
cost of a focus-stealing dialog immediately before each action, which is the
problem you were trying to avoid.

Two things to know before turning the gate off:

- **Screen content is untrusted input.** Anything visible — a document, an email,
  a web page — can contain text shaped like instructions, and with actuation
  auto-approved there is no human checkpoint between reading it and acting on it.
  This is what makes a computer-use server different in kind from a file-editing
  one.
- **Do not run your client elevated.** Unelevated, UIPI silently protects every
  administrator window on the machine from this server. That is a real safety net
  you get for free, and elevating removes it.

## Safety

The server is whole-desktop and autonomous — the model can click and type
anywhere, with no per-action approval. This is powerful and blunt: whatever is on
screen is a prompt-injection surface, and coordinate misfires click real buttons.

State the trust model plainly: **anything that can reach this server can drive
your machine.** That is the design, not an oversight. It runs in your desktop
session with your granted permissions, has no approval gate of its own, and the
only gates are the ones you set up around it — the kill-switch below, an
unelevated host, and your choice of which client may load it. Installing and
connecting it is the moment you decide to trust that whole pipeline: the client,
the model, and everything the model reads.

The **kill-switch** stops all actuation immediately (screenshots still work):

- Create the stop-file: `~/.tst-cu-mcp/STOP` (delete it to resume), or
- Set `TST_CU_MCP_STOP=1` in the host app's environment, or
- Set `actuation.enabled: false` in config.

The kill-switch is checked inside every actuation entry point, on every platform,
before any OS call.

## Configuration (optional)

No config file is needed. To customize, copy `config.example.yaml` to
`~/.tst-cu-mcp/config.yaml` (or point `TST_CU_MCP_CONFIG` at it). It exposes the
master actuation switch, the stop-file path, and a reserved `scoping` section for
future per-app restrictions.

## Using it from TST Desk

This server lives in the TST Desk repository but is **not part of the app**. It
ships nothing into `core/`, `shell/`, or `ui/`, and the app's CI does not build
or test it — those jobs are scoped to their own directories. TST Desk gains MCP
extension loading in its v0.8 milestone; when it lands, point it at this server
with a single stdio entry. Until then the two are independent, and this one works
with any MCP client today.

## Develop

```sh
uv run ruff check
uv run mypy
uv run pytest -q
```

> **If `uv sync` or `uv run` fails on Windows with "Access is denied"**, security
> software is blocking uv's `.venv\Scripts\python.exe` trampoline. A stdlib venv,
> whose `python.exe` is a copy of the real interpreter, works instead:
>
> ```powershell
> uv python install 3.12
> & "$env:APPDATA\uv\python\cpython-3.12*\python.exe" -m venv .venv
> uv pip install --python .\.venv\Scripts\python.exe -e . --group dev
> .\.venv\Scripts\python.exe -m pytest -q
> ```
>
> See the 2026-08-18 entry in the repository `DECISIONS.md` for the diagnosis.

The default `pytest` run is headless and passes on any platform. Tests that need
a real desktop are opt-in:

```sh
uv run pytest -m desktop      # reads live OS state, restores what it touches
```

`-m desktop` moves the pointer and puts it back. It does not type.

Keystroke tests are gated twice — their own module, plus an environment variable
that no marker expression can bypass:

```powershell
$env:TST_CU_MCP_ALLOW_INTRUSIVE_TESTS = "1"
uv run pytest -m intrusive
Remove-Item Env:\TST_CU_MCP_ALLOW_INTRUSIVE_TESTS
```

They send real keystrokes to whichever window has focus. Close anything you care
about first.

> The double gate is not belt-and-braces for its own sake. These tests were
> originally marked `intrusive` inside the `desktop` module, which meant they
> carried both marks — and `pytest -m desktop` selected them, because a
> command-line `-m` *replaces* the `addopts` filter rather than narrowing it.
> They typed into a chat window three times before it was noticed. A marker is a
> selector, not a guard.

## Security notes

- **stdio only** — no network socket is ever bound.
- **Screenshots are never written to disk.** The Windows backend captures
  straight to memory; the macOS backend uses a temp file that is unlinked in a
  `finally`. Image bytes are scrubbed from logs.
- **Typed text is never logged.**
- Logs go to **stderr**; stdout is reserved for the MCP protocol.

## License

MIT — see the repository `LICENSE`.
