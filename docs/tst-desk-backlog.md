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
| **M3 — Shippable** | E13, E14, E15 | A stranger can install and use it from a fresh machine |

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
- [ ] All seven decisions asked in a single message, each with a recommendation and rationale
- [ ] Answers recorded in `DECISIONS.md` with date
- [ ] Any answer that contradicts the spec is flagged, and the spec is updated to match
- [ ] No implementation work started before answers received

**Notes:** Do not offer defaults as a way of avoiding the question. The user explicitly wants
to decide these.

---

### TD-102 — Audit `tst-cua` and produce a reuse report
**Size:** 3 · **Depends on:** TD-101

Read the existing `tst-cua` repository in full. Produce `docs/REUSE.md` describing what will be
reused as-is, what will be adapted, and what will be written fresh.

**Acceptance criteria:**
- [ ] Every module in `tst-cua` classified: reuse / adapt / ignore / defer-to-later-phase
- [ ] The agent loop specifically assessed — its control flow, its tool interface, its
      assumptions about providers — with a statement of what must change for the 3-tier router
- [ ] Driver code (Atspi/Darwin/Browser/Mock) marked **defer to v0.4** but noted as intact
- [ ] The Mock driver assessed for reuse as the v0.1 test harness (likely valuable now)
- [ ] Report identifies any coupling that would make later computer-use integration painful
- [ ] Report reviewed with the user before porting begins

**Notes:** This is the highest-leverage hour in the project. A wrong reuse decision here costs
days later. Do not skim the code.

---

### TD-103 — Repository scaffold
**Size:** 2 · **Depends on:** TD-101

Create the directory structure from `AGENTS.md` §11.

**Acceptance criteria:**
- [ ] Directory tree matches `AGENTS.md` §11 exactly
- [ ] `LICENSE` present, matching the TD-101 answer
- [ ] `README.md` skeleton with one-paragraph pitch and a placeholder quickstart
- [ ] `AGENTS.md` and `docs/` populated with the three companion documents
- [ ] `DECISIONS.md` created with the TD-101 answers as its first entries
- [ ] `.gitignore` excludes `.tst/` runtime state, build artifacts, `.env`, and keychain
      material — but **not** `.tst/rules/`

---

### TD-104 — Python toolchain
**Size:** 2 · **Depends on:** TD-103

**Acceptance criteria:**
- [ ] `uv` project initialized under `core/` with `pyproject.toml`, Python 3.11+
- [ ] `ruff` configured for lint and format
- [ ] `mypy` configured in strict mode
- [ ] `pytest` with `pytest-asyncio` configured
- [ ] `uv run pytest` passes on an empty suite
- [ ] `uv run ruff check` and `uv run mypy` clean

---

### TD-105 — Tauri and frontend toolchain
**Size:** 3 · **Depends on:** TD-103

**Acceptance criteria:**
- [ ] Tauri 2 project under `shell/`, building on the development platform
- [ ] SvelteKit + Svelte 5 project under `ui/`, TypeScript strict
- [ ] `npm run tauri dev` opens an empty window
- [ ] `clippy` clean; `tsc --noEmit` clean
- [ ] Design token file created (colors, spacing, type scale) — empty of components but
      structurally in place

---

### TD-106 — CI pipeline
**Size:** 3 · **Depends on:** TD-104, TD-105

**Acceptance criteria:**
- [ ] CI runs on push and PR
- [ ] Jobs: Python lint, Python typecheck, Python tests, Rust clippy, TypeScript typecheck,
      frontend build
- [ ] Build matrix covers macOS, Linux, Windows
- [ ] Pipeline is green on the empty scaffold
- [ ] Failing any job blocks merge

---

### TD-107 — Pre-commit hooks
**Size:** 1 · **Depends on:** TD-104, TD-105

**Acceptance criteria:**
- [ ] Hooks run `ruff format`, `ruff check`, and secret detection on staged files
- [ ] A commit containing a plausible API key pattern is rejected
- [ ] Hook install documented in `README.md`

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
- [ ] `tstd` starts as an async process with clean startup and shutdown
- [ ] Structured logging (JSON lines) to a rotating file under the user data directory
- [ ] Log level configurable; secrets redacted by a logging filter, verified by test
- [ ] `SIGTERM` and `SIGINT` trigger graceful shutdown: sessions notified, sockets closed,
      state flushed
- [ ] Health endpoint or event returning version, uptime, and active session count

---

### TD-202 — Local WebSocket server
**Size:** 3 · **Depends on:** TD-201

**Acceptance criteria:**
- [ ] Server binds `127.0.0.1` on an ephemeral port
- [ ] Port and auth token written to a port file in the user data directory, mode `0600`
- [ ] **A test asserts the server refuses to bind any non-loopback interface** — this
      enforces prime directive §2.1 in code, not convention
- [ ] Multiple simultaneous client connections supported
- [ ] Client disconnect does not disturb daemon state

---

### TD-203 — Connection handshake and local auth
**Size:** 2 · **Depends on:** TD-202

Even on loopback, other local processes can connect. Require a token.

**Acceptance criteria:**
- [ ] Client presents the token from the port file in a `hello` message
- [ ] Bad or missing token closes the connection with a typed error, and is logged
- [ ] Handshake negotiates protocol version; mismatch produces an actionable error message
- [ ] Token rotates on daemon restart

---

### TD-204 — Protocol schema
**Size:** 5 · **Depends on:** TD-203

Define every message the shell and daemon exchange. This is a Class B decision surface —
record the design in `DECISIONS.md`.

**Acceptance criteria:**
- [ ] Pydantic models for all messages, versioned, with a discriminated `type` field
- [ ] Client→daemon: `hello`, `open_workspace`, `user_message`, `approve`, `deny`,
      `cancel`, `attach`, `detach`, `set_tier`, `get_instruction_stack`
- [ ] Daemon→client: `ready`, `session_state`, `assistant_delta`, `tool_call`, `tool_result`,
      `approval_request`, `decision_logged`, `cost_update`, `turn_complete`, `error`
- [ ] Every daemon→client event carries a monotonic `seq` scoped to the session
- [ ] TypeScript types generated or hand-mirrored, with a test asserting they match
- [ ] Round-trip serialization tests for every message type
- [ ] Unknown message types produce a typed error, never a crash

**Notes:** `attach`/`detach` are specified now even though v0.1 has no detached sessions. The
shape must be right; the behavior can be trivial.

---

### TD-205 — Session model and SessionRunner
**Size:** 5 · **Depends on:** TD-204

**The most important story in M1.** The session owns the loop. The socket is a viewer.

**Acceptance criteria:**
- [ ] `Session` has an id, a workspace path, a state machine
      (`idle` → `running` → `awaiting_approval` → `running` → `complete` / `failed` /
      `cancelled`), and an event log
- [ ] `SessionRunner` executes the loop as an asyncio task **owned by the daemon, not by any
      connection**
- [ ] **Test: a session started over a connection continues running after that connection is
      closed**, and its events continue to accumulate
- [ ] Event log is append-only in memory with monotonic `seq` (durable persistence is v0.3)
- [ ] Session registry supports lookup, list, and cancel
- [ ] Cancelling a session interrupts the loop cleanly, mid-tool if necessary

---

### TD-206 — Attach, detach, and replay
**Size:** 3 · **Depends on:** TD-205

**Acceptance criteria:**
- [ ] `attach{session_id, from_seq}` replays all events from `from_seq` then streams live
- [ ] Replay and live stream produce no gaps and no duplicates under a concurrent-write test
- [ ] `detach` stops streaming without affecting the session
- [ ] Attaching to an unknown session returns a typed error
- [ ] Two clients attached to one session both receive all events

---

### TD-207 — Crash resilience
**Size:** 2 · **Depends on:** TD-205

**Acceptance criteria:**
- [ ] An exception inside one session's loop fails only that session; others continue
- [ ] Failure emits a `session_state` event with a readable reason and full traceback in logs
- [ ] Daemon crash leaves no corrupt state that prevents restart
- [ ] Stale port file from a dead daemon is detected and replaced on startup

---

## Epic E3 — Model router

**Goal:** one client, three tiers, prices in config, costs accounted, caching exploited.

---

### TD-301 — Provider client
**Size:** 3 · **Depends on:** TD-201

**Acceptance criteria:**
- [ ] Async `httpx` client against an OpenAI-compatible `/v1/chat/completions`
- [ ] Streaming responses yielded as deltas
- [ ] Tool/function calling supported in both request and response parsing
- [ ] Base URL configurable — **verified against OpenRouter and against a local vLLM-style
      endpoint with no code change** (the EZER path must work by configuration alone)
- [ ] API key read from OS keychain, never from config or environment files
- [ ] Timeouts on connect, read, and total

---

### TD-302 — Model configuration schema
**Size:** 2 · **Depends on:** TD-301

**Acceptance criteria:**
- [ ] `config.yaml` defines per-tier: slug, base URL, input price, output price, cache-read
      price, context window, max output tokens
- [ ] Ships with the TST default stack from spec §7 (Kimi K3 / DeepSeek V4 Flash /
      DeepSeek V4 Pro) and the documented swaps as commented alternatives
- [ ] Presets: `tst-default`, `budget`, `local`
- [ ] Config validated on load with actionable error messages naming the offending key
- [ ] **No model slug, price, or URL appears anywhere in source code** — asserted by a test
      that greps the codebase for known slugs

**Notes:** The model landscape moves weekly. Users must update models without a release.

---

### TD-303 — Tier routing policy
**Size:** 3 · **Depends on:** TD-302

**Acceptance criteria:**
- [ ] Brain tier handles the first `lead_turns` turns (default 2, configurable), then worker
      takes over
- [ ] Validator invoked on demand, not on a schedule, in v0.1
- [ ] Escalation path: worker may hand back to brain on repeated failure, with a configurable
      threshold
- [ ] Active tier is emitted to the client on every turn
- [ ] Runtime tier override via `set_tier` takes effect on the next turn

---

### TD-304 — Cost and token accounting
**Size:** 3 · **Depends on:** TD-302

**Acceptance criteria:**
- [ ] Every call records prompt tokens, cached prompt tokens, completion tokens, model, and
      computed cost
- [ ] Cache-read tokens priced at the cache rate, not the input rate
- [ ] Cost aggregated per turn, per session, and per day
- [ ] `cost_update` events emitted as costs accrue
- [ ] **Unit tests with hand-computed expected dollar figures** for each tier, including a
      mixed cached/uncached case

---

### TD-305 — Cache-aware prompt assembly
**Size:** 3 · **Depends on:** TD-303, TD-501

**Acceptance criteria:**
- [ ] Prompt assembled in the stable-prefix order from spec §4.5: base prompt → steering →
      memory placeholder → workspace manifest → conversation
- [ ] Blocks 1–2 are byte-identical across turns when their source files have not changed —
      asserted by a test hashing the prefix across a multi-turn session
- [ ] Cache hit rate observable in logs and in the cost breakdown

**Notes:** This story is worth more than it looks. On a long session, prefix caching is the
difference between $2.80/M and $0.30/M on the brain tier.

---

### TD-306 — Resilience
**Size:** 3 · **Depends on:** TD-301

**Acceptance criteria:**
- [ ] Retry with exponential backoff and jitter on 429 and 5xx; bounded attempts
- [ ] `Retry-After` honored when present
- [ ] Context-length errors surface as a typed, actionable error, not a generic failure
- [ ] Auth failures produce a message telling the user exactly what to fix
- [ ] Partial stream interruption recovers or fails cleanly — never emits a half-parsed tool call
- [ ] All failure modes covered by tests against the mock provider

---

### TD-307 — Mock provider
**Size:** 2 · **Depends on:** TD-301

**Acceptance criteria:**
- [ ] Scripted responses: plain text, streaming text, tool calls, malformed output, errors,
      rate limits
- [ ] Deterministic and offline — no network, no spend
- [ ] Returns realistic usage numbers so cost tests are meaningful
- [ ] Used by every loop and router test

---

## Epic E4 — Agent loop

**Goal:** port the `tst-cua` loop onto the daemon and router without rewriting it.

---

### TD-401 — Port the loop core
**Size:** 5 · **Depends on:** TD-102, TD-303, TD-307

**Acceptance criteria:**
- [ ] Loop runs inside `SessionRunner`
- [ ] Provider calls go through the router, not directly to a client
- [ ] Reuse matches the TD-102 report; any deviation recorded in `DECISIONS.md`
- [ ] Loop is provider-agnostic — no vendor-specific assumptions in control flow
- [ ] Multi-turn conversation with tool calls passes against the mock provider

---

### TD-402 — Tool dispatch
**Size:** 3 · **Depends on:** TD-401, TD-601

**Acceptance criteria:**
- [ ] Tool calls parsed, validated against the tool's schema, and dispatched
- [ ] Invalid arguments return a structured error **to the model** so it can correct itself,
      rather than failing the turn
- [ ] Parallel tool calls executed concurrently where the tools declare themselves safe for it
- [ ] Results truncated to a configurable cap with clear truncation markers
- [ ] Every dispatch passes through the classifier (TD-702)

---

### TD-403 — Turn lifecycle events
**Size:** 2 · **Depends on:** TD-401, TD-204

**Acceptance criteria:**
- [ ] `assistant_delta` streamed token-by-token
- [ ] `tool_call` emitted before execution with name and arguments
- [ ] `tool_result` emitted after, with status and a display-safe summary
- [ ] `turn_complete` carries tokens, cost, tier used, and duration
- [ ] Event ordering is deterministic and covered by test

---

### TD-404 — Cancellation
**Size:** 3 · **Depends on:** TD-401

**Acceptance criteria:**
- [ ] `cancel` interrupts an in-flight model stream within one second
- [ ] A running shell command is terminated, and its process group with it
- [ ] Session ends in `cancelled` with partial work preserved and visible
- [ ] No orphaned processes or leaked tasks — asserted by test

---

### TD-405 — Context window management
**Size:** 5 · **Depends on:** TD-401, TD-305

**Acceptance criteria:**
- [ ] Token budget tracked against the tier's context window
- [ ] Approaching the limit triggers compaction of older conversation turns
- [ ] **Steering block and workspace manifest are re-injected after compaction, re-read from
      disk** — instructions must survive compaction
- [ ] Compaction is announced in the activity timeline, never silent
- [ ] Test: a session that exceeds the window continues correctly and still obeys steering rules

---

## Epic E5 — Context assembler (steering)

**Goal:** correct, inspectable, affordable instruction loading. Spec §4. **Highest test
coverage requirement in the project alongside E7.**

---

### TD-501 — File discovery and precedence
**Size:** 5 · **Depends on:** TD-201

**Acceptance criteria:**
- [ ] Resolves, lowest to highest precedence: `~/.tstdesk/AGENTS.md` → `<workspace>/AGENTS.md`
      → `<workspace>/.tst/rules/*.md` → nested `<workspace>/**/AGENTS.md`
- [ ] Nested files apply to their subtree only
- [ ] Concatenated into one block with provenance comments naming each source file
- [ ] Missing files are not errors
- [ ] Table-driven tests over a fixture workspace covering: none present, each level alone,
      all levels together, conflicting rules, deeply nested

---

### TD-502 — `CLAUDE.md` fallback
**Size:** 2 · **Depends on:** TD-501

**Acceptance criteria:**
- [ ] At any path, if `AGENTS.md` is absent and `CLAUDE.md` is present, `CLAUDE.md` is used
- [ ] If both are present, `AGENTS.md` wins and the shadowing is noted in the inspector
- [ ] Applies at every level including `~/.claude/CLAUDE.md` for the global scope
- [ ] Test: a workspace containing only `CLAUDE.md` files loads with full fidelity

**Notes:** This is an adoption feature. Existing repos configured for other tools must work on
day one with nothing to port. Do not treat it as an edge case.

---

### TD-503 — Path-scoped rules
**Size:** 5 · **Depends on:** TD-501

**Acceptance criteria:**
- [ ] `.tst/rules/*.md` support frontmatter with an `appliesTo` array of glob patterns
- [ ] Rules without `appliesTo` always load
- [ ] Rules with `appliesTo` load only when the session touches a matching file
- [ ] Activation is dynamic — a rule that becomes relevant mid-session is injected, and the
      injection is announced in the timeline
- [ ] Glob matching tested against: exact paths, `*`, `**`, extension patterns, negation if
      supported, and paths with spaces or unicode
- [ ] Unmatched rules are visible in the inspector as inactive, with their cost shown as zero

**Notes:** This is the mechanism that keeps a large ruleset affordable. Spec §4.3.

---

### TD-504 — Import resolution
**Size:** 5 · **Depends on:** TD-501

**Acceptance criteria:**
- [ ] `@path/to/file.md` resolved, relative and absolute, including `~` expansion
- [ ] Recursive imports to a maximum depth of 4; exceeding it is a clear error naming the chain
- [ ] Cycles detected and reported with the full cycle path, never infinite-looping
- [ ] **Import directives inside fenced code blocks and inline code spans are not evaluated** —
      documentation about imports must be safe to write
- [ ] Missing import files produce a warning naming the file, and do not abort the session
- [ ] Imported content carries provenance to its own file, not the importer

---

### TD-505 — External import approval
**Size:** 2 · **Depends on:** TD-504, TD-802

**Acceptance criteria:**
- [ ] First import from outside the workspace raises an approval request naming the file
- [ ] Approval is remembered per workspace per file path
- [ ] Denial omits the import and continues with a warning in the timeline

---

### TD-506 — Token counting
**Size:** 3 · **Depends on:** TD-501

**Acceptance criteria:**
- [ ] Per-source token counts computed with the tier's tokenizer where available, or a
      documented approximation with the method stated in the UI
- [ ] Total steering budget reported
- [ ] **A file exceeding 200 lines produces a soft warning in the inspector**, citing reduced
      adherence, with a link to the authoring guide
- [ ] Counts available via `get_instruction_stack`

---

### TD-507 — Workspace manifest
**Size:** 3 · **Depends on:** TD-501

**Acceptance criteria:**
- [ ] File tree built from the workspace, honoring `.gitignore` and a configurable ignore list
- [ ] Depth and entry count capped, with truncation clearly marked
- [ ] Rebuilt on change with debounce, not on every turn
- [ ] Large repositories (100k+ files) do not stall session start — verified with a synthetic tree

---

### TD-508 — Per-tier context routing
**Size:** 3 · **Depends on:** TD-501, TD-303

**Acceptance criteria:**
- [ ] Brain receives full steering plus manifest
- [ ] Worker receives steering plus current task and relevant files, no manifest
- [ ] Validator receives only the standards/conventions subset plus the diff and test output
- [ ] Subset selection is configurable, with a documented default
- [ ] Test asserts each tier's assembled prompt contains and excludes the expected blocks

**Notes:** Keeping validator context tight is what holds it near $0.08/session. Spec §4.6.

---

### TD-509 — Hot reload
**Size:** 2 · **Depends on:** TD-501

**Acceptance criteria:**
- [ ] Steering file edits are detected and re-resolved without restarting the session
- [ ] Reload announced in the timeline and reflected in the inspector
- [ ] Cache prefix invalidation handled correctly on reload
- [ ] Debounced against rapid successive saves

---

## Epic E6 — Tools

**Goal:** filesystem and shell, correctly bounded. Boundary enforcement here is a security
control, not a convenience.

---

### TD-601 — Tool registry
**Size:** 3 · **Depends on:** TD-201

**Acceptance criteria:**
- [ ] Tools declare name, description, JSON schema, side-effect class, and parallel-safety
- [ ] Registry produces provider-format tool definitions
- [ ] Registration is explicit; no dynamic discovery in v0.1
- [ ] Unknown tool names return a structured error to the model

---

### TD-602 — Path boundary enforcement
**Size:** 5 · **Depends on:** TD-601

**Security-critical. Write the attacks as tests first.**

**Acceptance criteria:**
- [ ] Every path resolved to canonical absolute form before any check
- [ ] Access outside `writable_paths` refused with a clear error
- [ ] **Traversal blocked:** `../` sequences, absolute paths, symlinks pointing outside the
      workspace, hardlinks, and paths that become external only after resolution
- [ ] Windows-specific cases covered: drive-relative paths, UNC paths, `8.3` short names,
      alternate data streams
- [ ] **Writes to `AGENTS.md`, `CLAUDE.md`, and `.tst/rules/**` are refused unconditionally**,
      enforced in the tool itself (prime directive §2.4)
- [ ] Refusals are logged to the audit trail as Class C events

---

### TD-603 — Filesystem read tools
**Size:** 2 · **Depends on:** TD-602

**Acceptance criteria:**
- [ ] `fs_read` with optional line ranges; returns content with line numbers
- [ ] `fs_list` and glob search, respecting ignore rules
- [ ] Binary files detected and refused with an explanatory message rather than dumping bytes
- [ ] Large files truncated with explicit markers and a stated total size
- [ ] Encoding errors handled without crashing

---

### TD-604 — Filesystem write tools
**Size:** 3 · **Depends on:** TD-602, TD-705

**Acceptance criteria:**
- [ ] `fs_write` creates or overwrites; parent directories created as needed
- [ ] `fs_edit` performs exact string replacement, failing loudly if the target is absent or
      ambiguous
- [ ] Every write produces a diff in the `tool_result` for display
- [ ] Writes are atomic (temp file plus rename) — no partial file on failure
- [ ] Every write is checkpointed per TD-705

---

### TD-605 — Shell execution
**Size:** 5 · **Depends on:** TD-602

**Acceptance criteria:**
- [ ] Commands run with the workspace as working directory
- [ ] Configurable timeout; process group killed on timeout or cancel
- [ ] stdout and stderr streamed to the timeline as they arrive, not buffered to the end
- [ ] Output capped with truncation markers
- [ ] Exit code returned; non-zero is a normal result the model can reason about, not an error
- [ ] Environment sanitized: no API keys, no keychain material, no tokens passed to child
      processes — asserted by test
- [ ] `allowed_commands` allowlist enforced when configured, matching on the resolved binary

---

## Epic E7 — Autonomy hooks

**Goal:** the classifier, the ledger, and checkpoints — shipping in v0.1 so that Class A
decisions stop interrupting the user immediately, and so autonomy is never a retrofit.
Spec §12.

---

### TD-701 — Decision class model and rule table
**Size:** 3 · **Depends on:** TD-601

**Acceptance criteria:**
- [ ] Classes A, B, C modeled per spec §12.2
- [ ] Static rule table classifies unambiguous cases without a model call: any path outside the
      workspace → C; any network call to a new host → C; steering-file write → C; cap exceeded
      → C; in-workspace source edit within `writable_paths` → A
- [ ] Rule table is data, not scattered conditionals, and is unit-tested case by case
- [ ] Every classification records which rule fired, for explainability

---

### TD-702 — Classifier chokepoint
**Size:** 3 · **Depends on:** TD-701, TD-402

**Acceptance criteria:**
- [ ] **Every tool dispatch routes through the classifier. There is no bypass path** — asserted
      by a test that enumerates dispatch call sites
- [ ] Classification precedes execution and precedes any approval decision
- [ ] Class is attached to the audit record and to the `tool_call` event
- [ ] A tool that somehow reaches execution unclassified raises immediately rather than
      proceeding

---

### TD-703 — Ambiguous-case classifier call
**Size:** 3 · **Depends on:** TD-702, TD-303

**Acceptance criteria:**
- [ ] Cases the rule table cannot decide are classified by a **worker-tier** call with a
      tight, cached prompt
- [ ] Result cached per (tool, argument-shape) within a session to avoid repeat cost
- [ ] Classifier failure defaults to **B**, never to A — fail toward asking, not toward acting
- [ ] Classifier cost is tracked separately and visible in the cost breakdown

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
- [ ] Each meaningful unit of work commits to a session branch `tst/session/<id>`
- [ ] **Never commits to `main`**
- [ ] Commit message references the decision and session
- [ ] Non-git workspaces degrade gracefully: feature disabled, user informed once, everything
      else still works
- [ ] Pre-existing uncommitted user changes are never clobbered — detected and reported first
- [ ] Test covers: clean repo, dirty repo, no repo, detached HEAD, mid-rebase

**Notes:** The branch is the undo stack, the audit trail, and the review surface. This story is
what makes aggressive Class A behavior safe.

---

### TD-706 — Boundary configuration
**Size:** 2 · **Depends on:** TD-602

**Acceptance criteria:**
- [ ] `.tst/config.yaml` defines `writable_paths`, `allowed_commands`, `network`, and caps
      (`spend_usd`, `wall_clock_hours`, `max_iterations`)
- [ ] Sensible defaults applied when the file is absent: workspace-only writes, no network,
      conservative spend cap
- [ ] Validated on load with actionable errors
- [ ] Boundary visible in the UI so the user always knows the current wall

---

### TD-707 — Cap enforcement
**Size:** 3 · **Depends on:** TD-706, TD-304

**Acceptance criteria:**
- [ ] Spend cap checked before each model call; exceeding it pauses the session
- [ ] Wall-clock and iteration caps enforced
- [ ] Pause is a **fault report**, not an approval request — session enters a distinct state
      with a clear summary
- [ ] User can raise the cap and resume without losing session state
- [ ] Test: a session with a $0.01 cap halts on the first call and reports correctly

---

## Epic E8 — Approvals and policy

---

### TD-801 — Policy model
**Size:** 3 · **Depends on:** TD-706

**Acceptance criteria:**
- [ ] Policy maps (tool, argument pattern) → `auto` | `ask` | `never`
- [ ] Defaults derive from decision class: A → auto, B → ask, C → ask-or-never per config
- [ ] Persisted per workspace in `.tst/config.yaml`
- [ ] Most-specific pattern wins; precedence tested
- [ ] Policy never grants what the boundary forbids — boundary always wins

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
- [ ] "Always allow in this workspace" writes a policy rule scoped as narrowly as the request
      allows — never a blanket grant for the whole tool
- [ ] The generated rule is shown to the user before it is saved
- [ ] Saved rules are listed and individually revocable in settings
- [ ] Class C actions can never be always-allowed

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
- [ ] Single window, native chrome, correct behavior on macOS, Linux, Windows
- [ ] Window state (size, position) persisted across launches
- [ ] Two-pane layout with a draggable divider whose position persists
- [ ] Design tokens applied; no hardcoded colors or spacing in components
- [ ] Light and dark themes following the OS preference

---

### TD-1002 — Daemon supervision
**Size:** 5 · **Depends on:** TD-1001, TD-202

**Acceptance criteria:**
- [x] Tauri host spawns `tstd` on launch and connects using the port file
- [x] Daemon crash is detected, reported in the UI, and recovered by restart with the session
      list intact
- [x] Closing the window shuts the daemon down cleanly in v0.1 — with a `TODO(v0.3)` marking
      where detached-session behavior will diverge
- [ ] No orphaned `tstd` processes after quit under any exit path, including force-quit —
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
- [ ] Multi-line composer with submit-on-Enter and newline-on-Shift-Enter
- [ ] Streaming assistant output rendered smoothly, without layout jump
- [ ] Markdown rendering with syntax-highlighted code blocks and copy buttons
- [ ] Auto-scroll that stops when the user scrolls up, with a jump-to-latest affordance
- [ ] Conversation history scrollable and virtualized for long sessions
- [ ] Cancel button available whenever a turn is running

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
- [ ] Actions: Approve, Deny, Always allow in this workspace
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
**Not in scope here:** "Always allow in this workspace" (criterion 2, third action) is
delivered by TD-803, not TD-1007 — implementing it here would collide with TD-803's
policy-rule model. The other two actions (Approve / Deny) are wired end-to-end via a new
`send()` client path plus the `error_code` propagation fix ("Gap B") that lets the UI
distinguish a denial from a handler error.

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
- [ ] Detects first run and presents: welcome → API key → preset → workspace → done
- [ ] Every step skippable-with-consequence and revisitable from settings
- [ ] The key step links to where to get one and validates it with a single cheap live call
- [ ] Completing the wizard lands the user in a working session, ready to type
- [ ] **Total time from launch to first message under two minutes**, measured

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
- [ ] Native folder picker
- [ ] Recent workspaces list with quick switching
- [ ] Opening a workspace scaffolds `.tst/` with a commented default config
- [ ] Switching workspaces re-resolves steering and rebuilds the manifest
- [ ] A workspace that has become unavailable is reported clearly and removed from recents on
      request

---

### TD-1104 — Diagnostics
**Size:** 2 · **Depends on:** TD-1101

**Acceptance criteria:**
- [ ] A doctor view checking: daemon reachable, key present and valid, provider reachable,
      git available, workspace writable, steering files parseable
- [ ] Each failure states the specific fix
- [ ] Output copyable as redacted text for bug reports

---

## Epic E12 — Instruction inspector

**Goal:** answer "did my rules take effect?" with a pane instead of guesswork. Spec §4.4.

---

### TD-1201 — Resolved stack panel
**Size:** 3 · **Depends on:** TD-506, TD-1003

**Acceptance criteria:**
- [ ] Lists every steering source in precedence order with per-file token counts
- [ ] Shows total token cost and whether the block is currently cached
- [ ] Path-scoped rules show matched or unmatched, and what they matched
- [ ] `CLAUDE.md` fallbacks and shadowed files clearly labeled
- [ ] Imports shown nested under their importer
- [ ] Files over 200 lines flagged with the adherence warning
- [ ] Clicking a file opens it in the system editor
- [ ] Live-updates on hot reload

---

### TD-1202 — Decisions ledger panel
**Size:** 3 · **Depends on:** TD-704, TD-1003

**Acceptance criteria:**
- [ ] Session decisions listed with class, choice, rationale, and commit
- [ ] Filterable by class
- [ ] Each entry offers a copyable revert command
- [ ] **Reviewing a full session of Class A decisions takes under a minute** — the density
      target that makes the whole autonomy trade work
- [ ] Links out to the markdown ledger file

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
- [ ] Path escape attempts across all vectors in TD-602
- [ ] Steering-file write refusal
- [ ] Environment sanitization for child processes
- [ ] Secret redaction across logs, audit, diagnostics, and error messages
- [ ] Non-loopback bind refusal
- [ ] Classifier bypass attempts
- [ ] **Every test in this suite is a release blocker**

---

### TD-1403 — Context assembler suite
**Size:** 3 · **Depends on:** TD-501, TD-503, TD-504

**Acceptance criteria:**
- [ ] Fixture workspace tree exercising every precedence, fallback, glob, and import case
- [ ] Golden-file assertions on assembled prompts
- [ ] Property test: assembly is deterministic for identical inputs
- [ ] Cache-prefix stability asserted across turns

---

### TD-1404 — Performance baselines
**Size:** 3 · **Depends on:** TD-1401

**Acceptance criteria:**
- [ ] Measured and recorded: daemon cold start, session start on a large repo, steering
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
| M2 The window | E10–E12 | 14 | 51 |
| M3 Shippable | E13–E15 | 11 | 43 |
| **Total v0.1** | **15** | **75** | **252** |

Points are relative sizing for sequencing and splitting decisions, not a schedule. Do not
convert them to dates.

**The single most important sequencing rule in this document: M1 exits when TD-1401 passes.**
A correct, tested, headless core is what makes the UI a presentation problem instead of a
debugging problem.
