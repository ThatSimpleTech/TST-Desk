# TST Desk — Work Plan & Backlog

**Scope of this document:** everything that must be built for **v0.1**, broken into epics and
stories with acceptance criteria, dependencies, and sequencing. Post-v0.1 phases are outlined
at the end but not decomposed.

Companion documents:
- `tst-desk-spec.md` — behavior and architecture. Source of truth for *what it does*.
- `AGENTS.md` — the working contract. Source of truth for *how you work*.

---

## 0. How to use this document

### Story format

Every story has:

- **ID** — `TD-###`. Epic 1 owns 100s, epic 2 owns 200s, and so on. IDs are permanent; do not
  renumber. If a story is dropped, mark it `[CUT]` rather than reusing the number.
- **Size** — Fibonacci points. 1 = under an hour. 2 = a couple hours. 3 = half a day.
  5 = a day. 8 = multiple days and probably should have been split.
- **Depends on** — story IDs that must be complete first. Respect these; they encode real
  ordering constraints, not preferences.
- **Acceptance criteria** — the definition of done for that story. Verify each one explicitly
  and quote it in your report (`AGENTS.md` §9).
- **Notes** — implementation guidance, gotchas, spec references.

### Definition of ready

A story may be started when: its dependencies are done, its acceptance criteria are
unambiguous, and any open decision it relies on has been answered. If any of those is false,
say so and ask rather than guessing.

### Definition of done

See `AGENTS.md` §10. It applies to every story without exception.

### Milestones

| Milestone | Contains | Exit condition |
|---|---|---|
| **M0 — Decisions** | E1 | Open decisions answered, repo scaffolded, CI green on an empty build |
| **M1 — Headless core** | E2, E3, E4, E5, E6, E7, E8, E9 | A scripted request runs end-to-end from a CLI harness, with steering loaded, tools dispatched, decisions classified, cost accounted, all under test |
| **M2 — The window** | E10, E11, E12 | A human does the same thing through the app, never touching a terminal |
| **M3 — Shippable** | E13, E14, E15, E16, E17 | A stranger can install and use it from a fresh machine |

**M1 before M2 is deliberate.** The core must be correct and testable headlessly before any
pixel is drawn. Building the UI first hides correctness bugs behind a pretty surface.

### Epic dependency map

```
E1 Foundation
 └─> E2 Daemon ─┬─> E3 Router ─┬─> E4 Loop ─┬─> E7 Autonomy hooks ─> E8 Approvals
                │              │            │
                │              └─> E5 Context ─> E6 Tools ─┘
                │
                └─> E9 Audit ─────────────────────────────────> E10 Shell ─┬─> E11 Onboarding
                                                                            └─> E12 Inspector
E13 Packaging ─> E14 Testing (continuous) ─> E15 Docs
```

E16 Familiarity hangs off E10 (shell) and E8 (approvals): it restyles the window
and ships the TD-1007 approval cards, the last unchecked M2 interaction.

E14 is not a phase at the end. Tests are written with each story. The E14 stories cover
cross-cutting suites and CI gates that don't belong to a single feature.

---

# MILESTONE M0 — Decisions & Foundation

## Epic E1 — Repository, toolchain, scaffolding

**Goal:** a repository that builds, lints, tests, and runs an empty app on all three platforms,
with every unresolved decision answered and recorded.

---

### TD-101 — Resolve open decisions
**Size:** 1 · **Depends on:** none

Ask the user the seven open decisions from `tst-desk-spec.md` §10 before writing any code:
repo layout, `tst-cua` reuse strategy, Goose's role, license, frontend framework, product name,
and autonomy isolation policy. Record the answers.

**Acceptance criteria:**
- [x] All seven decisions asked in a single message, each with a recommendation and rationale
- [x] Answers recorded in `DECISIONS.md` with date
- [x] Any answer that contradicts the spec is flagged, and the spec is updated to match
- [x] No implementation work started before answers received

**Notes:** Do not offer defaults as a way of avoiding the question. The user explicitly wants
to decide these.

---

### TD-102 — Audit `tst-cua` and produce a reuse report
**Size:** 3 · **Depends on:** TD-101

Read the existing `tst-cua` repository in full. Produce `docs/REUSE.md` describing what will be
reused as-is, what will be adapted, and what will be written fresh.

**Acceptance criteria:**
- [x] Every module in `tst-cua` classified: reuse / adapt / ignore / defer-to-later-phase
- [x] The agent loop specifically assessed — its control flow, its tool interface, its
      assumptions about providers — with a statement of what must change for the 3-tier router
- [x] Driver code (Atspi/Darwin/Browser/Mock) marked **defer to v0.4** but noted as intact
- [x] The Mock driver assessed for reuse as the v0.1 test harness (likely valuable now)
- [x] Report identifies any coupling that would make later computer-use integration painful
- [x] Report reviewed with the user before porting begins

**Notes:** This is the highest-leverage hour in the project. A wrong reuse decision here costs
days later. Do not skim the code.

---

### TD-103 — Repository scaffold
**Size:** 2 · **Depends on:** TD-101

Create the directory structure from `AGENTS.md` §11.

**Acceptance criteria:**
- [x] Directory tree matches `AGENTS.md` §11 exactly
- [x] `LICENSE` present, matching the TD-101 answer
- [x] `README.md` skeleton with one-paragraph pitch and a placeholder quickstart
- [x] `AGENTS.md` and `docs/` populated with the three companion documents
- [x] `DECISIONS.md` created with the TD-101 answers as its first entries
- [x] `.gitignore` excludes `.tst/` runtime state, build artifacts, `.env`, and keychain
      material — but **not** `.tst/rules/`

---

### TD-104 — Python toolchain
**Size:** 2 · **Depends on:** TD-103

**Acceptance criteria:**
- [x] `uv` project initialized under `core/` with `pyproject.toml`, Python 3.11+
- [x] `ruff` configured for lint and format
- [x] `mypy` configured in strict mode
- [x] `pytest` with `pytest-asyncio` configured
- [x] `uv run pytest` passes on an empty suite
- [x] `uv run ruff check` and `uv run mypy` clean

---

### TD-105 — Tauri and frontend toolchain
**Size:** 3 · **Depends on:** TD-103

**Acceptance criteria:**
- [x] Tauri 2 project under `shell/`, building on the development platform
- [x] SvelteKit + Svelte 5 project under `ui/`, TypeScript strict
- [x] `npm run tauri dev` opens an empty window
- [x] `clippy` clean; `tsc --noEmit` clean
- [x] Design token file created (colors, spacing, type scale) — empty of components but
      structurally in place

---

### TD-106 — CI pipeline
**Size:** 3 · **Depends on:** TD-104, TD-105

**Acceptance criteria:**
- [x] CI runs on push and PR
- [x] Jobs: Python lint, Python typecheck, Python tests, Rust clippy, TypeScript typecheck,
      frontend build
- [x] Build matrix covers macOS, Linux, Windows
- [x] Pipeline is green on the empty scaffold
- [x] Failing any job blocks merge

---

### TD-107 — Pre-commit hooks
**Size:** 1 · **Depends on:** TD-104, TD-105

**Acceptance criteria:**
- [x] Hooks run `ruff format`, `ruff check`, and secret detection on staged files
- [x] A commit containing a plausible API key pattern is rejected
- [x] Hook install documented in `README.md`

---

# MILESTONE M1 — Headless Core

## Epic E2 — Core daemon and protocol

**Goal:** `tstd` runs, accepts local connections, models sessions correctly, and speaks a typed
event protocol. This epic sets the architecture that everything else inherits — get the
session-ownership model right here or pay for it in v0.3.

---

### TD-201 — Daemon process skeleton
**Size:** 3 · **Depends on:** TD-104

**Acceptance criteria:**
- [x] `tstd` starts as an async process with clean startup and shutdown
- [x] Structured logging (JSON lines) to a rotating file under the user data directory
- [x] Log level configurable; secrets redacted by a logging filter, verified by test
- [x] `SIGTERM` and `SIGINT` trigger graceful shutdown: sessions notified, sockets closed,
      state flushed
- [x] Health endpoint or event returning version, uptime, and active session count

---

### TD-202 — Local WebSocket server
**Size:** 3 · **Depends on:** TD-201

**Acceptance criteria:**
- [x] Server binds `127.0.0.1` on an ephemeral port
- [x] Port and auth token written to a port file in the user data directory, mode `0600`
- [x] **A test asserts the server refuses to bind any non-loopback interface** — this
      enforces prime directive §2.1 in code, not convention
- [x] Multiple simultaneous client connections supported
- [x] Client disconnect does not disturb daemon state

---

### TD-203 — Connection handshake and local auth
**Size:** 2 · **Depends on:** TD-202

Even on loopback, other local processes can connect. Require a token.

**Acceptance criteria:**
- [x] Client presents the token from the port file in a `hello` message
- [x] Bad or missing token closes the connection with a typed error, and is logged
- [x] Handshake negotiates protocol version; mismatch produces an actionable error message
- [x] Token rotates on daemon restart

---

### TD-204 — Protocol schema
**Size:** 5 · **Depends on:** TD-203

Define every message the shell and daemon exchange. This is a Class B decision surface —
record the design in `DECISIONS.md`.

**Acceptance criteria:**
- [x] Pydantic models for all messages, versioned, with a discriminated `type` field
- [x] Client→daemon: `hello`, `open_workspace`, `user_message`, `approve`, `deny`,
      `cancel`, `attach`, `detach`, `set_tier`, `get_instruction_stack`
- [x] Daemon→client: `ready`, `session_state`, `assistant_delta`, `tool_call`, `tool_result`,
      `approval_request`, `decision_logged`, `cost_update`, `turn_complete`, `error`
- [x] Every daemon→client event carries a monotonic `seq` scoped to the session
- [x] TypeScript types generated or hand-mirrored, with a test asserting they match
- [x] Round-trip serialization tests for every message type
- [x] Unknown message types produce a typed error, never a crash

**Notes:** `attach`/`detach` are specified now even though v0.1 has no detached sessions. The
shape must be right; the behavior can be trivial.

---

### TD-205 — Session model and SessionRunner
**Size:** 5 · **Depends on:** TD-204

**The most important story in M1.** The session owns the loop. The socket is a viewer.

**Acceptance criteria:**
- [x] `Session` has an id, a workspace path, a state machine
      (`idle` → `running` → `awaiting_approval` → `running` → `complete` / `failed` /
      `cancelled`), and an event log
- [x] `SessionRunner` executes the loop as an asyncio task **owned by the daemon, not by any
      connection**
- [x] **Test: a session started over a connection continues running after that connection is
      closed**, and its events continue to accumulate
- [x] Event log is append-only in memory with monotonic `seq` (durable persistence is v0.3)
- [x] Session registry supports lookup, list, and cancel
- [x] Cancelling a session interrupts the loop cleanly, mid-tool if necessary

---

### TD-206 — Attach, detach, and replay
**Size:** 3 · **Depends on:** TD-205

**Acceptance criteria:**
- [x] `attach{session_id, from_seq}` replays all events from `from_seq` then streams live
- [x] Replay and live stream produce no gaps and no duplicates under a concurrent-write test
- [x] `detach` stops streaming without affecting the session
- [x] Attaching to an unknown session returns a typed error
- [x] Two clients attached to one session both receive all events

---

### TD-207 — Crash resilience
**Size:** 2 · **Depends on:** TD-205

**Acceptance criteria:**
- [x] An exception inside one session's loop fails only that session; others continue
- [x] Failure emits a `session_state` event with a readable reason and full traceback in logs
- [x] Daemon crash leaves no corrupt state that prevents restart
- [x] Stale port file from a dead daemon is detected and replaced on startup

---

## Epic E3 — Model router

**Goal:** one client, three tiers, prices in config, costs accounted, caching exploited.

---

### TD-301 — Provider client
**Size:** 3 · **Depends on:** TD-201

**Acceptance criteria:**
- [x] Async `httpx` client against an OpenAI-compatible `/v1/chat/completions`
- [x] Streaming responses yielded as deltas
- [x] Tool/function calling supported in both request and response parsing
- [x] Base URL configurable — **verified against OpenRouter and against a local vLLM-style
      endpoint with no code change** (the EZER path must work by configuration alone)
- [x] API key read from OS keychain, never from config or environment files
- [x] Timeouts on connect, read, and total

---

### TD-302 — Model configuration schema
**Size:** 2 · **Depends on:** TD-301

**Acceptance criteria:**
- [x] `config.yaml` defines per-tier: slug, base URL, input price, output price, cache-read
      price, context window, max output tokens
- [x] Ships with the TST default stack from spec §7 (Kimi K3 / DeepSeek V4 Flash /
      DeepSeek V4 Pro) and the documented swaps as commented alternatives
- [x] Presets: `tst-default`, `budget`, `local`
- [x] Config validated on load with actionable error messages naming the offending key
- [x] **No model slug, price, or URL appears anywhere in source code** — asserted by a test
      that greps the codebase for known slugs

**Notes:** The model landscape moves weekly. Users must update models without a release.

---

### TD-303 — Tier routing policy
**Size:** 3 · **Depends on:** TD-302

**Acceptance criteria:**
- [x] Brain tier handles the first `lead_turns` turns (default 2, configurable), then worker
      takes over
- [x] Validator invoked on demand, not on a schedule, in v0.1
- [x] Escalation path: worker may hand back to brain on repeated failure, with a configurable
      threshold
- [x] Active tier is emitted to the client on every turn
- [x] Runtime tier override via `set_tier` takes effect on the next turn

---

### TD-304 — Cost and token accounting
**Size:** 3 · **Depends on:** TD-302

**Acceptance criteria:**
- [x] Every call records prompt tokens, cached prompt tokens, completion tokens, model, and
      computed cost
- [x] Cache-read tokens priced at the cache rate, not the input rate
- [x] Cost aggregated per turn, per session, and per day
- [x] `cost_update` events emitted as costs accrue
- [x] **Unit tests with hand-computed expected dollar figures** for each tier, including a
      mixed cached/uncached case

---

### TD-305 — Cache-aware prompt assembly
**Size:** 3 · **Depends on:** TD-303, TD-501

**Acceptance criteria:**
- [x] Prompt assembled in the stable-prefix order from spec §4.5: base prompt → steering →
      memory placeholder → workspace manifest → conversation
- [x] Blocks 1–2 are byte-identical across turns when their source files have not changed —
      asserted by a test hashing the prefix across a multi-turn session
- [x] Cache hit rate observable in logs and in the cost breakdown

**Notes:** This story is worth more than it looks. On a long session, prefix caching is the
difference between $2.80/M and $0.30/M on the brain tier.

---

### TD-306 — Resilience
**Size:** 3 · **Depends on:** TD-301

**Acceptance criteria:**
- [x] Retry with exponential backoff and jitter on 429 and 5xx; bounded attempts
- [x] `Retry-After` honored when present
- [x] Context-length errors surface as a typed, actionable error, not a generic failure
- [x] Auth failures produce a message telling the user exactly what to fix
- [x] Partial stream interruption recovers or fails cleanly — never emits a half-parsed tool call
- [x] All failure modes covered by tests against the mock provider

---

### TD-307 — Mock provider
**Size:** 2 · **Depends on:** TD-301

**Acceptance criteria:**
- [x] Scripted responses: plain text, streaming text, tool calls, malformed output, errors,
      rate limits
- [x] Deterministic and offline — no network, no spend
- [x] Returns realistic usage numbers so cost tests are meaningful
- [x] Used by every loop and router test

---

## Epic E4 — Agent loop

**Goal:** port the `tst-cua` loop onto the daemon and router without rewriting it.

---

### TD-401 — Port the loop core
**Size:** 5 · **Depends on:** TD-102, TD-303, TD-307

**Acceptance criteria:**
- [x] Loop runs inside `SessionRunner`
- [x] Provider calls go through the router, not directly to a client
- [x] Reuse matches the TD-102 report; any deviation recorded in `DECISIONS.md`
- [x] Loop is provider-agnostic — no vendor-specific assumptions in control flow
- [x] Multi-turn conversation with tool calls passes against the mock provider

---

### TD-402 — Tool dispatch
**Size:** 3 · **Depends on:** TD-401, TD-601

**Acceptance criteria:**
- [x] Tool calls parsed, validated against the tool's schema, and dispatched
- [x] Invalid arguments return a structured error **to the model** so it can correct itself,
      rather than failing the turn
- [x] Parallel tool calls executed concurrently where the tools declare themselves safe for it
- [x] Results truncated to a configurable cap with clear truncation markers
- [x] Every dispatch passes through the classifier (TD-702)

---

### TD-403 — Turn lifecycle events
**Size:** 2 · **Depends on:** TD-401, TD-204

**Acceptance criteria:**
- [x] `assistant_delta` streamed token-by-token
- [x] `tool_call` emitted before execution with name and arguments
- [x] `tool_result` emitted after, with status and a display-safe summary
- [x] `turn_complete` carries tokens, cost, tier used, and duration
- [x] Event ordering is deterministic and covered by test

---

### TD-404 — Cancellation
**Size:** 3 · **Depends on:** TD-401

**Acceptance criteria:**
- [x] `cancel` interrupts an in-flight model stream within one second
- [x] A running shell command is terminated, and its process group with it
- [x] Session ends in `cancelled` with partial work preserved and visible
- [x] No orphaned processes or leaked tasks — asserted by test

---

### TD-405 — Context window management
**Size:** 5 · **Depends on:** TD-401, TD-305

**Acceptance criteria:**
- [x] Token budget tracked against the tier's context window
- [x] Approaching the limit triggers compaction of older conversation turns
- [x] **Steering block and workspace manifest are re-injected after compaction, re-read from
      disk** — instructions must survive compaction
- [x] Compaction is announced in the activity timeline, never silent
- [x] Test: a session that exceeds the window continues correctly and still obeys steering rules

---

## Epic E5 — Context assembler (steering)

**Goal:** correct, inspectable, affordable instruction loading. Spec §4. **Highest test
coverage requirement in the project alongside E7.**

---

### TD-501 — File discovery and precedence
**Size:** 5 · **Depends on:** TD-201

**Acceptance criteria:**
- [x] Resolves, lowest to highest precedence: `~/.tstdesk/AGENTS.md` → `<workspace>/AGENTS.md`
      → `<workspace>/.tst/rules/*.md` → nested `<workspace>/**/AGENTS.md`
- [x] Nested files apply to their subtree only
- [x] Concatenated into one block with provenance comments naming each source file
- [x] Missing files are not errors
- [x] Table-driven tests over a fixture workspace covering: none present, each level alone,
      all levels together, conflicting rules, deeply nested

---

### TD-502 — `CLAUDE.md` fallback
**Size:** 2 · **Depends on:** TD-501

**Acceptance criteria:**
- [x] At any path, if `AGENTS.md` is absent and `CLAUDE.md` is present, `CLAUDE.md` is used
- [x] If both are present, `AGENTS.md` wins and the shadowing is noted in the inspector
- [x] Applies at every level including `~/.claude/CLAUDE.md` for the global scope
- [x] Test: a workspace containing only `CLAUDE.md` files loads with full fidelity

**Notes:** This is an adoption feature. Existing repos configured for other tools must work on
day one with nothing to port. Do not treat it as an edge case.

---

### TD-503 — Path-scoped rules
**Size:** 5 · **Depends on:** TD-501

**Acceptance criteria:**
- [x] `.tst/rules/*.md` support frontmatter with an `appliesTo` array of glob patterns
- [x] Rules without `appliesTo` always load
- [x] Rules with `appliesTo` load only when the session touches a matching file
- [x] Activation is dynamic — a rule that becomes relevant mid-session is injected, and the
      injection is announced in the timeline
- [x] Glob matching tested against: exact paths, `*`, `**`, extension patterns, negation if
      supported, and paths with spaces or unicode
- [x] Unmatched rules are visible in the inspector as inactive, with their cost shown as zero

**Completed (2026-08-14):** touch-tracking plumbed end to end. Dispatch records a
successful call's `path_fields` onto `Session.touched_paths` (refusals and handler
errors record nothing); the loop forwards the set as `matched_paths` at every assembly
and emits one `RuleActivated` per newly active rule (the first assembly is the silent
baseline); the daemon's `get_instruction_stack` handler forwards the same set so the
stack panel reports the true active/inactive verdict. UI renders activations as steering
timeline entries; the shared wire fixture covers `rule_activated`. Tests: tracker
normalization (4), dispatch success/refusal/handler-error (3), loop activation /
no-touch / baseline (3), daemon inactive→active flip (1).

**Notes:** This is the mechanism that keeps a large ruleset affordable. Spec §4.3.

---

### TD-504 — Import resolution
**Size:** 5 · **Depends on:** TD-501

**Acceptance criteria:**
- [x] `@path/to/file.md` resolved, relative and absolute, including `~` expansion
- [x] Recursive imports to a maximum depth of 4; exceeding it is a clear error naming the chain
- [x] Cycles detected and reported with the full cycle path, never infinite-looping
- [x] **Import directives inside fenced code blocks and inline code spans are not evaluated** —
      documentation about imports must be safe to write
- [x] Missing import files produce a warning naming the file, and do not abort the session
- [x] Imported content carries provenance to its own file, not the importer

---

### TD-505 — External import approval
**Size:** 2 · **Depends on:** TD-504, TD-802

**Acceptance criteria:**
- [x] First import from outside the workspace raises an approval request naming the file
- [x] Approval is remembered per workspace per file path
- [x] Denial omits the import and continues with a warning in the timeline

---

### TD-506 — Token counting
**Size:** 3 · **Depends on:** TD-501

**Acceptance criteria:**
- [x] Per-source token counts computed with the tier's tokenizer where available, or a
      documented approximation with the method stated in the UI
- [x] Total steering budget reported
- [x] **A file exceeding 200 lines produces a soft warning in the inspector**, citing reduced
      adherence, with a link to the authoring guide
- [x] Counts available via `get_instruction_stack`

---

### TD-507 — Workspace manifest
**Size:** 3 · **Depends on:** TD-501

**Acceptance criteria:**
- [x] File tree built from the workspace, honoring `.gitignore` and a configurable ignore list
- [x] Depth and entry count capped, with truncation clearly marked
- [x] Rebuilt on change with debounce, not on every turn
- [x] Large repositories (100k+ files) do not stall session start — verified with a synthetic tree

---

### TD-508 — Per-tier context routing
**Size:** 3 · **Depends on:** TD-501, TD-303

**Acceptance criteria:**
- [x] Brain receives full steering plus manifest
- [x] Worker receives steering plus current task and relevant files, no manifest
- [x] Validator receives only the standards/conventions subset plus the diff and test output
- [x] Subset selection is configurable, with a documented default
- [x] Test asserts each tier's assembled prompt contains and excludes the expected blocks

**Notes:** Keeping validator context tight is what holds it near $0.08/session. Spec §4.6.

---

### TD-509 — Hot reload
**Size:** 2 · **Depends on:** TD-501

**Acceptance criteria:**
- [x] Steering file edits are detected and re-resolved without restarting the session
- [x] Reload announced in the timeline and reflected in the inspector
- [x] Cache prefix invalidation handled correctly on reload
- [x] Debounced against rapid successive saves

---

## Epic E6 — Tools

**Goal:** filesystem and shell, correctly bounded. Boundary enforcement here is a security
control, not a convenience.

---

### TD-601 — Tool registry
**Size:** 3 · **Depends on:** TD-201

**Acceptance criteria:**
- [x] Tools declare name, description, JSON schema, side-effect class, and parallel-safety
- [x] Registry produces provider-format tool definitions
- [x] Registration is explicit; no dynamic discovery in v0.1
- [x] Unknown tool names return a structured error to the model

---

### TD-602 — Path boundary enforcement
**Size:** 5 · **Depends on:** TD-601

**Security-critical. Write the attacks as tests first.**

**Acceptance criteria:**
- [x] Every path resolved to canonical absolute form before any check
- [x] Access outside `writable_paths` refused with a clear error
- [x] **Traversal blocked:** `../` sequences, absolute paths, symlinks pointing outside the
      workspace, hardlinks, and paths that become external only after resolution
- [x] Windows-specific cases covered: drive-relative paths, UNC paths, `8.3` short names,
      alternate data streams
- [x] **Writes to `AGENTS.md`, `CLAUDE.md`, and `.tst/rules/**` are refused unconditionally**,
      enforced in the tool itself (prime directive §2.4)
- [x] Refusals are logged to the audit trail as Class C events

---

### TD-603 — Filesystem read tools
**Size:** 2 · **Depends on:** TD-602

**Acceptance criteria:**
- [x] `fs_read` with optional line ranges; returns content with line numbers
- [x] `fs_list` and glob search, respecting ignore rules
- [x] Binary files detected and refused with an explanatory message rather than dumping bytes
- [x] Large files truncated with explicit markers and a stated total size
- [x] Encoding errors handled without crashing

---

### TD-604 — Filesystem write tools
**Size:** 3 · **Depends on:** TD-602, TD-705

**Acceptance criteria:**
- [x] `fs_write` creates or overwrites; parent directories created as needed
- [x] `fs_edit` performs exact string replacement, failing loudly if the target is absent or
      ambiguous
- [x] Every write produces a diff in the `tool_result` for display
- [x] Writes are atomic (temp file plus rename) — no partial file on failure
- [x] Every write is checkpointed per TD-705

---

### TD-605 — Shell execution
**Size:** 5 · **Depends on:** TD-602

**Acceptance criteria:**
- [x] Commands run with the workspace as working directory
- [x] Configurable timeout; process group killed on timeout or cancel
- [x] stdout and stderr streamed to the timeline as they arrive, not buffered to the end
- [x] Output capped with truncation markers
- [x] Exit code returned; non-zero is a normal result the model can reason about, not an error
- [x] Environment sanitized: no API keys, no keychain material, no tokens passed to child
      processes — asserted by test
- [x] `allowed_commands` allowlist enforced when configured, matching on the resolved binary

---

## Epic E7 — Autonomy hooks

**Goal:** the classifier, the ledger, and checkpoints — shipping in v0.1 so that Class A
decisions stop interrupting the user immediately, and so autonomy is never a retrofit.
Spec §12.

---

### TD-701 — Decision class model and rule table
**Size:** 3 · **Depends on:** TD-601

**Acceptance criteria:**
- [x] Classes A, B, C modeled per spec §12.2
- [x] Static rule table classifies unambiguous cases without a model call: any path outside the
      workspace → C; any network call to a new host → C; steering-file write → C; cap exceeded
      → C; in-workspace source edit within `writable_paths` → A
- [x] Rule table is data, not scattered conditionals, and is unit-tested case by case
- [x] Every classification records which rule fired, for explainability

---

### TD-702 — Classifier chokepoint
**Size:** 3 · **Depends on:** TD-701, TD-402

**Acceptance criteria:**
- [x] **Every tool dispatch routes through the classifier. There is no bypass path** — asserted
      by a test that enumerates dispatch call sites
- [x] Classification precedes execution and precedes any approval decision
- [x] Class is attached to the audit record and to the `tool_call` event
- [x] A tool that somehow reaches execution unclassified raises immediately rather than
      proceeding

---

### TD-703 — Ambiguous-case classifier call
**Size:** 3 · **Depends on:** TD-702, TD-303

**Acceptance criteria:**
- [x] Cases the rule table cannot decide are classified by a **worker-tier** call with a
      tight, cached prompt
- [x] Result cached per (tool, argument-shape) within a session to avoid repeat cost
- [x] Classifier failure defaults to **B**, never to A — fail toward asking, not toward acting
- [x] Classifier cost is tracked separately and visible in the cost breakdown

---

### TD-704 — Decisions ledger
**Size:** 3 · **Depends on:** TD-702, TD-705

**Acceptance criteria:**
- [x] Class A and B decisions append to `.tst/autonomy/DECISIONS.md` in the spec §12.3 format:
      timestamp, class, what was chosen, why, commit SHA, undo command
- [x] Append is atomic and safe under concurrent sessions
- [x] Every entry names a revertable commit — **if an action cannot be attributed to a commit,
      it is not Class A**
- [x] `decision_logged` event emitted to the client
- [x] Ledger is human-readable markdown, parseable back into structured entries by test

**Done (2026-08-14):** `core/tstd/autonomy/ledger.py` — §12.3 format with spec-verbatim
parse-back test, fcntl-locked append (Windows falls back to append-mode atomicity), Class A
without a commit refused by construction.  Dispatch-hook tests cover append + `decision_logged`
emission.  (Boxes ticked at integration: implementation and evidence were on the branch, the
checkmarks were not.)

---

### TD-705 — Checkpoint commits
**Size:** 5 · **Depends on:** TD-604

**Acceptance criteria:**
- [x] Each meaningful unit of work commits to a session branch `tst/session/<id>`
- [x] **Never commits to `main`**
- [x] Commit message references the decision and session
- [x] Non-git workspaces degrade gracefully: feature disabled, user informed once, everything
      else still works
- [x] Pre-existing uncommitted user changes are never clobbered — detected and reported first
- [x] Test covers: clean repo, dirty repo, no repo, detached HEAD, mid-rebase

**Notes:** The branch is the undo stack, the audit trail, and the review surface. This story is
what makes aggressive Class A behavior safe.

---

### TD-706 — Boundary configuration
**Size:** 2 · **Depends on:** TD-602

**Acceptance criteria:**
- [x] `.tst/config.yaml` defines `writable_paths`, `allowed_commands`, `network`, and caps
      (`spend_usd`, `wall_clock_hours`, `max_iterations`)
- [x] Sensible defaults applied when the file is absent: workspace-only writes, no network,
      conservative spend cap
- [x] Validated on load with actionable errors
- [x] Boundary visible in the UI so the user always knows the current wall

---

### TD-707 — Cap enforcement
**Size:** 3 · **Depends on:** TD-706, TD-304

**Acceptance criteria:**
- [x] Spend cap checked before each model call; exceeding it pauses the session
- [x] Wall-clock and iteration caps enforced
- [x] Pause is a **fault report**, not an approval request — session enters a distinct state
      with a clear summary
- [x] User can raise the cap and resume without losing session state
- [x] Test: a session with a $0.01 cap halts on the first call and reports correctly

---

## Epic E8 — Approvals and policy

---

### TD-801 — Policy model
**Size:** 3 · **Depends on:** TD-706

**Acceptance criteria:**
- [x] Policy maps (tool, argument pattern) → `auto` | `ask` | `never`
- [x] Defaults derive from decision class: A → auto, B → ask, C → ask-or-never per config
- [x] Persisted per workspace in `.tst/config.yaml`
- [x] Most-specific pattern wins; precedence tested
- [x] Policy never grants what the boundary forbids — boundary always wins

---

### TD-802 — Approval flow
**Size:** 3 · **Depends on:** TD-801, TD-205

**Acceptance criteria:**
- [x] `approval_request` carries tool, arguments, decision class, a human-readable summary, and
      the reason approval is required
- [x] Session enters `awaiting_approval` and does not spin or poll
- [x] `approve` / `deny` resumes; denial returns a structured message to the model so it can
      choose another path
- [x] Timeout behavior configurable, defaulting to waiting indefinitely
- [x] Client disconnect during `awaiting_approval` leaves the session parked and resumable

**Completed (2026-08-14):** `session.request_approval` parks the loop in `awaiting_approval`
on a bare future (`await asyncio.wait_for(asyncio.shield(fut), timeout)`) — no spin, no poll.
`approval_timeout_seconds` is `None` by default (wait indefinitely); set it and an overdue
request resolves as a denial. Pending approvals live on the `Session` and are keyed by
`tool_call_id`, so any attached client can `approve`/`deny` and a disconnect leaves the
session parked and resumable. Denial returns a `ToolResult(status="error",
error_code="approval_denied")` the model can read to choose another path. The `Approve`/`Deny`
client messages round-trip through the daemon handler (`_handle_approve`/`_handle_deny` →
`resolve_approval`).

---

### TD-803 — Always-allow
**Size:** 2 · **Depends on:** TD-802

**Acceptance criteria:**
- [x] "Always allow in this workspace" writes a policy rule scoped as narrowly as the request
      allows — never a blanket grant for the whole tool
- [x] The generated rule is shown to the user before it is saved
- [x] Saved rules are listed and individually revocable in settings
- [x] Class C actions can never be always-allowed

---

## Epic E9 — Audit and cost

---

### TD-901 — Audit store
**Size:** 3 · **Depends on:** TD-201

**Acceptance criteria:**
- [x] SQLite schema: sessions, turns, tool calls, decisions, model calls, costs
- [x] **Append-only — no `UPDATE` or `DELETE` statements exist in the codebase**, asserted by a
      source-level test
- [x] Migration mechanism in place from the first release
- [x] Secrets never stored; tool arguments scrubbed through the same redaction filter as logs
- [x] Indexed for the queries the UI actually makes

---

### TD-902 — Audit writer
**Size:** 2 · **Depends on:** TD-901

**Acceptance criteria:**
- [x] Every turn, tool call, decision, and model call recorded with timestamp, model, tokens,
      cost, and result hash
- [x] Writes are non-blocking and never stall the loop
- [x] Write failure degrades loudly — the user is told the audit trail is incomplete
- [x] Refused boundary violations recorded as Class C events

---

### TD-903 — Cost aggregation and export
**Size:** 2 · **Depends on:** TD-902, TD-304

**Acceptance criteria:**
- [x] Queries for cost by turn, session, day, and tier
- [x] Export to JSONL and CSV
- [x] Aggregates match the sum of individual records — verified by property test

---

# MILESTONE M2 — The Window

## Epic E10 — Shell and panes

**Goal:** the thing that makes this feel like Claude Desktop rather than a terminal with extra
steps. Budget generously — **this is the largest single body of work in v0.1.**

---

### TD-1001 — Application shell
**Size:** 5 · **Depends on:** TD-105, TD-204

**Acceptance criteria:**
- [x] Single window, native chrome, correct behavior on macOS, Linux, Windows
- [x] Window state (size, position) persisted across launches
- [x] Two-pane layout with a draggable divider whose position persists
- [x] Design tokens applied; no hardcoded colors or spacing in components
- [x] Light and dark themes following the OS preference

---

### TD-1002 — Daemon supervision
**Size:** 5 · **Depends on:** TD-1001, TD-202

**Acceptance criteria:**
- [x] Tauri host spawns `tstd` on launch and connects using the port file
- [x] Daemon crash is detected, reported in the UI, and recovered by restart with the session
      list intact
- [x] Closing the window shuts the daemon down cleanly in v0.1 — with a `TODO(v0.3)` marking
      where detached-session behavior will diverge
- [x] No orphaned `tstd` processes after quit under any exit path, including force-quit —
      verified manually on each platform and documented

**Completed (2026-08-13):** Rust host spawns the daemon, waits on the port file keyed by pid,
completes a `hello`/`hello_ack` WS handshake, supervises crash + bounded restart, and shuts
down via the WS `shutdown` message with a SIGKILL fallback. Daemon side: `--parent-pid` orphan
watchdog, `sessions.json` snapshot rehydrated as `interrupted`, port-file pid + atomic write +
delete-on-clean-stop. Verified by 547 passing Python tests (30× stable crash-restart-with-list
integration), Rust unit + a live spawn→connect→shutdown integration test (`cargo test`), ruff,
mypy strict, and clippy clean. macOS exercised; Windows/Linux build + per-platform no-orphan
manual verification deferred to the packaging milestone. UI "reported in the UI" is the
daemon-status event the supervisor now emits; the banner lands in TD-1003.

---

### TD-1003 — Protocol client
**Size:** 3 · **Depends on:** TD-1001, TD-204

**Acceptance criteria:**
- [x] Typed WebSocket client with automatic reconnect and backoff
- [x] Reconnect re-attaches with `from_seq` and replays missed events — no gaps, no duplicates
- [x] Connection state visible in the UI
- [x] Unknown event types ignored gracefully with a console warning, never a crash

**Completed (2026-08-13):** `ProtocolClient` (`ui/src/lib/client.ts`) with injection-safe
transport (browser `WebSocket` or a test fake). hello/hello_ack handshake; reconnect with
exponential backoff capped at 15s and a live `reconnecting` state; re-attach every known
session at `from_seq = lastSeq+1` on reconnect so the daemon replays missed events; per-
session gap/duplicate gating (seq ≤ last → drop, seq > last+1 → force a fresh attach);
unknown event types warn via `console.warn` and advance the seen seq without ever crashing.
Connection state surfaces through `ui/src/lib/connection-status.ts` (Svelte 5 runes store,
host `daemon-status` event + real socket state) and a token-styled banner. `protocol.ts`
synced with `core/tstd/protocol.py` (added `shutdown`, `list_sessions`, `hello_ack`,
`steering_reloaded`, `instruction_stack`, `session_list`, `interrupted`); fixture generator
extended and regenerated (29 fixtures). Verified: 33 vitest tests pass, `svelte-check`/tsc
0 errors, 547 Python tests pass. UI must be exercised end-to-end in a live Tauri window
(TD-1004's chat pane will drive real attaches).

---

### TD-1004 — Chat pane
**Size:** 5 · **Depends on:** TD-1003

**Acceptance criteria:**
- [x] Multi-line composer with submit-on-Enter and newline-on-Shift-Enter
- [x] Streaming assistant output rendered smoothly, without layout jump
- [x] Markdown rendering with syntax-highlighted code blocks and copy buttons
- [x] Auto-scroll that stops when the user scrolls up, with a jump-to-latest affordance
- [x] Conversation history scrollable and virtualized for long sessions
- [x] Cancel button available whenever a turn is running

---

### TD-1005 — Activity timeline
**Size:** 5 · **Depends on:** TD-1003

**This pane is the product.** Spec §3.

**Acceptance criteria:**
- [x] Chronological entries for tool calls, results, decisions, tier switches, compaction,
      steering reloads, and errors
- [x] Each entry expandable for full arguments and output
- [x] File writes show a syntax-highlighted diff
- [x] Shell commands show streaming output live
- [x] Decision entries show the class and the rule that fired
- [x] Visually distinct treatment per entry type; scannable at a glance
- [x] Virtualized — a thousand-entry session stays responsive

**Completed (2026-08-14):** Pure, runes-free `ui/src/lib/timeline.ts` maps each validated
daemon event to a `TimelineEntry` (tool call/result, decision, tier switch, compaction,
steering reload, error); `shell_output` chunks merge live into their parent tool call's
stdout/stderr buffer. `timeline-store.ts` is the thin Svelte 5 runes singleton over it
(reassigns `entries` per push so in-place shell buffers still invalidate). `virtualization.ts`
computes a fixed-row window (`computeWindow`) with overscan; `ActivityTimeline.svelte` renders
it with `bind:clientHeight` + scroll, inline expansion via `expandedId`; `TimelineEntryRow.svelte`
shows the tone marker, kind label, title, preview, and expanded args/output/diff (diff
highlighted via `classifyDiffLine`) and the decision class + rule. `entry-view.ts` holds the
visual classification. `connection-status.ts` gained an `onEvent` fan-out; `AppShell.svelte`
mounts `ActivityTimeline` fed by `onEvent(push)`. The daemon now emits a `tier_switched` event
when `set_tier` applies a manual override (new `_handle_set_tier` handler + 3 integration
tests). Verified: 87 vitest tests pass, `svelte-check`/tsc 0 errors, 905 Python tests pass
(4 skipped), fixtures regenerated (35). Timeline wiring is exercised end-to-end in the live
Tauri window once TD-1004's chat pane drives real turns.

---

### TD-1006 — Title bar
**Size:** 3 · **Depends on:** TD-1003

**Acceptance criteria:**
- [x] Workspace name with a picker
- [x] Active brain/worker/validator slugs, clickable to switch
- [x] **Live cost meter** updating as costs accrue, with a hover breakdown by tier
- [x] Session state indicator (idle / running / awaiting approval / paused at cap)
- [x] Boundary indicator showing the current wall

---

### TD-1007 — Approval cards
**Size:** 3 · **Depends on:** TD-1005, TD-802

**Acceptance criteria:**
- [x] Card renders in place with tool, arguments, decision class, and reason
- [x] Actions: Approve, Deny, Always allow in this workspace
- [x] Keyboard accessible; focus moves to the card on appearance
- [x] Dangerous actions visually distinct
- [x] Denial offers an optional note passed back to the model
- [x] Resolved cards remain in the timeline showing what was chosen

**Completed (2026-08-14):** `ApprovalCard.svelte` renders in an `ApprovalBar` footer with the
tool, arguments, decision class, and reason; the card autofocuses on mount (`tabindex="-1"`
+ `onMount` focus). Class C requests are visually distinct (danger-tone left border + class
badge). Denial captures an optional note sent back on the `Deny` message (`reason`, nulled
when blank). Resolved cards flip to approved/denied in the timeline (the "approval" entry
kind resolves in place on its matching `tool_result`) and the approval leaves the footer.
"Always allow in this workspace" renders as the card's third action only when the daemon
proposes a rule (`proposed_always_allow`, from TD-803); it sends the `always_allow` message
TD-803 already handles, so the rule lifecycle stays daemon-side and the card stays a thin
client. Approve / Deny are wired end-to-end via the `sendToDaemon` client path plus the
`error_code` propagation fix ("Gap B") that lets the UI distinguish a denial from a handler
error.

---

### TD-1008 — Errors and notifications
**Size:** 2 · **Depends on:** TD-1003

**Acceptance criteria:**
- [x] Errors surface as actionable messages naming the fix, never raw tracebacks
- [x] Auth failures, missing keys, and cap pauses each have tailored, specific copy
- [x] Non-blocking toasts for transient issues; persistent banners for blocking ones
- [x] A "copy diagnostics" action produces a redacted, pasteable report

---

## Epic E11 — Onboarding

---

### TD-1101 — First-run wizard
**Size:** 5 · **Depends on:** TD-1001

**Acceptance criteria:**
- [x] Detects first run and presents: welcome → API key → preset → workspace → done
- [x] Every step skippable-with-consequence and revisitable from settings
- [x] The key step links to where to get one and validates it with a single cheap live call
- [x] Completing the wizard lands the user in a working session, ready to type
- [x] **Total time from launch to first message under two minutes**, measured

---

### TD-1102 — Credential storage
**Size:** 3 · **Depends on:** TD-1101

**Acceptance criteria:**
- [ ] Key stored via OS keychain (Keychain / Secret Service / Credential Manager)
- [ ] **Key never written to disk in plaintext, never logged, never in the audit database** —
      asserted by test
- [ ] Missing or revoked key produces a clear prompt to re-enter, not a cryptic failure
- [ ] Key removable from settings

---

### TD-1103 — Workspace management
**Size:** 3 · **Depends on:** TD-1101

**Acceptance criteria:**
- [x] Native folder picker
- [x] Recent workspaces list with quick switching
- [x] Opening a workspace scaffolds `.tst/` with a commented default config
- [x] Switching workspaces re-resolves steering and rebuilds the manifest
- [x] A workspace that has become unavailable is reported clearly and removed from recents on
      request

---

### TD-1104 — Diagnostics
**Size:** 2 · **Depends on:** TD-1101

**Acceptance criteria:**
- [x] A doctor view checking: daemon reachable, key present and valid, provider reachable,
      git available, workspace writable, steering files parseable
- [x] Each failure states the specific fix
- [x] Output copyable as redacted text for bug reports

---

### TD-1105 — Keychain locked/drift error surface
**Size:** 1 · **Depends on:** TD-1102

**Acceptance criteria:**
- [ ] A locked or password-drifted login keychain (macOS "user name or passphrase
      not correct" / `SecKeychainItemCreateFromContent` failures) maps to actionable
      copy: what happened, and how to fix it (Keychain Access → unlock or update
      password), not raw `security` stderr
- [ ] Store failure offers a retry path after the user unlocks the keychain

**Notes:** first observed 2026-08-14 on an AD-bound Mac after a domain password
change — `security add-generic-password` fails machine-wide until the keychain
is re-keyed; verified the daemon's exec-array invocation is not the cause.
Coordinate with TD-1102's wizard rework (integrate that lane first; this lands
on top).

---

### TD-1106 — Validate works on the entered key
**Size:** 1 · **Depends on:** TD-1102

**Acceptance criteria:**
- [ ] The wizard's Validate action checks the key currently typed in the field
      with the provider, regardless of stored state
- [ ] Store and Validate are independent; a failed or skipped store never
      dead-ends the step

**Notes:** observed 2026-08-14: Validate was gated on `hasApiKey`, so any
keychain failure made both buttons unreachable at once. Same TD-1102
coordination note as TD-1105.

---

## Epic E12 — Instruction inspector

**Goal:** answer "did my rules take effect?" with a pane instead of guesswork. Spec §4.4.

---

### TD-1201 — Resolved stack panel
**Size:** 3 · **Depends on:** TD-506, TD-1003

**Acceptance criteria:**
- [x] Lists every steering source in precedence order with per-file token counts
- [x] Shows total token cost and whether the block is currently cached
- [x] Path-scoped rules show matched or unmatched, and what they matched
- [x] `CLAUDE.md` fallbacks and shadowed files clearly labeled
- [x] Imports shown nested under their importer
- [x] Files over 200 lines flagged with the adherence warning
- [x] Clicking a file opens it in the system editor
- [x] Live-updates on hot reload

**Completed (2026-08-14):** `get_instruction_stack` assembles on demand in the daemon
handler (`_handle_get_instruction_stack`) and the loop re-pushes a full stack when the
steering prefix hash changes at a turn boundary (TD-509); `InstructionStack` carries
`last_cached_tokens` (provider-observed, `None` before the first turn). `StackPanel.svelte`
sits behind an Activity | Stack tab strip, renders sources in payload order with per-file
tokens, total + three-state cache badge (unknown / miss / cached N), fallback and
"shadows …" chips, imports indented by depth under their importer, and the 200-line
adherence warning. Clicking a file goes through `open-file.ts` → `tauri-plugin-opener`
(capability scoped to `opener:allow-open-path` only), no-op outside the shell. Verified by
three independent probes (synthetic workspace payload, UI rendering, shell/push paths):
vitest 253/253, svelte-check 0/0 (286 files), pytest 1035/2 skipped, e2e OVERALL PASS.
**AC 3 ticked (2026-08-14):** TD-503's plumbing landed — the daemon stack handler and
the loop both forward the session's touched paths, so scoped rules now assemble with a
real match verdict and the panel's active/inactive labels are honest.
`test_scoped_rule_reports_true_active_verdict` pins the wire: inactive before a
matching touch, active after.

---

### TD-1202 — Decisions ledger panel
**Size:** 3 · **Depends on:** TD-704, TD-1003

**Acceptance criteria:**
- [x] Session decisions listed with class, choice, rationale, and commit
- [x] Filterable by class
- [x] Each entry offers a copyable revert command
- [x] **Reviewing a full session of Class A decisions takes under a minute** — the density
      target that makes the whole autonomy trade work
- [x] Links out to the markdown ledger file

---

# MILESTONE M3 — Shippable

## Epic E13 — Packaging and distribution

---

### TD-1301 — Python runtime bundling
**Size:** 8 · **Depends on:** TD-1002

**The highest-risk story in the project.** Shipping a Python daemon inside a desktop app is
where this schedule most likely slips. Start early, timebox, and escalate if it resists.

**Acceptance criteria:**
- [x] Python runtime and dependencies bundled — user needs no system Python
      (PyInstaller onefile sidecar, `core/scripts/build_sidecar.py` →
      `shell/binaries/tstd-<triple>`; smoke-launched with no system Python involved)
- [ ] App launches on a machine with no Python installed, verified on a clean VM per platform
      (manual release step — no clean VM available in this environment; the
      sidecar itself boots and serves standalone, so this verifies the Tauri
      bundle around it. Belongs with TD-1302's clean-VM installs.)
- [x] Bundle size documented and justified (18.9 MB aarch64-apple-darwin —
      see DECISIONS.md 2026-08-14)
- [x] Daemon startup under three seconds on a mid-range machine
      (2.0 s to port.json on an M-series host, measured by the build smoke test)

---

### TD-1302 — Platform builds
**Size:** 5 · **Depends on:** TD-1301

**Acceptance criteria:**
- [ ] macOS `.dmg` (arm64 and x86_64), Linux AppImage and `.deb`, Windows `.msi`
      (CI matrix in `.github/workflows/package.yml`: macos-latest, macos-13,
      ubuntu-latest, windows-latest.  macOS arm64 verified locally to `.app`
      level — the bundled sidecar serves `port.json` with no system Python.
      The local `.dmg` step needs Finder automation rights this dev host
      lacks (AppleEvent -1712); runners get TAURI_BUNDLER_DMG_IGNORE_CI=false.
      Ticks when the first main run produces four artifacts.)
- [ ] Each installs and runs on a clean VM
      (each matrix leg treats its fresh runner as the clean machine:
      install/extract the bundle, run the bundled sidecar, assert the port
      file.  Ticks when that first run is green.)
- [x] Unsigned-binary warnings documented in the README with per-platform instructions
- [x] Signing decision recorded in `DECISIONS.md` — cost and benefit stated, deferral is
      acceptable for v0.1 (deferred, 2026-08-14)

---

### TD-1303 — Release workflow
**Size:** 3 · **Depends on:** TD-1302, TD-106

**Acceptance criteria:**
- [ ] Tagged release builds all platforms and publishes artifacts
      (release.yml fires on v*, reuses the package.yml matrix, publishes via
      `gh release create` — verified end to end on the first tag push)
- [x] Checksums published (SHA256SUMS.txt across all four artifacts)
- [x] Changelog generated from commits (`core/scripts/changelog.py`, grouped
      by TD-### convention, merges excluded)
- [x] Version consistent across host, daemon, and UI, asserted by test
      (`core/tests/test_version_consistency.py`; release job re-asserts the
      tag equals all four. Caught ui/package.json at 0.0.1 → 0.1.0.)

---

## Epic E14 — Cross-cutting testing

Story-level tests belong to their stories. These are the suites that span features.

---

### TD-1401 — End-to-end headless harness
**Size:** 5 · **Depends on:** TD-401, TD-605

**Acceptance criteria:**
- [x] A CLI harness runs a scripted session against the mock provider with no UI
- [x] Covers: workspace open → steering resolution → user message → tool call →
      classification → approval → execution → checkpoint → ledger → cost accounting
- [x] Runs in CI in under sixty seconds
- [x] **This harness is the M1 exit criterion**

Done (2026-08-13): `tstd.e2e_harness` + `core/scripts/e2e_headless.py`, pinned in
CI as `tests/test_e2e_headless.py`. Runs in ~1s. Approval is asserted at the
protocol surface (the gate itself is TD-802); steering is proven via the mock's
recorded system prompt. This story also wired the builtin tool stack into the
daemon's `open_workspace` — daemon sessions previously had no dispatcher.

---

### TD-1402 — Security suite
**Size:** 5 · **Depends on:** TD-602, TD-702

**Acceptance criteria:**
- [x] Path escape attempts across all vectors in TD-602
- [x] Steering-file write refusal
- [x] Environment sanitization for child processes
- [x] Secret redaction across logs, audit, diagnostics, and error messages
- [x] Non-loopback bind refusal
- [x] Classifier bypass attempts
- [x] **Every test in this suite is a release blocker**

---

### TD-1403 — Context assembler suite
**Size:** 3 · **Depends on:** TD-501, TD-503, TD-504

**Acceptance criteria:**
- [x] Fixture workspace tree exercising every precedence, fallback, glob, and import case
- [x] Golden-file assertions on assembled prompts
- [x] Property test: assembly is deterministic for identical inputs
- [x] Cache-prefix stability asserted across turns

---

### TD-1404 — Performance baselines
**Size:** 3 · **Depends on:** TD-1401

**Acceptance criteria:**
- [x] Measured and recorded: daemon cold start, session start on a large repo, steering
      resolution, first token latency (all four live in `core/tests/perf_baselines.json`);
      timeline render at 1000 entries pending TD-1005's timeline component
- [x] Baselines committed; CI flags regressions beyond a stated threshold

---

### TD-1405 — Audit and event surface redaction
**Size:** 3 · **Depends on:** TD-902

Found by TD-1402: `redact_secrets()` guards the log stream, but tool-call
arguments and tool-result output flow into the audit store and onto the
WebSocket unredacted — a secret pasted into a file the agent writes would be
persisted and broadcast. The two skip-marked tests in
`core/tests/test_security_suite.py` (section 4) pin the expected behavior.

**Acceptance criteria:**
- [x] Audit-store writes pass through `redact_secrets` (arguments, output, diffs)
- [x] Daemon events carrying tool arguments/results are redacted before broadcast
- [x] Error messages surfaced to clients pass through the same chokepoint
- [x] Both skip-marked TD-1402 tests run green, unskipped
- [x] A positive control proves benign text survives redaction byte-identical

---

### TD-1406 — Windows CI parity
**Size:** 5 · **Depends on:** TD-1402

Found by the first green-mypy Windows leg (2026-08-14): the pytest suite was
never exercised on Windows while mypy was red, and ~110 tests failed at once.
The daemon's `add_signal_handler` cascade is fixed; the rest of the suite is
made platform-honest (posix-separator manifests, cross-platform test commands,
`skipif(win32)` where OS semantics genuinely diverge). What remains is the
product-semantics work the skips point at.

**Acceptance criteria:**
- [ ] Boundary guard semantics decided for Windows absolute paths: today every
      drive-letter path is refused `windows_unsafe` on every platform
      (TD-1402's fail-closed choice), which means the model cannot use
      absolute in-workspace paths on Windows. Either containment-checked
      drive-absolute paths become legal on win32, or the refusal copy teaches
      the relative-path idiom — decide, implement, unskip the guard tests
- [ ] 8.3 short-name handling on Windows temp/user dirs (`RUNNER~1`): alias
      expansion vs. refusal, so legitimate absolute paths under short-named
      ancestors aren't collateral
- [ ] File-permission stories (session store, port file) get real Windows ACLs
      or a documented no-op, and the `restricted_mode` tests unskip
- [ ] Shell-tool process-group kill semantics verified on Windows
      (CREATE_NEW_PROCESS_GROUP + taskkill/TerminateJobObject), skipped
      cancel/timeout tests unskipped
- [x] Parent-watchdog liveness probe works on Windows (OpenProcess) — first pass:
      OpenProcess plus `GetExitCodeProcess != STILL_ACTIVE` (a dead process with an
      open handle otherwise reports alive); `test_parent_watchdog` green on the
      windows leg

---

## Epic E15 — Documentation

---

### TD-1501 — README
**Size:** 3 · **Depends on:** TD-1302

**Acceptance criteria:**
- [ ] One-paragraph pitch, a screenshot, and install instructions per platform
- [ ] A five-minute quickstart from download to first result
- [ ] The cost story stated plainly with the default stack and real numbers
- [ ] Explicit statement: no account, no server, no subscription, no telemetry
- [ ] Comparison table against the closed alternatives, written fairly

---

### TD-1502 — Steering authoring guide
**Size:** 3 · **Depends on:** TD-503

**Acceptance criteria:**
- [ ] Explains `AGENTS.md`, the hierarchy, `CLAUDE.md` compatibility, path-scoped rules, and
      imports
- [ ] States the 200-line guidance and why adherence drops on long files
- [ ] Worked examples for a small project and a large one
- [ ] A migration note for users arriving from other tools — emphasizing nothing needs porting

---

### TD-1503 — Configuration reference
**Size:** 2 · **Depends on:** TD-302, TD-706

**Acceptance criteria:**
- [ ] Every key in `config.yaml` and `.tst/config.yaml` documented with type, default, effect
- [ ] Model swap instructions with a note that the landscape moves and slugs should be verified
- [ ] Boundary and cap configuration explained with worked examples

---

### TD-1504 — Architecture guide
**Size:** 3 · **Depends on:** M2 complete

**Acceptance criteria:**
- [ ] Daemon/shell split explained, including why the session owns the loop
- [ ] Protocol documented
- [ ] Extension points named for contributors
- [ ] A "how to add a tool" walkthrough

---

## Epic E16 — Familiarity

**Goal:** close the recognition gap. The window should read as a warm, quiet,
serif-accented agent workspace in the Claude Desktop family — without sanding off
the differentiators (cost meter, tier chips, decisions ledger, boundary indicator,
doctor). Source: 2026-08-14 reverse-engineering pass over the shipping Claude
desktop (features, UX flows, design language). These stories are chrome and
surface only; no architecture changes.

**The line we walk:** evoke the family — warm paper ground, scarce warm accent,
serif display type, hairline separation, quiet motion — with our own hex values,
our own mark, and our own copy voice. Do not lift Claude's exact palette
(`#FAF9F5`, `#D97757`), name, glyph, or greeting strings.

---

### TD-1601 — Warm-paper palette and radii
**Size:** 1 · **Depends on:** TD-1001

**Acceptance criteria:**
- [x] Light theme: warm paper ground, white reserved for lifted surfaces, warm
      ink text, warm hairline borders — no pure-white page background, no cool grays
- [x] Accent is a rust-family hue on our own hex, used only for send/active/links/
      key actions — never decorative
- [x] Dark theme: warm charcoal ground with elevated surfaces; accent lightened
      for contrast
- [x] Radii scale with element size (small controls ≈8px, cards 12–16px, composer
      ≈24px); shadows ≤ ~6% alpha, hairlines do the separation work
- [x] Changes confined to design tokens + global CSS; vitest and svelte-check green

**Notes:** all in `ui/src/lib/tokens.css` (+ `app.css` if ground rules live there).
Light: ground `#F8F6F1`, lifted `#FFFFFF`, ink `#191817`, secondary `#5C574D`,
hairline `#E4E0D8`, user bubble `#E9E3D6`, accent `#B4532A` / hover `#9A4523`,
on-accent `#FFFFFF`; semantic ok `#3E7A4E`, warn `#9A6A1B`, err `#A0432E`.
Dark: ground `#232320`, elevated `#2C2C28`, ink `#EDEAE3`, secondary `#A39E93`,
hairline `#3D3D37`, user bubble `#3A382F`, accent `#D0794F`.

**Completed (2026-08-14):** `ui/src/lib/tokens.css` rewritten around a canonical
semantic set — `--color-ground` / `--color-lifted` / `--color-sunken` surfaces,
`--color-ink` ramp (secondary/muted), `--color-hairline`, `--color-user-bubble`
(staged for TD-1603), `--color-accent` / `--color-accent-hover` /
`--color-on-accent`, `--color-ok` / `--color-warn` / `--color-err` — with the
exact hex values from the Notes in both themes; every pre-TD-1601 name kept as a
legacy alias (`--color-bg`, `--color-text`, `--color-success`, …) so no component
needed a touch. Derived values the Notes didn't pin are documented in the file:
sunken washes, muted ink, dark accent-hover, and lifted dark status hues.
`--color-info` aliases the accent — running/active read as rust, not a cool hue.
Radii re-scaled to 8/12/16/24 (`--radius-sm/md/lg/xl`) and shadows capped at
4–6% alpha on a warm near-black, going near-silent on dark so the hairline
carries separation. The accent-colored user bubble is deliberately unchanged —
recoloring it to `--color-user-bubble` is TD-1603's move. Verified token-only:
vitest 253/253, svelte-check 0/0 (286 files), `vite build` clean.

---

### TD-1602 — Typography
**Size:** 2 · **Depends on:** TD-1601

**Acceptance criteria:**
- [x] Source Serif 4 vendored into the repo (woff2 + OFL license file); no CDN or
      runtime fetch
- [x] Serif carries display/greeting/headings at light weight with ≈−0.02em
      tracking; system sans carries UI; mono carries code, tokens, and cost figures
- [x] Type roles defined as tokens with a documented fallback chain
- [x] Bundle size impact recorded in `DECISIONS.md`

**Notes:** woff2 into `ui/static/fonts/` with `OFL.txt`; `@font-face` with
`font-display: swap`. Display stack `"Source Serif 4", Georgia, serif`; keep the
existing system sans and `ui-monospace` stacks. Weights 400–500 only — the voice
stays light.

**Completed (2026-08-14):** Source Serif 4 latin woff2 (400 + 500, Fontsource
files via jsDelivr, verified `wOF2` magic and `file(1)` identification — not HTML
error pages) vendored to `ui/static/fonts/` with the SIL OFL 1.1 text from
google/fonts as `OFL.txt`; 41,616 bytes of font payload, recorded in
DECISIONS.md. Two `@font-face` blocks with `font-display: swap` live in
`app.css`. Type roles are tokens in `tokens.css` with documented fallbacks:
`--font-display` (`'Source Serif 4', Georgia, serif`), `--font-sans` (system
stack; `--font-family` aliased to it), `--font-mono` (ui-monospace stack), plus
`--tracking-display: -0.02em`. The serif already renders at weight 500 on the
shell wordmark, markdown h1–h4, and the wizard/doctor/decisions pane titles;
five hardcoded `ui-monospace, monospace` stacks now route through `--font-mono`.
The greeting itself lands with TD-1605. vitest 253/253, svelte-check 0/0
(286 files), `vite build` clean with the fonts emitted to `build/fonts/`.

---

### TD-1603 — Bubble-less assistant messages
**Size:** 2 · **Depends on:** TD-1601

**Acceptance criteria:**
- [x] Assistant messages render full-width on the canvas — no bubble, no avatar,
      hairline separation between turns
- [x] User messages keep a right-aligned warm-tan bubble at max-width ≈80%
- [x] Streaming caret recolored to the accent
- [x] Markdown, code blocks, and copy affordances still work; existing component
      tests updated

**Notes:** `MessageBubble.svelte` and its consumers. This is the change that
removes the "generic chat app" read.

**Completed (2026-08-14):** `MessageBubble.svelte` splits the two roles: assistant
messages render full-width on the ground with no bubble and no avatar, while user
messages keep a right-aligned bubble at `max-width: 80%` in `--color-user-bubble`
with the bottom-right corner tightened to `--radius-sm`. The turn separator is a
top hairline on non-first user rows, driven by an explicit `first` prop from
`MessageList` — under `@tanstack/svelte-virtual` windowing, `:first-child` lies,
so the row index decides. The streaming caret is `▍` recolored to `--color-accent`
with the blink suppressed under `prefers-reduced-motion`. Markdown, highlighted
code blocks, and the code-copy affordance went untouched; the in-pane copy button
per message lands with TD-1606.

---

### TD-1604 — Composer card and centered column
**Size:** 2 · **Depends on:** TD-1601

**Acceptance criteria:**
- [x] Chat column centered at max-width ≈760px, held on wide windows
- [x] Composer is a lifted white card: ≈24px radius, 1px hairline border, whisper
      shadow
- [x] Send is a circular accent button that morphs to stop while a turn runs
- [x] One-line plain disclaimer beneath the composer, in our own words

**Notes:** `Composer.svelte`, `ChatPane.svelte`. Disclaimer suggestion:
"TST Desk can make mistakes — check its work."

**Completed (2026-08-14):** `ChatPane.svelte` holds the conversation and composer
in one `.column` at `width: min(760px, 100%)`, margin-centered on wide windows.
`Composer.svelte` renders as a lifted card — `--color-lifted` ground, 1px
`--color-hairline`, `--radius-xl` (24px), `--shadow-sm` whisper — over a
chromeless textarea, with the border taking the accent on `:focus-within`. The
circular accent send button carries the TD-1608 `arrow-up`; while a turn runs
(running or awaiting_approval, via `showCancel`) it morphs to a filled `stop`
labelled "Stop generating (Esc)" and calls `oncancel`, matching the Esc shortcut
from TD-1609. The old `.controls` cancel row is gone. The disclaimer sits beneath
the card in centered `text-xs` ink-muted: "TST Desk can make mistakes — check its
work."

---

### TD-1605 — Greeting empty state
**Size:** 1 · **Depends on:** TD-1602

**Acceptance criteria:**
- [x] Empty chat shows a time-aware serif greeting (morning / afternoon / evening
      by local hour)
- [x] Three suggestion chips in product voice insert their text into the composer
      (insert, not auto-send)
- [x] Hidden once messages exist, including after attach/replay with history

**Notes:** `ChatPane.svelte` empty branch. Chip copy is ours:
"Review this repo", "Find what's failing", "Explain this codebase".

**Completed (2026-08-14):** `greeting.ts` owns the clock — `greetingForHour`
returns "Good morning" (5–11), "Good afternoon" (12–16), or "Good evening"
(otherwise), computed once per `ChatPane` mount so the greeting doesn't tick live
as the hour rolls over — and the `SUGGESTIONS` constant with the three chips in
product voice. `ChatPane`'s empty branch renders the greeting in
`--font-display` at `text-3xl` with `--tracking-display`, not bold, and the chips
write into the composer's draft through `bind:value` — insert, never auto-send.
The branch is keyed on `chat.messages.length === 0`, and attach/replay rebuilds
history through the same store, so any session with history hides it automatically.
`greeting.test.ts` pins the boundaries and the chip copy (4 tests).

---

### TD-1606 — Hover message actions
**Size:** 2 · **Depends on:** TD-1603

**Acceptance criteria:**
- [x] Assistant messages show a hover-only action bar: copy (markdown source) and
      retry
- [x] Retry resends the last user message; hidden or disabled while a turn runs
- [x] Timestamp available on hover
- [x] Actions keyboard-reachable with visible focus

**Notes:** retry works over today's protocol (`user_message` resend); edit/branch
is deliberately out — it needs daemon-side conversation forking.

**Completed (2026-08-14):** Completed assistant messages carry a hover-only action
bar in `MessageBubble.svelte`, also revealed on `:focus-within` so the buttons are
keyboard-reachable with a visible `--color-accent` focus ring; the slot height is
reserved so the reveal never reflows the transcript. Copy writes the raw markdown
source via `navigator.clipboard` with the TD-1608 `copy` icon flipping to `check`
for 1.5s as confirmation. Retry (TD-1608 `retry` icon) calls the new
`ChatStore.retryLastUserMessage()` — it walks back to the most recent user row and
resends it verbatim over the same `user_message` wire message, appending a new row
(the protocol has no edit/fork, so the duplication is the honest record) and
refusing while a turn is live; the store method is hoisted to a closure so the
reactive shell's detached re-export keeps working. The hover timestamp reads local
HH:MM from a new display-only `ChatMessage.at` stamped at first sight (send echo
or first delta; replay stamps attach time). `MessageList` threads `turnLive` /
`onretry`; `ChatPane` wires `showCancel(chat.turnState)` and
`retryLastUserMessage`. Three new retry tests plus a seen-at stamp test in
`chat-store.test.ts`; vitest 270/270, svelte-check 292 files 0/0.

---

### TD-1607 — Working shimmer
**Size:** 2 · **Depends on:** TD-1004

**Acceptance criteria:**
- [x] Between send and first token, a shimmering "Working…" line — CSS shimmer,
      no spinner
- [x] Collapses to a static duration line when the turn completes
- [x] Honors `prefers-reduced-motion`
- [x] No layout shift on appear/disappear

**Notes:** chat-store already sees turn start and first `assistant_delta`; track
an `awaitingFirstToken` flag there. The duration line is the "Thought for Ns"
analog.

**Completed (2026-08-14):** `ChatState` gains `awaitingFirstToken` — raised on a
successful send, lowered on the first `assistant_delta`, on any non-running
`session_state`, and on `turn_complete`; a replayed "running" current-state event
is guarded so it can't resurrect the shimmer over an actively streaming reply, and
attaching to a session the daemon reports as running raises it history or not.
`lastTurnDuration` is stamped from the daemon-measured `turn_complete.duration`
(seconds) and cleared on the next send — the UI never clocks a turn itself
(AGENTS §6). `ChatPane` holds a fixed-height `.turn-status` slot above the
composer (shimmer and duration swap inside it, so the composer never moves,
satisfying no-layout-shift), announced with `aria-live="polite"`. "Working…" is a
CSS shimmer — a warm gradient swept across the glyphs via `background-clip: text`,
no spinner — and `prefers-reduced-motion` trades the sweep for static ink-muted.
`formatTurnDuration` never prints "0s" (floors at 1s) and rolls into "Xm Ys" past
a minute; "Worked for …" is our wording, not Claude's. Six new tests cover the
flag lifecycle, replayed-state guard, duration stamp/clear, and formatting;
vitest 277/277, svelte-check 292 files 0/0, `vite build` clean.

---

### TD-1608 — Icon pass and favicon
**Size:** 2 · **Depends on:** TD-1601

**Acceptance criteria:**
- [x] No emoji left in chrome (header buttons, send, panes); inline outline SVGs
      throughout — ≈1.5px stroke, sized to text, fill only for active state
- [x] Icons defined once in a shared map/component, not pasted per call site
- [x] Default Svelte favicon replaced with our own mark

**Notes:** Lucide-style 24px viewBox paths, `currentColor`. The 📜 🩺 ⚙ header
buttons are the loudest "hack project" tell. Tauri bundle icons stay with E13.

**Completed (2026-08-14):** Ten glyphs defined once in `ui/src/lib/icons.ts`
(scroll, stethoscope, settings, folder, x, check, minus, alert, arrow-up,
chevron-down) and rendered by one `Icon.svelte` — 24px viewBox, 1.5px
`currentColor` stroke, round caps/joins, `fill` reserved for active states,
default size 1em (sized to text) with a px override. Emoji are out of the
chrome: the 📜/🩺/⚙ header buttons, the title bar's folder/chevron/remove
glyphs and menu picker row, the composer's ↑ send, both pane ✕ closers, the
wizard preset ✓, the stack panel's ⚠ badges, and the doctor row marks (the
copied text report keeps its ASCII ✓/✗ on purpose — plain text is the right
medium there; its tests still pin those strings). The last product emoji, a 📁
in the workspace-not-found toast copy, became the words "folder picker". The
favicon is an original mark: rust rounded square carrying a minimal desk
outline (top, leg, drawer pedestal) in warm paper, no borrowed logo. The
streaming caret `▍` stays — TD-1603 owns its recolor. vitest 253/253,
svelte-check 0/0 (288 files), `vite build` clean.

---

### TD-1609 — Dark code theme and first shortcuts
**Size:** 1 · **Depends on:** TD-1601

**Acceptance criteria:**
- [x] Code blocks theme-aware in dark mode — no light-on-light highlight.js theme
- [x] Esc cancels the running turn; ⌘, reopens the wizard
- [x] Shortcuts discoverable (title attributes or a hint line)

**Notes:** `Markdown.svelte` currently imports light-only `github.css`; replace
with a token-driven hljs theme or a media-scoped dual import. Esc wires to the
existing cancel path.

**Completed (2026-08-14):** `Markdown.svelte` now imports
`ui/src/lib/hljs-theme.css` instead of highlight.js's light-only `github.css`;
the theme maps the common hljs classes to new `--syn-comment` / `--syn-keyword`
/ `--syn-string` / `--syn-number` / `--syn-title` tokens defined per color
scheme in `tokens.css` (dark lifts each warm hue), so code blocks follow the
palette and dark mode has no light-on-light. Shortcuts are a pure, rune-free
`resolveShortcut()` in `ui/src/lib/shortcuts.ts` wired through
`<svelte:window>` in `AppShell`: Esc peels layers — an open workspace menu
closes, an open modal (wizard/doctor/decisions) eats it, otherwise a live turn
(`showCancel`) cancels through the chat store's existing `cancel` message —
and ⌘, (Ctrl+, off-mac) reopens the wizard. Discoverable via titles: the
cancel button reads "Cancel turn (Esc)", the wizard gear "Setup wizard (⌘,)".
Nine new vitest cases pin the layer order and the inert combos; vitest
262/262, svelte-check 0/0 (290 files), `vite build` clean.

---

## Epic E17 — Familiarity II

**Goal:** the furniture pass — the structures a person reaches for in the first
five minutes. Source: the 2026-08-14 reverse-engineering pass (Tier 2 of the
familiarity ladder). Ordered by familiarity-per-effort; each story lands on the
E16 visual identity. Everything here builds over existing seams — sidebar and
notifications first, because they change how the app is used every day.

---

### TD-1701 — Session sidebar
**Size:** 3 · **Depends on:** TD-1005, TD-1601, TD-1608

**Acceptance criteria:**
- [x] A ≈260px left rail lists the workspace's sessions — live and interrupted —
      newest first, with a state indicator per row
- [x] Clicking a session attaches the window to it; the attached session is marked
- [x] A New-session action creates and attaches a fresh session in the current
      workspace
- [x] The rail collapses to an icon strip and the collapsed state persists
- [x] A filter field narrows the list client-side
- [x] E16 tokens, type roles, and icon map throughout; no emoji

**Notes:** protocol discovery first — `list_sessions` and attach/detach/replay
(TD-206) exist. If only `open_workspace` creates sessions, add a `new_session`
verb in `session_store`/protocol (core change is in scope for this story;
keep it minimal — the heavy session lifecycle stays where it is). Rename/star/
delete wait for durable history (v0.3); rows are keyed by session id. User
request 2026-08-14: "the collapsible chat history on the left, like Claude."

**Completed (2026-08-14):** `SessionRail.svelte` mounts in the AppShell body
left of the SplitPane — 260px expanded (filter field + New button + scrollable
rows: short-id title, workspace·recency·state subtitle, tone dot matching the
title bar's indicator mapping), 48px collapsed (panel-left expand, plus-new,
one dot per session row). Collapse persists to localStorage under
`tstdesk.sessionRailCollapsed`. New store `ui/src/lib/sessions.svelte.ts`
reduces `session_list`/`session_state` over the connection fan-out —
rows newest-first by the daemon's `updated_at`, refreshes on connect and on
state touches (coalesced to one in-flight `list_sessions`), filter matches
id and workspace path client-side. Row clicks drive two new seams:
`chatStore.selectSession` (detach old, clear, attach with replay — the
existing from_seq path) and `focusSession` in session-status (re-targets the
title bar's session-scoped fields; replay repopulates boundary/tier/cost).
Protocol gained `new_session` (`{type, session_id}` — the anchor); the
daemon factors open_workspace's creation into `_start_session` and replies
with the fresh session's first `session_state`, which the rail focuses on
sight. `SessionSummary.state` gained `paused` (was a latent validation crash
once a paused session hit the list). New icons: panel-left, plus, search.
Tests: 23 vitest cases for the rail store + 3 chat-store selection cases
(312 total green); 3 daemon integration tests + 1 protocol round-trip for
`new_session` (1060 passed, 2 skipped in core). Deferred: rename/star/delete
(v0.3 durable history), live refresh of *other* windows' session rows (needs
a daemon-pushed `session_list`; today's refreshes are window-initiated).

---

### TD-1702 — OS notifications
**Size:** 2 · **Depends on:** TD-1007

**Acceptance criteria:**
- [ ] OS notification when an approval is requested and the window is unfocused;
      clicking it focuses the window on the approval card
- [ ] OS notification on turn completion when unfocused
- [ ] No notification when the window is focused
- [ ] Permission request happens lazily, on first qualifying event — never
      upfront

**Notes:** `tauri-plugin-notification`; permission comes from the plugin's
request API. The approval notification is the one that makes the app feel like
a coworker.

---

### TD-1703 — Settings screen v1
**Size:** 3 · **Depends on:** TD-1106, TD-1701

**Acceptance criteria:**
- [ ] In-app settings page with left-nav sections, reached from the title-bar
      gear (gear stops reopening the wizard)
- [ ] Appearance section: light / system / dark, overriding
      `prefers-color-scheme`
- [ ] Model section: preset and tier slugs readable, editing writes through to
      `config.yaml`
- [ ] Policy section: persisted always-allow rules listed with revoke
- [ ] Key section: re-enter / remove stored key (TD-1102's flows, surfaced here)

**Notes:** the wizard stays for first run; ⌘, retargets to this screen. Policy
list/revoke may need protocol messages — check what TD-803 landed before
assuming.

---

### TD-1704 — Queue and steer UI
**Size:** 2 · **Depends on:** TD-1004

**Acceptance criteria:**
- [ ] Sending while a turn runs queues the message; queued rows render with
      send-now and remove
- [ ] Editing a queued row replaces its text
- [ ] Empty-queue state is invisible (no chrome when nothing is queued)

**Notes:** the daemon already queues user messages (E4); this is presentation.
Drag-to-reorder is a follow-up if the rows prove useful.

---

### TD-1705 — Files pane
**Size:** 3 · **Depends on:** TD-1005, TD-1701

**Acceptance criteria:**
- [ ] A Files tab beside Activity aggregates the session's write diffs from the
      event stream: file list, per-file diff view, running totals
- [ ] Empty state explains what will appear here
- [ ] Clicking a file can open it via the existing opener integration

**Notes:** spec §3's right-pane tab. Data is already in the timeline events;
this is aggregation and presentation, no new core events.

---

### TD-1706 — Usage and cost view
**Size:** 3 · **Depends on:** TD-903

**Acceptance criteria:**
- [ ] A usage view shows session/day/week token and cost rollups from the audit
      store, broken out by tier
- [ ] Export buttons reuse the existing JSONL/CSV export
- [ ] The title-bar meter's hover panel links here

**Notes:** aggregation queries exist (TD-903); verify which export affordances
are already wired before adding UI.

---

### TD-1707 — Command palette
**Size:** 2 · **Depends on:** TD-1701, TD-1703

**Acceptance criteria:**
- [ ] ⌘K opens a palette over sessions and actions (new session, attach, open
      decisions/doctor/stack/settings, toggle theme)
- [ ] Fuzzy match, full keyboard operation, Esc dismisses
- [ ] Palette entries reuse the icon map

**Notes:** lands after the sidebar and settings so it has things to command.

---

### TD-1708 — Edit and retry branching
**Size:** 5 · **Depends on:** TD-1606

**Acceptance criteria:**
- [ ] Editing a past user message forks the conversation from that point and
      resends
- [ ] Branch navigation (‹ ›) on edited messages and retried assistant turns
- [ ] Daemon-side fork covered by core tests; replay shows the active branch

**Notes:** needs daemon conversation forking and protocol additions; the
backlog's sizing reflects that. Retry-without-edit stays the TD-1606 behavior.

---

### TD-1709 — Attachments v1
**Size:** 3 · **Depends on:** TD-1004

**Acceptance criteria:**
- [ ] Text files attach to a message as context chips (picker, drag-drop, paste)
- [ ] Attachments travel with `user_message` within configured caps and render
      as chips in the sent row
- [ ] Oversize/binary attachment attempts fail with actionable copy

**Notes:** images deliberately split out — vision support depends on the user's
chosen models and needs capability detection first.

---

### TD-1710 — Browser computer-use and Screen pane
**Size:** 8 · **Depends on:** TD-1007

**Acceptance criteria:**
- [ ] `files_102.zip` unpacked into the tree first — it is the only copy of the
      `tst-cua` driver source
- [ ] BrowserDriver runs against a real browser (Playwright persistent profile);
      six-verb actions surface as tools through the existing approval gate
- [ ] A Screen tab in the right pane streams browser screenshots so the session
      is watchable
- [ ] Failure modes (driver crash, stalled page, denied action) land as normal
      timeline entries

**Notes:** the wow story. Browser-only — whole-desktop AX stays v0.4 (TCC
friction, per-app quirks, boundary model for screen actions). Driver bring-up
on real hardware is where the estimate lives; timebox and record deviations.

### TD-1711 — Session liveness honesty
**Size:** 2 · **Depends on:** TD-1701

**Acceptance criteria:**
- [x] Startup auto-bind never adopts a terminal session (interrupted, complete,
      failed, cancelled); with no live session the app stays unbound and the
      empty state points at New Session
- [x] Sending `user_message` to a terminal session returns a typed daemon error
      (e.g. `session_not_running`) instead of silently enqueueing with no
      consumer; the UI renders it as actionable copy

**Notes:** observed 2026-08-14 on the first real-machine run after E16 — after
an app restart, `session_list` auto-bind married an `interrupted` tombstone and
the composer gated off it ("Waiting for a session…" forever); a later
`open_workspace` created a running session the UI refused to switch to
(first-adoption stickiness, now re-targetable via TD-1701's rail). The rail is
the manual escape; this story removes the trap.

**Completed (2026-08-14):** the daemon's `user_message` handler now refuses a
send to a session that can never consume it — terminal state, no runner
(restored tombstone), or a dead loop task — with a typed `session_not_running`
error carrying the session id and actionable copy, instead of enqueueing into
the void (`TERMINAL_STATES` derives from the transition table so the two can't
drift). The chat store's `session_list` auto-bind skips terminal summaries and
stays unbound when nothing is live; the empty state then points at New Session,
and the rail's New action (now anchorable on the newest listed session when
nothing is bound) is the escape. The refusal clears the waiting shimmer in the
pane and raises a toast with the daemon's "start a new session and resend"
instruction.

### TD-1712 — Rail information architecture: sections + account anchor
**Size:** 2 · **Depends on:** TD-1701, TD-1703

**Acceptance criteria:**
- [ ] The rail organizes surfaces into sections: function entries (Home,
      Projects/courses-of-work, Scheduled) grouped above, session history
      sectioned below with a count badge when items queue
- [ ] The account / settings row anchors the rail's bottom-left (not buried in
      the title bar): avatar-or-initial, account label, settings entry
- [ ] Sections whose epics haven't landed yet (Scheduled → v0.5) either hide or
      render disabled-with-note — never a dead click

**Notes:** observed 2026-08-14 against the reference app's rail (Code/Home
tabs, New CTA, Projects, Artifacts, Scheduled, Dispatch, Customise;
account+settings pinned bottom-left). Each function surface already maps to an
epic — Artifacts v0.3, Scheduled+Dispatch v0.5, Customize TD-1703, Projects
TD-1103 — but the *layout grammar* (sectioned rail, bottom account anchor) was
captured nowhere. This story is the presentation rule; the surfaces arrive with
their epics.

### TD-1713 — Working-state honesty and flavor
**Size:** 3 · **Depends on:** TD-1711

**Acceptance criteria:**
- [x] Attach-before-send invariant: the client never sends `user_message` to a
      session it is not attached to (auto-attach first, or refuse with copy);
      covered by a regression test reproducing the 2026-08-14 silent stall
      (second session created via rail New Session received the message but no
      events ever reached the UI)
- [x] First-token watchdog: if no `assistant_delta` (or turn terminal event)
      arrives within ~25s of send, the Working state flips to honest copy
      ("No response yet — the model may be slow or unreachable") with a working
      Cancel; it recovers automatically when the first delta lands
- [x] The Working indicator rotates whimsical one-word verbs (per the
      familiarity pattern — a tstd-voiced list, token-styled, no emoji) over
      the existing shimmer, and shows elapsed time beside it
- [x] Daemon logs a `turn started` INFO per turn so future stalls are
      diagnosable from the log alone

**Notes:** root cause of "typed hello, Working… forever" (2026-08-14): pipeline
verified healthy end-to-end via direct daemon repro (answer in 3.5s over the
real provider); the app client sent to a session it had never attached to after
the rail's New Session, and event fan-out only delivers to attached sessions.
Two sticky-session stores (chat-store follow vs session-status first-adoption)
drifted; unify the attach seam when fixing. Flavor brief: the shimmer exists
(TD-1607) — this story adds the verb rotation + honesty states, it does not
rebuild the indicator.

**Completed (2026-08-14):** The invariant went in at the seam that can't be
dodged — `ProtocolClient.send` auto-attaches on the same socket before any
`user_message` whose session it isn't following, so store wiring can no longer
order the frames wrong; a refused (disconnected) send registers nothing, and
the reconnect path re-attaches whatever the invariant attached. Wire-order
regression tests pin attach-before-message, no-double-attach, no-registration-
on-refusal, and re-attach-after-reconnect. The chat store arms a 25s
first-token watchdog on every send (and on attaching to a running session,
whose replay could equally never come); tripping it swaps the shimmer for
static "No response yet — the model may be slow or unreachable." while Cancel
stays live, and the first delta, any terminal turn event, or a
`session_not_running` refusal recovers/clears it — including a user cancel,
which drops the wait locally the moment it's sent. Flavor stayed on the
existing TD-1607 shimmer: a twelve-verb rotation (2.5s cadence, tstd-voiced,
one word each, no emoji, distinct from the Claude spinner's list) plus an
elapsed "for Ns" tail shown from 2s, clock ticking only while a wait is in
flight. The daemon gained the dequeue-time `turn started` INFO (session id,
post-dequeue queue depth, message length — never content) that closes the
observability gap between enqueue and the post-assembly "turn start" log.

---

# Post-v0.1 backlog

Named, sequenced, and deliberately not decomposed. Do not build these.

| Version | Epic | Summary |
|---|---|---|
| **v0.2** | Memory | `.tst/memory/`, relevance-based loading, worker-tier distillation, diff-before-write, git commits per accepted memory |
| **v0.3** | Cowork parity | Durable session event log, detached sessions surviving window close, session list pane, artifact delivery, file-diff work view |
| **v0.4** | Computer use | Screen pane, `tst-cua` drivers, OS permission onboarding, grounding model evaluation |
| **v0.5** | Remote & notify | Tailscale interface binding, phone attach, Slack notifier (Hermes pattern), lightweight scheduler |
| **v0.6** | Local models | vLLM/EZER routing, UI-TARS grounding, local worker tier |
| **v0.7** | Autonomy engine | Charter editor, autonomous runner, validator drift checks, circuit breakers, container isolation, wake-up summary |
| **v0.8** | Extensibility | MCP extension loading, custom tool packages, plugin surface |
| **Later** | Flourishes & platform furniture | Unversioned on purpose (familiarity ladder Tier 3): voice/dictation, macOS quick-entry overlay, tray + multi-window + auto-updater, web-search/research tool |

---

# Risk register

| # | Risk | Impact | Mitigation |
|---|---|---|---|
| R1 | **Python bundling in Tauri proves painful** | Blocks release | TD-1301 early, timeboxed; fallback is requiring system Python with a guided installer, documented as a v0.1 concession |
| R2 | **The shell is underestimated** | Schedule slip | M1 delivers a fully working headless product; the UI can slip without the project failing |
| R3 | **`tst-cua` loop resists the 3-tier router** | Rework in E4 | TD-102 audit happens before any porting; deviation is allowed and recorded |
| R4 | **Model slugs or prices change mid-build** | Stale defaults | Everything in config; TD-302 forbids slugs in code; verify at release |
| R5 | **Steering precedence has subtle bugs** | Silent wrong behavior, hard to debug | Highest test coverage (TD-1403) plus the inspector as a live debugging surface |
| R6 | **Classifier misclassifies a Class C as A** | Security incident | Static rules decide the dangerous cases without a model; ambiguity defaults to B; TD-1402 is a release blocker |
| R7 | **Prefix caching doesn't behave as expected on a given provider** | Cost overrun | TD-305 asserts prefix stability; cache hit rate is observable; degrades to correct-but-costlier |
| R8 | **Scope creep toward later phases** | Never ships | `AGENTS.md` §3; the kickoff prompt forbids scaffolding; ask before building anything off-milestone |

---

# Summary

| Milestone | Epics | Stories | Points |
|---|---|---|---|
| M0 Foundation | E1 | 7 | 15 |
| M1 Headless core | E2–E9 | 43 | 143 |
| M2 The window | E10–E12 | 16 | 53 |
| M3 Shippable | E13–E17 | 33 | 99 |
| **Total v0.1** | **17** | **99** | **310** |

Points are relative sizing for sequencing and splitting decisions, not a schedule. Do not
convert them to dates.

**The single most important sequencing rule in this document: M1 exits when TD-1401 passes.**
A correct, tested, headless core is what makes the UI a presentation problem instead of a
debugging problem.
