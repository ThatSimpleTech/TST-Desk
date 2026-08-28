# GitHub Actions blocked — investigation (TD-4904)

**Status (2026-08-28 night):** Workflows reworked so the $20/month
budget is not spent on every PR. `ci.yml` is three `ubuntu-latest`
jobs (was 3 × 3 OS = 9, with macOS at 10×). `package.yml` no longer
runs on push to `main`; five-platform bundles are
`workflow_dispatch` or a `tstdesk-v*` tag via `release.yml`.

**Earlier (2026-08-28 evening):** Spending limit hit again. Tag
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

Keep this file: empty steps, `runner_id: 0`, and jobs that finish in ~2–4s
are still billing, not workflow syntax. YAML now spends less when runners
do start.

## What ate the minutes

GitHub multiplies hosted minutes on private repos (Linux 1×, Windows 2×,
macOS 10×) against the included quota, then bills overage. Org budget
is **$20/month**. Two workflows did the damage:

1. **`ci.yml` 3 jobs × 3 OS on every PR and every `main` push.** One
   green run (33134175167) was ~8 minutes wall-clock but ~70–80
   Linux-equivalent minutes because three jobs ran on `macos-latest`.
   Fourteen open PRs each queued nine jobs. After the budget died those
   jobs failed in ~2s (`runner_id: 0`) and did not add spend; they also
   could not go green.

2. **`package.yml` five-platform matrix on every `main` push** that
   touched `shell/`, `ui/`, `core/`, or the workflow file. Two macOS
   legs (arm64 + `macos-15-intel`) at 10× dominate. One green run
   (33134175163) was ~15 minutes wall, ~200 Linux-equivalent minutes.
   `cancel-in-progress: false` (correct for finishing a bundle) meant
   rapid TD-1302/TD-1406 merges *queued* overlapping matrices instead of
   replacing them. A cancelled `workflow_dispatch` (33127765115) ran
   ~64 minutes overnight. The `tstdesk-v0.1.0` tag then tried another
   five-leg Release against an already-empty budget.

macOS is the expensive SKU (~$0.062/min vs ~$0.006 Linux). Intel and
Apple-silicon package legs together were one successful Package run's
largest line. Windows Python CI (~7.5 min at 2×) was the slowest test
job, not the biggest bill.

## What blocked it (2026-08-27)

The org had a **ProductPricing budget for Actions at $0** with
`prevent_further_usage: true`. Jobs showed `runner_id: 0`, empty
steps, and failed in ~4s. Package sat `pending` with `total_count: 0`
jobs, or queued behind a stale concurrency holder
(`cancel-in-progress: false` on `Package-${{ github.ref }}`).

Raising the budget above $0 unblocks hosted runners. Keeping the
matrix small is what keeps a $20 cap from dying the same day.

## In-repo mitigations

- **`ci.yml` is ubuntu-only.** Python / Rust / TypeScript still run on
  every code PR and `main` push. Path filter skips doc-only changes.
  macOS and Windows product tests stay in the tree; run them locally
  or restore a dispatch matrix when the budget is raised.
- **`package.yml` is on-demand.** `workflow_dispatch` or
  `release.yml` on a `tstdesk-v*` tag. Do not push tags to "test CI."
- **`timeout-minutes: 60`** on each bundle job (a stuck macOS leg once
  ran 24h).
- **`macos-15-intel`** instead of retired `macos-13`.
- **Local smoke unchanged:** `smoke_linux_bundle.sh` is still the Linux
  guest when you do not want to spend Actions minutes.

## Prime directives

No workaround here binds a non-loopback socket or phones home. Failed
Actions runs do not affect the local daemon.
