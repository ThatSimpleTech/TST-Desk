# GitHub Actions blocked — investigation (TD-4904)

**Status (2026-08-28 evening):** Spending limit hit again. Tag
`tstdesk-v0.1.0` fired Release
[33177767804](https://github.com/ThatSimpleTech/TST-Desk/actions/runs/33177767804);
every bundle job failed in ~4s with empty steps, `runner_id: 0`, annotation
"recent account payments have failed or your spending limit needs to be
increased." TD-1303 published anyway from the last green Package
([33147408105](https://github.com/ThatSimpleTech/TST-Desk/actions/runs/33147408105)
at `4330c37`). Raise the org Actions budget, then re-run that Release
workflow to rebuild from the tag.

**Earlier (2026-08-28 morning):** Recovered at **$20/month**. CI green on
all nine legs. Package green on all five legs (`259cc49`, run
[33134175163](https://github.com/ThatSimpleTech/TST-Desk/actions/runs/33134175163)):
macOS arm64, macOS x86_64 (`macos-15-intel`; `macos-13` retired Dec 2025),
Linux x86_64, Linux aarch64, Windows x86_64. TD-1302 ticked from that run.

Keep this file: the failure mode (empty steps, `runner_id: 0`, Package
pending with zero jobs) is billing, not workflow syntax. If it returns,
raise the budget before rewriting YAML.

## What blocked it (2026-08-27)

The org had a **ProductPricing budget for Actions at $0** with
`prevent_further_usage: true`. Jobs showed `runner_id: 0`, empty
steps, and failed in ~4s. Package sat `pending` with `total_count: 0`
jobs, or queued behind a stale concurrency holder
(`cancel-in-progress: false` on `Package-${{ github.ref }}`).

Raising the budget above $0 unblocks hosted runners.

## In-repo mitigations that remain useful

- **`package.yml` push filter:** Bundles on `main` only when `shell/`,
  `ui/`, `core/`, or the workflow file change. Doc-only merges do not
  enqueue a 30–40 minute matrix.
- **`timeout-minutes: 60`** on each bundle job (a stuck macOS leg once
  ran 24h).
- **`macos-15-intel`** instead of retired `macos-13`.
- **Local smoke unchanged:** `smoke_linux_bundle.sh` is still the Linux
  guest when you do not want to spend Actions minutes.

## Prime directives

No workaround here binds a non-loopback socket or phones home. Failed
Actions runs do not affect the local daemon.
