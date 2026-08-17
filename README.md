# TST Desk

**An open-source, bring-your-own-model desktop agent workspace.**

Looks and feels like Claude Desktop + Cowork. Runs on your models, your machine, your keys.
No server, no hosted backend, no account, no subscription.

---

## What is this?

TST Desk is a local application that runs on user-selected models through an OpenAI-compatible
API. It gives you the experience of working alongside an AI agent in a dedicated workspace,
with full visibility into what it's doing, when it needs your approval, and how it's spending
your tokens.

## Quickstart

> *Coming soon.*

```
# After the app is packaged:
# 1. Download the latest release
# 2. Open it, paste your API key
# 3. Point it at a folder
# 4. Start working
```

## Development

### Prerequisites

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) (Python package manager)
- Rust 1.77+ (for the Tauri host)
- Node.js 24+ (for the frontend)

### Setup

```bash
# Python core
cd core
uv sync

# Frontend
cd ../ui
npm install
```

### Pre-commit hooks

This repository uses [pre-commit](https://pre-commit.com) to run linting, formatting, and
secret detection on every commit.

```bash
# Install hooks (one-time, from the core/ directory)
cd core
uv run pre-commit install
```

The hooks will then run automatically on `git commit`:
- **ruff check** — lint Python files
- **ruff format** — format Python files
- **Secret detection** — blocks commits containing API keys, tokens, passwords, and other
  credential-like patterns

To bypass the hooks (e.g. for a false positive):
```bash
git commit --no-verify
```

## Installing an unsigned build

v0.1 builds are not code-signed or notarized (see `DECISIONS.md`, 2026-08-14).
Each OS will warn on first launch; that is expected, not a defect.

- **macOS** — Gatekeeper blocks the unsigned app. Right-click the app and
  choose **Open**, then confirm in the dialog (once per install). If the file
  was download-quarantined and the Open trick does not appear:
  `xattr -d com.apple.quarantine "/Applications/TST Desk.app"`.
- **Windows** — SmartScreen shows "Windows protected your PC". Click
  **More info → Run anyway**.
- **Linux** — no signing warning. For the AppImage, `chmod +x` and run; the
  `.deb` installs with `sudo dpkg -i`.

## Status

v0.1 — in development. See [`docs/tst-desk-backlog.md`](docs/tst-desk-backlog.md) for the
current milestone and [`docs/tst-desk-spec.md`](docs/tst-desk-spec.md) for the full
architecture spec. How the pieces fit together — the daemon/shell split, the wire protocol, the
extension points, and how to add a tool — is in
[`docs/architecture.md`](docs/architecture.md). Every configuration key is documented in
[`docs/configuration.md`](docs/configuration.md), and writing `AGENTS.md` and path-scoped rules
is covered in [`docs/steering.md`](docs/steering.md).

## License

Apache-2.0. See [`LICENSE`](LICENSE) and [`NOTICE`](NOTICE).