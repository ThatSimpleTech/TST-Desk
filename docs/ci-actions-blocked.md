# GitHub Actions blocked — investigation (TD-4904)

**Status (2026-08-27):** Workflows trigger, but jobs do not execute steps. This
blocks TD-1302, TD-1303, and TD-4704 until an org admin fixes the underlying
GitHub setting. Local Linux smoke (`core/scripts/smoke_linux_bundle.sh`) remains
the Linux install evidence.

## Symptoms

### `ci.yml` — fails in ~4–5 seconds

Every matrix leg (`python`, `rust`, `typescript` × three OSes) reports
`conclusion: failure` with **`steps: []`** in the Actions API — no checkout, no
logs. Example run: merge `TD-4905` on `main` (2026-08-27).

This is not a test failure inside the repo. The runner never reaches the first
step.

### `package.yml` — pending with zero jobs

Recent `main` pushes queue or sit **`pending`** with **`total_count: 0` jobs**
for minutes or until cancelled. Older runs (before 2026-08-27) sometimes
spawned four matrix jobs; those also showed empty step lists when they failed
fast.

The workflow file itself is valid: full checkout → sidecar build → Tauri bundle
→ per-platform smoke → artifact upload (see `.github/workflows/package.yml`).

## Likely causes (org settings)

Check these in **GitHub → Organization settings → Actions** (or the repo's
Actions settings if not org-owned):

1. **Actions disabled** for the org or this repository.
2. **Spending limit / billing** — private repos need paid minutes; macOS minutes
   bill at 10×. When the limit is hit, jobs may fail immediately or never leave
   the queue.
3. **Allowed actions / policy** — third-party actions (`actions/checkout@v4`,
   `astral-sh/setup-uv@v5`, etc.) blocked by an org policy.
4. **Runner group access** — required runner labels (`ubuntu-latest`,
   `macos-latest`, `ubuntu-24.04-arm`, …) not permitted for this repo.

Nothing in the application code can fix (1)–(4). A maintainer with org admin
must change the setting and re-run **Package** via **Actions → Package → Run
workflow**.

## What we changed in-repo

- **`package.yml` push filter:** Bundles on `main` push only when paths under
  `shell/`, `ui/`, `core/`, or the workflow file change. Doc-only merges no
  longer enqueue a 30–40 minute matrix behind an already-blocked queue.
- **`workflow_dispatch`:** Manual bundle runs remain available once Actions works.
- **Local smoke unchanged:** `smoke_linux_bundle.sh` / `smoke_linux_e2e.py` are
  still the Linux guest gate when CI is down.

## How to verify recovery

1. Re-run **CI** on `main`. Expect nine jobs with **non-empty step lists** and
   pytest/clippy/vitest logs.
2. Run **Package** (`workflow_dispatch` or a packaging-path push). Expect **four**
   jobs (macOS arm64, macOS x86_64, Linux x86_64, Windows x86_64) and uploaded
   artifacts. After TD-4902 lands, expect **five** jobs (adds Linux aarch64).
3. When `package.yml` is green, TD-1302 can tick; when release tags work,
   TD-1303 can tick; then TD-4903 / TD-4704 become unblocked.

## Prime directives

No workaround here binds a non-loopback socket or phones home. Failed Actions
runs do not affect the local daemon.
