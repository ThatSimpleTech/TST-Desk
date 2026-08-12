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

## Status

v0.1 — in development. See [`docs/tst-desk-backlog.md`](docs/tst-desk-backlog.md) for the
current milestone and [`docs/tst-desk-spec.md`](docs/tst-desk-spec.md) for the full
architecture spec.

## License

Apache-2.0. See [`LICENSE`](LICENSE) and [`NOTICE`](NOTICE).