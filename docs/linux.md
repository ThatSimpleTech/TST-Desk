# Linux

What "runs on Linux" means for TST Desk, where the semantics genuinely differ from
macOS and Windows, and which of those differences are decisions rather than gaps.
Written for someone on an X11 or Wayland session wondering whether a refusal is
the product or their desktop.

Most of the daemon is platform-neutral. The places it is not are the same shape
as on Windows: an operating-system concept that has no equivalent elsewhere.

---

## 1. Data directory

The product data directory on Linux / BSD is `$XDG_DATA_HOME/tst-desk`, or
`~/.local/share/tst-desk` when that variable is unset. That is the path
`docs/configuration.md` documents and the path both the host and
`user_data_dir()` now join.

The Tauri identifier `com.thatsimpletech.tstdesk` is the bundle id and the
keychain *service* name. It is not the XDG directory name. A host that joined
the identifier onto `dirs::data_dir()` stored GUI sessions and `port.json` in
`~/.local/share/com.thatsimpletech.tstdesk` while `tst run` / `tstd` used
`tst-desk`. Those two trees are now the same leaf.

If only the reverse-DNS leftover exists, it is renamed once to `tst-desk`. If
both exist, `tst-desk` wins and the leftover is left alone — two live stores
are not merged.

`--data-dir` still moves the session store and the audit database. It does
**not** move `config.yaml`.

---

## 2. Keychain

API keys go through `secret-tool` (libsecret) over stdin. The service is
`com.thatsimpletech.tstdesk`. Install `libsecret-tools` and have a Secret
Service running (GNOME Keyring, KWallet, or the desktop's equivalent).

A locked collection is `keychain_locked`, not raw CLI stderr. Unlock the login
keyring, then retry Store. There is no Keychain Access app on Linux.

`secret-tool` is discovered at call time. The Linux backend is selected
whenever `sys.platform` is Linux, even if the binary is missing — the first
store or lookup is where that shows up. A missing binary is a typed
`KeychainError` ("install libsecret-tools"), not a crash: `setup_state`
probes every named credential, and a clean guest without libsecret must
still handshake and run the keyless `local` preset.

---

## 3. File permissions

`sessions.json`, `port.json`, and `remote-token` are written `chmod 0600`.
Unlike Windows, that is a real restriction here. The containing XDG directory
is usually `0700` as well.

---

## 4. Computer use

Live desktop computer-use is **X11 only** (TD-2001). `mcp/tst-cu-mcp` talks to
`libX11` / `libXrandr` / `libXtst` and captures through Pillow. Policy
(kill-switch, `expect_window`) is the same layer as macOS and Windows.

`health` decides support from the environment, without opening a Display:

| Session | `health.supported` | `session_type` |
|---|---|---|
| Native X11 (`XDG_SESSION_TYPE=x11`) | `true` | `x11` |
| Wayland | `false` | `wayland` |
| XWayland `DISPLAY` on a Wayland session | `false` | `wayland` |

X11 has no TCC-style gate. `check_permissions` says so (`no_gate`) and names
the limits that actually bite: a missing `DISPLAY`, a missing XTEST
extension, or a Wayland session. There is nothing to grant in Settings.

Wayland cannot be served by extending the X11 backend (TD-2002). Capture
would be portal ScreenCast over PipeWire; input would be portal RemoteDesktop
or libei; `foreground_window` is not compositor-neutral. Until a later epic,
the product refuses rather than driving XWayland clients and ignoring native
Wayland apps. `expect_window` never degrades: if foreground cannot be read,
actuation is refused.

The real-display rust ring is the same session-scoped signal as macOS and
Windows (TD-3407): it lights on the first computer-use tool of a turn and
stays until turn end, cancel, or the kill-switch. Wayland has no ring.
Desktop Design-mode hit-test (TD-3406) is observe-only: AT-SPI when
the library is present, otherwise the EWMH window under the point on
X11. Wayland still has no Design AX path.

The browser computer-use path (Playwright) is not X11-specific.

---

## 5. Close vs Quit

Closing the window is not quitting the app (TD-2902).

- **Close** (title-bar X) hides the window. `tstd` keeps running.
- **Quit** (menu / palette **Quit TST Desk**) sends `shutdown` and reaps the
  process group.

A second host process attaches to the live listener instead of spawning
another daemon. There is no tray (TD-4703). Restore after hide depends on the
desktop: launching the app again attaches; there is no macOS `Reopen` event.

While hidden, a running session or a parked approval updates the window title
(the cheap Linux / Windows badge path). OS notifications still fire.

---

## 6. Things that are not different

- **Loopback binding.** `127.0.0.1` on every platform. Prime directive §2.1
  has no platform clause.
- **Steering-file writes.** `AGENTS.md`, `CLAUDE.md` and `.tst/rules/**` are
  refused identically everywhere.
- **Manifest and config paths.** Written with forward slashes regardless of
  host.
- **Killing a command.** POSIX `setsid` + `killpg` is the reference
  implementation. Windows is the special case (`docs/windows.md` §4).

---

## 7. Building from source

The README Development section lists the apt packages. On Ubuntu 26.04 use
`libayatana-appindicator3-dev`, not `libappindicator3-dev`: the older name
removes `network-manager-applet`.

For where each of these decisions was made, see the TD-2001, TD-2002, and
Linux data-dir entries in `DECISIONS.md`.

---

## 8. Clean-guest smoke

This host has no `/dev/kvm`, so the Linux "clean VM" is Docker
(`ubuntu:22.04` for the extracted sidecar, `ubuntu:24.04` for `dpkg -i`
plus the windowed host). From a built `.deb` / AppImage:

```
core/scripts/smoke_linux_bundle.sh [path-to-deb] [path-to-appimage]
```

That script proves: no system Python on the sidecar (`ldd` + empty
`PATH`), `dpkg -i` + `xvfb-run tst-desk` writes `port.json`, a protocol
turn against the shipped `local` preset (loopback mock → `fs_write` →
reply), and AppImage `--appimage-extract` of the same sidecar. It does
not tick the four-platform packaging boxes. macOS and Windows still need
their own guests. GitHub Actions `package.yml` is a separate gate.
