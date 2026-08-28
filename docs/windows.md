# Windows

What "runs on Windows" means for TST Desk, where the semantics genuinely differ from macOS and
Linux, and which of those differences are decisions rather than gaps. Written for someone
reading a refusal message on Windows and wondering whether it is the product or their path.

Most of the daemon is platform-neutral. The four places it is not are all the same shape: an
operating system concept that has no equivalent elsewhere, or a POSIX concept Windows does not
implement. Each one is decided here rather than left to whichever host happened to run the
suite last.

---

## 1. Path forms

Every path a tool touches is resolved to canonical absolute form and then checked against the
workspace wall (`core/tstd/tools/boundary.py`). Before that, the raw string is checked for
Windows-specific forms that are ambiguous, unresolvable, or capable of aliasing somewhere else.

| Form | Example | On Windows | Elsewhere |
|---|---|---|---|
| Drive-absolute | `C:\work\proj\src\a.py` | **Allowed**, then checked for containment like any absolute path | Refused `windows_unsafe` |
| Drive-relative | `C:src\a.py` | Refused `windows_unsafe` | Refused `windows_unsafe` |
| UNC | `\\server\share\a.py` | Refused `windows_unsafe` | Refused `windows_unsafe` |
| Extended-length | `\\?\C:\work\a.py` | Refused `windows_unsafe` (reads as UNC) | Refused `windows_unsafe` |
| 8.3 short name | `C:\Users\RUNNER~1\...` | Allowed **if resolution expands it** — see §2 | Refused `windows_unsafe` |
| Alternate data stream | `a.txt:hidden` | Refused `windows_unsafe` | Refused `windows_unsafe` |

Two things follow from the first row. Absolute in-workspace paths work on Windows — they have
to, because every absolute Windows path carries a drive letter, and refusing the form would
leave the model with relative paths and nothing else. And `C:\Windows\system32\...` is still
refused on Windows; it is refused for being **outside the workspace** rather than for its form.
The refusal code differs by platform, the outcome does not.

Drive-*relative* paths stay refused everywhere, including Windows. `C:src\a.py` resolves against
whatever directory that drive was last on, which is process state nobody declared — it can land
anywhere, and there is no containment check worth doing on a path whose meaning is not fixed.

Everything in the table except the drive-absolute and short-name rows is refused on **every**
platform, not just Windows. A workspace can be shared or moved across operating systems, so a
path form that would be dangerous on Windows is refused on the Mac that writes it.

---

## 2. 8.3 short names

Windows keeps a short alias for long filenames — `Program Files` is also `PROGRA~1`, and a CI
runner's temp directory usually lives under `C:\Users\RUNNER~1\AppData\Local\Temp`. The alias is
a boundary problem in principle: a check performed against the short form could be checking a
different path from the one that gets opened.

In practice it is not, because Windows resolves it for us. `os.path.realpath` reaches
`GetFinalPathNameByHandle`, which returns the long form, so by the time the guard checks
containment the alias is already gone. **On Windows the short-name check therefore runs against
the canonical path**, and a short-named ancestor you did not choose — a runner's home directory,
a legacy `Documents and Settings` junction — costs you nothing.

A segment that *survives* resolution is still refused. Surviving means the filesystem had
nothing to expand it to, so the name is taken at face value and the fail-closed rule applies.
Two consequences worth knowing:

- A short name pointing at something that does not exist is refused rather than created.
- A directory whose real long name happens to look like an alias — literally naming a folder
  `my~1` — is refused on Windows too. Narrow, and refusing is the safe side of it.

Off Windows the refusal is unconditional. No other filesystem knows the short→long mapping, so
there is nothing to resolve with, and a table we invented would be a guess with a security
boundary resting on it.

---

## 3. File permissions

Three files are written with `chmod(0o600)`: the session store (`sessions.json`), the port file
(`port.json`, which carries the loopback auth token), and — when a non-loopback listener is
up — `remote-token` (a rotating token distinct from the port file). **On Windows that chmod is a
no-op, and that is the decision, not an oversight.**

`os.chmod` on Windows can only toggle the read-only attribute. `0o600` carries a write bit, so
nothing is set, and `stat` reports the Windows default `0o666`. What actually protects both
files is the directory they live in: `%LOCALAPPDATA%` grants Full control to the user, SYSTEM
and Administrators by default, and to nobody else. The POSIX mode bits were never the thing
securing these files on Windows.

We considered calling `icacls` on every write and decided against it. It would restate a
restriction the containing directory already provides, at the cost of a subprocess on the
startup path — and an Administrator, the only extra principal in scope, can take ownership of
the file regardless.

This is asserted, not assumed. `test_restricted_mode` in `core/tests/test_session_store.py`,
`test_write_port_file_restricted_mode`, and `test_write_remote_token_restricted_mode` in
`core/tests/test_ws.py` all branch on platform: POSIX asserts `0o600`, Windows asserts the file
exists, reads back, and carries `0o666`. If a future Python makes `os.chmod` meaningful on
Windows, those tests fail — which is exactly when this page should be rewritten.

**What this means for you:** on a shared or Administrator-managed Windows machine, the port
file's token is readable by any local Administrator. That is true of anything under
`%LOCALAPPDATA%` and is not specific to TST Desk, but it is worth knowing before running the
daemon on a machine you do not control.

---

## 4. Killing a command

The shell tool must be able to kill a command and everything it spawned — a timeout or a cancel
that leaves a background process holding a pipe open is not a cancel.

On POSIX the child is started with `setsid`, so it leads its own process group and one
`SIGKILL` to the group takes backgrounded grandchildren with it.

Windows has no `killpg`. The child is spawned with `CREATE_NEW_PROCESS_GROUP` so it is a group
leader, and the kill path runs `TerminateProcess` on the direct child followed by
`taskkill /T /F /PID`, which walks the parent chain the OS already records and kills the tree.
`taskkill` ships with Windows, so this adds no dependency. The direct child dies first and
independently, so a `taskkill` that cannot run still leaves the immediate command dead.

**This is implemented and unverified.** No Windows host has executed it. A process-tree kill is
a claim about an operating system's behaviour and the only evidence that counts is a Windows
machine performing one, so the corresponding backlog criterion (TD-1406) remains unticked.
The cancel and timeout tests in `core/tests/test_shell_tools.py` now run on `win32`: they use
a PowerShell escape probe that writes the `cmd.exe` group-leader pid plus a parked
grandchild, then `_assert_group_gone` asks `OpenProcess` whether those pids are still
`STILL_ACTIVE`. Helper argv tests in `core/tests/test_shell_windows_kill.py` pin
`taskkill /T /F /PID` on every host. A green Linux suite still does not tick the box.

---

## 5. Things that are not different

Worth stating, because "Windows" tends to attract blame:

- **Loopback binding.** `127.0.0.1` on every platform. Prime directive §2.1 has no platform
  clause.
- **Steering-file writes.** `AGENTS.md`, `CLAUDE.md` and `.tst/rules/**` are refused identically
  everywhere.
- **Manifest and config paths.** Written with forward slashes regardless of host, so a workspace
  committed on Windows reads the same on a Mac.
- **The parent watchdog.** Windows uses `OpenProcess` plus `GetExitCodeProcess` rather than
  `kill(pid, 0)`. The host always passes `--parent-pid`. Close hides the window and leaves
  the host process alive, so the watchdog stays quiet. Force-quit / SIGKILL of the host
  is what shuts `tstd` down.

---

## 6. Close vs Quit

Closing the window is not quitting the app (TD-2902).

- **Close** (title-bar X, Alt+F4) hides the window. `tstd` keeps running. In-flight turns
  and parked approvals continue. OS notifications still fire while the window is gone.
  While hidden, a running session or a parked approval badges the dock / taskbar (or the
  taskbar tooltip). Clicking the app icon shows the window, focuses a parked approval if
  there is one, and re-attaches; a second host process attaches to the live listener
  instead of spawning another daemon.
- **Quit** (menu / palette **Quit TST Desk**, Cmd+Q, or dock Quit) sends
  `shutdown`, reaps the process group, and leaves no listener. That is the TD-1002 v0.1
  contract, kept for Quit.

The first time this happens: closing the window hides TST Desk; **Quit TST Desk** is what
stops it.

For where each of these decisions was made and what was rejected, see the TD-1402, TD-1406,
TD-2902, TD-2903, and TD-2904 entries in `DECISIONS.md`.

---

## 7. Computer-use integrity

Windows does not have a Screen Recording or Accessibility analog. There is no TCC-style grant
dialog, nothing to click, and nothing to grant. `check_permissions` reports capture and input
as allowed (`all_granted: true`) because that is true — and then names the two conditions
under which actuation succeeds and does nothing.

**UIPI (User Interface Privilege Isolation).** A process cannot send synthetic input to a
window running at a higher integrity level. Clicks and keystrokes aimed at an elevated (Run as
administrator) window are discarded by the OS. The sidecar detects a short `SendInput` result
and raises rather than reporting success; it still cannot deliver the input. The typed tool
error is `uipi`. Fix: launch TST Desk elevated too, or do not target administrator windows.

**Secure desktop.** UAC consent prompts, the lock screen, and Ctrl+Alt+Del run in a separate
session that cannot be captured or driven. Screenshots come back black and input never
arrives. There is no workaround; that is the boundary that makes UAC meaningful. The typed
tool error is `secure_desktop`.

Neither is a permission the user can grant in Settings. A hang waiting for a prompt that will
not appear is a defect. The first desktop computer-use attempt on Windows emits `cu_permissions`
with this explanation (`platform: "windows"`). Settings reopens the same pane. macOS first-run
is unchanged (Screen Recording + Accessibility).

The report itself is `tst_cu_mcp.permissions.build_windows_report`. The daemon parses it; it
does not grow a second Windows backend.

See the TD-3303 entry in `DECISIONS.md`.

---

## 9. Clean-guest smoke

After `npm run tauri:build` in `ui/`, from a built `.msi` / sidecar:

```powershell
pwsh -File core/scripts/smoke_windows_bundle.ps1 [path-to-tstd.exe]
```

The script proves: bundled sidecar serves with no Python on `PATH`, a protocol
turn against the shipped `local` preset (loopback mock → `fs_write` → reply),
and that a missing API key returns actionable copy instead of hanging on
Connecting…. Linux uses `smoke_linux_bundle.sh`; macOS uses
`smoke_macos_bundle.sh`. These scripts are maintainer/local guests; the
CI four-/five-artifact box is `package.yml` (green on `259cc49`, 2026-08-28).
