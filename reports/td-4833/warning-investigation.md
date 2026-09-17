# TD-4833 — Residual warning investigation

## Scope

Investigation only; no product code, dependencies, steering, or backlog scope changed. Tests use local mock providers, not live provider calls or desktop actuation. This checkout now includes concurrent TD-4834 commit `354bfd7`; earlier UI/Rust/build results are historical, not rerun here.

## Findings

### 1. Intermittent subprocess teardown defect — medium priority, unresolved

The original warning is real, not just noisy logging: a subprocess transport finalizer tries to close its pipes after its event loop has closed. The test shown by pytest is where garbage collection exposed it, not necessarily where the process was created.

- `warning-focused.txt`: 51 passed, 1 failed with `-W error::pytest.PytestUnraisableExceptionWarning`. The failure repeats `BaseSubprocessTransport.__del__` → `RuntimeError: Event loop is closed`, this time attributed to `TestLaunch.test_launch_marks_the_session_and_seeds_the_prompt`. The transport representation includes a running process and open pipes. This does not establish that the process remained alive indefinitely.
- `warning-single.txt`: that test alone passed.
- `warning-full-strict.txt`: full suite passed with the same pytest warning promoted to error: **3348 passed, 8 skipped, 3 deselected**, 158.02 seconds. No warnings reported. One clean run does not prove this intermittent defect fixed.
- `warning-focused-trace.txt`: allocation tracing changed timing; 52 passed, with 24 config-file ResourceWarnings, not the subprocess warning.

Code-inspection leads (not established allocation origins):

- `core/tstd/autonomy/checkpoint.py::Checkpointer._git`: timeout cleanup exists, but cancellation during `communicate()` has no explicit kill/reap cleanup.
- `core/tstd/tools/shell.py::run_shell`: the external `CancelledError` path kills the process group, then cancels the work task, without explicitly awaiting process reaping in that branch.
- `core/tstd/grok_acp.py::AcpClient.close`: reader tasks are cancelled without being awaited; stdin closure is not awaited. Investigate shutdown races rather than assuming this is the source.

Next diagnostic increment: capture test node + creation stack for each local subprocess, force collection before each test loop closes, and reduce the failing test order. Do not log environment values or arbitrary subprocess arguments. Then add deterministic cancellation/shutdown regressions before selecting a fix. Avoid sleeps or warning suppression as a remedy.

### 2. Confirmed config file-handle cleanup defect — medium priority

`core/tstd/config.py:879`, `ensure_user_config`, opens both the bundled source and destination directly as arguments to `shutil.copyfileobj`. Neither handle is explicitly closed. CPython reference-count cleanup generally closes them promptly, but emits ResourceWarning; it is not deterministic resource management.

- `warning-autonomy.txt`: 13 passed, two ResourceWarnings with allocation traces pointing to this call (one reader and one writer).
- `config-warning-before.txt`: the launch test fails when both ResourceWarning and PytestUnraisableExceptionWarning are treated as errors, reporting those same two handles.
- These file warnings are distinct from the subprocess warning; fixing them must not be presented as fixing the subprocess leak.

Proposed fix: context-manage both streams. Test closure on success and copying failure, preservation of an existing user config, and creation with the resource-warning gates enabled. No new dependency required. No contents of user configuration need to be logged.

### 3. Frontend notices — optimization work, not build errors

Historical `vite.txt` exits zero. Two minified chunks exceed 500 kB:

| Chunk | Minified | Gzip | Evidence / implication |
| --- | ---: | ---: | --- |
| `DmcLAKkL.js` | 967.88 kB | 320.41 kB | Emitted content contains marked and extensive highlight.js language registration; statically imported by the main page node. `ui/src/lib/markdown.ts` imports the full `highlight.js` package. This is a concrete initial-load optimization candidate. |
| `DHwB1DNv.js` | 662.12 kB | 143.15 kB | Mermaid-related emitted dependency. Source already dynamically imports Mermaid in `markdown-mermaid.ts`; a large async dependency is not itself proof of slow startup. |

`Markdown.svelte` imports the KaTeX helper only when math placeholders exist; that helper's static KaTeX import is behind this dynamic boundary. Mermaid likewise loads only for relevant placeholders, after streaming. Simply adding another lazy wrapper is not an evidence-based fix.

Proposed sequence: measure packaged cold-start, main-thread parse time, text-only transcript load, first code fence, first math, and first diagram separately. Evaluate highlight.js core plus a deliberate language set or on-demand languages; preserve current language behavior/fallbacks and sanitization tests. Do not merely raise the size threshold.

The plugin-timing notice reports 4.9 seconds wall time versus 5.1 seconds in hooks, with SvelteKit compile/writeBundle at 3.2 seconds and overlapping guard resolution calls explicitly described as not measurable. These are build profiling diagnostics, not runtime latency, CPU utilization, or a build failure. Profile with the installed toolchain before proposing dependency upgrades or removing guards.

### 4. Remaining coverage gaps

The fresh full run identifies all eight skips:

- Four native Windows boundary semantics tests (`test_boundary.py`).
- One Windows Toolhelp32 parent-walk test (`test_build_sidecar.py`).
- Three README conditional documentation checks (`test_docs_readme_numbers.py`) whose trigger text is absent.

The three deselected live tests are in `test_e2e_live.py`, `test_e2e_m6.py`, and `test_e2e_m8.py`. They are not passes. No sustained-load, native GUI, packaged cross-platform, live provider failure, or external CU-backend typing claim follows from the automated results.

## Test execution limitations

- `warning-full.txt`: whole-suite run with tracemalloc was killed by the tool's 480-second limit near 89%. It has no completed suite result. Its Python-command-line warning filter was also invalid (`pytest` unavailable while Python parsed the `-W` option); the subsequent completed run passes the filter to pytest correctly. This aborted diagnostic is not counted as a product failure or verification pass.
- `warning-acp-autonomy.txt`: exploratory command used a nonexistent `test_acp*.py` glob, exited 4, and ran no tests. Corrected to `test_grok_acp.py` in the focused runs.
- UI, Rust, linters, and production build were not rerun for this report-only increment. Their previous passing results remain in README.md.

## Proposed plan, in order

1. Approve a small resource-lifecycle follow-up story: fix the confirmed file-handle defect; isolate and fix subprocess cleanup with deterministic regressions. Gate on resource/unraisable warnings without suppressions and rerun the full standard suite.
2. Approve a separate measured frontend performance story: establish baselines, optimize eager syntax-highlighting payload if measurements justify it, then verify rich-content rendering, fallback behavior, and sanitizer regressions.
3. Define a controlled live/soak milestone with explicit provider/spend permission and agreed duration, concurrency, memory, latency, cancellation and reconnect thresholds. Start with mocks; separately exercise native tray/multi-window, permissions, dictation and packaged platform behavior. Do not describe the application as comprehensively stress-tested until these criteria exist and pass.

No Class B product decisions made. These are proposals, not new approved backlog stories. TD-4833's stated scope excludes lifecycle repair and live/soak work; implementation needs a follow-up scope decision.
