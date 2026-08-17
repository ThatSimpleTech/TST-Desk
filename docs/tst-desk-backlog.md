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
| **M1.5 — Local models** | E18 | A scripted request runs end-to-end against a local OpenAI-compatible endpoint with no API key and no spend |
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

E18 Local models hangs off E3 (router) and TD-1401 (the headless harness): it makes the
model plane run without a key or a bill.

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

### TD-208 — `parse_daemon_event` rejects `rule_activated`, an event the daemon emits
**Size:** 1 · **Depends on:** TD-204

**Acceptance criteria:**
- [x] `parse_daemon_event` round-trips every member of `DaemonEventT`, `rule_activated` included
- [x] A test derives the expected set from the union rather than restating it, so the next
      message added to `DaemonEventT` without its frozenset entry fails the suite
- [x] The same check covers `ClientMessageT` / `_KNOWN_CLIENT_TYPES`, which agree today and
      should stay that way
- [x] The TD-208 note in `docs/architecture.md` §3 comes out with the fix — its doc test asserts
      the gap still exists and goes red when it does not

`RuleActivated` is declared at `protocol.py:615`, is a member of `DaemonEventT` at
`protocol.py:883`, and is emitted by the agent loop at `loop.py:759`. It is absent from
`_KNOWN_EVENT_TYPES` (`protocol.py:931`). `_check_known_type` runs before validation, so the
parser refuses an event the daemon itself sends:

    >>> raw = RuleActivated(seq=3, session_id="s1", rule_path=".tst/rules/api.md").model_dump_json()
    >>> parse_daemon_event(raw)
    UnknownMessageTypeError: unknown_message: Unknown message type 'rule_activated'.

The union has 25 members; the frozenset has 24. Every other member matches, and
`ClientMessageT` and `_KNOWN_CLIENT_TYPES` agree at 27 each — so this is a single missed line,
not a pattern.

No user-visible symptom today, which is why it survived: the shipping clients do not use this
parser. The Rust host only checks for `hello_ack`, and the UI has its own hand-mirrored
`KNOWN_EVENT_TYPES` in `ui/src/lib/client.ts` — which *does* list `rule_activated`, so the
timeline renders the event correctly. The break is latent in the Python client surface, and
`tst attach` (spec §2) is a stated goal that would hit it.

`test_protocol.py` round-trips only the message types it imports, and `RuleActivated` is not
among them. That gap is the second criterion: a hand-listed test cannot catch a hand-listed
frozenset drifting.

Found while writing TD-1504 and confirmed by execution, not by reading.

**Done (2026-08-17):** `"rule_activated"` added to `_KNOWN_EVENT_TYPES`. The guard is
`TestKnownTypeGate` in `core/tests/test_protocol.py`: both frozensets are derived from their
unions and compared in both directions, so a missing entry and a stale one each fail, and every
union member is round-tripped through the parser that gates it — 56 cases, sample instances
built from the models rather than hand-listed. Counts today: `DaemonEventT` 27 members,
`ClientMessageT` 29, both frozensets now equal. The architecture guide's TD-208 note and the doc
test pinning it are gone; §3 now says the sets are checked against the unions instead.

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

### TD-510 — Frontmatter outside `.tst/rules/` is neither honoured nor stripped
**Size:** 2 · **Depends on:** TD-503

**Acceptance criteria:**
- [x] An `AGENTS.md` or `CLAUDE.md` opening with a YAML frontmatter block does not send the
      `---` delimiters or the YAML to the model
      — Done: `parse_frontmatter` now runs for every precedence level, not just `RULES`.
- [x] Whether `appliesTo` is honoured outside `.tst/rules/` is decided and documented — today
      it is silently neither honoured nor removed
      — Done: stripped, never honoured, and flagged in the inspector. DECISIONS.md 2026-08-17.
- [x] `docs/steering.md`'s migration section matches whatever is decided
      — Done: §9 rewritten; §4 example and §10 updated with it, all verified live.
- [x] A test drives a frontmattered `AGENTS.md` through the assembler and asserts the block is
      absent from the assembled prompt
      — Done: `TestFrontmatterStrippedAtEveryLevel`, table-driven over all six file kinds.

`assemble_sync` calls `parse_frontmatter` only when `source.precedence == Precedence.RULES`
(`core/tstd/context/assembler.py:190`); for every other level `body = content` unchanged. A
steering file that opens with frontmatter therefore ships its YAML to the model verbatim.

This is the one that bites arrivals from other tools, whose instruction files commonly open with
frontmatter — exactly the users TD-1502's migration note tells that nothing needs porting.

Found while writing TD-1502, confirmed independently by reading the gate.

---

### TD-511 — A bare-name `appliesTo` pattern matches a filename suffix, not a basename
**Size:** 1 · **Depends on:** TD-503

**Acceptance criteria:**
- [x] `appliesTo: [config.py]` activates on `config.py` and on `pkg/config.py`, but not on
      `oldconfig.py`
      — Done: `**/` translates to `(?:.*/)?`, anchoring the next literal to a segment boundary.
- [x] An anchored spelling exists and is documented; today `config.py` and `**/config.py`
      compile to the identical regex, so there is no way to ask for the strict match
      — Done: both still compile identically, but now both *anchor*, so the bare name is the
      anchored spelling. §4 says so and names `src/config.py` for narrowing further. Root-only
      anchoring (a leading `/`) is still unexpressible — left alone as out of scope, see below.
- [x] Table-driven tests over the glob translator cover the suffix case
      — Done: `_GLOB_TABLE`, 38 rows over `_path_matches_glob`, plus 4 end-to-end rows.

`_glob_to_re` rewrites a bare name to `**/config.py`, then translates `**` to `.*` and skips the
following `/` (`core/tstd/context/assembler.py:315-321`), yielding `^.*config\.py$`. The correct
translation for a leading `**/` is `(?:.*/)?`.

Severity is scope-widening, not correctness: an over-matching rule activates when it should not,
costing tokens and attention rather than producing a wrong answer. Filed at size 1 for that
reason.

Two smaller observations from the same pass, not filed separately: `~/.tstdesk/AGENTS.md` sits
outside every workspace, so its own sibling imports trip TD-505's external-import gate once per
workspace ever opened — defensible, since the gate is about where a file lives, but it makes
splitting personal steering across files more friction than it looks. And
`ui/src/lib/components/StackPanel.svelte:41` hardcodes `warning.includes('exceeds 200 lines')`
to choose its badge text, so changing `LINE_LIMIT` silently drops the short badge back to the
full warning string.

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
### TD-606 — An empty `allowed_commands` refuses every command instead of allowing any
**Size:** 2 · **Depends on:** TD-605

**Acceptance criteria:**
- [x] A workspace with no `.tst/config.yaml` can run a shell command — the default is
      unrestricted, matching the shipped template's own comment
- [x] An explicitly empty `allowed_commands: []` means whatever the documented semantics are
      decided to be, and the template comment and the configuration reference agree with the
      code — today they contradict it
- [x] A non-empty allowlist still restricts to exactly those binaries; the fix must not turn
      the allowlist off
- [x] A test covers the daemon's own wiring, not just `ShellPolicy` constructed directly —
      this defect lives in the wiring and every existing shell test bypasses it

`ShellPolicy.allowed_commands` uses `None` as the unrestricted sentinel (`tools/shell.py`).
`daemon.py:1179` passes `tuple(sess.boundary_config.boundary.allowed_commands)`, and
`BoundaryConfig.allowed_commands` defaults to `[]` (`boundary_config.py:36`). `tuple([])` is
`()`, which is not `None`, so the allowlist is present and empty and every command is refused:

    'echo' (resolved to 'echo') is not in allowed_commands []

Because `[]` is the default, this applies to any workspace without a `.tst/config.yaml` — which
is the default state. The shipped template says the opposite at `boundary_config.py:107`:
"Command allowlist for the shell tool; empty means any command."

Found while writing TD-1503, and confirmed independently: the default loads as `[]`, and
`tuple([]) is None` is `False`. The whole existing shell suite passes because it constructs
`ShellPolicy` directly with either `None` or a populated list, so nothing exercises the path the
daemon actually takes. That gap is the fourth criterion.

Milestone note: this is an M1 product defect surfacing after M1 closed. It is filed in E6 rather
than a testing epic because the product is wrong, not the test — but the missing coverage is
what let it survive.

**Done (2026-08-17).** `BoundarySection.shell_allowlist()` returns `None` — `ShellPolicy`'s
unrestricted sentinel — for an empty or omitted list, and `daemon.py` asks it instead of passing
the raw list. The shipped template comment needed no change: it was right all along, and the
code now agrees with it.

Permissive is safe here because the allowlist is not the gate. No rule in `RULE_TABLE` matches on
tool name, so a plain shell call falls through to the ambiguous classifier and defaults to class
B — an approval the user answers (§2.6). The list narrows a path that is already guarded.

The two other `allowed_commands` readers in `daemon.py` were left alone deliberately: both build
`BoundaryUpdateEvent`, which reports the configured wall to the UI, and an empty list there
means "no allowlist configured" rather than the sentinel.

12 tests. The first cut of them mirrored the daemon's wiring and passed with `daemon.py`
reverted — the same gap that let this ship, reproduced in its own fix. `TestTheDaemonsOwnWiring`
now spies on `register_builtin_handlers` while calling `_start_session`; two of its runs go red
on revert, verified. `docs/configuration.md` updated, and its executable-examples harness keeps
it honest.

---

### TD-607 — `dispatch_many` returns results out of order when a batch is mixed
**Size:** 2 · **Depends on:** TD-601

**Acceptance criteria:**
- [x] `dispatch_many` returns results in the same order as the input tool calls, as its
      docstring already promises — or the docstring and callers change to say order is not
      guaranteed, and the timeline ordering is handled explicitly
- [x] A test covers a *mixed* batch (parallel-safe and sequential in one call); every existing
      test uses a homogeneous batch, which is why this survived
- [x] Parallelism is preserved — the fix must not serialise parallel-safe tools
- [x] A tool handler raising inside the parallel batch produces an error `ToolResult` rather
      than propagating out of `dispatch_many`

`dispatch_many` (`tools/dispatch.py:496`) partitions the batch, runs the parallel-safe tools
concurrently and the rest sequentially, then returns `parallel_results + sequential_results`
(`dispatch.py:544`). Concatenating the two partitions discards the caller's order. The docstring
four lines above says the opposite: "Results in the same order as the input tool calls."

Reproduced with a three-call batch, one parallel-safe tool between two sequential ones:

    input order : ['c1', 'c2', 'c3']
    result order: ['c2', 'c1', 'c3']

`loop.py:397` is the only production caller, and it iterates the results to emit one
`tool_result` event each. So a turn that mixes a read with a shell command emits the read's
result first regardless of the order the model asked for them, and the timeline shows them that
way. The conversation itself is likely unaffected — tool messages carry `tool_call_id` and
OpenAI-compatible providers match on it — but "likely" is doing real work in that sentence, and
the ordering is not something the loop should have to reason about.

The fourth criterion is a second, smaller bug in the same method: `dispatch.py:525-535` calls
`asyncio.gather(..., return_exceptions=True)` and then `task.result()`. `return_exceptions`
governs what `gather` returns, not what `task.result()` does — `.result()` re-raises. So the
`isinstance(result, Exception)` branch below it is unreachable, and a handler that raises inside
the parallel batch propagates out of `dispatch_many` instead of becoming the `concurrent_error`
result that branch was written to produce.

Found while writing TD-1504, and confirmed by execution. Every existing `dispatch_many` test
(`test_dispatch.py:347`, `:387`) uses a batch of one tool type, so neither the ordering nor the
exception path is exercised today.

**Done (2026-08-17).** Each call now carries its index in `tool_calls` through the partition,
and the two partitions write into one `dict` keyed on that index, read back out in range order.
The invariant is the input list position — deliberately not `tool_call_id`, which the client
supplies and may repeat, and not a sort over anything derived from the call. Parallelism is
untouched: the parallel-safe coroutines still go to one `asyncio.gather`, and a mixed batch of
three parallel plus one sequential still finishes in about one parallel round plus the
sequential call.

The exception path now reads `gather`'s returned list instead of calling `task.result()` on each
task, which is what made the `concurrent_error` branch unreachable. That result also carries the
call's real `tool_call_id` and `name` rather than `""` — `loop.py` keys its `tool_result` events
on the id, so an empty one would have been a second bug wearing the first one's clothes.

The fourth criterion was already half-true and half-false, which the story could not have known.
A tool handler raising is caught inside `dispatch` itself (`dispatch.py:354`) and returns a
`handler_error` result, so that exact case never escaped. What escaped was anything raised
around the handler — the classifier, the approval gate, the path guard, or truncation choking on
a handler that returned `None` instead of a string. The `concurrent_error` test uses that last
one, since a handler forgetting its `return` is the realistic shape, and it fails on the unfixed
code with a `TypeError` out of `dispatch_many`.

`UnclassifiedToolCall` is deliberately re-raised rather than converted (see DECISIONS.md): the
chokepoint is a §2.6 guarantee, and it already propagates from the sequential path. Six tests in
`TestDispatchManyOrdering`; five of the six go red on the unfixed method, verified by stashing
`dispatch.py` alone and rerunning.

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

# MILESTONE M1.5 — Local Models

## Epic E18 — Local model support

**Goal:** a workspace can run entirely on a local OpenAI-compatible endpoint — no API key, no
spend, no network beyond loopback. The headless core already works against keyed remote
providers; this makes the free path a first-class one.

A spike on 2026-08-15 drove the daemon against `qwen3.8:27b` on Ollama and completed a turn
end to end — `fs_write` → approval gate → `tool_result` → `cost_update` → `turn_complete`,
with boundary refusal and the shell allowlist both firing correctly against a live model. The
provider contract holds. These stories make that repeatable rather than a one-off.

---

### TD-1801 — Keyless local provider
**Size:** 3 · **Depends on:** TD-301

**Acceptance criteria:**
- [x] A tier whose `base_url` is a loopback address resolves with no keychain entry and no
      key prompt
- [x] `_ensure_provider` does not raise when no key is stored for a keyless endpoint
- [x] The wizard's `has_api_key` signal does not block a workspace whose active preset is
      local-only
- [x] Remote tiers still require a key — asserted by test

**Done (2026-08-16):** `config.is_loopback_url` classifies an endpoint (127.0.0.0/8, `::1`,
`localhost`) and `daemon._brain_client` skips the keychain for one — the shipped `local` preset
resolves to a `ProviderClient` with `api_key=None` and zero keychain reads, and
`ProviderClient._headers` omits `Authorization` rather than sending an empty bearer, so keyless
is provable on the wire.  `setup_state` gains an additive `key_required` (default `True`, no
`PROTOCOL_VERSION` bump); the wizard's auto-open gate becomes `!has_api_key && key_required` so
`has_api_key` stays an honest probe instead of lying to skip a modal, and the doctor's `api_key`
row skips rather than fails.  Remote tiers key off the URL rather than a caught `KeychainError`,
so `tst-default` with nothing stored still raises and `KeychainLockedError` still surfaces —
negatives in `core/tests/test_local_provider.py`.  Deferred: resolution is brain-tier-wide, so a
mixed loopback/remote preset resolves keyless while the conservative `requires_api_key()` still
reports `key_required: true` — fails safe, raised not built (DECISIONS.md TD-1801 §5).

Ollama's OpenAI-compatible endpoint ignores the `Authorization` header entirely; it accepts
any value or none. `_ensure_provider` currently calls `ProviderClient.from_keychain(...)`
unconditionally, which makes a stored key mandatory for every tier and blocks a fresh
local-only setup at the first turn. This removes a requirement rather than adding a storage
location — §2.2 still holds, keys never leave the keychain.

---

### TD-1802 — Local preset and zero-cost accounting
**Size:** 2 · **Depends on:** TD-302, TD-304, TD-1801

**Acceptance criteria:**
- [x] A `local` preset ships in `config.yaml` with all three tiers pointing at an
      OpenAI-compatible loopback endpoint
- [x] Prices of `0.00` flow through cost accounting without divide-by-zero or NaN; the meter
      reads `$0.00`
- [x] The ledger still records real token counts for a zero-price tier — free is not untracked
- [x] `context_window` and `max_output_tokens` come from config, never inferred from the slug

**Done (2026-08-16):** the `local` preset points all three tiers at `http://127.0.0.1:11434/v1`
at price 0.0 with `context_window: 32768` — a compaction budget sized to what a server actually
serves rather than the model's 262144 ceiling, with the reasoning next to the value and
`budget_threshold` its only consumer.  `core/tests/test_zero_cost.py` sweeps the whole
`CostTracker` reporting surface at price 0.0 and asserts finite, lands a full free turn in the
audit ledger with real token counts, and tracks `budget_threshold` against config with the slug
held fixed — omitting `context_window` or `max_output_tokens` is a validation error, so there is
no inferred default to fall back on.  `formatUsd` moved to `ui/src/lib/cost-format.ts` and
renders exact zero as `$0.00`, guarded on exact equality so spend that merely rounds to zero
keeps four decimals.  Ledger tracking on the *shipped* local path was broken when this landed —
usage arrives on a chunk carrying no `finish_reason`, so only `MockProvider` exercised the
working path; TD-1804 fixed `loop.py` and corrected this story's DECISIONS.md entry, and a live
pass now writes real `model_calls` rows at cost 0.0.  Deferred: the shipped slug is one
developer's Ollama tag — TD-1805.  (Boxes ticked at validation: the behaviour and its evidence
were on the branch, the checkmarks were not.)

A `local` preset already exists but points at `http://localhost:8000/v1` with Qwen 2.5 slugs
that no longer match anything running. The spike confirmed zero prices already flow through
cost accounting without crashing and that `context_window` is read from config; this story
retargets the preset and puts both behaviours under test rather than leaving them incidental.

---

### TD-1803 — Live-provider end-to-end harness
**Size:** 5 · **Depends on:** TD-1401, TD-1801, TD-1802

**Acceptance criteria:**
- [x] The headless harness runs against a real OpenAI-compatible endpoint, selected by flag,
      still defaulting to the mock
- [x] One scripted task completes end to end against a local model: message → tool call →
      classification → execution → ledger → cost
- [x] Live runs are excluded from the default CI leg and marked as requiring a reachable model
- [x] Failures distinguish provider-contract breakage from agent-loop breakage
      (exit 3 vs 1; exercised by hand against broken fake endpoints — no offline test
      pins the attribution path)
- [x] **This harness is the M1.5 exit criterion**

**Done (2026-08-16):** `tstd.e2e_live` plus a `HarnessPlan` (`tstd.e2e_plan`) that
`e2e_harness.run` takes as data, so the live leg added no branch the mock pass skips —
`--live-endpoint` on `scripts/e2e_headless.py` selects it and the mock stays the default.  Pinned
as `tests/test_e2e_live.py` and deselected by `addopts = -m 'not live'`, so `ci.yml`'s bare
pytest excludes it with no workflow change; an unreachable or wrong-slug endpoint skips with the
reason and a non-loopback one is refused before any request leaves the box.  Failures are
attributed by exit code: 3 with a `PROVIDER CONTRACT` line when `LiveProvider` recorded a
`ProviderError`, 1 when the endpoint held its contract and the loop failed a check anyway.  The
live `ledger` and `cost accounting` checks failed on first run and were raised Class C rather
than patched here; TD-1804's `loop.py` fix landed and the full pass now goes green against
`qwen3.8:27b` on Ollama in ~28s.

TD-1401 proves the loop against `MockProvider` only, by design — deterministic and offline.
This adds a live leg without disturbing that: the mock stays the default and TD-1401's
behaviour must remain unchanged. Note the live path must approve on `approval_request`, not on
`tool_call` — TD-802's gate registers the pending approval when it emits the request, and the
spike failed against the older ordering before being corrected.

---

### TD-1804 — Record usage independently of `finish_reason`
**Size:** 2 · **Depends on:** TD-1803

**Acceptance criteria:**
- [x] Usage is recorded whenever a stream chunk carries it, whether or not that same chunk
      also carries `finish_reason`
- [x] A provider that splits `finish_reason` and `usage` across separate chunks produces
      exactly one ledger row and one `cost_update` for the call
- [x] A provider that co-emits them on one chunk still produces exactly one ledger row and one
      `cost_update` — no double counting; `MockProvider`'s behaviour and TD-1401 are unchanged
- [x] The live harness's `ledger` and `cost accounting` checks pass against a local endpoint
- [x] A regression test pins the split-chunk ordering using a scripted provider, so this is
      caught without a reachable model

**Done (2026-08-16):** `_stream_and_parse` in `core/tstd/loop.py` keeps the usage from any chunk
that carries one and records once after the stream closes — last-wins, so a provider that repeats
cumulative usage bills once with the complete figure rather than three times or a partial first
count.  The provider-error and cancellation exits now `break` instead of returning, so reported
tokens are recorded even when a call ends badly.  `core/tests/test_usage_recording.py` pins it
offline with a `ScriptedProvider` that replays chunk sequences verbatim: five usage placements,
repeated cumulative usage, a stream with none, two calls in one turn, two turns, plus a direct
assertion that `MockProvider` still co-emits — `mock.py` is untouched and TD-1401 is unchanged.
Live against Ollama 0.32.13 `qwen3.8:27b`: `ledger sessions=1`, `cost accounting cost=0.0
tokens=1391`, OVERALL PASS; the same run on the pre-fix loop fails exactly those two checks.
DECISIONS.md's TD-1802 entry is corrected in place.  Deferred: `turn_complete.tokens` still
reports the final provider call only — filed as TD-1806.

`loop.py` gates recording on `if chunk.finish_reason and chunk.usage:`, which demands both
fields on one chunk. Ollama 0.32.13 sends `finish_reason` on one chunk and `usage` on a later
one, so the branch never fires and a local turn produces zero ledger rows and zero
`cost_update` events — the meter never moves on the free path. Verified against a running
server: `finish_reason='length' usage=None`, then `finish_reason=None usage=present`.

This lives in E18 rather than E4 because the scope unit is "make local models work"; the code
it touches happens to be the agent loop. It affects every provider that defers usage, not just
Ollama, so the fix must be idempotent rather than merely relocated — recording on any chunk
carrying usage would double count a provider that sends it twice.

DECISIONS.md's TD-1802 entry defers this on the premise that "Ollama co-emits usage with
`finish_reason`". That premise is false; correct the entry as part of this story.

---

### TD-1805 — Resolve the local model from the endpoint
**Size:** 3 · **Depends on:** TD-1802

**Acceptance criteria:**
- [x] The shipped `local` preset names no specific model tag; a fresh install works against any
      local OpenAI-compatible server without hand-editing config
- [x] When a tier's slug is unset, it resolves from the endpoint's `/v1/models` on first use;
      a slug set in config always wins over discovery
- [x] Discovery failure (endpoint unreachable, or serving no models) surfaces a typed error
      naming the endpoint and the fix — not a generic provider error
      (the turn path carries it as `model_unresolved` with tailored UI copy; the endpoint
      itself is named in the doctor's `provider` row, not on the wire — see DECISIONS)
- [x] Remote tiers never trigger discovery; a remote tier with no slug stays a config error
- [x] Discovery reads and requires no API key, and writes none anywhere (§2.2)

**Done (2026-08-16):** the preset, the discovery path and the loud typed failure landed first;
the story was reopened because it shipped a regression and left AC-3 unmet on the turn path.
`TierConfig.require_slug()` was called on the brain tier by `e2e_harness.mock_plan` and
`benchmarks.measure_first_token_latency` before anything resolved it, so under
`active_preset: local` — the one preset that leaves the slug unset — neither the headless
harness nor the perf baselines would run at all; the suite hid it by running everything under
the pinned remote default. Both now resolve before requiring, which is what the daemon does on
its first turn against the same shared `ModelConfig`, so the pass costs one `/v1/models`
round-trip and an unresolvable endpoint still fails loudly rather than substituting a model.
The mock plan's `expect_spend` is derived from the tier's prices instead of hardcoded, so a
zero-price preset is not failed for honestly billing nothing. `model_unresolved` gained a
banner in `ui/src/lib/error-copy.ts` (it was reaching the wire and rendering as a raw code),
and discovery's fix text now matches the failure — a `base_url` missing `/v1` no longer tells
the user to start the server that is answering it. Also fixed: discovery ran ahead of the
harness's loopback check, so a remote `--live-endpoint` was contacted (10.3s, a full discovery
timeout) and only then refused; the refusal now fires first, in 0.23s, with nothing sent.
`docs/tst-desk-spec.md` §7 documents the config contract (the Class C raised under the first
pass). Verified on Ollama 0.32.13: mock harness and benchmarks green under `active_preset:
local`, live leg 5/5.

TD-1802 retargeted the `local` preset at one developer's Ollama tag (`qwen3.8:27b` at
`127.0.0.1:11434/v1`), which then ships as every user's default. For a bring-your-own-model
product that is the wrong default: the endpoint is a reasonable convention, the model tag is
not. §2.7 already puts slugs in config rather than code, so the shape is right — this fixes the
shipped value by making the slug optional and discoverable.

`/v1/models` is served by Ollama, vLLM, LM Studio and llama.cpp alike; verified returning
`qwen3.8:27b` from the local Ollama on 2026-08-16. Prefer discovery over making every user edit
YAML, and keep config authoritative when it does specify.

---

### TD-1806 — Count a whole turn's tokens and duration in `turn_complete`
**Size:** 2 · **Depends on:** TD-1804

**Acceptance criteria:**
- [x] `turn_complete.tokens` is the sum across every provider call in the turn, including calls
      made before a tool round-trip
- [x] `turn_complete` duration covers the whole turn, not only its final leg
- [x] Per-call ledger rows and `cost_update` events stay at exactly one per provider call —
      TD-1804's guarantee is preserved, not traded away
- [x] A regression test pins a two-call turn (one tool round-trip) offline, with no model running

**Done (2026-08-16):** `tracker.begin_turn()` and `turn_start = time.time()` move out of the
tool-call round-trip loop in `core/tstd/loop.py` and up to the turn boundary, so both
accumulators measure the thing they are named for.  The round-trip loop is otherwise untouched
and per-call accounting still rides on `record()`, once per provider call (TD-1804).  Live
against Ollama 0.32.13 `qwen3.8:27b`: a two-call turn wrote `model_calls` rows of (1249, 174)
and (1448, 50) and `turn_complete` reported `tokens=2921` — their sum, where the pre-fix number
was the last leg alone (1498) — with `turns.duration` 25.6s across a 25.9s pass.
`core/tests/test_turn_totals.py` pins it offline through TD-1804's `ScriptedProvider` replaying
the split `finish_reason`/`usage` shape: the sum, that the total is neither leg on its own, a
first leg slowed to 0.25s so a stopwatch started at the wrong moment cannot hide inside
sub-millisecond legs, exactly two ledger rows and two `cost_update`s, and a second turn starting
from zero so the reset did not leave the turn loop as well.  Entailed and pinned rather than
left to be discovered: `cost_update.turn_cost` is now the turn so far, and the last one agrees
with the `turn_complete` it lands on.  Deferred: `router.record_turn_start()` sits in the same
round-trip loop, so `turn_count` still counts provider calls rather than turns — the same defect
on a different counter, raised in DECISIONS.md for a story of its own because moving it would
change TD-303's tier selection.

`agent_loop` opens its tool-call round-trip loop at `core/tstd/loop.py:630` and calls
`tracker.begin_turn()` at :649 — inside it. Every round-trip resets the turn accumulator, so a
turn containing one tool call reports only the last provider call's tokens. `turn_start =
time.time()` is reset on the same line-pair, so the reported duration is the final leg only.

Surfaced by TD-1804's live run: the harness reported `tokens=1783` for a turn whose ledger rows
summed to 3520. The ledger was right; the turn total was not.

---

### TD-1807 — The live harness must not assume tool-call ordering
**Size:** 3 · **Depends on:** TD-1803

**Acceptance criteria:**
- [x] The harness selects the tool call it checks by name, not by list position; a run in which
      the model calls another tool first still passes when it performs the required write
- [x] The approval-gate check matches an approval to the tool call it belongs to, not to
      whichever request arrived first
- [x] The execution check inspects the result of the write being verified, not `results[0]`
- [x] Extra tool calls neither fail the run nor pass it silently — the harness reports what the
      model actually did
      (closed by TD-1808 on 2026-08-17: extras were already reported as NOTE lines and excluded
      from the verdict; the remaining half — an extra `fs_write` failing a run whose checked
      call succeeded — went with `wrote_file`, which no longer reads the file's final content.)
- [x] The live leg passes five consecutive runs against a local endpoint
      (5/5 green against `qwen3.8:27b` on Ollama 0.32.13, 13–30s each; none of the five
      happened to make an extra call, so the selection rule itself is pinned offline)
- [x] `core/tstd/e2e_harness.py` is back under AGENTS.md §6's ~400-line limit

**Done (2026-08-16):** the verdict half — `HarnessResult` and every check — moved to
`core/tstd/e2e_checks.py`, leaving `e2e_harness.py` with workspace prep, the mock plan, the
protocol client and the CLI; `run()` and `main()` keep their signatures, so
`scripts/e2e_headless.py` and TD-1401's test did not move a line.  333 and 310 lines, both under
§6.  Selection is by identity: the first `fs_write` in the transcript, then its approval and its
result matched on that call's `tool_call_id`, so all three checks are about one call by
construction rather than by coincidence.  *First*, not whichever one passes — hunting for the
call that makes the pass green would turn the M1.5 exit criterion into a search for its own
agreement (§7).  Extra calls land in `HarnessResult.notes` as `name#id → status`, printed as
`NOTE` lines and excluded from `ok`, and the checked call's line states where it sat
(`call 2 of 2`).  `core/tests/test_e2e_ordering.py` pins all of it offline through the real
daemon, classifier, policy gate and dispatcher with only the model scripted — `MockProvider`
reuses one call id for every call and must stay byte-identical for TD-1401 — and two of the five
transcripts it replays must FAIL, which is what keeps the new rule from being one that passes
everything.

`core/tstd/e2e_harness.py:265` takes `calls[0]` and requires `name == "fs_write"`; :283 compares
`gate[0]`'s `tool_call_id` to that same positional pick; :289 takes `results[0]`. All three
assume the model's first tool call is the one under test. A local model that reads before it
writes fails a run it should pass — measured at one failure in five consecutive live runs.

This matters more than an ordinary flake because TD-1803 declares this harness the M1.5 exit
criterion. An exit criterion that passes four times in five for reasons unrelated to the system
under test is worse than one that fails honestly.

The file crossed §6's limit on the same branch (302 → 422 lines); bring it back under while the
selection logic is being rewritten rather than filing that separately.

---

### TD-1808 — Scope the execution check to the call it is checking
**Size:** 2 · **Depends on:** TD-1807

**Acceptance criteria:**
- [x] The execution check judges the effect of the checked call, not the workspace's final state
- [x] A second `fs_write` that rewrites the file after the checked call succeeded does not fail
      the run; it is reported as an extra call, closing TD-1807's fourth criterion
- [x] A checked call that reports success while writing nothing still FAILS — the check must not
      be weakened into always passing
- [x] The failure detail distinguishes "file absent" from "file present with other content";
      today both render as `file='missing'`
- [x] Offline tests pin all three: extra write after, no write at all, wrong content

`core/tstd/e2e_checks.py` derives `wrote_file` from the file's final content on disk, so an
extra `fs_write` arriving after the checked call fails a run in which the checked call did
exactly what was asked. That is the same conflation TD-1807 removed from `calls[0]`,
`gate[0]` and `results[0]` — one call's outcome judged by the transcript's aggregate state —
left behind on the file-content half.

**Done (2026-08-17):** the verdict now rests on two facts that have to agree, neither of them
the workspace's final state — the checked call asked to write content `plan.content_ok`
accepts, and the diff the dispatcher rendered around that one handler (TD-604) proves that
exact content landed.  A `tool_result` carries `diff` only when the write actually changed
the file, so "reported success, wrote nothing" fails on the diff rather than being taken on
the handler's word — a strictly stronger check than reading disk, where a later call creating
the file used to mask it.

Content is compared as `added == asked.splitlines()`, not by rebuilding the file from the
diff: `render_diff` is built from `splitlines()` and no longer knows about line terminators,
so a rebuilt post-image can never compare equal to the mock plan's `"hello from M1\n"`.
Putting both sides through the same transformation is the only lossless match, and it avoided
adding a second content callable to `HarnessPlan` — no Class B decision was needed, so
`DECISIONS.md` is untouched.

Disk state survives only in the detail line, now `absent` / `other-content` / `written`
instead of one `missing` covering the first two.  `core/tests/test_e2e_ordering.py` grew the
four runs (extra write after, no change at all, wrong content, refused write); all four fail
against the previous `e2e_checks.py`, so they pin the new rule rather than restating it.
That file covers TD-1807 and TD-1808 together — same defect, two halves — and five of its
nine runs must FAIL (§7).

---

### TD-1809 — The doctor's probe test must not depend on the developer's model list
**Size:** 1 · **Depends on:** TD-1805

**Acceptance criteria:**
- [x] `TestDoctorRows` resolves the brain tier's slug without reaching for a live endpoint
- [x] The keyless-preset run passes on a machine serving no models, one model, or several
- [x] The negative row — a remote preset with no key — still fails, so the fix does not blunt
      what the class is for
- [x] No test in `core/tests/` reaches a live model server as a side effect of a doctor probe

`Daemon._provider_probe` calls `resolve_tier_slugs` before it builds a client, and the shipped
`local` preset leaves every slug unset, so the probe discovers against the real loopback
endpoint. `TestDoctorRows.test_local_preset_does_not_fail_the_key_row` patches `ProviderClient`
but not discovery, so the row's verdict is decided by how many models the developer happens to
have pulled: `discover_model` refuses to guess among several (TD-1805), the doctor reports
`provider: fail`, and the assertion of `ok` fails. Measured on an endpoint serving four.

The product behaviour is right — refusing to guess is what TD-1805 shipped, and the row carries
the `slug:` fix text. The test is what assumes a machine.

**Done (2026-08-17):** `TestDoctorRows` now patches `tstd.daemon.resolve_tier_slugs` with a
stand-in that fills unset slugs the way a single-model endpoint would, so the probe never
leaves the process.  Both rows patch it, including the remote one, where it is a no-op — the
preset's slugs are already set — so neither row can regain a network dependency by having its
preset retargeted later.  `_config_with_preset` still reads the shipped config, so no slug or
URL is duplicated into test source (§2.7).

Hermeticity is verified rather than assumed: with `discover_model` replaced by a stub that
raises on call, both rows still pass.  No product code changed — the doctor was reporting the
truth about a four-model endpoint.  Full suite green at 1255 passed, restoring the §10 gate
that this row had been quietly holding red.

---

### TD-1810 — Tell the model the workspace root
**Size:** 2 · **Depends on:** TD-1802

**Acceptance criteria:**
- [x] The assembled system prompt states the workspace's absolute root, once, in a stable
      position that does not disturb the cached prefix (TD-305)
- [x] A model given only the prompt can construct a valid absolute path for any file the
      manifest lists, without guessing
- [x] The manifest's relative listing is unchanged — this adds the root, it does not rewrite
      every entry
- [x] A test asserts the root appears in the assembled prompt for a workspace whose path was
      never mentioned in the user's message

**Done (2026-08-17):** Block `[1b]` states the root once, between the base prompt and steering,
inside the TD-305 cache prefix — the root is constant for a session, so keeping it out of the
prefix would re-bill unchanging bytes every turn, and putting it *ahead* of steering keeps it
out of the blast radius of a mid-session steering reload (TD-509), since a prefix cache dies
from the first changed byte onward.  All three tiers get it at the same offset, so the
`base + root` head they share stays shared.  Cost, paid once per session: the block is fixed
prose plus the root written twice, so it scales with the root — `71 + ceil(len(root)/2)`
heuristic tokens, i.e. **72** for a two-character root, **93** for this repository's own
44-character root, **121** for a 100-character pytest temp root.  (This originally read
"~80–125 heuristic tokens", stated as a constant; both ends were wrong for the block as first
written, which cost 87–136 over those same roots.  Measured through `tstd.context.tokens` —
tiktoken is not a dependency, so `make_token_counter` returns the `ceil(chars/4)` heuristic for
every slug.)  The block spells the join out with a worked example built from the real root
rather than implying it.
The manifest is untouched: `test_manifest_listing_is_unchanged` asserts its rendered text is
embedded verbatim and no entry starts with `/`, and
`test_absolute_path_derivable_for_every_manifest_entry` reads the root and the entries back out
of the assembled text, joins them the way the prompt says to, and checks the result against the
disk — the criterion with no guessing step available to the reader.  Proved on the wire, not
just in a unit test: the same fixture workspace and the same real tool schemas sent to
`gemma4:26b-a4b-it-q4_K_M`, A/B against the prompt as it was assembled before this story, went
**0/3 → 3/3** absolute at temperature 0 and **0/12 → 12/12** at temperature 0.8 across four
tasks (`fs_read`, `fs_list`, `fs_write`, and one naming no file at all).  (This sentence
originally continued: "The BEFORE prompt already contained the root *substring* — steering
provenance renders `<!-- from: /abs/ws/AGENTS.md (workspace) -->` — and still drew a relative
path every time, which is the finding: the bytes were never the problem, stating them as the
root was."  Both halves are wrong.  The BEFORE prompt contained `<root>/AGENTS.md`, not the
root: reconstructing it over this fixture, the root occurs once on every tier and always as
the head of that longer path, and zero times on every tier when the workspace carries no
steering file of its own — so recovering it needs a filename stripped, which is the inference
this story removes, and a workspace steered only from `~/.tstdesk` has nothing to strip.  And
the A/B swapped the whole block in and out — labelled statement, worked join, and the "never
pass a relative path to a tool" imperative together — so it shows the block works, not which
of the three sentences did the work.  Corrected in `DECISIONS.md`.)  The TD-1803 live leg still
passes end to end with the block present (fs_write → class A → approval gate → write → ledger,
4.8s, cost 0.0).  Class B: the prefix position, and the decision *not* to interpolate the root
into the four `fs_*` schema descriptions — four copies per request is the opposite of stating
it once, and it would make `create_registry()` workspace-dependent for no measurable gain.
Both in `DECISIONS.md`, along with why a workspace path is not a §2.2 secret and which sinks
were checked.

**Defect pass (2026-08-17):** six defects logged by the story's verifiers, all closed.  The
block no longer claims "workspace files are listed relative to this root" — only the brain
tier receives the manifest, so that sentence was false on the worker and the validator; it now
states the resolution rule instead, which is true whatever follows it, and costs 15 tokens
less.  A control character in the workspace path is refused with a `ValueError` naming the
codepoint rather than silently truncating the stated root at the label line.
`steering_reloaded` now carries `steering_tokens` beside `prefix_tokens`, because the prefix
figure also counts the base prompt and block `[1b]` and was being read as the cost of the
user's steering files.  `test_root_stated_once` asserts exactly one labelled statement
carrying the resolved root — it previously counted the bare label, which a statement with no
path after it would have satisfied.  The two wrong figures in this note are corrected above.
`docs/tst-desk-spec.md` §4.5 now shows `[1b]` in the block order.

Every `fs_*` tool advertises its `path` argument as "Absolute path to the file to …", but
`WorkspaceManifest.build()` renders entries workspace-relative (`README.md`, `src/app.py`) and
no prompt block states the root. The model is shown relative paths, required to emit absolute
ones, and never told the prefix — so correct behaviour depends on it guessing.

Measured 2026-08-17 across a 552-trial tool-fidelity suite: relative-path emission was the
single largest failure class, and the dominant one for `gemma4:e4b` (3/12 on the
absolute-path category). The suite had to inject the root into its own steering block to make
those trials winnable at all, so every score it produced is an OPTIMISTIC bound on production
behaviour.

Fix this before choosing a local model. It plausibly lifts every candidate and may reorder
them, since the failure is concentrated in exactly one category rather than spread.

---

### TD-1811 — Do not report prefix reuse that did not happen
**Size:** 3 · **Depends on:** TD-1802, TD-304

**Acceptance criteria:**
- [x] Cache telemetry (`last_cached_prompt_tokens`, the cache ratio in the turn log, and any
      cached-token figure the meter surfaces) reflects reuse the provider actually reported,
      never an assumption
- [x] A provider that reports no cached tokens produces a cache ratio of zero, not a blank or a
      silently-carried previous value
- [x] The zero-price local path still records real prompt-token counts — free is not untracked,
      the same rule TD-1802 established for output
- [x] A test drives a provider that reports zero cached tokens across two turns with an
      identical prefix and asserts the reported ratio stays zero

**Done (2026-08-17):** the one number that was invented is gone. `usage.prompt_tokens_details
.cached_tokens` is the only ground truth, and `Usage.cached_prompt_tokens` /
`CallRecord.cached_prompt_tokens` are now `int | None` — `None` when the response carried no
such field, an integer (including `0`) when it did.  The old parse folded absent into `0`, and
`0` reached `StackPanel` as **cache miss**: the story's defect pointed the other way, and the
one the local path actually hits.  Measured on the live endpoint at `127.0.0.1:11434` — both
`qwen3.8:27b` and `gemma4:26b-a4b-it-q4_K_M` return exactly `prompt_tokens`,
`completion_tokens`, `total_tokens`, streaming and blocking alike, so two identical-prefix
turns now surface `last_cached_prompt_tokens=None`, `turn_cache_ratio=0.0`, `prompt_tokens`
1955/1956 recorded in full at `$0.00`, and the badge reads *provider reports no cache figure*
where it used to read *cache miss*.  `mypy --strict` is the enforcement: `None` is
unrepresentable as a token count, so no caller can inherit a fabricated zero by accident.
`InstructionStack` gains an additive `cache_observed` (no `PROTOCOL_VERSION` bump, TD-1801's
precedent) so the viewer tells "no turn yet" from "the provider said nothing" instead of
guessing — TD-1810 §3's split, applied to the reuse figure it said TD-1811 would need.  Cost
keeps a single documented fallback, `cost.billable_cached_tokens`: an unreported figure bills
the whole prompt at the input rate, erring toward overstating spend.  Ratios stay turn-scoped,
so a cached turn followed by an uncached one reports `0.0` and not `0.8`.  19 tests in
`core/tests/test_cache_honesty.py`, including the named two-turn identical-prefix case in both
the reported-zero and the reported-nothing flavours; suite 1436 passed / 2 skipped, vitest
544, svelte-check 0/0. Decisions in DECISIONS.md TD-1811 §§1–4.

TD-305 assembles the prompt in stable-prefix order so a provider can cache it, and the cost and
latency story assumes that reuse happens. On a local hybrid model it does not. Measured
2026-08-17 against `qwen3.8:27b` on Ollama 0.32.13: llama.cpp builds context checkpoints, then
discards them —

    forcing full prompt re-processing due to lack of cache data
      (likely due to SWA or hybrid/recurrent memory)
    erased invalidated context checkpoint ... cached n_tokens = 0

166 such events in one day's logs, across both `qwen3.8:27b` and `gemma4:26b-a4b`. Every turn
re-prefills the whole system prompt and tool schemas (~1083 tokens observed). The same defect is
documented upstream in vLLM, which disables prefix caching outright for hybrid-attention models.

This is not a bug we can fix in the engine, and TD-305's ordering stays correct — it still pays
off against cloud providers. What must not happen is the meter claiming a saving the user never
received. Report the truth and let the number be zero.

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
### TD-1009 — The activity timeline is never scoped to a session
**Size:** 3 · **Depends on:** TD-1005

**Acceptance criteria:**
- [x] Switching the bound session shows that session's activity, not the previous one's
- [x] The Files pane (TD-1705), which folds the same store, scopes with it
- [x] Replay after re-attach does not double-count entries already shown
- [x] A test drives two sessions through one client and asserts the second's view contains
      none of the first's entries

`timeline-store.svelte.ts` exports `clear()`, but nothing imports it — `AppShell.svelte`,
`ActivityTimeline.svelte` and `FilesPanel.svelte` import only `push` and `entries`. Neither
`session-status.svelte.ts` nor `sessions.svelte.ts` clears on switch. Every daemon event the
client sees is appended to one process-wide list, so attaching to a second session shows the
first session's activity underneath it.

Entries carry no `session_id` of their own — only two event details do — so the store cannot
filter after the fact either. Either entries gain the session they belong to, or the store is
cleared and re-hydrated from replay on bind. The second is simpler and matches the daemon
being the source of truth, but it interacts with TD-1711/TD-1713's attach machinery, which is
why this is filed rather than fixed inside a UI story.

Found by the TD-1705 agent, which inherited the symptom, and confirmed independently: `clear`
has no callers anywhere in `ui/src`.

Milestone note: an M2 defect surfacing after M2 closed. Filed in E10 because the defective
store is TD-1005's; the fix likely coordinates with E17's attach work.

**Done (2026-08-17):** `Timeline` now holds a bound session: `bind()` drops the previous
session's entries, `push()` folds only events naming the bound one, and the chat store
declares the binding through a new optional `ChatDeps.onBind` from `switchSession` — the one
place the pane's session changes — just before the attach whose replay re-hydrates the list.
Because `entries` stays the single list the components read, the Files pane scopes with no
markup change. Double-counting is refused by the store itself: an event at or below the log
position already folded is dropped, so a re-attach at `lastSeq + 1` (TD-1716's resume) and the
full replay after a switch both land exactly once. `timeline-scope.test.ts` drives two sessions
through a real `ProtocolClient` and a fake daemon with per-session logs; it failed on all four
criteria before the fix. See DECISIONS.md — TD-1009.

---


### TD-1010 — The client's event gate silently drops events the daemon sends
**Size:** 1 · **Depends on:** TD-1003

**Acceptance criteria:**
- [x] `usage_report` and `usage_exported` reach the usage store; the panel loads
- [x] A test compares the client's gate against `DaemonEventUnion` in both directions and fails
      when they disagree
- [x] The gate still drops a genuinely unknown frame — the fix must not become "accept
      everything", which would satisfy the criteria above trivially

`ProtocolClient.dispatch` drops any frame whose type is absent from `KNOWN_EVENT_TYPES`
(`ui/src/lib/client.ts`) before `sink.onEvent`, with only a `console.warn`. A new daemon event
that reaches `DaemonEventUnion` but not that set is therefore invisible at runtime and green in
every unit test, because the stores are tested by calling their reducers directly.

This is the second occurrence. `policy_rules` carries the scar in a comment — "TD-803: was
missing; settings events arrived as unknown". TD-1706's usage panel shipped the same way: both
of its events were absent, so `usage.svelte.ts` never received a `usage_report` and the panel
would have sat on `loading` forever, with an export's in-flight flag never clearing. Found by
the TD-208 agent, which fixed the identical defect class on the Python side and looked across
the boundary; confirmed by comparing the union against the set — exactly two missing, none
stale.

**Done (2026-08-17):** both events added, and `client-event-gate.test.ts` now parses
`DaemonEventUnion` out of `protocol.ts` and `KNOWN_EVENT_TYPES` out of `client.ts` and compares
them both ways. TypeScript cannot enumerate a union at runtime, so reading the source is crude
and is the only thing that closes the gap from this side. Verified it fails by removing the two
entries again. A fourth test pins that the gate still refuses unknown frames.

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
- [x] Key stored via OS keychain (Keychain / Secret Service / Credential Manager)
- [x] **Key never written to disk in plaintext, never logged, never in the audit database** —
      asserted by test
- [x] Missing or revoked key produces a clear prompt to re-enter, not a cryptic failure
- [x] Key removable from settings

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
- [x] A locked or password-drifted login keychain (macOS "user name or passphrase
      not correct" / `SecKeychainItemCreateFromContent` failures) maps to actionable
      copy: what happened, and how to fix it (Keychain Access → unlock or update
      password), not raw `security` stderr
- [x] Store failure offers a retry path after the user unlocks the keychain

**Notes:** first observed 2026-08-14 on an AD-bound Mac after a domain password
change — `security add-generic-password` fails machine-wide until the keychain
is re-keyed; verified the daemon's exec-array invocation is not the cause.
Coordinate with TD-1102's wizard rework (integrate that lane first; this lands
on top).

---

### TD-1106 — Validate works on the entered key
**Size:** 1 · **Depends on:** TD-1102

**Acceptance criteria:**
- [x] The wizard's Validate action checks the key currently typed in the field
      with the provider, regardless of stored state
- [x] Store and Validate are independent; a failed or skipped store never
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

### TD-1203 — The decisions panel duplicates every row on re-attach
**Size:** 1 · **Depends on:** TD-1202

**Acceptance criteria:**
- [ ] Switching away from a session and back shows each decision once, not twice
- [ ] The panel scopes to the bound session — it already does; the fix must not lose that
- [ ] A test drives A → B → A through a real client and asserts no row repeats

`decisions.svelte.ts`'s reducer appends unconditionally: `decisions.rows.push({ id:
`${event.session_id}:${event.seq}`, ... })`. The id is documented as "stable identity" and
nothing consults it, and nothing clears the rows when the bound session changes.

The session filter one line above (`event.session_id !== session.sessionId`) stops *another*
session's decisions arriving, so the panel looks correct in the obvious test. It does not stop
the same session's own replay: attach re-delivers every `decision_logged` from `from_seq`, each
passes the filter because it genuinely is this session's, and each is appended a second time.
TD-1716's resume healing makes this more reachable — every resume re-attaches.

Found by the TD-1009 agent, which had just fixed the identical shape in the timeline store, and
confirmed by reading the reducer: the append has no guard and the stable id has no reader.

The timeline's answer is next door and probably transfers: bind-and-clear plus idempotence keyed
on the log position, in `timeline.ts` (TD-1009). Worth checking whether the two stores should
share it rather than growing a second copy.

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

### TD-1407 — `test_cancelled_error_path_kills_group` times a race with fixed sleeps
**Size:** 2 · **Depends on:** TD-605

**Acceptance criteria:**
- [x] The run waits on conditions — the child being spawned, the group being gone — rather
      than on wall-clock sleeps sized to beat the marker
- [x] It fails when a grandchild genuinely escapes the group kill; the fix must not become a
      test that cannot fail
- [x] The existing `kill refused` escape hatch still short-circuits the assertion, so the
      macOS EPERM veto stays a skip rather than a failure
- [x] 30 consecutive full-suite runs on a loaded machine with no failure

`tests/test_shell_tools.py::TestCancel::test_cancelled_error_path_kills_group` failed once
during TD-1703 and then passed three consecutive runs in isolation. It is a timing race, not a
product defect — the product's cancel path is what TD-605 hardened and it works.

The mechanism is in the test's own arithmetic. `_GROUP_ESCAPE_CMD` is
`{ sleep 2; touch kicked.txt; } & wait`, so a backgrounded subshell writes the marker at
t≈2.0s. The run sleeps 0.3s, cancels, sleeps 2.5s, then asserts the marker is absent — about
1.7s of margin for the group kill to land before the escapee writes.

That margin is wall-clock, and `asyncio.sleep` only guarantees a floor. Under full-suite load
the same test measured 3.0s, 13.7s and 14.2s against a nominal 2.8s budget — a ~5× overshoot.
Once the loop is delayed past t≈2.0s between the cancel and the kill, the subshell wins and
the assertion fails for a reason that has nothing to do with the code under test. Isolated
runs almost always beat the clock, which is why it looks intermittent.

Note the sibling `test_cancel_during_spawn_kills_group` already comments that "the flake
family above traced to this window" — the spawn race was fixed in the product, but this run
kept the wall-clock assumption.

**Hammer (2026-08-17):** 30 consecutive full-suite runs at `7db6320`, 0 failures — 1277 passed
every time. Run wall-clock ranged 82s to 213s, a 2.6× spread, so the machine was genuinely under
varying load: a run taking 2.6× the baseline is exactly the condition the old 0.3s/2.5s
arithmetic lost to, and the condition-waiting version did not notice.

Two process notes. Runs 24+ were once recorded against a worktree that had been switched to
another branch mid-hammer; those results were discarded, not kept. The script now pins the
expected HEAD and refuses to record a run if the worktree moves, so the invariant is checked
rather than remembered. "Loaded" is only partly satisfied: the load was the suite's own
concurrency plus whatever else the machine was doing, not synthetic contention.


---

### TD-1408 — The config suite asserts shipped defaults against the developer's own config
**Size:** 2 · **Depends on:** TD-302

**Acceptance criteria:**
- [x] `test_config.py` and `test_cost.py` read a fixture config, never `user_data_dir()`
- [x] The runs pass whatever the developer's active preset is and whatever slugs they have
      pinned
- [x] The shipped default is still asserted somewhere — a config that stops matching its own
      documented tags should still fail something, so this must not trade one gap for another
- [x] No test in `core/tests/` reads configuration from `user_data_dir()`

Five runs call `load_config()` with no path. That resolves to
`user_data_dir()/config.yaml` — the developer's real file — and they then assert on the values
the *shipped* config ships with:

- `test_config.py::TestLoading::test_default_config_is_valid`
- `test_config.py::TestTiers::{test_tier_method, test_tiers_method, test_worker_max_output}`
- `test_cost.py::TestTracker::test_auto_lookup_tier_config`

They pass only while the user config is still a byte copy of the shipped one. Pinning a slug
breaks them — which is not a misuse but the documented fix for an endpoint serving more than
one model, since `discover_model` refuses to guess (TD-1805). Measured expecting
`moonshotai/kimi-k3` and getting `gemma4:26b-a4b-it-q4_K_M` on a machine whose `local` preset
had been pinned by hand.

This matters more than five red rows: §10 requires a green suite for every story, so while
this stands, no story can satisfy its own definition of done on a machine that has been
configured. It reads as a regression in whatever is being worked on at the time.

The repo already has the pattern. `test_e2e_live.py` and `test_local_preset_paths.py` redirect
`HOME` before touching preset state, with a docstring saying that changing the developer's
preset from a test "would be a rude side effect that survives the run." These five want the
same treatment. The suite does not write the user's config today, and should not start.

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
- [x] Explains `AGENTS.md`, the hierarchy, `CLAUDE.md` compatibility, path-scoped rules, and
      imports
- [x] States the 200-line guidance and why adherence drops on long files
- [x] Worked examples for a small project and a large one
- [x] A migration note for users arriving from other tools — emphasizing nothing needs porting

---

**Done (2026-08-17).** `docs/steering.md` covers the four-level hierarchy, `CLAUDE.md` fallback
and shadowing, `appliesTo` scoping, `@path` imports (depth, cycles, fences, the external
approval gate) and the 200-line guidance. Thirteen worked examples are executable:
`core/tests/test_docs_steering_guide.py` materialises each as a real workspace plus home
directory, runs it through `ContextAssembler`, and compares the documented stack, prompt block,
import issues and pending approvals against real output — following TD-1503's pattern.

The line-limit warning and the depth-exceeded message are quoted from strings the test provokes
out of the assembler rather than restated, so rewording either in code fails the suite until the
guide catches up. A test also fails if any `Precedence` level, any `_SKIP_DIRS` entry, or any
`DEFAULT_VALIDATOR_SUBSET` pattern goes undocumented. Harness mutation-tested: falsifying an
activation flag, an import result, or the quoted warning each fails it.

---

### TD-1503 — Configuration reference
**Size:** 2 · **Depends on:** TD-302, TD-706

**Acceptance criteria:**
- [x] Every key in `config.yaml` and `.tst/config.yaml` documented with type, default, effect
- [x] Model swap instructions with a note that the landscape moves and slugs should be verified
- [x] Boundary and cap configuration explained with worked examples

---

**Done (2026-08-17).** `docs/configuration.md` documents every key of both config files with
type, default and runtime effect, plus the three-layer story: the packaged seed inside the wheel
(which an upgrade overwrites), the user copy the daemon actually loads, and the per-workspace
file. It also documents `policy` and `approved_external_imports`, two `.tst/config.yaml` sections
`policy.py` reads that the scaffolded template never mentioned.

Every YAML example is executable — `core/tests/test_docs_config_reference.py` runs each through
the real loader, and separate tests fail if a schema field goes undocumented, a stated default
drifts from the code constant, or a shipped slug is copied into the prose. 33 tests,
mutation-checked four ways.

Two product defects found while documenting and filed rather than fixed: TD-606 (an empty
`allowed_commands` refuses every command) and the unenforced `network` setting recorded inside
it.

---


### TD-1504 — Architecture guide
**Size:** 3 · **Depends on:** M2 complete

**Acceptance criteria:**
- [x] Daemon/shell split explained, including why the session owns the loop
- [x] Protocol documented
- [x] Extension points named for contributors
- [x] A "how to add a tool" walkthrough

**Done (2026-08-17).** `docs/architecture.md`, checked by
`core/tests/test_docs_architecture_guide.py` in the same spirit as TD-1502 and TD-1503: nothing
in the guide is taken on trust.

Both protocol tables are compared against `ClientMessageT` and `DaemonEventT` in both
directions, so an undocumented message and a documented-but-deleted one both fail the suite —
verified by adding a real message to the union and watching it go red. The per-row metadata is
derived too: whether a client message carries `session_id`, and whether an event's `seq` is
session- or connection-scoped, are read off the models rather than typed by hand. The session
state machine is compared against `Session.VALID_TRANSITIONS`, every named extension seam is
resolved through a real import at the path the guide gives for it, and the "how to add a tool"
walkthrough is executed — its blocks are run against a real registry and a real `ToolDispatcher`
and the output is compared with the result the guide promises. Six deliberate mutations of the
doc were each caught by exactly one test.

Ownership (prime directive §2.5) gets both treatments: the guide quotes `SessionRunner`'s own
docstring verbatim so rewording the guarantee in the code fails here, and
`test_a_session_outlives_its_viewer` proves it end to end — real daemon, real socket, attach,
drop the socket, assert the runner still runs and the event log still accepts events.

Two product defects found while documenting and filed rather than fixed: TD-208
(`parse_daemon_event` rejects `rule_activated`) and TD-607 (`dispatch_many` reorders a mixed
batch). Two documentation-vs-code divergences were corrected in the guide rather than filed,
because in both cases the code is right and the older prose is wrong: AGENTS.md §6 lists the OS
keychain among the Rust host's jobs, but there is no keychain code in `shell/` at all — it is
`core/tstd/keychain.py`; and `ready` is a declared, parseable `DaemonEventT` member that no code
in `core/tstd` ever constructs, so it is documented as declared-but-not-emitted and clients are
told not to wait for it.

The TD-208 note in §3 is pinned by a test that asserts the gap is still there, so the note has
to come out with the fix rather than outliving it.

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
- [x] In-app settings page with left-nav sections, reached from the title-bar
      gear (gear stops reopening the wizard)
- [x] Appearance section: light / system / dark, overriding
      `prefers-color-scheme`
- [x] Model section: preset and tier slugs readable, editing writes through to
      `config.yaml`
- [x] Policy section: persisted always-allow rules listed with revoke
- [x] Key section: re-enter / remove stored key (TD-1102's flows, surfaced here)

**Notes:** the wizard stays for first run; ⌘, retargets to this screen. Policy
list/revoke may need protocol messages — check what TD-803 landed before
assuming.

**Done (2026-08-17).** The gear opens a four-section pane; the wizard is first-run only, and
⌘, follows the gear rather than the wizard — a test asserts the wizard is no longer reachable
that way.

- **Config write.** `save_tier_slug` edits `user_data_dir()/config.yaml`, never the packaged copy
  an upgrade would overwrite. Surgical, because the shipped config is mostly teaching and a
  PyYAML round-trip would drop it; the value is JSON-encoded so a colon in a model tag is fine
  and a newline cannot inject YAML. Split into `config_write.py` when `config.py` passed §6.
- **Wire.** `SetTierSlug`, narrow rather than a general `update_config` — §2.2 forbids a secret
  reaching a config file, and a message carrying only a tier and a slug cannot smuggle one in.
  `setup_state` gained `tier_slugs`, additive like `key_required`.
- **Slugs as configured, not as resolved.** `resolve_tier_slugs` fills unset slugs *in place*, so
  after one probe a loopback tier holds a tag that was never in the file. The daemon snapshots
  slugs when it adopts a config; `model_copy` is shallow and shares tier objects, so keeping a
  copy is not a snapshot — pinned by a test, because the obvious implementation silently fails.
  The pane renders such a tier as *discovered from the endpoint*, never as an empty box: an empty
  box invites a save, and saving would pin a model TD-1805 left floating.
- **Theme.** The OS rule is scoped to `:root:not([data-theme="light"])` with the same palette
  reachable from `:root[data-theme="dark"]`, so an explicit choice wins in both directions.
  `tokens.test.ts` compares the two copies and fails naming whichever value drifts — verified by
  drifting one on purpose. "System" *removes* the attribute rather than resolving in JS, so the
  media query keeps re-answering when the OS flips.
- **Policy.** TD-803 had already landed `list_policy_rules`, `revoke_policy_rule` and
  `policy_rules`, so AC-4 needed no new wire types — the note's open question, answered. Rules
  are per-workspace, so `PolicyRuleList` renders three states: no session is a different claim
  from no rules. Revoke does not splice locally; the daemon's reply refreshes the list.
- **Key.** Drives TD-1102's `storeKey` / `validateKey` / `removeKey` rather than repeating them —
  one code path for a credential. A test asserts no credential appears anywhere in store state.

`PolicyRuleList` was split out when `SettingsPane` hit 405 lines; its styles are exclusive to it,
so the split cost no duplicated scoped CSS. 340 and 101 now.

Tests: 13 config-write, 9 wire, 18 store, 5 token-drift. Python 1277 green, UI 378 green,
`ruff`/`mypy --strict`/`svelte-check` all clean.

### TD-1704 — Queue and steer UI
**Size:** 2 · **Depends on:** TD-1004

**Acceptance criteria:**
- [x] Sending while a turn runs queues the message; queued rows render with
      send-now and remove
- [x] Editing a queued row replaces its text
- [x] Empty-queue state is invisible (no chrome when nothing is queued)

**Notes:** the daemon already queues user messages (E4); this is presentation.
Drag-to-reorder is a follow-up if the rows prove useful.

---

**Done (2026-08-17).** The story's premise was verified before any UI was written: the daemon
*does* queue a mid-turn `user_message` (`daemon.py` enqueues with no in-flight gate,
`session.py` holds it in an `asyncio.Queue`, `loop.py` dequeues one per turn), so this was
correctly scoped as presentation and `core/` was not touched.

What the daemon's queue cannot do is give a message back — no protocol message edits or
withdraws a `user_message` — which is why the rows are parked client-side and handed over one
per `turn_complete`. That is what makes send-now, edit and remove real rather than decorative.
Recorded as Class B: the steerable queue is the client's, drained at turn end, never flushed
into a terminal session.

The empty-queue criterion is asserted as a negative — the whole template sits inside one
`showQueue` guard, so a zero-length queue emits no node, border or reserved height.

---


### TD-1705 — Files pane
**Size:** 3 · **Depends on:** TD-1005, TD-1701

**Acceptance criteria:**
- [x] A Files tab beside Activity aggregates the session's write diffs from the
      event stream: file list, per-file diff view, running totals
- [x] Empty state explains what will appear here
- [x] Clicking a file can open it via the existing opener integration

**Notes:** spec §3's right-pane tab. Data is already in the timeline events;
this is aggregation and presentation, no new core events.

---

**Done (2026-08-17).** A Files tab sits between Activity and Stack, driven through the
`right-pane.svelte.ts` store TD-1707 extracted — no tab state returned to `AppShell`. It folds
the session's write diffs by path: list ordered most-recent-first, expandable per-file diff,
per-file line counts, running totals, and a click that opens the file through the existing
`open-file.ts`.

No protocol change was needed, and that was verified rather than assumed: `diff` is already on
`tool_result` and already carried onto the entry by `timeline.ts`. A file's identity comes from
the diff's `+++ b/` header — the canonical path the daemon wrote — rather than the tool call's
arguments, so a two-target write is handled and the UI needs no table of which tools write. The
accepted cost is that an undiffable write (binary, oversize, non-UTF-8) does not appear.

21 tests over a pure reducer, driven by diff strings generated verbatim by
`core/tstd/tools/diff.py`, so a change to the emitted header shape fails here instead of
silently emptying the pane. Surfaced TD-1009 as an inherited defect.

---


### TD-1706 — Usage and cost view
**Size:** 3 · **Depends on:** TD-903

**Acceptance criteria:**
- [x] A usage view shows session/day/week token and cost rollups from the audit
      store, broken out by tier
- [x] Export buttons reuse the existing JSONL/CSV export
- [x] The title-bar meter's hover panel links here

**Notes:** aggregation queries exist (TD-903); verify which export affordances
are already wired before adding UI.

**Done.** The premise check came back negative and shaped the story: TD-903's
queries and exporters exist but nothing reaches them from the client — the only
non-test caller is `e2e_checks.py`, and `cost_update` carries the meter's live
session totals, not audit history. So this needed a protocol addition:
`get_usage`/`export_usage` in, `usage_report`/`usage_exported` back, all four
registration points plus both TS unions (see DECISIONS.md).

`usage_rollup` is a fifth query beside the TD-903 four, reading the same `costs`
view but grouping on bucket *and* tier at once — the existing ones answer one
scope at a time. Weeks key on the Monday that opened them, which is the Sunday
boundary a bare `weekday 0` gets wrong. `export_usage` carries a format and no
path: the daemon writes to its own exports directory and reports where, so the
message cannot become a general write-a-file verb. Reads open their own sqlite
connection rather than borrowing the audit writer's, whose safety comes from its
drain task serializing access.

Cost math is tested per §7 with hand-computed dollar figures — cache reads priced
at the cache rate, tiers priced apart, classifier spend off main-loop cost. 24
core tests and 35 UI tests added; the bucket-limit test pins that a cap truncates
buckets, never a bucket mid-tier. The pane is a right-pane tab through
`right-pane.svelte.ts`, which is also what lets the title-bar popover link to it.

---

### TD-1707 — Command palette
**Size:** 2 · **Depends on:** TD-1701, TD-1703

**Acceptance criteria:**
- [x] ⌘K opens a palette over sessions and actions (new session, attach, open
      decisions/doctor/stack/settings, toggle theme)
- [x] Fuzzy match, full keyboard operation, Esc dismisses
- [x] Palette entries reuse the icon map

**Notes:** lands after the sidebar and settings so it has things to command.
⌘K joins the one pure mapping in `shortcuts.ts`; Escape gains a palette layer
between the menu and the panes. The registry and the matcher are pure
(`palette.ts`), the dispatch is a store (`palette-store.svelte.ts`) that calls
the same exported actions the buttons call. The Stack tab moved out of
AppShell into `right-pane.svelte.ts` so a command can reach it.

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
- [x] Text files attach to a message as context chips (picker, drag-drop, paste)
- [x] Attachments travel with `user_message` within configured caps and render
      as chips in the sent row
- [x] Oversize/binary attachment attempts fail with actionable copy

**Notes:** images deliberately split out — vision support depends on the user's
chosen models and needs capability detection first.

**Done (2026-08-17):** caps and the text/binary test live in the daemon
(`tstd/attachments.py`), not only the composer — a client is not trustworthy, so
`user_message` carries base64 bytes and the daemon decides for itself whether
they are text (strict UTF-8 + NUL scan). The composer refuses early for better
copy on top of that. Caps are a new `attachments` section in `.tst/config.yaml`,
ridden out on `boundary_update` so the composer judges against the workspace's
real numbers; documented in `docs/configuration.md` §4.3. One bad file refuses
the whole message. See DECISIONS.md 2026-08-17 TD-1709 for the four Class B
calls.

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
- [x] The rail organizes surfaces into sections: function entries (Home,
      Projects/courses-of-work, Scheduled) grouped above, session history
      sectioned below with a count badge when items queue
- [x] The account / settings row anchors the rail's bottom-left (not buried in
      the title bar): avatar-or-initial, account label, settings entry
- [x] Sections whose epics haven't landed yet (Scheduled → v0.5) either hide or
      render disabled-with-note — never a dead click

**Notes:** observed 2026-08-14 against the reference app's rail (Code/Home
tabs, New CTA, Projects, Artifacts, Scheduled, Dispatch, Customise;
account+settings pinned bottom-left). Each function surface already maps to an
epic — Artifacts v0.3, Scheduled+Dispatch v0.5, Customize TD-1703, Projects
TD-1103 — but the *layout grammar* (sectioned rail, bottom account anchor) was
captured nowhere. This story is the presentation rule; the surfaces arrive with
their epics.

**Done (2026-08-17).** The rail has a shape instead of one flat list: function surfaces grouped
above, session history sectioned below under a heading that badges its count, and the
account/settings row anchored bottom-left in both the expanded column and the collapsed strip.
The grammar lives in a pure module (`rail.ts`) — registry, grouping, badge, account derivation —
unit-tested without rendering. The settings gear left the shell header; the account row and ⌘,
are the doorways now, and the account row calls TD-1703's `openSettings()`.

Class B: entries carry `state: "current" | "ready" | "planned"` rather than a landed boolean.
Home began as a landed entry that dismissed the stacked overlays, and driving the real app
killed it — the settings pane covers the rail with a full-viewport overlay, so the one situation
that action existed for is the one where it cannot be clicked. Home therefore renders selected
and the dispatcher refuses it, a different claim from "arrives in v0.5" and rendered
differently. Account identity is the daemon's `active_preset` plus `has_api_key` — §2 says there
is no account, §2.2 says presence only, never the key.

An invariant test asserts every entry the registry calls `ready` actually activates, so marking
a surface ready without wiring it fails the suite rather than shipping a silent button.

---


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

### TD-1714 — Turn-state semantics: "running" is liveness, not a turn
**Size:** 1 · **Depends on:** TD-1713

**Acceptance criteria:**
- [x] Binding to a live session (auto-bind, rail select, or attach replay)
      never fabricates an in-flight turn: the composer offers send, no Working
      shimmer, no first-token watchdog armed
- [x] `turnState "running"` is raised only by turn evidence — an
      `assistant_delta`, an approval round-trip, or an open assistant tail
      corroborating a `session_state` — and `turn_complete` stands it down
- [x] A `session_list` refresh never stamps a summary's "running" over local
      turn state (neither raising a phantom turn nor standing down a real one)
- [x] Regression test reproduces the 2026-08-14 lockout: auto-bind to a live
      session plus the replayed open-time `session_state` → send fires
      immediately

**Completed (2026-08-14):** Root cause of "new session, typed hello, could not
send at all": the daemon's `session_state "running"` means the session's loop
runner is alive — set once at open, spanning the session's whole life, and
replayed at the head of every attach — but the chat store mapped it 1:1 into
`turnState`, where `showCancel("running")` morphs the composer's send button
into stop. Every bind to a live session (which is every healthy session)
locked the composer and spun the Working shimmer with the watchdog armed; the
only sends that ever fired went to terminal sessions, which the daemon then
refused. The fix makes turn state evidentiary: deltas prove a turn live,
`turn_complete` proves it over, an approval resolution keeps it live, and
`session_state "running"` may only corroborate existing evidence, never
create it — bind-time summaries and refreshes map it to no-turn. The Working
shimmer and the 25s watchdog now arm exclusively on a local send, which is
the only wait the user can actually be watching.

### TD-1715 — Archive, delete, and re-project sessions from the rail
**Size:** 3 · **Depends on:** TD-1701

**Acceptance criteria:**
- [x] Rail session rows offer Archive (hidden from the default list;
      restorable through an Archived filter or section) and Delete behind a
      confirm; deleting removes the session's event log and drops it from
      `session_list`
- [x] Archive state persists in the daemon's session metadata and survives
      restart; archived sessions never win auto-bind
- [x] Move to project: a rail affordance reassigns the session's
      `workspace_path` to another known workspace (daemon validates the
      target and updates durable metadata; the event log moves with the
      session)
- [x] Archiving, deleting, or moving the bound session moves the pane to the
      next live session or the empty state — never a stranded composer
- [x] A session with an in-flight turn refuses Delete and Move with copy
      (Archive is allowed and does not cancel the turn)

**Notes:** user ask 2026-08-14/15 ("option to delete or archive the older
sessions … or to regroup into another project"). Rename/star stay deferred
to the v0.3 cowork parity epic. Move-to-project reassigns the session's
working context — the agent's cwd and boundary root change on the next
turn — so the copy must say that plainly; if the reassignment proves deep,
split it into its own story.

### TD-1716 — Resume healing: survive webview suspension
**Size:** 2 · **Depends on:** TD-1713

**Acceptance criteria:**
- [x] On `visibilitychange` → visible (and window focus), the client
      unconditionally re-attaches every followed session at `lastSeq+1` — the
      daemon replay closes whatever gap the suspension caused; no user
      action required
- [x] Daemon emits an application-level `ping` event (no session, no seq)
      every ~15s; on resume, a client that believes it is connected but has
      seen no frame (ping or event) for >30s treats the socket as a zombie:
      force close → existing reconnect path → re-attach replays the miss
- [x] The first-token watchdog re-evaluates from wall-clock on resume: an
      `awaitingSince` older than the stall threshold flips to the honest
      copy immediately instead of waiting for a coalesced timer
- [x] Regression test (client-level, fake timers): suspend = drop all
      frames + freeze timers; resume → re-attach issued, replay applied,
      watchdog state honest

**Notes:** root-caused 2026-08-15 (the 20-minute "Whittling…"): the daemon
answered two turns into a healthy, never-closed socket while the WKWebView's
JS was suspended — timers dead (the 25s watchdog never fired), socket events
queued at the OS, UI frozen mid-frame. macOS App Nap/occluded-window
suspension is legitimate power management; do NOT fight it
(NSAppSleepDisabled et al. rejected) — heal on resume instead. Transport
ping/pong cannot detect this: the network stack answers those while JS is
suspended; only an application-level frame that JS must process proves the
client is live. The event-log replay makes healing lossless by design.

---

**Done (2026-08-17).** `ping` is a frame, not a `DaemonEvent`: it inherits `BaseModel`, so the
base's `seq: int = Field(gt=0)` contract is untouched, and it is handled out-of-band in the
client exactly as `hello_ack` is — no store sees it and no exhaustive handling changed. Making
`seq` optional on the base was considered and rejected.

The zombie check runs only on the resume edge, never on a heartbeat timer: a timer is precisely
what a suspension defeats, and a quiet backgrounded window is not sick.

`resume-healing.test.ts` (13 runs) drives the real `ProtocolClient` and chat store against a
fake daemon with a per-session log and a real attach replay; suspend drops every inbound frame
and advances the wall clock with `vi.setSystemTime` while pending timers are dragged along
unfired. 8 daemon tests cover ping shape, cadence, handshake gating, shutdown, and a re-attach
superseding its predecessor.


# Post-v0.1 backlog

Named, sequenced, and deliberately not decomposed. Do not build these.

| Version | Epic | Summary |
|---|---|---|
| **v0.2** | Memory | `.tst/memory/`, relevance-based loading, worker-tier distillation, diff-before-write, git commits per accepted memory. Local embeddings run as a **sidecar**, not through Ollama: measured 2026-08-17, an `/api/embed` call evicts the resident 17 GB chat model and pays a 20–48s reload, while `llama-server --embeddings` on its own port stays resident at 0.8 GB alongside it and serves the same OpenAI `/v1/embeddings` shape. Ollama's scheduler only evicts models Ollama loaded |
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
| M1 Headless core | E2–E9 | 51 | 153 |
| M1.5 Local models | E18 | 11 | 28 |
| M2 The window | E10–E12 | 19 | 57 |
| M3 Shippable | E13–E17 | 40 | 117 |
| **Total v0.1** | **18** | **128** | **370** |

Points are relative sizing for sequencing and splitting decisions, not a schedule. Do not
convert them to dates.

Recompute this table from the story headings and their `**Size:**` fields when you add or split
a story; do not increment the total by hand. Hand-incrementing is how it drifted to 110/338
against an actual 115/347.

**The single most important sequencing rule in this document: M1 exits when TD-1401 passes.**
A correct, tested, headless core is what makes the UI a presentation problem instead of a
debugging problem.
