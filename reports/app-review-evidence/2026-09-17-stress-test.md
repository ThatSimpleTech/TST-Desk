# TST Desk — automated regression review, 2026-09-17

Commit under test: `0e41bd3` (TD-1902), branch `td/4829-background-computer-use`.
Evidence logs: `reports/app-review-evidence/*.txt`. Logs have ANSI styling and trailing whitespace removed. Original observations below are historical; the final verification section supersedes them.
Working tree before test: user's own uncommitted TD-708 backlog + DECISIONS.md edits (untouched by this review).

## A. Verified failures

| # | Area | Failure | Evidence / repro |
|---|------|---------|------------------|
| A1 | core tests | `test_mcp_load.py::TestDeadServer::test_daemon_starts_and_doctor_fails` — doctor list gained a `grok_cli` row, test pins exact 6-row order (`daemon, api_key, provider, git, workspace, steering`); got `grok_cli` inserted at index 4 | `core-tests.txt`; reproduces standalone in 0.29 s |
| A2 | core tests | `test_docs_architecture_guide.py::test_every_client_message_is_documented` — client message `set_cu_policy` (added by TD-4830) is missing from `docs/architecture.md`'s client-message table | `core-tests.txt` |
| A3 | core lint | `ruff check`: 3 errors — I001 import order in `scripts/generate_protocol_fixtures.py:16` and `tests/test_keychain.py:6`; **F601 duplicate dict key `"transcribe"`** at `scripts/generate_protocol_fixtures.py:901` | `ruff.txt` |
| A4 | core format | `ruff format --check`: 3 files unformatted — `scripts/generate_protocol_fixtures.py`, `tests/test_grok_acp.py`, `tstd/grok_loop.py` | stated in §6 DoD |
| A5 | core types | `mypy --strict tstd`: 6 errors in 4 files — `session_store.py:121` (str|None → str), `grok_acp.py:166` (`StreamReader._limit` private reach), `cu_host.py:324` (`tst_cu_mcp` stub missing), `grok_loop.py:361,541,696` (content str|list confusion ×3) | `mypy.txt` — DoD §10 requires mypy --strict clean |
| A6 | ui tests | `tokens.test.ts` ×2 — `WakeupCard.svelte` styles use retired/undeclared tokens: `--color-border`, `--color-bg-raised`, `--color-text`, `--color-text-secondary`, `--color-text-muted`; `SettingsAbout.svelte` uses undeclared `--text-muted`. WakeupCard (TD-4303) predates the September token-vocabulary retirement and was missed in the migration | `ui-tests.txt` diff output |
| A7 | ui types | `svelte-check`: **1 error** — `src/lib/icons.ts:128` duplicate `mic` property in the ICONS literal (two hold-to-talk icons, one from merge `4b5f19e`); 2 warnings — `Composer.svelte:301` `aria-expanded` on implicit-textbox textarea; `Composer.svelte:471` unused CSS `.attach.listening` | `ui-check.txt` |

## B. Flakiness observed (improvement, not reproducible failure)

- B1: first full UI vitest run had **5 test files fail to import** (`katex`, `mermaid` "Failed to resolve import") although `node_modules/katex`/`mermaid` are installed; the identical run minutes later passed them with no install step in between. A cold Vite optimizer cache / parallel first-run race is a hypothesis, not an established cause. Costs trust in the suite: rerun before concluding.
- B2: when those import failures occurred, the summary read "5 failed test files / 2 failed tests" — file-level failures are easy to miss if a gate only counts test results.

## C. Passing (verified, not assumed)

- Core suite: **3238 passed**, 2 failed (A1, A2), 8 skipped, 3 deselected (`live` marker) in 133 s — `core-tests.txt`.
- Perf gates (TD-1404): all four core metrics pass — daemon cold start 11 ms, session start 15 ms, steering resolution 1 ms, first token 41 ms vs 2026-08-14 baselines; `timeline_render_1000` correctly UI-owned. `benchmarks.txt`.
- Shell: `cargo check` clean but 1 dead-code warning (D1); `cargo clippy --all-targets` clean; `cargo test` 70 tests green across all targets.
- UI build: `vite build` succeeds; static adapter writes site. Note: 967 kB / 320 kB-gzip chunk warning (improvement area).
- Prime directives spot-checks: `ws.py` refuses `0.0.0.0`/`::`/`*` hosts and e2e_m7 asserts bind refusal; no non-loopback bind found in non-test sources; credential-write/hygiene/redaction suites green in the run.

## D. Environment / onboarding findings

- D1: fresh shell has **no `node`, `npm`, `uv`, `cargo`, or `python3 ≥ 3.11` on PATH**. uv lives in `~/.local/bin`, cargo in `~/.cargo/bin`; the only Node on the machine is a kiro-cli-bundled binary and npm was **not found in the locations checked** — `npm install`/CI scripts cannot run as documented. "Clone and run one command" fails at step one on a new machine.
- D2: system Python is 3.9.6 vs required ≥3.11 — works only because uv provisioned 3.11.16 into `core/.venv`; README should say `uv run` explicitly.
- D3: `vite-plugin-svelte` warns "no Svelte config found" — `svelte.config.js` is absent (config is inline in `vite.config.ts`). Noisy, nonstandard.

## E. Areas of improvement (no failure today)

- E1: A1/A2 are stale test expectations and a stale doc table after TD-4829/4830 — cheap fixes, restore green.
- E2: A5 `grok_loop.py` type errors suggest real str/list ambiguity in ACP payload handling — worth a look beyond the type fix.
- E3: `tstd/daemon.py` 3493 lines, `protocol.py` 2657, `AppShell.svelte` 740 — §6 asks for ~400-line files; either split or record the exception.
- E4: `shell/src/tray.rs:9` `pub fn setup` is dead code (§6 forbids it).
- E5: consider pre-warming the vite optimizer cache in CI (B1) or sharding UI tests.
- E6: 967 kB chunk — `mermaid`/`katex` are already split out; the main chunk could take dynamic imports next.
- E7: pytest unraisable warning in `test_autonomy_loop.py::test_class_c_stops_and_notifies` (subprocess transport closed after loop close) — minor, but a leak pattern.

## Final verification and limitations

Follow-up: TD-4831 on `td/4831-stress-review-final`, based on `d12ea54`
plus the changes listed below. Other work advanced the repository during this
review (including TD-708 and a merge); initial and final counts are not directly
comparable, and these are working-tree results, not an immutable release certification.
`followup-*` logs are intermediate, including a subsequently corrected test-path
TypeScript error; use `final-*` for the latest recorded gates.

### Fixes verified

- Dead MCP server test accepts additional doctor rows while retaining failed-server,
  remediation and daemon-start assertions.
- `set_cu_policy` is documented in the architecture table (already in the current
  base). Its generated cross-language fixture was accidentally removed during this
  review and has been restored from the generator; the existing UI test passes.
- WakeupCard and SettingsAbout use canonical color tokens; About's error color also
  uses `--color-err`. The token test previously overlooked `--danger` because its
  declaration regex can mistake a similarly named CSS selector for a declaration;
  tightening that test is an improvement opportunity.
- Duplicate microphone glyph removed; a new TypeScript-AST test detects duplicate
  icon names before JavaScript can silently overwrite them. Filesystem access uses
  the existing test convention (`node:path.resolve`), not URL pathname conversion.
- Ruff formatting/import ordering applied to the modified Python files. No runtime
  Python type fixes were attempted in this increment.

### Latest gate results

| Gate | Result | Evidence |
|---|---|---|
| Core default offline suite | 3335 passed, 8 skipped, 3 live tests deselected | `final-core-tests.txt` |
| UI | 1338 passed, 82 files | `final-ui-tests.txt` |
| Svelte check / TypeScript | 0 errors, 2 Svelte warnings; tsc exit 0 | `final-ui-check.txt`, `final-tsc.txt` |
| UI production build | exit 0, large-chunk warnings | `final-ui-build.txt` |
| Rust tests | 63 passed (53 + 3 + 7) | `final-rust-tests.txt` |
| Clippy | exit 0, unused `tray::setup` warning | `final-clippy.txt` |
| Ruff lint / format | pass | `final-ruff.txt`, `final-format.txt` |
| mypy strict | **11 errors in 5 files** | `final-mypy.txt` |

Correction: the initial prose reported 70 Rust tests and described clippy as clean.
The recorded follow-up/final logs show **63**, and clippy succeeds **with a warning**.
The initial benchmark log reports medians, not a full sustained-load experiment;
no blanket security or performance certification follows from those results.

### Remaining errors and improvements, prioritized

1. **Release gate blocker — Python types:** `session_store.py:121` optional string;
   `grok_acp.py:166` private StreamReader attribute; `grok_loop.py:361,539,694`
   string/list and optional-image handling; `cu_host.py:324` and
   `desktop/inprocess.py:55,66,83,135,143` missing sibling-package types.
   Review runtime payload semantics before applying casts or suppressions.
2. **Accessibility:** Composer's textarea has unsupported `aria-expanded`;
   `.attach.listening` is unused CSS. Verify slash-menu behavior with assistive
   technology when correcting the semantics.
3. **Lifecycle:** Rust `tray::setup` is unused. Investigate intended tray ownership
   rather than deleting it solely to silence a warning. An intermittent Python
   subprocess-transport cleanup warning occurred in earlier full runs but not
   the final rerun; absence once does not establish a fix.
4. **Build and maintainability:** client chunks reach 967.88 kB (320.41 kB gzip)
   and 662.12 kB. Profile before changing lazy loading. Large modules exceed the
   repository's ~400-line guidance. Vite reports missing Svelte config and earlier
   UI import flakiness remains unexplained.
5. **Onboarding:** tool binaries were absent from the initial shell PATH; checks
   used installed uv/cargo and the bundled Node directly. This is an environment
   finding, not proof that installation fails on every fresh machine.

### What this review does NOT verify

- Exhaustive feature-by-feature interaction in a packaged desktop application.
- Sustained concurrency, soak testing, memory growth, disk exhaustion or recovery
  under resource pressure. Existing benchmarks are short measurements.
- Live provider behavior/spend, real credential workflows, microphone capture,
  OS permissions and desktop actuation, external notifications, updates/signing,
  or Windows/Linux runtime behavior. Mock/headless tests are not substitutes.
- A comprehensive security audit. Passing boundary/redaction tests are useful
  evidence only for the cases they exercise.

**Status: blocked against the repository Definition of Done.** Automated test
suites pass, but strict typing and warnings remain. A separately scoped live/soak
plan with controlled fixtures and explicit permissions is needed before calling
this an all-features stress test.
