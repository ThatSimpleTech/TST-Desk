# TD-4833 — Strict typing and compiler-warning cleanup

## Summary

Resolved the 11 reported strict-mypy errors, removed the Composer accessibility/unused-CSS warnings, and removed the unused tray initializer without changing the active Show / New window / Quit path. ACP now configures its reader limit through asyncio's public subprocess API. Assistant continuation no longer raises a TypeError for list-based content and retains attachments and tool-call metadata.

This is an automated regression and cleanup review, **not an exhaustive application stress test**. No live model calls, desktop actuation, prolonged soak, or cross-platform GUI validation were performed by this story.

## Verification

Tests ran on macOS with local dependencies and offline dependency resolution. The shared checkout incorporated TD-4832 during this work; full-suite results include its tests. Its changes are not part of this cleanup commit. The TD-4833 backlog and decision entries were already included in commit `31922cf` by concurrent work.

| Check | Result | Evidence |
| --- | --- | --- |
| Full core pytest suite | 3348 passed, 8 skipped, 3 deselected; one warning | [core-tests.txt](core-tests.txt) |
| Full UI Vitest suite | 1343 passed in 84 files | [vitest.txt](vitest.txt) |
| Rust tests | 63 passed (53 unit + 10 integration) | [rust-tests.txt](rust-tests.txt) |
| Strict mypy, product code | Pass, 150 source files | [mypy.txt](mypy.txt) |
| Ruff lint | Pass | [ruff.txt](ruff.txt) |
| Ruff format | Pass, 366 files | [format.txt](format.txt) |
| TypeScript | Pass | [tsc.txt](tsc.txt) |
| Svelte check | Zero errors and warnings | [svelte-check.txt](svelte-check.txt) |
| Clippy, all targets, warnings denied | Pass | [clippy.txt](clippy.txt) |
| Production Vite build | Pass, with notices below | [vite.txt](vite.txt) |

### Acceptance criteria

- **PASS:** “Core strict mypy passes; optional computer-use import typing limitations are documented, not represented as verified backend types” — strict check passes; the package-scoped missing-import exemption and its limitations are recorded in DECISIONS.md.
- **PASS:** “ACP accepts large stdout lines through the public asyncio API; assistant text updates preserve multimodal content and message metadata” — existing scripted 70 KiB ACP stdout test passes; five new continuation cases cover None/empty/text, multimodal parts, metadata, and preserving the preceding user message. The pre-fix helper reproduced `TypeError: can only concatenate list (not "str") to list`; the corrected helper preserves the image part and appends text.
- **PASS:** “Session engine persistence and image-capability fallback tests pass” — session-store and Grok loop/ACP tests passed in the 58-test focused run and full suite.
- **PASS:** “Composer compiles without accessibility or unused-CSS warnings; slash-command tests pass” — new compiler-diagnostic regression and existing slash-command tests pass; whole-project Svelte check reports zero warnings.
- **PASS:** “Only the active tray initializer remains; Rust tests and clippy with warnings denied pass” — removed unused `tray::setup`; active `lib.rs::build_tray` and running-count command remain. Rust lifecycle tests pass. Native tray interaction was not manually exercised.
- **PASS:** “Full core and UI suites, Ruff lint/format, TypeScript and production build pass; residual review limitations are recorded” — see table and limitations below.

## Remaining errors, risks, and improvements

1. **Investigate subprocess cleanup (medium priority).** The full core run emitted a `PytestUnraisableExceptionWarning` from `BaseSubprocessTransport.__del__`: `RuntimeError: Event loop is closed`, reported during `test_class_c_stops_and_notifies`. All tests passed, but the allocation source and reproducibility have not been established; the reported test is not necessarily the origin. Trace allocations and verify subprocess pipes/tasks are awaited before loop teardown. Do not call the full run warning-free.
2. **Measure frontend loading and bundle size (medium priority).** Vite emitted a large-chunk warning and plugin-timing notices. Build completion is verified, not acceptable startup latency or memory consumption. Profile before selecting lazy-loading/code-splitting changes; do not merely raise the warning threshold.
3. **Live and soak coverage remains open (coverage gap).** Exercise real provider failures and streaming, native tray/multi-window behavior, browser/desktop permissions, dictation, reconnection, long conversations, and sustained concurrent sessions in a controlled test workspace. Define durations, concurrency, latency and memory thresholds before calling this stress-tested. Live tests were deselected; skipped tests are not passes.
4. **External backend typing is not verified (known limitation).** The optional `tst_cu_mcp` package and submodules retain a narrow missing-import exemption. A passing core mypy run does not establish correctness of that sibling package's signatures or platform behavior.
5. **Platform coverage is limited.** Rust and Python results here are from macOS, not Windows/Linux packaged application runs. A successful frontend build is not an end-to-end packaged application validation.

## Changes

- `core/pyproject.toml`: extend the existing optional-package typing exemption to its submodules.
- `core/tstd/session_store.py`: explicitly type the optional engine local.
- `core/tstd/grok_acp.py`: public subprocess reader-limit argument; remove duplicate constant declaration.
- `core/tstd/grok_loop.py`: project prior content to text for delta comparison; explicitly type optional prompt images; preserve continuation content and metadata.
- `core/tests/test_grok_acp.py`: retain behavioral large-line coverage, remove private-reader implementation assertions.
- `core/tests/test_grok_message_continuation.py`: five continuation regression cases.
- `ui/src/lib/components/chat/Composer.svelte`: remove unsupported textarea `aria-expanded` and obsolete attachment listening CSS.
- `ui/src/lib/composer-warnings.test.ts`: assert warning-free Composer compilation.
- `shell/src/tray.rs`: remove inactive duplicate initializer and unused imports.
- `DECISIONS.md`, `docs/tst-desk-backlog.md`: decision, scope and acceptance records (already committed in `31922cf`).

No provider configuration, credentials, steering files, or dependency versions were changed by this cleanup.
