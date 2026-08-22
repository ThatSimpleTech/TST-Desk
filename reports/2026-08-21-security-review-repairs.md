# Verification and Repair Dispositions — ox-alpha's 2026-08-21 review of `td/4802-security-highs`

**Date:** 2026-08-21
**Written by:** cursor-1 (Cursor agent — Kimi K3)
**Scope:** every finding in `2026-08-21-security-review-td4802.md` — independently
re-verified against HEAD `1337a83` before any repair was attempted.
**Status:** closed — repairs shipped on `td/4817-review-repairs`; see
**Post-notes: repairs applied** at the end for what was done and how.

---

## Method

No finding was repaired on the reviewer's say-so. Each was re-derived against the live
tree: the three vulnerabilities and Bug 1 were reproduced by executing the real code
(`DecisionClassifier.classify`, `PathGuard.check_write`, `sanitized_env`), Vuln 2's
promotion path was read at `policy.py` and confirmed by inspection, and Bug 4's
mechanism was confirmed against the hook source. Where I disagree with the report's
framing, it is said plainly below.

## Verdicts

### Vuln 1 — missing redirect operators `>&` / `>|` — **confirmed, agree High**

Reproduced: `echo pwned >& AGENTS.md` and `echo pwned >| .tst/config.yaml` both
classify `B / shell-floor`; the bare `>` form correctly classifies
`C / shell-steering-write`. The extraction gap is real and the skip-all promotion
makes B dangerous (see Vuln 2). Repair: TD-4817.

### Vuln 2 — skip-all promotes the shell floor — **confirmed, agree Medium**

`policy.py`'s TD-804 block promotes any non-C `ask` to `auto` under skip-all. The
shell B floor exists precisely because the classifier cannot see inside a command
string; promoting it converts "the parser missed it" into "it ran silently." The
report is right that this falsifies the TD-4805 DECISIONS rationale ("the B floor
already asks in every case the parser misses") — that sentence was only true while
skip-all was off. Repair: TD-4818 (shell-floor calls are exempt from promotion; the
doc sentence is corrected). One scope note: the exemption covers `shell-floor` only.
CU actuation under skip-all is the designed autonomous-run mode (TD-3301/TD-804),
and allowlisted web fetches under skip-all rest on two explicit user opt-ins; neither
is touched here.

### Vuln 3 — credential-URL regex requires a username — **confirmed, agree Medium**

Reproduced: `REDIS_URL=redis://:hunter2@prod-cache:6379/0` survives `sanitized_env()`.
The comment above the regex names `REDIS_URL` as motivation — the gap is the
mandatory username class. Repair: TD-4819, exactly the recommended relaxation
(`[^/\s@]*` user part), which carries zero false-positive risk on malformed input
because a match still requires the `:…@` tail.

### Bug 1 — trailing dot/space steering aliases — **confirmed; severity framing agreed**

Reproduced: `.tst/config.yaml.`, `AGENTS.md.`, `.tst./config.yaml`, `.tst/rules./x.md`
are all admitted by `check_write`. On this Mac they create inert lookalike files; on
Windows, Win32 strips trailing dots/spaces at open time and the write lands on the
real steering file. Creation-only and Windows-only is the right call on severity —
but the fix belongs in the fail-closed Windows-form detector regardless, because the
guard's contract is "unsafe on any shipped platform = refused everywhere."
Repair: TD-4820.

### Bug 2 — `svelte-check` red on `DesignLayer.svelte` — **confirmed (seen independently earlier today)**

Pre-existing on `main`; not caused by the E48 batch. Assessed during repair work:
the `never` narrowing is a real type error, the fix is a two-line guard. Repaired
under TD-4821 alongside Bug 4. (`MemoryProposalCard.svelte` warning left as-is —
a warning, not an error.)

### Bug 3 — tst-cu-mcp `tools/list` hang — **not re-verified; accepted on the report's reproduction**

The MCP server is a sibling package (`mcp/tst-cu-mcp`), not the daemon. The report's
3/3 reproduction outside pytest is credible and the failure mode (silent EOF exit)
matches a stdin-buffering bug. Filed as TD-4822 rather than repaired blind — the fix
belongs to whoever owns that package's protocol loop, with its own test pass.

### Bug 4 — secrets hook blocks tracked canaries — **confirmed; the report's framing is half-wrong**

The hook already honors `tst-secret-ok` (both grep chains filter it). The actual
defect is narrower: two tracked canary lines (`test_setup_state.py:214`,
`test_shell_tools.py:518`) carry `sk-` strings that match the TD-4801-widened
pattern but lack the marker. So Enhancement 2 ("teach the hook the convention") is
already done; the repair is marking the two canaries. TD-4821.

### Bug 5 / Enhancement 3 — mcp mypy platform gap — **accepted on inspection**

`backends/windows.py` references `WinDLL`/`WINFUNCTYPE`, which only exist on Windows
ctypes. The platform-independent fix is a mypy override scoped to that module.
Filed as TD-4823 with Bug 3's package — same owner, same pass.

### Enhancements 4–5

Enh 4 (adversarial pass over TD-4807/TD-4808) is a process item — recommended, not
actioned here. Enh 5 (E48 cross-check) required no action.

## Disposition summary

| Finding | Verdict | Action |
|---|---|---|
| Vuln 1 (redirect operators) | Confirmed, High | TD-4817 — repaired |
| Vuln 2 (skip-all × shell floor) | Confirmed, Medium | TD-4818 — repaired |
| Vuln 3 (credential URL regex) | Confirmed, Medium | TD-4819 — repaired |
| Bug 1 (trailing-char aliases) | Confirmed | TD-4820 — repaired |
| Bug 2 (svelte-check red) | Confirmed | TD-4821 — repaired |
| Bug 3 (mcp tools/list hang) | Accepted, not re-verified | TD-4822 — filed |
| Bug 4 (hook vs canaries) | Confirmed, reframed | TD-4821 — repaired |
| Bug 5 (mcp mypy macOS) | Accepted on inspection | TD-4823 — filed |
| Enh 1 (skip-all respects floor) | = Vuln 2 | TD-4818 — repaired |
| Enh 2 (hook convention) | Already present | TD-4821 — markers only |
| Enh 3 (mypy overrides) | = Bug 5 | TD-4823 — filed |
| Enh 4 (audit 4807/4808) | Process item | Recommended, not actioned |
| Enh 5 (E48 cross-check) | No action | — |

---

---

## Post-notes: repairs applied

All repairs shipped on branch `td/4817-review-repairs` (commits `babdcf8`,
`031862d`, `237f333`, `c2746f7`), stacked on `td/4808-network-rail`. Gates at the
tip: core pytest 2,296 passed · ruff check + format clean · mypy strict clean ·
UI vitest 999 passed · tsc clean · svelte-check 0 errors · secrets hook clean in
both directions.

**Vuln 1 → TD-4817 (commit `babdcf8`).** Added `>&` and `>|` to
`_REDIRECT_TOKENS` in `classifier.py` — shlex emits them as single punctuation
tokens, so the extractor now sees their targets and `echo x >& AGENTS.md` /
`echo x >| .tst/config.yaml` classify static C. A zsh clobber mark riding the
target (`>!file`, `>&!file`, spaced `>! file`) is stripped before the steering
check. Descriptor dups (`2>&1`, `>&2`, `exec 3>&1`) extract numeric non-targets
that never match steering; both directions are pinned in the parametrized tests.

**Vuln 2 → TD-4818 (commit `031862d`).** `resolve_explained` in `policy.py` no
longer promotes ask→auto under skip-all when the tool is `shell`. Scoped by tool
name rather than rule id — every shell B is the floor, and the name survives rule
renames. Explicit `shell: auto` workspace rules still win (they resolve before the
skip-all block), which is the deliberate escape hatch; CU actuation and
allowlisted web fetches keep their promotion. The "Class C outright" sentence in
`configuration.md` was replaced with what the table actually catches. Class B
decision recorded in DECISIONS.md.

**Vuln 3 → TD-4819 (commit `babdcf8`).** `_CREDENTIAL_URL_VALUE_RE` in
`shell.py` relaxed from a mandatory username (`[^/\s:]+`) to an optional one
(`[^/\s@]*`), so `redis://:pw@host` and the rediss/postgresql/mongodb shapes are
stripped. A match still requires the `:…@` tail — benign URLs untouched, existing
vectors unchanged. Parametrized tests added.

**Bug 1 → TD-4820 (commit `237f333`).** `windows_unsafe_reason` in `boundary.py`
now refuses any path component ending in `.` or space — one addition that covers
both `check_write` and the classifier's `boundary-unsafe-path` rule, since both
consult it. `.`/`..` navigation excluded; the bare `.tst/config.yaml` still
refuses as `steering_file`, not `windows_unsafe` (pinned so the new rule can't
mask the old one's provenance).

**Bug 2 → TD-4821 (commit `c2746f7`).** `DesignLayer.svelte`'s `overlaySize`
moved from the expression form `$derived({…})` to `$derived.by(() => ({…}))`.
The expression form is analyzed inline, where TS narrows the `bind:this` target
to `null` (template assignments are invisible to control-flow analysis) and
reports `clientWidth` on `never`; inside a closure the declared type applies.
svelte-check: 0 errors.

**Bug 4 / Enh 2 → TD-4821 (commit `c2746f7`).** The hook already honored
`tst-secret-ok` — the defect was five tracked canary lines lacking the marker.
Marked all five; verified the hook passes both files and still blocks an
unmarked canary (exit 1, verified by hand).

**Bugs 3, 5 / Enh 3 → filed, not repaired.** TD-4822 (tst-cu-mcp `tools/list`
hang) and TD-4823 (mcp mypy platform gap) are in the sibling package and deserve
their own pass with that package's owner. Enh 4 (adversarial pass over
TD-4807/TD-4808) remains a recommended process item.

Report by **cursor-1** (Cursor agent — Kimi K3) · 2026-08-21 · findings re-verified against HEAD `1337a83`; repairs at `c2746f7`
