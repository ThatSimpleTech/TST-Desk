# TST Desk — Work Plan & Backlog

**Scope of this document:** everything that must be built for **v0.1** and **v0.2**, plus
a full decomposition of **v0.3–Later** (M5–M10 and E47). Later phases may be planned;
they may not be started until the previous milestone exits (`AGENTS.md` §3).

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
| **M3 — Shippable** | E13, E14, E15, E16, E17, E19 | A stranger can install and use it from a fresh machine |
| **M4 — Memory** | E21–E28 | The brain prompt carries a relevant memory subset; a session end proposes a diff the user accepts; a project home shows Instructions / Memory / Context for the workspace |
| **M5 — Cowork (v0.3)** | E29–E32, E48 | Close the window; the session keeps running. CLI attach. Artifacts. Named sessions |
| **M6 — Computer use (v0.4)** | E20, E33–E34 | Screen pane watches a real desktop or browser; glow/cursor; Design mode; OS permission onboarding |
| **M7 — Remote (v0.5)** | E36–E38 | Tailscale bind (never `0.0.0.0`). Phone attach. Slack. Scheduler rail goes live |
| **M8 — Local remainder (v0.6)** | E39 | EZER/vLLM path. UI-TARS grounding. Floor already shipped as M1.5 |
| **M9 — Autonomy (v0.7)** | E40–E43 | Charter, unattended runner, drift checks, circuit breakers, hard-required container, wake-up |
| **M10 — Extensibility (v0.8)** | E44–E46 | MCP through the classifier. Slash + `SKILL.md`. One-level subagent. Plan lock |
| **Later** | E47 | Voice, tray, multi-window, updater, vision. Unversioned; do not pull forward |

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

E19 Reasoning visibility hangs off E18 (the local preset's brain is a reasoning model)
and E10 (the chat pane has to show what the loop now emits). It is a bugfix epic:
a shipped preset that thinks in silence fails M3's exit condition.

M4 Memory hangs off E5 (the assembler already has a brain-only memory slot) and E7
(checkpoints and the classifier). Embeddings hang off E13 only for optional sidecar
supervision — heading-match loading is the floor if the sidecar is not running.

M5 Cowork hangs off TD-205 / TD-1002: the session already outlives the socket; the
window still kills the daemon. Persist revive (events.jsonl) landed early — TD-2901
pins and bounds it. M6 hangs off TD-1710 (browser Screen) and E20 (`tst-cu-mcp`).
M7 hangs off M5 (something must be alive to attach to). M9 hangs off M5 and M7
(spec §9). M10 hangs off E6 (every MCP tool is still a classified tool).

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

### TD-608 — `fs_read` relative paths and continuation
**Size:** 2 · **Depends on:** TD-603, TD-1810

Found 2026-08-19 on a live turn: `fs_read path=docs/tst-desk-backlog.md`
was refused as outside the workspace (sidecar cwd is `/`), then the
same file as an absolute path succeeded. The backlog is 4071 lines; the
default cap is 2000; the schema said `limit: 0 = all`; nothing told the
model to continue with `offset`.

**Acceptance criteria:**
- [x] A workspace-relative path is joined to the workspace root, not the
      process cwd — proven with cwd *outside* the workspace
- [x] `../` still refuses as outside
- [x] Tool description states the 2000-line cap and the continue-with-offset
      protocol; `0` does not mean the whole file
- [x] A truncated or windowed read names the next `offset`
- [x] The workspace-root prompt block matches the join the guard performs

**Completed (2026-08-19):** `PathGuard.canonicalize` joins relative inputs
to `workspace_root`. Dispatch rewrites path arguments to that canonical
form before classification so the classifier and the handler see the
same target.

---

### TD-609 — `web_search` tool
**Size:** 2 · **Depends on:** TD-601, TD-1410

The agent had no way to look anything up outside the workspace. A second
HTTP client is a §2.3 event: the destination must come from config, the
module must be named in `_OUTBOUND_CAPABLE`, and there must be no remote
host literal in Python source.

**Acceptance criteria:**
- [x] `web_search` is a registered tool: query in, titles/URLs/snippets out
- [x] Destination is `search.base_url` from `config.yaml`; empty disables
      the tool with copy that names the key
- [x] No remote host appears as a string literal under `tstd/`
- [x] `test_outbound_hosts.py` names `tools/web_search.py` as the new
      outbound-capable module
- [x] Calls are Class B (ask) — no `host_fields` for the model to spoof
- [x] A user config from before this key existed still gets the shipped
      search block at load

**Completed (2026-08-19):** JSON and HTML responses are both parsed.
The shipped `search.base_url` lives only in `config.yaml`.

---

### TD-610 — `web_fetch` and a search-then-read loop
**Size:** 2 · **Depends on:** TD-609

Search snippets are not a research loop. The model needs to open the
best hits, and it needs to be told to fire several searches at once
then compile.

**Acceptance criteria:**
- [x] `web_fetch` takes one http(s) URL and returns readable text
- [x] Loopback, link-local, metadata, and non-http schemes are refused
- [x] A redirect onto a blocked address is refused before the body is read
- [x] Script/style are stripped from HTML
- [x] `web_search` description and the base system prompt tell the model
      to fan out, fetch, then compile — not to stop at snippets
- [x] Calls are Class B (ask); no `host_fields` for the model to spoof

**Completed (2026-08-19):** Fetch lives in the same module as search so
there is still one outbound-capable file. The wall is SSRF, not an
internet allowlist — the user sees each URL on the card.

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

### TD-804 — Skip all approvals
**Size:** 3 · **Depends on:** TD-802, TD-1703

**A settings toggle, not a CLI flag.** One switch in Settings → Policy
turns off approval cards for every Class B call on this machine. Asked
for 2026-08-18 after Approve on a live card returned `no_pending_approval`
and the user named YOLO / `--dangerously-skip-permissions` as the
expected feature.

This is not a wall bypass. The classifier still runs. Class C still
cannot auto. A `never` rule still refuses. The boundary still wins
before policy is consulted.

**Acceptance criteria:**
- [x] Settings → Policy has a Skip all approvals control, on or off
- [x] The choice persists in the user data dir (not `.tst/config.yaml`,
      so it is not committed with the workspace)
- [x] While it is on, a Class B call that would have asked runs as auto
- [x] A Class C call still parks or refuses — skip-all cannot make it auto
- [x] A `never` rule still refuses
- [x] Turning it on approves every currently parked non-C call
- [x] `setup_state` reports the flag so the toggle is honest after restart
- [x] Tests cover B→auto, C stays put, `never` stays never, and persist

**Notes:** `effect: yolo` stays invalid. Skip-all is a separate bit, not
a new policy effect, so existing rules keep their meaning.

**Completed (2026-08-18):** Settings → Policy has an On/Off switch. The
daemon persists it as `approvals.yaml` in the user data dir, reports it
on `setup_state`, and `resolve()` promotes Class B `ask` to `auto` when
the bit is on. Class C and `never` are unchanged. Turning it on approves
every parked non-C call.

### TD-805 — Skip-all includes shell (yolo)
**Size:** 2 · **Depends on:** TD-804, TD-4818

The 2026-08-18 ask was YOLO / `--dangerously-skip-permissions`. TD-4818
then exempted `shell` from skip-all after a review found unparsed write
forms auto-ran. That left the toggle lying: Always-allow still writes
the exact command, so a computer-use worker that shells `limactl` asks
on every new line.

**Acceptance criteria:**
- [x] `resolve_explained` promotes Class B `shell` ask→auto when skip-all is on
- [x] Class C and `never` still stop — this is not a wall bypass
- [x] Settings → Policy names the toggle honestly and says shell is included
- [x] Tests: skip-all + shell B auto-runs; skip-all + shell C still asks

Completed 2026-08-22 (branch `td/805-dangerously-skip-permissions`): the
TD-4818 tool-name exemption is removed. The same machine-wide bit is
the yolo switch; `effect: yolo` stays invalid. Copy and configuration.md
updated. Class B decision recorded in DECISIONS.md.

### TD-806 — Skip-all is skip everything
**Size:** 2 · **Depends on:** TD-805

The packaged app still asked on every new `limactl` line: skip-all
was on, TD-805 was only in source, and Always-allow is exact-match.
The user asked for skip everything with no cap pause.

**Acceptance criteria:**
- [x] `resolve_explained` promotes Class C `ask`→auto when skip-all is on
- [x] A `never` rule still refuses
- [x] Turning skip-all on releases parked Class C as well as B
- [x] Cap checks return no violation while skip-all is on
- [x] Settings copy says shell, Class C, and caps are included

Completed 2026-08-22 (branch `td/805-dangerously-skip-permissions`).
The fs-tool boundary still refuses steering-file writes; a Class C
shell redirect will run. Class C decision recorded in DECISIONS.md.

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

### TD-1814 — `cache_reported` must share the scope of the ratio beside it
**Size:** 2 · **Depends on:** TD-1811

**Acceptance criteria:**
- [x] `cache_ratio` and `cache_reported` in the turn log describe the same window, so the pair
      can never contradict itself
- [x] Neither field carries a previous turn's answer into a turn whose calls reported nothing
- [x] A test drives two turns where the first reports cached tokens and the second reports none,
      and asserts the second turn's log says the provider reported nothing
- [x] `billable_cached_tokens` is not called from any path that reports cache state to the user,
      matching the guardrail its own docstring states

TD-1811 shipped the right idea with a scope bug. `core/tstd/loop.py:1002` reads
`tracker.turn_cache_ratio()`, which sums `_turn_calls` and resets every turn; `:1007` reads
`tracker.last_cached_prompt_tokens`, which is the last call's value and is not turn-scoped. The
two are logged side by side as if they described one thing.

The consequence is the failure TD-1811's own second criterion forbids — "not a blank or a
silently-carried previous value" — reintroduced one field over. A turn whose calls reported no
cache figure can still log `cache_reported: true` inherited from an earlier turn, which is
exactly the wrong answer for the local path, where the honest report is that no engine figure
exists at all.

Also flagged by verification and grouped here because it is the same surface:
`cost.billable_cached_tokens` is called from `audit_writer.py:334`, while its docstring states it
is "a pricing fallback only. Nothing that reports cache state to the user routes through it."
Either the call site or the docstring is wrong; decide which.

**Completed (2026-08-18):** `turn_cache_reported()` reads `_turn_calls`. The audit writer
keeps the fallback: the column is billed reuse, silence stores as `0`. The docstring now
names that as ledger storage, not user-facing cache state.

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
      where detached-session behavior will diverge (TD-2902)
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

### TD-1011 — The split divider latches when its pointer capture is not honoured
**Size:** 2 · **Depends on:** TD-1001

**Bugfix.** Filed 2026-08-18 from a packaged-app pass: the findings were not
on this main's backlog.

**Acceptance criteria:**
- [x] Releasing the pointer anywhere on screen ends the drag: `dragging` is false afterwards
      and the divider returns to its resting colour
- [x] A drag that outruns the 4px divider keeps resizing instead of stopping the moment the
      cursor leaves it
- [x] The divider's pointer target is at least 8px wide without widening the painted rule
- [x] Keyboard operation and the ARIA separator semantics are unchanged
- [x] Regression test: pointerdown on the divider → pointermove outside it → pointerup outside
      it asserts the final width and that `dragging` cleared

**Done (2026-08-18):** move/up/cancel attach to `window` for the drag
(`attachDragListeners`); pointer capture is kept as an optimisation.
The painted rule is still `--space-1`; `::before` is the 8px hit target.
`splitpane-drag.test.ts` mounts the pane, stubs layout, and drives
pointerdown on the divider then move/up on `window`.

`SplitPane.svelte` binds `onpointermove` and `onpointerup` to the divider itself and relies
solely on `setPointerCapture` to keep receiving them. Where the capture is not honoured the
pointerup lands on another element, `onDividerUp` never runs, and `dragging` latches `true` —
the pane freezes at whatever width it reached and the divider stays painted
`--color-accent`, because `.splitpane.dragging .divider` shares the hover rule.

Observed 2026-08-18 in the packaged webview, pinned at the 80% clamp with the divider stuck
accent-coloured. Not reproducible in Chromium, where the capture is honoured and a scripted
drag resizes and persists correctly — which is why the existing unit tests pass. Attach move
and up to `window` for the duration of the drag and treat capture as an optimisation rather
than the mechanism. Widening the hit area is a separate concern but belongs in the same fix:
4px is below every platform's minimum pointer target, and the two together are what make the
divider read as broken rather than fiddly.

Consequence worth recording, because it presents as four unrelated bugs: at the 80% clamp the
right pane is roughly 164px, narrower than its own tab strip, so `Usage` clips off-window and
Activity, Files and Stack have no room to render. The pane is not broken, it is crushed by a
divider that cannot be dragged back.

Milestone note: an M2 defect surfacing after M2 closed. Filed in E10 because the divider is
TD-1001's.

---

### TD-1012 — The message list's virtualizer effect re-triggers itself and freezes the window
**Size:** 2 · **Depends on:** TD-1004

**Bugfix.** Filed 2026-08-18 from a packaged-app pass: the findings were not
on this main's backlog.

**Acceptance criteria:**
- [x] Mounting the chat pane with a replayed conversation raises no
      `effect_update_depth_exceeded`
- [x] Options are still re-set when the conversation grows and when the scroll element
      appears — the fix must not trade the loop for a list that stops tracking
- [x] A regression test mounts the list against a growing `messages` array and fails if the
      options-setting effect runs unboundedly

**Done (2026-08-18):** the options `$effect` reads `messages.length` and
`scrollEl` explicitly, then `untrack`s `$virtualizer.setOptions`. Ten
ticks of a 40-row replay no longer throw; appending rows still grows
the sizer.

`MessageList.svelte` re-sets the virtualizer's options from an `$effect`. `$virtualizer` is a
store, so reading it inside the effect subscribes the effect to it, and `setOptions` notifies
that store's subscribers — the effect writes the state it reads and re-triggers itself until
Svelte gives up. The option getters are fresh closures on every run, so the notification fires
every time rather than settling.

The consequence is out of all proportion to the cause. Svelte throws the error out of the
runtime, and once thrown it stops processing further updates — so the whole window goes inert,
not just the transcript. Every button, tab, dropdown and the split divider stop responding,
which reads as four unrelated defects and sent the live pass after the divider, the tab
strip, and the provider before landing here.

Reproduced 2026-08-18 on the packaged app (and earlier on a freshly rebooted machine at load
2.9). It predates the live pass; a starved machine had masked it as resource contention.

Milestone note: an M2 defect surfacing after M2 closed. Filed in E10 because the list is
TD-1004's.

---


### TD-1013 — One modal stack: Escape, click-outside, shared z-index
**Size:** 3 · **Depends on:** TD-1008, TD-1703

Found 2026-08-18 on the packaged app: Decisions is `z-index: 90` and covers the
chrome that opened it; Settings is 40; the palette is 50. Cmd+K over Decisions
opens the palette behind the dialog. Escape is swallowed (`modalOpen` returns
null) and the overlay has no click-outside, so the only exit is a 14px X.

**Acceptance criteria:**
- [x] Wizard, doctor, decisions, settings, and the palette share one overlay
      z-index (`--z-modal`)
- [x] Escape closes the top layer: menu, then palette, then other modal, then
      cancel turn
- [x] Clicking the dimmed overlay dismisses the dialog
- [x] Close buttons are at least 24px
- [x] Appearance hint describes the selected theme, not always System

---

### TD-1014 — Approval cards survive their call
**Size:** 2 · **Depends on:** TD-1007, TD-1203

Found 2026-08-18 on a live turn: Approve on a visible card returned
`no_pending_approval` for `call_tlqom3pz`. Same shape as the 2026-08-18
failure that prompted skip-all — skip-all does not clear a leftover card.

The footer is a process-global list. Timeline and the decisions pane
bind-and-clear on session switch; approvals just `push`. Detach also
wipes `lastSeq`, so the next attach replays every `approval_request`.
A resolved pair nets out one of two cards and leaves a ghost. The card
also stayed up until `tool_result`, so a second click on a live card
hit the daemon after `resolve_approval` had already finished.

**Acceptance criteria:**
- [x] Binding a session drops leftover cards; attach replay rebuilds
      anything still pending
- [x] Re-binding the session already shown is a no-op
- [x] A replayed `approval_request` for a tool call already on screen
      does not add a second card
- [x] Approve / Deny / Always allow dismiss the card when the send
      lands; a second click sends nothing
- [x] A refused send (socket down) leaves the card

**Completed (2026-08-18):** `bindApprovals` rides the chat store's
`onBind` next to the timeline and the decisions pane. The send is
refused if the id is no longer in `pending`.

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
- [x] Switching away from a session and back shows each decision once, not twice
- [x] The panel scopes to the bound session — it already does; the fix must not lose that
- [x] A test drives A → B → A through a real client and asserts no row repeats

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

**Completed (2026-08-18):** The two stores share the *contract*, not a type. Timeline folds
every event; this list only folds `decision_logged`. A shared helper would be a third
abstraction over two call sites. `bindDecisions` rides the chat store's `onBind` (same
moment as the timeline), and `reduce` drops a seq already folded. Re-binding the same
session is a no-op so a gap-only replay cannot empty the pane.

---

### TD-1204 — The stack panel stays empty on a live session
**Size:** 2 · **Depends on:** TD-1201

**Bugfix.** Filed 2026-08-18 from a packaged-app pass: the findings were not
on this main's backlog.

**Acceptance criteria:**
- [x] Opening Stack on a bound workspace with steering files shows those
      sources — not "No instruction stack yet."
- [x] A live `instruction_stack` push (turn boundary / hot reload) lands
      even while the Stack tab is not the visible one
- [x] Switching session while Stack is open re-asks for the new session
- [x] Leaving the tab and coming back still shows the current session's
      stack — unmounting the panel must not drop the subscription
- [x] A test pins the mount contract: the store is subscribed before
      `get_instruction_stack` is sent, so a reply cannot arrive with
      nobody listening

**Done (2026-08-18):** `startStack` lives on `AppShell` for the window's
life, matching `startUsage`. The panel is presentational. An `$effect`
in the shell refreshes when the Stack tab is visible and when the bound
session changes under it — palette and tab button both go through
`showRightPane`. The store test names the subscribe-then-apply order.

Packaged-app retest the same day found a second, larger drop: the
on-demand reply is stamped `seq=1` and is not in the session log, so
the client's gap/dup gate discarded it after attach replay had advanced
`lastSeq`. `instruction_stack` now reaches the sink even when its seq
is behind the cursor; a live TD-509 push that *is* the next log event
still advances the cursor. `client.test.ts` pins both.

`StackPanel` is only in the DOM on the Stack tab. `initStack` (the
`onDaemonEvent` subscribe) lives in that panel's `onMount`, and
`refreshStack` lives in a sibling `$effect`. Two things follow.

First, a `get_instruction_stack` reply — or a loop-pushed stack from
TD-509 — that arrives before the subscriber is registered is dropped,
and the panel sits on the empty copy forever. Second, leaving the tab
tears the subscriber down, so a turn that runs while the user is on
Activity never updates the store; opening Stack then races the same
refresh-before-subscribe path.

The usage pane already has the shape this wants: subscribe for the
shell's lifetime (`startUsage` from `AppShell.onMount`), refresh when
the tab is shown. The panel itself is presentational.

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

**Local (2026-08-24):** Ubuntu 26.04 XFCE built AppImage (101 MB), `.deb`
(27 MB), and `.rpm`. Extracted-deb `tstd` served `port.json` on this
host, with `PATH` empty, and inside `ubuntu:22.04` (no system Python).
The two open ACs still need the first green `package.yml` run on main
(four artifacts) and the other matrix legs.

---

### TD-1303 — Release workflow
**Size:** 3 · **Depends on:** TD-1302, TD-106

**Acceptance criteria:**
- [ ] Tagged release builds all platforms and publishes artifacts
      (release.yml fires on `tstdesk-v*` — namespaced in TD-4812 after the
      tst-cu-mcp package's `v0.2.0` tag matched the old bare `v*` trigger and
      fired a failed app-release run — reuses the package.yml matrix,
      publishes via `gh release create`; still unverified end to end)
- [x] Checksums published (SHA256SUMS.txt across all four artifacts)
- [x] Changelog generated from commits (`core/scripts/changelog.py`, grouped
      by TD-### convention, merges excluded)
- [x] Version consistent across host, daemon, and UI, asserted by test
      (`core/tests/test_version_consistency.py`; release job re-asserts the
      tag equals all four. Caught ui/package.json at 0.0.1 → 0.1.0.)

---

### TD-1304 — Packaged sidecar PID must attach, and a failed start must not leak daemons
**Size:** 5 · **Depends on:** TD-1301

Found 2026-08-18 on the 0.1.0 `.app`: the UI stays on **Connecting…** forever.
The host waits for `port.json.pid == spawned_child.pid`. PyInstaller's
`--onefile` bootloader is that child; the process that writes the port file is
its grandchild. After 30s the host kills the bootloader and respawns.
`MAX_RESTARTS` is 3, so one launch left four `tstd` listeners
(53347 / 53355 / 53369 / 53373) and zero `handshake ok`.

The debug path hides this: it runs the venv `tstd` as a direct child. The
sidecar smoke test only checked that `port.json` appeared, not that the pid
matched.

**Acceptance criteria:**
- [x] `wait_for_port_file` accepts a port-file pid that is the spawned child
      or a live descendant of it
- [x] The sidecar is spawned in its own process group (Unix) / killed as a
      tree (Windows `taskkill /T`)
- [x] Timeout, handshake failure, crash, and quit all reap the group — no
      leftover listener
- [x] A wrapper-process test (the onefile shape) attaches and group-kill
      reaps the grandchild
- [x] After the host gives up, the pill says the daemon could not start —
      never **Connecting…** forever
- [x] Packaged `.app` launches, the pill goes Connected, quit leaves no `tstd`
      (2026-08-18: rebuilt host + UI, bundled `.app`, handshake in ~60ms,
      one onefile pair, Cmd+Q left zero listeners)

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
- [x] Boundary guard semantics decided for Windows absolute paths:
      containment-checked drive-absolute paths are legal on win32 (the only
      absolute form Windows has), where they face the workspace wall like any
      other absolute path; every other Windows-unsafe form — drive-relative,
      UNC, alternate data stream — stays fail-closed on all platforms. The
      carve-out itself landed early in `dfe1ab7`, 45 minutes before the skip
      complaining about it was written; what was missing was its companion
      security test. `test_windows_unsafe_refused_on_every_platform` still
      asserted `windows_unsafe` for `C:\Windows\system.ini` unconditionally
      and would have gone red on the first Windows leg — the drive-absolute
      vector now has its own test asserting `outside_workspace` on win32 and
      `windows_unsafe` elsewhere. Refused either way, for different reasons
- [x] 8.3 short-name handling: expand on Windows, keep refusing everywhere
      else. The check ran against the raw string on every platform, so every
      `tmp_path` on a runner (`C:\Users\RUNNER~1\...`) was refused for a
      segment belonging to the machine. `realpath` reaches
      `GetFinalPathNameByHandle` and returns the long form, so on win32 the
      check now reads the canonical path — the alias is already gone by the
      time containment is checked. A segment that survives resolution named
      nothing expandable and stays refused; off win32 no filesystem knows the
      mapping, so the refusal is unchanged. `test_ledger.py`'s skip, whose
      reason cited the criterion above rather than this one, is removed
- [x] File permissions: documented no-op on Windows, and the `restricted_mode`
      tests assert it rather than skipping. `%LOCALAPPDATA%` already grants
      Full to the user, SYSTEM and Administrators only, so the containing
      directory is what protects both files and `0o600` never was; `icacls` on
      every port-file write would restate that for a subprocess. POSIX asserts
      `0o600`, win32 asserts the file exists, reads back, and carries `0o666`.
      Written up in `docs/windows.md`
- [ ] Shell-tool process-group kill semantics verified on Windows
      (CREATE_NEW_PROCESS_GROUP + taskkill/TerminateJobObject), skipped
      cancel/timeout tests unskipped — **implemented, unverified.**
      `CREATE_NEW_PROCESS_GROUP` at the spawn and `taskkill /T /F /PID` after
      the direct-child kill have landed, replacing a `proc.kill()` whose own
      docstring admitted grandchildren escape. The PowerShell escape probe
      and host-safe `taskkill` argv tests have also landed; cancel/timeout
      tests no longer `skipif(win32)`. Not ticked: no Windows host has run
      the live tree kill, and a process-tree kill is a claim about an OS
      that only that OS can settle. Ticking it from a green Linux or macOS
      suite, where `_kill_windows_tree` is never entered for real, would be
      this backlog's eighth "green suite, dead feature"
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
      — corrected: the hammer at `7db6320` proved 30 green runs on an
      idle-to-variously-busy host, not the concurrent-toolchain load that
      later reproduced the flake (TD-1409)

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

### TD-1409 — TD-1407's fix narrowed the cancel race but did not close it
**Size:** 2 · **Depends on:** TD-1407

**Acceptance criteria:**
- [x] The escape assertion no longer depends on wall-clock margin at all: the marker's
      writer is driven by a condition the test controls, not by `sleep 2` racing a kill
- [x] Reproduced under deliberate load before the fix, and the reproduction is what goes red
- [x] 30 consecutive runs of the cancel battery under load — the writer no longer
      races a clock, so the proof is that battery, not 30 full suites
- [x] TD-1407's fourth criterion is corrected to say what its hammer actually proved

Observed 2026-08-17 during the M3 batch: `test_cancelled_error_path_kills_group` failed on a
full-suite run in `wt-e1406`, then passed the next two full runs, five isolated runs, and two
file-scoped runs. The failing run was the only one executed while two other agents were
building concurrently — three Python/Node toolchains on one machine.

That is precisely the condition TD-1407 diagnosed and measured: "under full-suite load the
same test measured 3.0s, 13.7s and 14.2s against a nominal 2.8s budget — a ~5× overshoot."
The fix moved the run onto conditions for the *spawn* and the *group kill*, but the escapee
still announces itself on a wall clock — `{ sleep 2; touch kicked.txt; } & wait` — so the
1.7s of margin between the kill landing and the marker appearing is still wall-clock, and
still loses under a ~5× overshoot.

TD-1407's fourth criterion, "30 consecutive full-suite runs on a loaded machine with no
failure," is ticked. Either the hammer's host was not loaded the way this one was, or 30 runs
is not enough to catch a rate this low. Both readings point the same way: the criterion
proved less than it claims, and the honest fix is to remove the last wall clock rather than
to hammer harder.

Not urgent — it is a test defect, not a product defect, and TD-605's cancel path is sound.
It matters because §10 requires a green suite for every story, so an intermittent red row
reads as a regression in whatever is being built at the time, which is exactly how it
surfaced here.

**Completed (2026-08-18):** the escapee waits on `release.txt`, not
`sleep N`. A 3s hold after spawn — the old race window — fails the test
if the writer is still a clock. The group-gone assertion is unchanged.

---

### TD-1410 — No test enumerates the daemon's outbound hosts
**Size:** 3 · **Depends on:** TD-1402

**Acceptance criteria:**
- [x] A test enumerates every outbound HTTP/WS destination the daemon can reach in a full
      run, and asserts each one traces to configuration — not to a literal in the source
- [x] It fails when a new hardcoded remote host is introduced, proved by introducing one
- [x] Covers the paths that already send: the provider client, model discovery, and the
      key-validation call — plus any transport a dependency opens on import
- [x] The README's no-telemetry line is updated to cite the test rather than construction

§2.3 — "no telemetry, no analytics, no phone-home, no crash reporting to any remote; zero
network calls the user did not initiate" — is a prime directive, and §7 says a gap in
boundary enforcement is a defect rather than a missing nice-to-have. Every *other* prime
directive has a test: `validate_interface()` for the loopback bind, the redactor for secrets,
`tools/boundary.py` for steering writes. This one holds by construction only.

Construction is a real argument here, and it is not nothing: there is exactly one chat client
(`core/tstd/provider.py`), its endpoint comes from config, and `discovery.py` refuses a
non-loopback endpoint before it sends. But "we only wrote one" is an argument about today's
source, and the directive is a promise about every future version. A second client added in
good faith by someone who never read §2 would break it silently, and nothing in the suite
would notice.

Surfaced while writing TD-1501 (2026-08-17). It matters more now than it did last week: the
README states the promise in public, in a section whose whole claim is that these are
enforced in code rather than asserted in prose. Three of the four promises name their
enforcing test. This one names a habit.

**Completed (2026-08-18):** `test_outbound_hosts.py` scans every `tstd` module for URL
literals (must be loopback), confines which modules may open a transport, records the
provider / discovery / key-validation paths under a patched httpx transport, and imports
the package in a subprocess with sockets refused. The README now cites that file.

---

### TD-1411 — Settings appearance tests assume `localStorage` exists
**Size:** 1 · **Depends on:** TD-1703

Node 26's experimental `localStorage` is off unless `--localstorage-file`
is set, and jsdom does not always install one either. Two appearance
tests in `settings.test.ts` then fail: restart cannot persist "dark",
and the junk-value case throws on `localStorage.setItem`. The store
already guards; the tests did not.

**Acceptance criteria:**
- [x] Appearance tests stub `localStorage` the same way `workspaces.test.ts`
      and `sessions.test.ts` already do
- [x] A missing store still falls back to system and can stamp an explicit
      theme on the document
- [x] `settings.test.ts` is green on Node 26 without `--localstorage-file`

**Completed (2026-08-18):** the stub is per-test, cleared in `beforeEach`,
unstubbed in `afterEach`. Policy and key sections never touch it.

---

### TD-1412 — `decisions.commit_sha` is NOT NULL on existing audit databases
**Size:** 2 · **Depends on:** TD-901

Found 2026-08-18 on a live turn: every Class B decision toasted
`audit_write_failed` (`NOT NULL constraint failed: decisions.commit_sha`).
The session continued; the trail stopped.

TD-704 records Class B without a checkpoint, so `DecisionLogged.commit`
is `None` and `append_decision` writes NULL. `_SCHEMA_V1` in source was
later edited to `TEXT NULL`, but a database already at version 1 never
re-runs v1. The machine that hit this still had the original
`TEXT NOT NULL`.

**Acceptance criteria:**
- [x] A version-2 migration rebuilds `decisions` so `commit_sha` is nullable
- [x] A fixture that matches the original v1 (NOT NULL, one Class A row)
      opens, keeps the row, and accepts a Class B NULL
- [x] Fresh databases still land at `len(MIGRATIONS)` and store Class B
      as NULL
- [x] The writer path (`DecisionLogged` with `commit=None`) no longer
      emits `audit_write_failed`

**Completed (2026-08-18):** `_SCHEMA_V2` copies `decisions` into a
nullable table. v1 is left as it sits in source — editing it again would
not reach any database already stamped version 1.

---

## Epic E15 — Documentation

---

### TD-1501 — README
**Size:** 3 · **Depends on:** TD-1302

**Acceptance criteria:**
- [x] One-paragraph pitch, a screenshot, and install instructions per platform
      (pitch and per-platform install both written, against the artifact names
      `package.yml` actually produces.  Screenshot is `docs/images/window.png`,
      captured from the packaged `.app` on 2026-08-18: Connected, greeting
      empty state, rail and activity pane live against the bundled sidecar.
      Not a browser capture.)
- [x] A five-minute quickstart from download to first result
      (**caveat: the download step is written ahead of the first release.**  No
      `v*` tag has been pushed, so the linked releases page is empty until one
      is.  Steps 2–5 are the real first-run wizard, whose launch-to-first-message
      time TD-1101 already measured under two minutes.  Install detail is not
      duplicated — the quickstart points at TD-1302's unsigned-build section.)
- [x] The cost story stated plainly with the default stack and real numbers
      (every price derived from `core/tstd/config.yaml`, not hand-typed:
      `core/tests/test_docs_readme_numbers.py` checks the table against the
      shipped tiers in both directions, re-runs the worked example through
      `compute_call_details`, traces the per-session figure to the spec line it
      cites, and refuses a slug copied into the prose — TD-1503's trick, fourth
      application.  Mutation-checked four ways.)
- [x] Explicit statement: no account, no server, no subscription, no telemetry
      (written as architectural fact with the enforcing code named for each:
      `requires_api_key()`, `ws.validate_interface()`, the single config-sourced
      provider client, `discovery.py`'s pre-send refusal, the shared redactor,
      and `tools/boundary.py`'s steering refusal.  The telemetry line carries an
      honest caveat: it holds by construction, not by a runtime egress gate —
      there is no test enumerating outbound hosts.)
- [x] Comparison table against the closed alternatives, written fairly
      (Claude Desktop / Cowork / hosted workspaces.  Seven axes where they
      genuinely win are named first — setup, polish, support, managed
      infrastructure, out-of-the-box model quality, mobile and sync, predictable
      flat pricing — and the maturity row says v0.1, unreleased, unsigned.
      **Needs a human read before it ships**: fairness is the one thing here
      that cannot be self-verified.)

---

**Done (2026-08-18).** `README.md` is a user-facing front door — pitch, a real
window capture, install, quickstart, cost, promises, comparison, status — with
the developer sections kept below it. The screenshot is the packaged `.app`
on the greeting empty state (`docs/images/window.png`);
`test_docs_readme_numbers.py` refuses a README that drops the image or a
tree that drops the file.

The cost section is the part that would have rotted silently, so it does not: nine price rows,
one per shipped preset and tier, all bound to `config.yaml` by
`core/tests/test_docs_readme_numbers.py`. Slugs are deliberately absent, matching TD-1503's
decision that prose points at the shipped config rather than restating a landscape that moves
weekly — and the same guard enforces it here.

Every prose claim was then audited back against the code, which caught six overstatements worth
recording: the title bar pins the routing tier but does not swap the *model* behind it (that is a
settings change, and `daemon.py` applies it to new sessions only); the `local` preset's `base_url`
has no UI and needs a hand edit; only `model_calls` has an export path, not the whole audit store;
and the credential-hygiene test's audit-row scan is conditional on a database existing.

Two claims stay labelled rather than dropped. The per-session dollar figure is quoted
from spec §1 and labelled an estimate, because this build has published no measurement of its
own; the test asserts the README quotes the spec exactly rather than drifting its own version.
The no-telemetry promise is now pinned by `core/tests/test_outbound_hosts.py` (TD-1410).

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
- [x] OS notification when an approval is requested and the window is unfocused;
      clicking it focuses the window on the approval card
- [x] OS notification on turn completion when unfocused
- [x] No notification when the window is focused
- [x] Permission request happens lazily, on first qualifying event — never
      upfront

**Notes:** `tauri-plugin-notification`; permission comes from the plugin's
request API. The approval notification is the one that makes the app feel like
a coworker.

**Completed (2026-08-18):** `os-notify.ts` decides the copy; the store asks
permission on the first unfocused approval or turn-complete and is silent
while focused. The plugin is wired in the host and the default capability.

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
- [x] Editing a past user message forks the conversation from that point and
      resends
- [x] Branch navigation (‹ ›) on edited messages and retried assistant turns
- [x] Daemon-side fork covered by core tests; replay shows the active branch

**Notes:** needs daemon conversation forking and protocol additions; the
backlog's sizing reflects that. Retry-without-edit stays the TD-1606 behavior.

**Completed (2026-08-18):** `fork_from` / `set_branch` / `conversation_reset`.
The loop's conversation lives on the session so a fork can truncate it.
Retry of a finished turn is a fork of that user index, so ‹ › applies.
A turn in flight (`turn_in_flight` / open turns) refuses the fork.

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
- [x] `files_102.zip` unpacked into the tree first — it is the only copy of the
      `tst-cua` driver source
- [x] BrowserDriver runs against a real browser (Playwright persistent profile);
      six-verb actions surface as tools through the existing approval gate
- [x] A Screen tab in the right pane streams browser screenshots so the session
      is watchable
- [x] Failure modes (driver crash, stalled page, denied action) land as normal
      timeline entries

**Notes:** the wow story. Browser-only — whole-desktop AX stays v0.4 (TCC
friction, per-app quirks, boundary model for screen actions). Driver bring-up
on real hardware is where the estimate lives; timebox and record deviations.

**Completed (2026-08-21):** `files_102.zip` was not in the tree — Class B
deviation recorded in `DECISIONS.md`. Product driver is first-party
`tstd.browser` (Playwright persistent profile under the user data dir, plus
`MockBrowserDriver` for CI) with the same six verbs as tools through the
existing `Tool.actuates` gate. `screen_frame` carries a session-dir path;
the Screen tab streams those frames. Crash / stall / deny are `tool_result`
rows the timeline already renders.

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


### TD-1717 — Named API keys, bound per model
**Size:** 5 · **Depends on:** TD-1703, TD-1801

A single keychain slot is not enough: people keep an OpenRouter key and a
local-server key (and often a third). Names live in `config.yaml`; secrets
stay in the OS keychain. Each tier picks which named key it sends. A
loopback tier with no binding still sends nothing (TD-1801). Binding a
named key to a loopback tier is how a local server that requires `--api-key`
authenticates.

**Acceptance criteria:**
- [x] Settings → API keys lists every named key by the user-given name,
      never the secret; add / rename / test / remove work through the
      keychain
- [x] Settings → Model lets each tier pick a named key (shown by given
      name) or none
- [x] An off-box tier with no binding still uses the existing
      `openrouter` keychain account, so current installs keep working
- [x] A loopback tier with no binding sends no `Authorization` header
- [x] A loopback tier bound to a named key sends that key
- [x] Secrets never appear in `config.yaml`, `setup_state`, logs, or the
      audit database
- [x] The first-run wizard still stores one key as `openrouter` /
      "OpenRouter"

**Done (2026-08-22).** Catalog is `credentials:` in the user `config.yaml`
(id → name). Secrets stay in the OS keychain as `tst-{id}`. Each tier
may set `credential:`. Unbound loopback is still keyless; unbound remote
still uses `openrouter`. Settings → API keys lists given names; Model
picks a key per tier by that name, or none on loopback. Wizard path
unchanged. Class B in `DECISIONS.md`.

**Notes:** Catalog is `credentials:` in the user `config.yaml` (`id` →
`name`). Keychain account remains `tst-{id}`. Reserved ids:
`slack-webhook`, `ntfy-topic`. Additive protocol: `setup_state.credentials`
and `tier_credentials`; `set_api_key` gains optional `credential` + `name`;
new `set_credential`, `delete_credential`, `set_tier_credential`. No
`PROTOCOL_VERSION` bump.

User ask 2026-08-22.

---

### TD-1718 — Named key owns the host
**Size:** 3 · **Depends on:** TD-1717

Settings lets you pick a model slug and a named key. The key is the
provider; the provider has a host. Picking OpenRouter on a local preset
must call OpenRouter, not `127.0.0.1`.

**Acceptance criteria:**
- [x] `credentials.<id>.base_url` is optional in `config.yaml`; the shipped
      `openrouter` entry has OpenRouter's endpoint
- [x] A bound key with a host is the URL the provider client calls, even
      when the tier's own `base_url` is loopback
- [x] A keyed local server with no host still uses the tier URL
- [x] `openrouter-2` without its own host inherits the shipped OpenRouter
      URL
- [x] Settings → Model shows the selected key's host
- [x] Secrets never appear in `config.yaml`, `setup_state`, or logs
- [x] No provider URL is hardcoded in Python

**Done (2026-08-25).** User ask 2026-08-25.

---

### TD-1719 — Configurable retry for shared-pool 429s
**Size:** 3 · **Depends on:** TD-1718

*(Filed after the fact. The work landed with the ox-alpha / OpenRouter
session that produced TD-1718. These criteria describe what shipped.)*

A free or preview slug on a shared upstream pool answers `429` with
"temporarily rate-limited upstream, please retry shortly" and no
`Retry-After`. The old four-attempt budget died in eight seconds. A
`200` whose body is an error envelope, or an SSE stream that closes
with no events, was reported as an empty completion and did not retry.

**Acceptance criteria:**
- [x] `provider_retry` (`max_retries`, `initial_delay`, `max_delay`) is
      optional in `config.yaml` and documented
- [x] The configured budget reaches the provider client for keyed and
      keyless tiers
- [x] A `200` body that is an error envelope (no `choices`, embedded
      status) is a retryable failure, not a parse error
- [x] An accepted SSE stream that yields no events is retryable
      `empty_stream`, not an empty completion
- [x] The CLI turn deadline covers the daemon's worst-case retry budget
- [x] A failed turn writes an `assistant_delta` so the pane is not blank

**Done (2026-08-25).** User ask 2026-08-25.

---

### TD-1720 — Title bar shows the live slug and host
**Size:** 2 · **Depends on:** TD-1006, TD-1718

TD-1006 asked for active slugs on the chips. The bar shows
`brain` / `worker` / `validator` and hides the slug in a tooltip.
After binding an OpenRouter key on a local preset, that is not a
sanity check — the role name stays `brain` while the turn may be
on `:8002` or `openrouter.ai`.

**Acceptance criteria:**
- [x] `tier_state` carries `preset` and `hosts` (tier → hostname:port)
      from the same resolution the provider client uses
- [x] The title bar shows the active tier's slug and host without hover
- [x] The UI does not infer a host from the slug
- [x] A missing slug (discovery pending) still shows the host
- [x] Additive protocol: no `PROTOCOL_VERSION` bump; an older client
      ignores the new fields

**Notes:** Keep the three role chips as the router pin. The pill is
read-only. Tooltip may add preset and full URL. Spec §2.7: hosts come
from config / `resolve_base_url`, never a Python literal.

**Done (2026-08-25).** `tier_state` carries `preset` + `hosts`. The title
bar paints `{slug} · {host}` from those fields. User ask 2026-08-25.

---

### TD-1721 — Per-session preset
**Size:** 5 · **Depends on:** TD-1720, TD-1101

A session already captures the `ModelConfig` it opened with;
`set_preset` only changes new sessions. Settings still shows one
global preset, so two chats on `local` and `vllm` look identical
and you cannot retarget an existing chat.

**Acceptance criteria:**
- [x] A session persists the preset it opened with and revive restores it
- [x] The title bar (or composer) can change *this* session's preset;
      the change applies on the next idle turn
- [x] Changing Settings' `active_preset` does not rewrite open sessions
- [x] The session list shows each row's preset
- [x] A per-session slug edit does not write back onto the global preset
- [x] Refuse a preset switch while a turn is running

**Notes:** Not a second Settings document. Settings remains the catalog.
Class B: persist the preset name on the session, not a forked config
tree. Do not start until TD-1720 is on main.

**Done (2026-08-26).** `sessions.json` stores the catalog name.
`set_session_preset` retargets the open chat; Settings' `set_preset`
stays the default for new sessions.

---

### TD-1812 — Split the workspace picker and cost meter out of `TitleBar`
**Size:** 2 · **Depends on:** TD-1006

*(Filed after the fact. The work landed in `1f6e407` under an id no story existed for; these
criteria are reconstructed from what shipped, so they describe it rather than having driven it.
The id sits in E18's block because a background task chose it, not the epic's range.)*

**Acceptance criteria:**
- [x] `TitleBar.svelte` is back under §6's ~400 lines, and so is everything split out of it
- [x] The split follows the seams already in the file — each extracted concern owns its markup
      and its style block
- [x] Extracted components stay presentational, reading the same stores they read inline; no
      store state moves into a component
- [x] Nothing outside the file changes — `TitleBar` keeps the `pickDirectory` prop and forwards
      it

**Done (2026-08-17).** `TitleBar` 507 → 175, with `WorkspacePicker.svelte` (243) taking the name
button, recents menu, `pick()` and the Escape backdrop, and `CostMeter.svelte` (120) taking the
meter, its hover breakdown and the link to the usage pane. `TitleBar` keeps the row itself —
tier chips, state indicator, wall summary.

The drift was not one story's doing: the file was already 482 before TD-1706 added the popover
link. No component tests, per §7 — this repo tests stores and pure functions, not `.svelte`.

---

### TD-1813 — Extract the first-token wait from `chat-store`
**Size:** 2 · **Depends on:** TD-1714

*(Filed after the fact. The work landed in `9d8ac11` under an id no story existed for. The id
sits in E18's block because a background task chose it, not the epic's range.)*

**Acceptance criteria:**
- [x] The first-token wait moves to its own module: the timer, `STALL_TIMEOUT_MS`, and
      begin/end/resume, with no protocol knowledge — no session id, no events, no wire
- [x] The store keeps deciding *when* a wait starts and stops from turn evidence; the module
      decides what a wait already in progress is worth
- [x] `chat-store.ts` is under §6's ~400 lines — **met: it is 379.** The second cut was session
      binding, and it needed the pure/effectful seam rather than `switchSession` alone.

**Done (2026-08-17).** `first-token-wait.ts` (98 lines) owns the wait; `arm()` and
`clearStallTimer()` are no longer visible to the store at all. Class B, recorded at the time: the
wait takes its state as a structural slice (`FirstTokenWaitState`), the way `chat-queue` already
takes `{ queued }`, so `ChatState` goes on declaring all its own fields.

The second cut is `session-binding.ts` (103 lines): `chooseBoundSession` holds the whole
auto-bind policy as a pure function returning keep/unbind/bind, and `applyBind` holds the
teardown-and-attach that used to be `switchSession`'s body. The store keeps the wiring — the
`session_list` case reads the choice, `switchSession` forwards to the effect. 431 → 379.
`session-binding.test.ts` drives the policy table directly (12 cases); the store's own tests
pass unchanged, which is what says the refactor moved code and not behavior.

---

### TD-1815 — Extract the transcript reducer and fork map from `chat-store`
**Size:** 2 · **Depends on:** TD-1708, TD-1813

**Acceptance criteria:**
- [x] The event switch moves to its own module: one function, a collaborator
      context, no socket
- [x] Sibling snapshots move to their own module; `conversation_reset` is
      applied there, covered by a focused test
- [x] `chat-store.ts` is under §6's ~400 lines
- [x] Existing chat-store tests pass unchanged

**Done (2026-08-18).** TD-1708 pushed the store to 590. `chat-events.ts`
owns the reducer; `chat-fork.ts` owns the sibling map. The store keeps
send, queue, wait, and bind.

---


## Epic E19 — Reasoning visibility

**Goal:** show the agent's thinking while it happens, folded away when it doesn't matter. A
reasoning model whose reasoning the UI discards is indistinguishable from a hung one, and
since M1.5 the default brain tier is exactly that model.

**This is a bugfix, not a flourish.** Nothing that worked stopped working, but a shipped
preset that thinks for a minute in complete silence fails M3's exit condition. Support for
reasoning models was never built; M1.5 shipped a preset that needs it. Filed as a new epic
rather than folded into E17 so it stays separate from the familiarity stories it sits beside.

Measured 2026-08-17 against `qwen3.8:27b` on Ollama. The endpoint streams reasoning as
`delta.reasoning` with `delta.content` set to `""`:

```
"delta":{"content":"","reasoning":"The"}
"delta":{"content":"","reasoning":" user"}
```

`provider.py` parses only `content`, and `loop.py` gates emission on its truthiness — an empty
string is falsy, so an entire reasoning phase produces no `assistant_delta` at all. A
59-character prompt bought 63 seconds of complete silence before the first content token, with
the working shimmer running throughout. Reproduced 2026-08-18 on this main: send "Find what's
failing" → Whittling, no thinking UI, no tokens, until the answer (if it) arrives.

**Milestone: M3.** Pulled in on 2026-08-18 (see `DECISIONS.md`). M3 exits when "a stranger can
install and use it from a fresh machine", and `local` is a shipped preset — a stranger who
picks it gets an application that looks hung for a minute at a time.

---

### TD-1901 — Reasoning passthrough
**Size:** 3 · **Depends on:** TD-1801

**Bugfix.**

**Acceptance criteria:**
- [x] `Delta` carries a `reasoning` field parsed from the chunk, alongside `content`
- [x] A chunk carrying reasoning with an empty `content` produces an event — the
      `if chunk.delta.content:` gate no longer swallows it
- [x] Reasoning is emitted as its own event kind rather than merged into `assistant_delta`, so
      the transcript can still tell thinking from answer after the fact
- [x] Reasoning never enters `collected_content`, so it is not replayed to the provider as
      assistant content on the next round trip
- [x] Reasoning is redacted on the audit and event surface on the same terms as content
      (TD-1405)
- [x] The first-token watchdog (TD-1713) counts a reasoning delta as a first token, so a
      thinking model no longer trips "No response yet — the model may be slow or unreachable"
- [x] Scripted-provider tests cover reasoning-only chunks, reasoning interleaved with content,
      and a provider that emits neither field

**Done (2026-08-18):** both spellings parse to `Delta.reasoning`; empty
strings normalise to `None`. The loop emits `AssistantReasoning` and
does not touch `collected_content` — a two-turn test asserts the
scratchpad never appears in any message handed back to the provider.
`Script.reasoning` emits `content=""` beside each thinking chunk so the
mock reproduces the defect. Redaction is the same path `assistant_delta`
already takes (`session._redact_event` does not scrub assistant prose).
`assistant_reasoning` is in `KNOWN_EVENT_TYPES` and `DaemonEventUnion`.
The chat store ends the first-token wait on reasoning.

**Notes:** the field name is from a live capture, not from documentation. Ollama emits
`reasoning`; some OpenAI-compatible providers emit `reasoning_content`. Accept both and do not
invent a third. This is not local-only plumbing — remote reasoning models reach the same
parser through OpenRouter.

Do not conflate this with TD-1716. That was a twenty-minute "Whittling…" caused by WKWebView
suspension freezing a healthy client; this is a healthy client being sent nothing. Both
present identically to the user, which is the argument for the distinct event kind.

---

### TD-1902 — Collapsible thinking and tool blocks
**Size:** 3 · **Depends on:** TD-1901

**Bugfix** for the thinking half — without it TD-1901 stops the shimmer and leaves an empty
bubble with a blinking caret for the minute the model spends thinking.

**Acceptance criteria:**
- [x] Reasoning renders as a collapsed disclosure labelled with its duration ("Thought for
      63s"), expandable in place
- [x] While it is the live thing the block is expanded and streaming; it collapses on its own
      once content begins
- [x] Tool calls and their results fold into the same disclosure treatment instead of growing
      the transcript without bound
- [x] Collapsed state is per-block and survives scrolling away and back
- [x] Expanded reasoning is selectable and copyable
- [x] `prefers-reduced-motion` suppresses the expand and collapse animation

**Completed (2026-08-18):** `ReasoningBlock` renders above the answer —
open and shimmering while thinking is live, collapsing to "Thought for
1m 3s" when content starts. Tool calls land on the same assistant row
and fold through the same `Disclosure` chrome: open while the result is
outstanding, closed once it arrives. An explicit toggle wins permanently
and is keyed per block, so a virtualized row keeps its fold. Bind/dispose
clears the map (`onUnbind`) because ids restart at m1.

**Notes:** the disclosure is the resting state, not a setting to find. At the brain tier's
measured throughput a 27B thinker will out-produce its own answer several times over, so
rendering reasoning inline and unfolded would bury the reply — which is the failure mode this
story exists to avoid, not a smaller version of the one TD-1901 fixes.

---


# MILESTONE M4 — Memory and project home (v0.2)

Spec §5 and §9 named Memory. The project home (E28) was added 2026-08-19 from
the Claude Projects reference: a workspace you open, with Instructions /
Memory / Context on the right and recents in the middle. Computer-use
(TD-1710, E20), packaging clean-VM boxes (TD-1301–1303), and Windows
process-group verify (TD-1406) stay where they are — they do not block M4.

**v0.1 leftover that is not M4:** the window still kills the daemon on close. Distill
therefore runs on **graceful quit** and on an explicit End session, not on crash, and
does not wait for v0.3 detached sessions.

**Already decided (do not reopen):** local embeddings are a **sidecar**
(`llama-server --embeddings` or any OpenAI `/v1/embeddings` endpoint), never an
Ollama `/api/embed` on the same scheduler as the chat model. Measured 2026-08-17: an
Ollama embed evicts a resident 17 GB chat model and pays a 20–48s reload; a separate
embeddings process stays at ~0.8 GB alongside it.

**Already wired:** the brain prompt has a memory slot
(`MEMORY_PLACEHOLDER`). Worker and validator never get that slot (spec §4.6). The
cache prefix is base + workspace root + steering — memory is *after* the prefix so a
new memory set does not bust steering cache (pin this in TD-2503).

**M4 exit:** TD-2701 — a headless harness loads a matching memory file into the brain
prompt only, proposes a distill diff, accepts it, and sees a `tst: memory update`
commit.

```
E21 Store ─> E22 Relevance ─┬─> E25 Prompt ─> E26 UI ─> E28 Project home
       │                    │
       └─> E23 Distill ─> E24 Diff-before-write ─┘         │
                                    │                      │
                                    └─> E27 Harness (memory exit)
                                    └──────────────────────> TD-2808 (home)
```

Heading-match loading (TD-2201) is the floor. Embeddings (TD-2202–2204) are in
milestone but a missing sidecar must not kill a session.

---

## Epic E21 — Memory store

**Goal:** markdown under `.tst/memory/`, writable by the agent, capped, committed.
Spec §5 layout and hard rules.

---

### TD-2101 — Memory layout and scaffold
**Size:** 2 · **Depends on:** TD-507

**Acceptance criteria:**
- [x] Opening a workspace creates `.tst/memory/` with commented templates for
      `MEMORY.md`, `decisions.md`, and `gotchas.md` when the directory is missing
- [x] Topical files are allowed (`<topic>.md`); the store does not invent names
      until distill or the user does
- [x] Layout is documented in `docs/configuration.md` and `docs/steering.md`
- [x] Scaffold is idempotent — a second open does not clobber existing files

**Notes:** same shape as the `.tst/config.yaml` scaffold. These files are the
product, not runtime state — they are git-tracked on purpose.

---

### TD-2102 — Memory write permission
**Size:** 3 · **Depends on:** TD-602, TD-2101

**Acceptance criteria:**
- [x] `fs_write` / `fs_edit` of a path under `.tst/memory/` is allowed
- [x] `AGENTS.md`, `CLAUDE.md`, and `.tst/rules/**` remain refused — a test
      writes a memory file then fails a steering write in the same session
- [x] Memory writes classify as Class A (in-workspace, reversible, git)
- [x] The classifier rule is data in the table, not a special case in the handler

**Notes:** today steering refusal is path-based and would catch `.tst/memory`
if someone naively used `.tst/**`. Pin the carve-out at the classifier and
the guard together.

---

### TD-2103 — Line cap
**Size:** 2 · **Depends on:** TD-2101

**Acceptance criteria:**
- [x] A memory file is capped at ~200 lines (exact number in config, tested)
- [x] A write that would exceed the cap is refused with copy that says to
      distill, not to append
- [x] Distill (E23) is the only path that may replace a file that is at cap

**Notes:** spec §5 — distilled, not appended forever. The number lives in
config, never in a handler literal.

---

### TD-2104 — Memory git commit
**Size:** 3 · **Depends on:** TD-705, TD-2102

**Acceptance criteria:**
- [x] An accepted memory write commits on the workspace repo as
      `tst: memory update` — working tree and HEAD, not the
      `tst/session/<id>` checkpoint branch
- [x] A non-git workspace degrades: the write still lands, a one-time notice
      says there is no commit
- [x] `git revert` of that commit restores the previous memory bytes
- [x] Checkpoint commits (TD-705) are unchanged

**Notes:** checkpoint plumbing never touches HEAD. Memory commits are the
opposite on purpose — the user asked to be able to revert from their own
history. Do not reuse `Checkpointer` for this.

---

### TD-2105 — Memory is not steering
**Size:** 2 · **Depends on:** TD-2101

**Acceptance criteria:**
- [x] `.tst/memory/**` never appears in the instruction stack as a steering
      source
- [x] Frontmatter in a memory file is not an `appliesTo` rule
- [x] A golden-file test: a workspace with both `AGENTS.md` and `MEMORY.md`
      assembles steering from the former only

**Notes:** two markdown trees in `.tst/`. Mixing them is the quiet failure.

---

## Epic E22 — Relevance

**Goal:** load a subset. Not everything, every time. Spec §5 mechanic 1.

---

### TD-2201 — Heading-match loader
**Size:** 3 · **Depends on:** TD-2101

**Acceptance criteria:**
- [x] Session start (or first brain turn) loads `MEMORY.md` plus any topic
      file whose heading tokens overlap the user task
- [x] No embeddings required — this path is the floor
- [x] Empty memory directory loads nothing; the placeholder stays
- [x] Deterministic for identical files + task (property test)

**Notes:** ship this before the sidecar. A session with no embedder still
remembers.

---

### TD-2202 — Embeddings client
**Size:** 3 · **Depends on:** TD-2201

**Acceptance criteria:**
- [x] An embeddings caller speaks OpenAI `POST /v1/embeddings`
- [x] Endpoint and model slug come from `config.yaml` (`search`-shaped:
      no host in Python)
- [x] A missing or loopback-down endpoint is a fallback to TD-2201, not a
      failed turn
- [x] `test_outbound_hosts.py` names the new module; destination traces to
      config

**Notes:** do **not** call Ollama `/api/embed`. The 2026-08-17 measurement
is the reason. Default the config at a loopback embeddings port; empty
disables.

---

### TD-2203 — Rank and budget
**Size:** 5 · **Depends on:** TD-2202

**Acceptance criteria:**
- [x] Topic files are ranked by embedding similarity to the task
- [x] Loader takes top-k that fit a stated token budget (config)
- [x] `MEMORY.md` is always included if it exists, then topics fill the rest
- [x] Heading-match is the tie-break and the fallback
- [x] Identical inputs → identical selected set

**Notes:** size 5 because the budget interaction with TD-506 counters is
easy to get slightly wrong. Propose the ranker shape before building.

---

### TD-2204 — Embeddings sidecar supervision
**Size:** 5 · **Depends on:** TD-2202, TD-1002

**Acceptance criteria:**
- [x] The host can spawn a configured embeddings binary the way it spawns
      `tstd`, or attach to an already-running loopback endpoint
- [x] A dead sidecar does not take down the daemon; TD-2201 runs instead
- [x] Quit reaps the embeddings child
- [x] Optional — a workspace with no embeddings config never tries to spawn

**Notes:** size 5 is the host work. If this slips, M4 still exits on
heading-match + distill. Do not block TD-2701 on this story.

---

## Epic E23 — Distill

**Goal:** session end proposes memory writes. Worker tier. Cheap. Spec §5
mechanic 2.

---

### TD-2301 — Distill on the worker
**Size:** 3 · **Depends on:** TD-2201, TD-1802

**Acceptance criteria:**
- [x] A worker-tier call, given the session's user/assistant turns and the
      current memory files, returns a structured proposal: create / replace /
      delete paths under `.tst/memory/`
- [x] The proposal is a diff against the files on disk, not a free-form essay
- [x] Cost is recorded as a worker call; it is not a user turn
- [x] Mock-provider test: scripted distill output becomes a typed proposal

**Notes:** this is not `fs_write`. The model that distilled must not be the
path that writes.

---

### TD-2302 — Distill trigger
**Size:** 3 · **Depends on:** TD-2301

**Acceptance criteria:**
- [x] Graceful app quit runs distill for every live session that had at
      least one completed turn
- [x] An explicit End session action runs the same path
- [x] A crash, a killed sidecar, or a force-quit writes nothing
- [x] Distill is skipped when memory is unchanged (no proposal event)

**Notes:** v0.1 quit is graceful (TD-1002). Do not wait for detached
sessions (v0.3). End session can be a rail action; if the rail is too
small, a command-palette entry is enough.

---

### TD-2303 — Distill is not a tool write
**Size:** 2 · **Depends on:** TD-2301

**Acceptance criteria:**
- [x] A distill proposal never enters `fs_write` / the dispatcher
- [x] The classifier is not asked to approve a steering write
- [x] A test that the only writers of `.tst/memory/` after distill-accept
      are the memory store (TD-2104), not the tool handlers

---

## Epic E24 — Diff-before-write

**Goal:** the user sees the memory diff and accepts, edits, or rejects.
Spec §5 mechanics 3–4.

---

### TD-2401 — `memory_proposal` protocol
**Size:** 3 · **Depends on:** TD-2301, TD-204

**Acceptance criteria:**
- [x] Daemon event `memory_proposal` carries session id, file diffs, and a
      proposal id
- [x] Client messages `memory_accept` / `memory_edit` / `memory_reject`
- [x] Architecture tables and protocol fixtures updated
- [x] Unknown-event gate still lists the new type

**Notes:** same fixture discipline as every other protocol story.

---

### TD-2402 — Proposal card
**Size:** 3 · **Depends on:** TD-2401, TD-1007

**Acceptance criteria:**
- [x] A card (or modal) shows the unified diff per file
- [x] Accept writes via TD-2104 and dismisses
- [x] Reject writes nothing and dismisses
- [x] Bind-and-clear on session switch (TD-1014 contract)

---

### TD-2403 — Edit before accept
**Size:** 2 · **Depends on:** TD-2402

**Acceptance criteria:**
- [x] The user can edit the proposed markdown in the card
- [x] Accept commits the edited bytes, not the original proposal
- [x] Emptying a file in the editor is a delete, matching the proposal
      vocabulary

---

### TD-2404 — Unanswered proposal is a reject
**Size:** 2 · **Depends on:** TD-2401

**Acceptance criteria:**
- [x] Shutdown with a live unanswered proposal writes nothing
- [x] It is not silently accepted
- [x] The next session does not resurrect a stale proposal

---

## Epic E25 — Prompt integration

**Goal:** the brain sees the subset. The worker does not. Spec §4.6 / §5.

---

### TD-2501 — Replace the placeholder
**Size:** 3 · **Depends on:** TD-2201

**Acceptance criteria:**
- [x] Brain prompt contains the loaded memory bytes instead of
      `MEMORY_PLACEHOLDER` when any file loaded
- [x] Worker and validator prompts contain no memory slot
- [x] Empty load keeps the placeholder (prefix-stable "none")
- [x] Existing prompt tests updated, not deleted

---

### TD-2502 — Memory block budget
**Size:** 2 · **Depends on:** TD-2501, TD-506

**Acceptance criteria:**
- [x] Loaded memory is token-counted with the same counter as steering
- [x] Lowest-ranked topic files drop until under a config cap
- [x] `MEMORY.md` is the last file dropped
- [x] The inspector (TD-2604) can name what was dropped and why

---

### TD-2503 — Memory is after the cache prefix
**Size:** 2 · **Depends on:** TD-2501, TD-1811

**Acceptance criteria:**
- [x] Changing memory files does not change the prefix hash
      (base + root + steering)
- [x] The memory bytes are present in the full prompt
- [x] A regression test swaps `MEMORY.md` between two assembles and
      asserts prefix hash equality, full-text inequality

**Notes:** already the assembler's shape. This story is the pin, so a
later "put memory in the prefix" cannot land quietly.

---

## Epic E26 — Memory UI and global opt-in

**Goal:** the user can read and correct memory without waiting for distill.
Spec §5: "open, correct, diff, grep, and revert."

---

### TD-2601 — Memory pane
**Size:** 3 · **Depends on:** TD-2501, TD-1705

**Acceptance criteria:**
- [x] A Memory surface lists `.tst/memory/**` for the bound workspace
- [x] Opening a file shows the markdown
- [x] Empty directory has copy that points at the first distill
- [x] Planned rail entry (`state: planned`) becomes `ready` — no Memory
      rail row exists. The pane is the project-home Memory column
      (DECISIONS 2026-08-20). Scheduled is `ready` with the other
      function rows; the rail has no `planned` entry.

**Done (2026-08-27):** `list_memory` / `memory_files` plus the project-home
column already satisfied the first three ACs. The leftover rail row is
closed as cancelled: adding `memory` to `RailSurface` would fight Home /
Projects / Scheduled. `rail.test.ts` pins that.

---

### TD-2602 — Manual edit
**Size:** 2 · **Depends on:** TD-2601, TD-2104

**Acceptance criteria:**
- [x] The user can save an edit from the pane
- [x] Save goes through the memory commit path
- [x] The agent cannot use this path to write steering files

---

### TD-2603 — Global memory opt-in
**Size:** 3 · **Depends on:** TD-2501

**Acceptance criteria:**
- [x] Settings has an off-by-default "load global memory" toggle
- [x] When on, `~/.tstdesk/memory/` is loaded after workspace memory
- [x] Global files are never committed into the workspace repo
- [x] Off means zero reads of that directory (test)

**Notes:** spec §5. The toggle is machine-wide, like skip-all — not a
workspace file.

---

### TD-2604 — Inspector names memory sources
**Size:** 2 · **Depends on:** TD-2501, TD-1201

**Acceptance criteria:**
- [x] The stack pane lists each loaded memory file, its token count, and
      why it was chosen (always-index / heading / embedding)
- [x] Dropped files are listed separately
- [x] A live session with no memory shows the placeholder honestly

**Notes:** the Memory column of the project home (TD-2803) is this pane
hosted there, not a second store.

---

## Epic E28 — Project home

**Goal:** opening a workspace feels like opening a Claude Project: a named
home, recents for that folder, and three human-facing columns —
Instructions, Memory, Context. The folder is still the project. There is
no account and no uploaded cloud knowledge base.

The three columns map onto files we already named, and they stay split:

| Column | On disk | Who writes |
|---|---|---|
| Instructions | `AGENTS.md` / `CLAUDE.md` / `.tst/rules/**` | The human only |
| Memory | `.tst/memory/**` | The agent (distill) and the human (correct) |
| Context | `.tst/context/pins.yaml` plus the pinned workspace paths | The human pins; the assembler reads |

---

### TD-2801 — Project home surface
**Size:** 5 · **Depends on:** TD-1712, TD-1103

**Acceptance criteria:**
- [x] Rail **Projects** opens a project list (known workspaces, pin-able),
      not only the title-bar recents menu
- [x] Selecting a project shows a home: project name (folder name), New
      chat, and recents filtered to that `workspace_path`
- [x] New chat is `new_session` in that workspace
- [x] Home / Projects selection is honest in the rail (`current` vs `ready`)

**Notes:** size 5 — the rail's Projects row is already `ready` and today
only toggles the recents menu. Changing that is a real IA change. Propose
the layout against the 2026-08-19 reference shot before building.

---

### TD-2802 — Instructions column
**Size:** 3 · **Depends on:** TD-2801, TD-1502

**Acceptance criteria:**
- [x] The column lists the workspace's steering files (`AGENTS.md` or
      `CLAUDE.md` fallback, then `.tst/rules/*`)
- [x] `+` creates a new `.tst/rules/` file or opens the existing
      `AGENTS.md` — it never writes through the agent tools
- [x] Editing is a human file write; the instruction stack reloads (TD-509)
- [x] Empty state copy points at `docs/steering.md`

**Notes:** this is a viewer/editor on spec §4, not a new instruction
format. The agent still cannot write these paths (TD-2102).

---

### TD-2803 — Memory column
**Size:** 2 · **Depends on:** TD-2801, TD-2601

**Acceptance criteria:**
- [x] The column shows `.tst/memory/**` for this workspace (E26's pane)
- [x] Copy says it is local to this machine / this folder — no "sync"
- [x] Distill proposals (E24) can open against this column
- [x] Empty state points at the first End session / quit distill

---

### TD-2804 — Context pins
**Size:** 5 · **Depends on:** TD-2801, TD-2501

**Acceptance criteria:**
- [x] The human pins workspace files or folders onto the project
      (stored as `.tst/context/pins.yaml`, git-tracked)
- [x] Pinned paths render as cards (name, kind, line count)
- [x] `+` is a file picker inside the workspace wall; outside is refused
- [x] Unpin removes the pin, not the file
- [x] Search-in-pins filters the cards; it does not fetch the web

**Notes:** this is Claude's "Context" column, not the whole repo and not
an upload-to-cloud knowledge base. The repo stays on disk. Size 5 because
the pin file is a new schema.

---

### TD-2805 — Context rides the brain
**Size:** 3 · **Depends on:** TD-2804, TD-2502

**Acceptance criteria:**
- [x] Pinned files load as a brain-only `project_context` block, after
      memory, still after the cache prefix
- [x] A capacity meter shows tokens for instructions + memory + pins
      against a config cap (the "12% of project capacity" read)
- [x] Over cap, pins drop last-in-first-out; the meter says so
- [x] Worker/validator do not receive the pin block

---

### TD-2806 — Pin list on the rail
**Size:** 2 · **Depends on:** TD-2801

**Acceptance criteria:**
- [x] A workspace can be pinned to the Projects list
- [x] Pins persist in the user data dir (machine-wide), not in the
      workspace (so a clone does not inherit another person's pins)
- [x] Unpinned known workspaces still appear under Recents

---

### TD-2807 — Project recents are sessions
**Size:** 2 · **Depends on:** TD-2801, TD-1701

**Acceptance criteria:**
- [x] The home recents list is `session_list` filtered to this
      `workspace_path`, newest first
- [x] Clicking a row attaches that session (same as the rail history)
- [x] Archived sessions do not appear unless the Archived filter is on

---

### TD-2808 — Project home is wired, not a mock
**Size:** 2 · **Depends on:** TD-2802, TD-2803, TD-2804, TD-2807

**Acceptance criteria:**
- [x] A store-level test: bind a workspace with steering, memory, and
      pins → the three columns expose those paths
- [x] Rail Projects → home → New chat creates a session in that workspace
- [x] An invariant test: every `ready` rail entry still activates
      (TD-1712)

**Notes:** not the M4 memory exit (that stays TD-2701). This is the
product-shape pin so the home cannot ship as three empty cards.

---

## Epic E27 — M4 exit

**Goal:** the same job as TD-1401, for memory.

---

### TD-2701 — Memory headless harness
**Size:** 3 · **Depends on:** TD-2501, TD-2401, TD-1401

**Acceptance criteria:**
- [x] A scripted session: seed `MEMORY.md` and a topic file, send a
      matching task, assert the brain prompt contains the topic and the
      worker prompt does not
- [x] Distill produces a proposal; accept writes the file and creates a
      `tst: memory update` commit
- [x] Reject leaves the tree identical
- [x] **This harness is the M4 exit criterion**
- [x] Runs in CI without an embeddings sidecar

---


# MILESTONE M5 — Cowork parity (v0.3)

Spec §8 and §9. The session already outlives the socket (TD-205). The window
still kills the daemon (TD-1002). Session list is shipped (TD-1701). Event
logs already land on disk (`events.jsonl` / `conversation.json`) — that was
pulled forward as "open last chat," not as coworker. M5 is **close the
window, it keeps working**, plus the CLI door and artifacts.

**Do not start until M4 exits (TD-2701).** Persist-revive is not M5 done.

**M5 exit:** TD-3204.

```
E29 Host detach ─> E31 CLI
       │
       └─> E30 Names ─> E32 Artifacts ─> TD-3204
```

---

## Epic E29 — Detached host

**Goal:** Close window ≠ quit. The dock (or a leftover process) owns `tstd`.
A full tray icon is Later (TD-4703).

---

### TD-2901 — Durable log is the source of truth, and it is bounded
**Size:** 5 · **Depends on:** TD-205

`events.jsonl` already exists. This story makes it the attach source after
a daemon restart *and* after a window-close detach, and stops an unbounded
file from becoming the next outage.

**Acceptance criteria:**
- [x] Attach after a clean daemon restart replays from disk at `from_seq`
      with the same gap/dup rules as the in-memory log
- [x] A session with no snapshot is still an `interrupted` tombstone
      (DECISIONS 2026-08-20 revive cut is unchanged)
- [x] The on-disk log is rotated or windowed at a config cap (events or
      bytes); attach from a seq that was rotated returns a typed
      `log_trimmed` and replays from the earliest kept seq
- [x] Secrets stay redacted on the way to disk (TD-1405)
- [x] A test kills the daemon mid-session, restarts, attaches, and sees
      every event that was `seq`-committed before the kill

---

### TD-2902 — Closing the window does not stop the daemon
**Size:** 5 · **Depends on:** TD-1002, TD-2901

**Acceptance criteria:**
- [x] Window close hides the window and leaves `tstd` running; in-flight
      turns and parked approvals continue
- [x] Reopening the app attaches with `from_seq` and does not spawn a
      second daemon
- [x] OS notifications for approval and turn-complete still fire
      (TD-1702) while the window is gone
- [x] `--parent-pid` does not kill `tstd` when the window process exits
      if coworker mode is on
- [x] Documented per platform: close vs quit

**Notes:** The v0.1 AC on TD-1002 stays true for "Quit." This story is
the divergence the TODO named.

**Completed (2026-08-20):** Close hides the window and leaves the host
and `tstd` running. Quit still reaps. Coworker defaults on
(`coworker.yaml`). TD-2903 restored `--parent-pid` — close keeps the
host alive, so the watchdog stays quiet; SIGKILL of the host still
reaps. A live `port.json` is attached, not spawned over. OS notify
already fires when unfocused; a hidden window is unfocused.

---

### TD-2903 — Quit is explicit
**Size:** 3 · **Depends on:** TD-2902

**Acceptance criteria:**
- [x] Menu / palette **Quit TST Desk** sends `shutdown`, reaps the
      process group, and leaves no listener (TD-1304 contract)
- [x] Close window is not Quit. Copy in the first-run of this behavior
      says so once
- [x] Force-quit / SIGKILL still has no orphan (watchdog or host)

**Completed (2026-08-20):** Palette and the native menu **Quit TST Desk**
call `quit_app` → daemon `shutdown` + embeddings reap + `app.exit`.
Close still hides. First hide stamps `{user_data_dir}/close-is-not-quit.yaml`
and shows the copy once. `--parent-pid` is always passed so a force-quit
host does not leave a listener.

---

### TD-2904 — Coworker indicator
**Size:** 2 · **Depends on:** TD-2902

**Acceptance criteria:**
- [x] While the window is hidden and a session is running or
      `awaiting_approval`, the dock/taskbar badge or tooltip says so
- [x] Clicking the app icon shows the window and focuses the parked
      approval if there is one
- [x] No tray icon required (TD-4703)

---

### TD-2905 — Settings: coworker on
**Size:** 2 · **Depends on:** TD-2902

**Acceptance criteria:**
- [x] Settings toggle **Keep running when the window closes**, default
      on once M5 ships, persisted in the user data dir
- [x] Off restores TD-1002 v0.1 behavior (close = shutdown)

---

## Epic E30 — Session names

**Goal:** the rail stops showing `session_id[:8]`. TD-1701 deferred this
to v0.3.

---

### TD-3001 — Auto-title from the first user message
**Size:** 2 · **Depends on:** TD-1701

**Acceptance criteria:**
- [x] After the first user message the record stores a one-line title
      (trimmed, length-capped)
- [x] `session_list` carries it; rail and palette show it
- [x] Later messages do not retitle
- [x] Empty / attachment-only first messages keep the short id

---

### TD-3002 — Rename
**Size:** 2 · **Depends on:** TD-3001

**Acceptance criteria:**
- [x] Rail row can rename; the daemon persists the title
- [x] Empty rename restores the auto-title or the short id
- [x] Rename of a busy session is allowed (metadata only)

---

### TD-3003 — Star
**Size:** 2 · **Depends on:** TD-1701

**Acceptance criteria:**
- [x] A session can be starred; stars persist in the user data dir
      (machine-wide, not the workspace — same cut as TD-2806)
- [x] Starred rows sort above the rest in the rail, then newest
- [x] Filter can show starred only

---

## Epic E31 — CLI door

Spec §2: same daemon, different door.

---

### TD-3101 — `tst run`
**Size:** 5 · **Depends on:** TD-2901, TD-1401

**Acceptance criteria:**
- [x] `tst run --workspace <path> --message <text>` uses a running
      daemon or starts one, prints assistant text, exits non-zero on a
      failed turn
- [x] Same port-file + hello token as the window
- [x] No bind except the daemon's existing interface
- [x] Headless harness (TD-1401) stays the mock path

---

### TD-3102 — `tst attach`
**Size:** 3 · **Depends on:** TD-3101, TD-206

**Acceptance criteria:**
- [x] `tst attach <session_id>` replays from `from_seq` and streams
      live events as text
- [x] Unknown id is a typed error
- [x] Ctrl+C detaches; it does not cancel the session

---

### TD-3103 — TTY approvals
**Size:** 2 · **Depends on:** TD-3102, TD-802

**Acceptance criteria:**
- [x] A TTY prints the card and accepts `y` / `n` / `always`
- [x] Non-TTY refuses with copy to use the window
- [x] Class C never accepts `always`

---

## Epic E32 — Artifacts and the work view

Spec §3 / §9. The Files pane (TD-1705) is writes-this-session. Artifacts
are things the model *made for you* (a doc, a preview) that persist with
the session. The work view is the right-pane surface that is not chat.

---

### TD-3201 — Artifact record
**Size:** 5 · **Depends on:** TD-2901, TD-1709

**Acceptance criteria:**
- [x] Daemon can store an artifact (id, session, mime, path under the
      workspace or the session data dir, title)
- [x] Protocol: `artifact_ready` event; list/open messages
- [x] Workspace-wall applies; no write outside it
- [x] Gate and fixtures updated (TD-1010 contract)

**Done (2026-08-20):** `Daemon.record_artifact` writes
`sessions/{id}/artifacts.json` plus optional bytes under
`sessions/{id}/artifacts/`, or registers a workspace path that passes
`PathGuard`. `artifact_ready` is session-scoped; `list_artifacts` /
`open_artifact` answer with path metadata, not bytes. Unknown id is
`artifact_not_found`. See DECISIONS.md 2026-08-20 TD-3201.

---

### TD-3202 — Artifacts rail and preview
**Size:** 5 · **Depends on:** TD-3201, TD-1712

**Acceptance criteria:**
- [x] Rail **Artifacts** becomes `ready` (today it is absent or planned)
- [x] List for the bound session; click opens a preview (markdown /
      highlighted code / sandboxed HTML — no network in the preview)
- [x] Copy source; open-in-OS-editor if it is a workspace path
- [x] Not a file tree. Not Monaco. Not apply/reject

**Done (2026-08-20).** Artifacts is a ready rail surface. The pane lists
the bound session from `artifact_list` / `artifact_ready`; a click
sends `open_artifact` and previews through a wall-limited
`read_text_file`. Markdown and highlighted code reuse the chat stack;
HTML is `sandbox=""` plus `default-src 'none'`. Copy source always;
Open in editor only for workspace paths. See DECISIONS.md 2026-08-20
TD-3202.

---

### TD-3203 — Work view: session diffs as a first-class pane
**Size:** 5 · **Depends on:** TD-1705, TD-3202

**Acceptance criteria:**
- [x] A Work / Diffs surface shows the session's writes as a reviewable
      stack (path, +/- , expand), not only the Files tab fold
- [x] Click opens the file; the OS editor remains the editor
- [x] Empty state explains it fills as the agent writes

**Notes:** This is spec §3's "file diffs" work view. It is aggregation,
not an in-app editor.

---

### TD-3204 — M5 exit harness
**Size:** 3 · **Depends on:** TD-2902, TD-3101, TD-3201

**Acceptance criteria:**
- [x] Scripted: start a session, close the viewer, assert the loop
      still accepts a turn, reopen, attach, replay is complete
- [x] `tst run` against the mock provider is green in CI
- [x] **This harness is the M5 exit criterion**

Done (2026-08-20): `tstd.e2e_m5` + `core/scripts/e2e_m5.py`, pinned in
CI as `tests/test_e2e_m5.py`. Protocol client: drop the socket, second
turn on a new attach, `from_seq=1` replay, `cli.run_turn` on the mock
daemon. Not marked `live`.

---

# MILESTONE M6 — Computer use (v0.4)

Spec §9. Browser slice is already TD-1710 (E17). This milestone is
**desktop** drive + the chrome that makes it watchable and pointable.
Linux MCP backends stay E20 (already filed; counted here).

**Do not start until M5 exits**, except TD-1710 which is already on M3.

**M6 exit:** TD-3405.

```
TD-1710 (browser) ─> E34 Screen chrome
E33 Desktop drivers ─> E34
E20 Linux MCP (parallel; needs a Linux box)
```

---

## Epic E33 — Desktop drivers in the product

**Goal:** the loop can move the real pointer, through the same
classifier and approval gate as every other tool. Prefer wrapping
`mcp/tst-cu-mcp` over a third copy of the backends.

---

### TD-3301 — Register desktop computer-use tools in `tstd`
**Size:** 8 · **Depends on:** TD-601, TD-1710

**Acceptance criteria:**
- [x] Screenshot / move / click / type / scroll are tools on the
      session dispatcher, Class B (ask) except screenshot which may be
      A if it cannot actuate
- [x] Path/host rules do not apply; a **focus guard** (`expect_window`)
      does — mismatch refuses without actuating
- [x] Kill-switch stops actuation; capture still works
- [x] macOS and Windows each have a live path; Linux is E20
- [x] Mock driver for CI (the tst-cua mock, TD-102)

**Notes:** Size 8. Split if the MCP-bridge and the product tools
diverge. Do not bind a socket; stdio to the sidecar is enough.

Done (2026-08-21): five `desktop_*` tools; `Tool.actuates` → Class A/B;
stdio MCP sidecar; mock when `computer_use.command` is empty.

---

### TD-3302 — macOS permission onboarding
**Size:** 5 · **Depends on:** TD-3301, TD-1101

**Acceptance criteria:**
- [x] First desktop CU attempt explains Screen Recording +
      Accessibility, links to System Settings, and retries
- [x] Denied is a typed error, not a hang
- [x] Wizard/settings can reopen the explanation

Done (2026-08-21): `cu_permissions` + Settings reopen; typed
`permission_denied`; first-run `{user_data_dir}/cu-macos-permissions.yaml`.

---

### TD-3303 — Windows permission / integrity onboarding
**Size:** 5 · **Depends on:** TD-3301

**Acceptance criteria:**
- [x] First desktop CU attempt names what Windows will prompt for and
      what fails if refused
- [x] Denied is a typed error
- [x] Documented in `docs/windows.md`

Done (2026-08-21): first-run `cu_permissions` with Windows integrity
copy (`{user_data_dir}/cu-windows-permissions.yaml`); typed `uipi` /
`secure_desktop`; `docs/windows.md` §7.

---

### TD-3304 — Grounding honesty
**Size:** 3 · **Depends on:** TD-3301

**Acceptance criteria:**
- [x] A recorded evaluation: click target vs landing, on one fixture
      page per supported OS, written in `DECISIONS.md`
- [x] Misses over a stated tolerance fail the eval; they do not ship
      as "it works"
- [x] UI-TARS as a grounding *model* is TD-3902, not this story

Done (2026-08-21): mock eval, 4.0 point hypot; darwin + win32 fixture
rows; no live claim; Linux is E20.

---

## Epic E34 — Screen, indicators, Design mode

---

### TD-3401 — Screen pane for desktop
**Size:** 5 · **Depends on:** TD-1710, TD-3301

**Acceptance criteria:**
- [x] Screen tab streams desktop (or the driven display) the way TD-1710
      streams the browser
- [x] Failure modes are timeline entries
- [x] No Screen tab until a CU tool has run this session — empty copy
      points at the first computer-use turn

**Completed (2026-08-21):** `persist_screen_frame` lives in `tstd.screen.frames`
so desktop and browser share one write. A successful `desktop_screenshot`
emits `screen_frame` (path, not bytes). The Screen tab gates on `hasCuTool`
(any `browser_*` or `desktop_*` this session). Failed desktop clicks stay
`tool_result` timeline rows.

---

### TD-3402 — Glow and agent cursor
**Size:** 5 · **Depends on:** TD-3401

**Acceptance criteria:**
- [x] Settings: **Computer-use glow** and **Agent cursor**, default on,
      user-data-dir
- [x] Screen pane shows both while a CU turn is live
- [x] **Show indicators on the real display** is a third toggle,
      default off; if on, hidden during every `screenshot`
- [x] Not a second hardware pointer (OS has one)
- [x] `prefers-reduced-motion`: static border, no trail
- [x] Clears on turn end, cancel, and kill-switch

**Completed (2026-08-21):** Settings → Appearance persists the three bits
in `{user_data_dir}/cu-indicators.yaml` via `set_cu_indicators`. Glow and
the agent cursor are CSS overlays on the Screen pane (not a second OS
pointer). The real-display host path is a no-op; screenshot tools raise
`real_display_overlay_hidden` while capturing when that toggle is on.
`prefers-reduced-motion` is a static border and no trail. The store
clears on `turn_complete`, cancel, and `cu_kill_state`.

**Addendum (2026-08-22):** The real-display path is no longer a no-op: the
sidecar paints a rust ring on every display while the agent drives
(`tst_cu_mcp/overlay`, helper AppKit child; DECISIONS.md 2026-08-22). The
toggle now defaults **on** and reaches the sidecar as `TST_CU_MCP_OVERLAY`
at spawn; captures hide the ring with an acked `grab_begin`/`grab_end`
bracket instead of the old `real_display_overlay_hidden` error. Verified
end to end on macOS: ring pulses through the 8s linger, absent from every
server-path capture, dark under the kill-switch.

---

### TD-3407 — Cross-platform CU session glow
**Size:** 5 · **Depends on:** TD-3402, TD-2001

The real-display rust ring is the same signal on every live desktop
path, and it stays up for the whole computer-use episode — not eight
seconds after the last click. An internal `cu_session` tag opens when
the agent first takes a `desktop_*` / `browser_*` tool this turn, and
a matching close tag ends it.

**Acceptance criteria:**
- [x] `cu_session { active: true }` is emitted on the first computer-use
      tool of a turn; `{ active: false }` on turn end, cancel, and
      kill-switch
- [x] The real-display ring stays lit from open until close (no linger
      timeout). Screenshots still hide it for the grab
- [x] macOS, Windows, and Linux X11 paint the same rust ring (inset,
      pulse, click-through). Wayland stays unlit (TD-2002)
- [x] **Show indicators on the real display** still turns the ring off
- [x] Screen-pane glow uses the same open/close tags
- [x] An overlay failure never breaks capture or actuation

**Completed (2026-08-25):** `cu_session` is the tag. Sidecar
`overlay_session` drives macOS / Windows / Linux X11 painters. Linger
removed. Wayland stays `NullOverlay`.

---

### TD-3403 — Design mode
**Size:** 8 · **Depends on:** TD-3401, TD-1704, TD-1709

Point at the running UI instead of describing it. Browser first
(TD-1710). Desktop picking is a follow-up inside this story only if
the AX hit-test is cheap; otherwise file a split.

**Acceptance criteria:**
- [x] ⌘⇧D toggles Design on the Screen pane. On: clicks select. Off:
      watch surface
- [x] Click / shift-click / shift-drag on a frozen frame
- [x] Chip on the composer: xpath or AX role, attributes, computed
      box/styles, cropped screenshot
- [x] Travels with `user_message` under attachment caps
- [x] Cannot run while the agent is actuating that surface
- [x] Voice is TD-4701. React fiber is out. Source maps are a follow-up

**Completed (2026-08-21):** Design freezes the last `screen_frame`. Clicks
map into CSS pixels. Browser hit-test is `design_hit_test` → driver
`hit_test` (mock scripted node in CI; Playwright `elementFromPoint` live).
The chip is a `design-pick.json` text attachment (xpath/role/attrs/box/
styles + data-URL crop) under TD-1709 caps. Actuating CU tools force
Design off. Desktop AX was not cheap in `tst-cu-mcp` — split to TD-3406.

---

### TD-3406 — Desktop Design-mode hit-test
**Size:** 5 · **Depends on:** TD-3403, TD-3301

AX hit-test is not in `tst-cu-mcp` (permission probe only; no
element-at-point). Design mode v1 is browser-only; desktop frames get a
geometric box + crop, not an AX role.

**Acceptance criteria:**
- [x] Desktop Screen frames accept Design picks via AX role + attributes
- [x] Crop + box still travel as TD-1709 attachments
- [x] Same ⌘⇧D / actuating gate as TD-3403
- [x] Observe only — never actuates

**Done (2026-08-26):** Desktop frames send the same `design_hit_test`
verb. The daemon routes from the last `desktop_` / `browser_`
`tool_call` on the session log. `tst-cu-mcp` adds observe-only
`hit_test` (AX / UIA-adjacent HWND / AT-SPI or EWMH window). Last
screenshot metadata maps image pixels ↔ global points. Kill-switch
does not block. Not an agent tool. Wayland still has no AX path.

---

### TD-3404 — In-window kill-switch
**Size:** 3 · **Depends on:** TD-3301

**Acceptance criteria:**
- [x] A visible control stops all actuation immediately (same as the
      MCP kill-switch)
- [x] Capture and the Screen pane keep working
- [x] Keyboard reachable; discoverable from the palette

Done (2026-08-21): title bar + palette + ⌘. send `set_cu_kill`;
connection-scoped `cu_kill_state`; screenshot still runs.

---

### TD-3405 — M6 exit
**Size:** 5 · **Depends on:** TD-1710, TD-3301, TD-3401

**Acceptance criteria:**
- [x] Headless: mock driver, one screenshot + one refused click
      (focus mismatch) + one approved click, all through the classifier
- [x] Live: TD-1710 browser path green on one OS
- [x] **This harness is the M6 exit criterion**

**Completed (2026-08-21):** `tstd.e2e_m6` + `core/scripts/e2e_m6.py`,
pinned in CI as `tests/test_e2e_m6.py`. Headless: mock desktop
screenshot (A), `expect_window` refuse (`focus_mismatch`, no
actuation), approved click (B). CI's TD-1710 path is the mock six
verbs + `screen_frame`. Playwright is `@pytest.mark.live` and is not
claimed unless Chromium launched. Not `e2e_harness.run`.

---

# MILESTONE M7 — Remote and notify (v0.5)

Spec §8. Slack first. Never `0.0.0.0`. Scheduler is small: wake, run
one instruction, deliver.

**Do not start until M5 exits.** Remote attach to a dead daemon is a lie.

**M7 exit:** TD-3806.

---

## Epic E36 — Tailscale bind

---

### TD-3601 — Bind the existing socket to a Tailscale interface
**Size:** 5 · **Depends on:** TD-202, TD-2902

**Acceptance criteria:**
- [x] Config names an interface or a Tailscale IPv4; the server binds
      that address and loopback, never `0.0.0.0` / `::`
- [x] A bind to a non-Tailscale non-loopback address is refused
- [x] `test_outbound_hosts` / bind tests name the new path
- [x] Off by default

Done (2026-08-21): `remote.bind` dual-listens loopback + Tailscale;
`0.0.0.0` / LAN refused; off by default.

---

### TD-3602 — Non-loopback auth
**Size:** 5 · **Depends on:** TD-3601, TD-203

**Acceptance criteria:**
- [x] Loopback keeps the port-file token
- [x] A non-loopback hello requires a user-data-dir token with
      rotation; a leaked port file is not enough
- [x] Failed auth is a typed close, not a session

Done (2026-08-21): remote hello needs `{user_data_dir}/remote-token`;
port-file token stays loopback-only; failed auth is `auth_failed`.

---

### TD-3603 — Settings: allow remote attach
**Size:** 2 · **Depends on:** TD-3602

**Acceptance criteria:**
- [x] Toggle + the bound address shown (not a secret)
- [x] Off unbinds the Tailscale iface and leaves loopback

Done (2026-08-21): Settings `Allow remote attach` sends `set_remote_attach`;
`setup_state` reports `remote_attach_enabled` and `remote_bind` (address,
never a token). Off drops the extra listener and leaves loopback.

---

## Epic E37 — Phone and remote viewer

---

### TD-3701 — Attach from another device
**Size:** 8 · **Depends on:** TD-3602, TD-1003

**Acceptance criteria:**
- [x] The same protocol client works from a browser on the Tailscale
      address (read transcript, send, approve)
- [x] No account. No hosted relay
- [x] Layout degrades to one pane on a narrow viewport (chat +
      approval). Inspector is optional
- [x] Size 8 because a second client surface will sprawl — split if
      the mobile layout becomes its own product

Done (2026-08-21): same AppShell + ProtocolClient without Tauri; form or
`ws`+`token` query/hash; remote hello uses the 3602 token; <640px is
chat + approval.

---

### TD-3702 — Remote approve
**Size:** 3 · **Depends on:** TD-3701, TD-802

**Acceptance criteria:**
- [x] Approve / deny / always-allow from the remote client resolve
      the parked session
- [x] Two clients cannot double-resolve (TD-1014 contract)

Done (2026-08-21): remote hello (3602 token) approve/deny/always-allow
unparks the same session future; a second client gets
`no_pending_approval` and cannot flip the outcome. Same ApprovalCard
store dismisses on send (TD-1014).

---

## Epic E38 — Notify and the scheduler

Hermes pattern: `send(config, message)`. Rebuild the cron. Do not lift
the 20-platform gateway.

---

### TD-3801 — Slack incoming webhook
**Size:** 3 · **Depends on:** TD-1702

**Acceptance criteria:**
- [x] Config holds a webhook URL (user-data-dir, not the workspace,
      not the audit log in plaintext — treat as a secret, keychain or
      equivalent)
- [x] Approval-needed and turn-complete can deliver to Slack when
      enabled
- [x] Destination is config-sourced (`test_outbound_hosts`)

Done (2026-08-21): `send(config, message)`; URL in keychain
`tst-slack-webhook`; host from `notify.slack.host`; off by default.

---

### TD-3802 — ntfy (optional extra)
**Size:** 2 · **Depends on:** TD-3801

**Acceptance criteria:**
- [x] Same `send` shape; topic URL from config; off by default
- [x] Discord/Telegram are TD-4707, not this story

Done (2026-08-21): `send(config, message)`; topic URL in keychain
`tst-ntfy-topic`; host from `notify.ntfy.host`; off by default.

---

### TD-3803 — Scheduler store
**Size:** 5 · **Depends on:** TD-2901

**Acceptance criteria:**
- [x] Jobs persist in the user data dir: id, workspace, instruction,
      cadence or next-run, deliver-to (window / Slack / ntfy)
- [x] Natural-language create is a *worker* parse into that schema,
      shown for edit before save
- [x] No job runs until M7's runner (TD-3804) exists

Done (2026-08-21): `{user_data_dir}/scheduler/jobs.json`; parse is a
draft; save is a second call; no runner.

---

### TD-3804 — Wake, run, deliver
**Size:** 5 · **Depends on:** TD-3803, TD-3101

**Acceptance criteria:**
- [x] Due jobs start a session (or `tst run`) in the named workspace,
      then deliver a summary to the configured channel
- [x] Missed runs while the daemon was down fire once on revive, not
      in a stampede
- [x] Caps and the classifier still apply

Done (2026-08-21): daemon tick + in-process `_start_session` turn;
one fire then cadence advances `next_run` (or pause if one-shot).

---

### TD-3805 — Scheduled rail is live
**Size:** 2 · **Depends on:** TD-3804, TD-1712

**Acceptance criteria:**
- [x] Rail **Scheduled** becomes `ready` and lists jobs
- [x] Create / pause / delete
- [x] The invariant test (every `ready` entry activates) stays green

Done (2026-08-21): Scheduled is `ready`/`current`; `list_jobs` /
`save_job` / `delete_job` at the end of the protocol unions; pane
lists and edits draft fields. The runner still ticks; the rail does
not fire jobs.

---

### TD-3806 — M7 exit
**Size:** 3 · **Depends on:** TD-3601, TD-3801, TD-3804

**Acceptance criteria:**
- [x] Headless: bind refused on `0.0.0.0`; a loopback job runs and
      a mock Slack `send` is invoked
- [x] **This harness is the M7 exit criterion**

Done (2026-08-21): `tstd.e2e_m7` + `core/scripts/e2e_m7.py`,
pinned in CI as `tests/test_e2e_m7.py`. Headless: `validate_interface`
refuses `0.0.0.0` (never listens); a due `deliver_to: slack` job fires
once through `run_due_jobs` on the in-process daemon; injected
`notify_send` is called. Not `e2e_harness.run`. Not a live webhook.


---

# MILESTONE M8 — Local models remainder (v0.6)

M1.5 already ships keyless loopback + the `local` preset. This milestone
is EZER/vLLM as a first-class path and UI-TARS for computer-use grounding.

**Do not start until M6 if the work is grounding; EZER routing can start
after M1.5.** Prefer after M6 so CU has something to ground.

**M8 exit:** TD-3904.

---

## Epic E39 — EZER, vLLM, UI-TARS

---

### TD-3901 — EZER / vLLM preset
**Size:** 5 · **Depends on:** TD-1805

**Acceptance criteria:**
- [x] A shipped preset points at a documented EZER or vLLM loopback
      URL; slugs still optional on loopback
- [x] Docs: how to attach, what "unresolved" means
- [x] Doctor rows name the endpoint, not the developer's model list
      (TD-1809)

---

### TD-3902 — UI-TARS grounding
**Size:** 8 · **Depends on:** TD-3304, TD-3901

**Acceptance criteria:**
- [x] Computer-use click targeting can use a configured local grounding
      model instead of raw pixels + guess
- [x] Off / missing model falls back to TD-3304's path
- [x] Cost is zero on loopback; latency is measured and recorded
- [x] Size 8 — split if the grounding client and the driver glue
      diverge

Done (2026-08-21): `computer_use.grounding` (empty `base_url` = off);
`tstd/desktop/grounding_client.py` + click-path glue in
`tstd/tools/desktop.py`. TD-3304 eval unchanged.

---

### TD-3903 — Local worker default for pixel loops
**Size:** 3 · **Depends on:** TD-3902, TD-303

**Acceptance criteria:**
- [x] A CU-heavy turn can pin the worker to the local slug without
      changing the brain
- [x] Title bar shows that honestly
- [x] Remote worker remains the default for non-CU turns

Done (2026-08-21): `computer_use.local_worker_preset` (default `vllm`)
remaps the worker *client* after a `desktop_` / `browser_` tool. Brain,
lead-turns, `set_tier`, and escalation stay TD-303. `tier_state` carries
the remapped worker slug or omits it if unresolved (TD-1805).



---

### TD-3904 — M8 exit
**Size:** 3 · **Depends on:** TD-3901

**Acceptance criteria:**
- [x] Live harness against a loopback vLLM or EZER fixture (or skip
      with copy if the binary is absent — heading-match style)
- [x] **This harness is the M8 exit criterion**

Done (2026-08-21): `tstd.e2e_m8` probes the shipped `vllm` loopback;
skip with heading-match copy if nothing is listening. Live turn is
one keyless completion at cost 0. Not `e2e_harness.run`.

---

# MILESTONE M9 — Autonomy engine (v0.7)

Spec §12. Hooks shipped in v0.1. This is the engine. **Hard-required
container** (TD-101). Depends on M5 (it must run with the window closed)
and M7 (it must notify).

**Do not start until M5 and M7 exit.**

**M9 exit:** TD-4304.

```
E40 Charter ─> E41 Runner ─┬─> E42 Supervisor
                           └─> E43 Isolation ─> wake-up ─> TD-4304
```

---

## Epic E40 — Charter

---

### TD-4001 — `CHARTER.md` schema
**Size:** 3 · **Depends on:** TD-706

**Acceptance criteria:**
- [x] `.tst/autonomy/CHARTER.md` validates the spec §12.4 shape
      (objective, DoD, source_of_truth, boundary, caps, stop_conditions)
- [x] Invalid charter refuses start with field names
- [x] Git-tracked; the agent cannot write it (Class C, same as steering)

Closed in `tstd.autonomy.charter`. The parser takes YAML frontmatter (or a
whole-file YAML mapping) into a strict model: unknown keys are refused at every
level — including inside `boundary:`/`caps:`, whose shared models now forbid
extras for `.tst/config.yaml` too — and duplicate YAML keys refuse instead of
last-winning. `boundary` and `caps` are required: an omitted section must not
default to the loosest wall on the shelf, and the charter may only narrow the
workspace wall, never widen it (the runner enforces that from TD-4003 on; this
module only answers "may a run start here?"). The git gate fails closed and
verifies bytes rather than trusting status: it hashes the working file against
`HEAD:<path>` so skip-worktree cannot hide a tamper, pops `GIT_DIR` and friends
so ambient env can't bless the wrong repository, and accepts any-case tracked
names. Adversarial-review hardening beyond the boxes: column-0-only frontmatter
fences (an indented `---` is content), BOM tolerance, duplicate-key refusal,
a 1 MiB size cap with parse off the event loop, and GIT_* repo-selection vars
stripped from child env. Flagged, deliberately not done here: shell redirect
targets still skip the TD-4820 unsafe-form checks (`> CHARTER.md.` classifies B
— byte-for-byte the same gap as `> AGENTS.md.`, inherited, not introduced); and
whether the editor needs a reserved `version:` field is TD-4002's call.

---

### TD-4002 — Charter editor
**Size:** 5 · **Depends on:** TD-4001, TD-1001

**Acceptance criteria:**
- [x] A pane edits the charter as structured fields, writes the file
      as the human (not via tools)
- [x] `source_of_truth` paths are workspace-walled
- [x] Empty state points at spec §12.4

Closed as `get_charter` / `save_charter` (human path, never a tool) plus
the project-home Charter column. The write validates with `parse_charter`
and walls `source_of_truth` through `PathGuard.check_read`. It does not
commit — signing is TD-4003. No reserved `version:` field: `extra="forbid"`
would make one a schema change, and the committed file is the signature.

---

### TD-4003 — Sign and start
**Size:** 3 · **Depends on:** TD-4002, TD-4301

**Acceptance criteria:**
- [x] Start is an explicit human action: shows the wall, the caps, and
      "this runs in a container"
- [x] Without a valid charter and a live sandbox, start is refused
- [x] Interactive sessions are unchanged

---

## Epic E41 — Autonomous runner

---

### TD-4101 — Unattended loop
**Size:** 8 · **Depends on:** TD-4003, TD-401, TD-2902

**Acceptance criteria:**
- [x] A run does not wait on a user message; it iterates against the
      objective until a stop condition
- [x] Class A never prompts. Class B logs. Class C stops and notifies
      (M7 channel)
- [x] Window may be closed (M5)
- [x] Size 8 — this is the engine. Split only at a real seam
      (scheduler vs act)

---

### TD-4102 — Checkpoint branch `tst/auto/<slug>`
**Size:** 5 · **Depends on:** TD-705, TD-4101

**Acceptance criteria:**
- [x] Every iteration commits on `tst/auto/<charter-slug>`, never
      `main` / default branch
- [x] Revert of a commit is the undo the ledger prints
- [x] Non-git workspace refuses the run with copy

---

### TD-4103 — Definition-of-done polling
**Size:** 5 · **Depends on:** TD-4101

**Acceptance criteria:**
- [x] DoD commands run on the worker (or shell through the gate)
- [x] All green is a stop condition: run completes, summary fires
- [x] A red DoD is not an instant stop (that's TD-4203's N-in-a-row)

---

## Epic E42 — Supervisor and breakers

---

### TD-4201 — Validator drift check
**Size:** 5 · **Depends on:** TD-4101, TD-508

**Acceptance criteria:**
- [x] Every N iterations (config, default 5) and on Class B, the
      validator receives charter + source_of_truth + diff + ledger +
      tests and answers spec §12.6's three questions
- [x] Cost is a validator call, not a user turn
- [x] `source_of_truth` is re-read from disk each check

**Done:** Supervisor hook in `advance_autonomy`'s continue path. Detects
and reports only — no auto-revert (TD-4202), no circuit breakers
(TD-4203). Result lives on `session.last_drift_check`.

---

### TD-4202 — Drift reverts
**Size:** 5 · **Depends on:** TD-4201, TD-4102

**Acceptance criteria:**
- [x] Drift → revert to last good checkpoint, log, re-plan on brain
- [x] Drift twice in a row → stop and notify
- [x] Interactive verify (TD-4204) does not auto-revert

**Done:** Continue-path hook after `maybe_check_drift`. A detection
restores drifted auto-branch paths to `autonomy_last_good_sha`, logs,
and `set_tier("brain")`. Two in a row set `breaker:drift` (existing
notify). Interactive verify does not import revert. First drift with
no last-good SHA is a no-op that still counts.

---

### TD-4203 — Circuit breakers
**Size:** 5 · **Depends on:** TD-4101, TD-707

**Acceptance criteria:**
- [x] Spec §12.7: spend/time cap, tests red N times, same file
      thrashed N times, no DoD progress N times, any Class C, identical
      tool-call loop
- [x] Trip is a fault report + notify, not an approval card
- [x] Each breaker has a test that trips it

**Completed (2026-08-26):** `maybe_trip` on the `advance_autonomy`
continue path. New trips are `breaker:tests_red`, `breaker:file_thrash`,
`breaker:no_dod_progress`, `breaker:tool_loop`. Spend/wall-clock caps
and Class C keep their existing stop strings. A trip notifies via the
wake-up path; it never mints an `ApprovalRequest`. Interactive sessions
never record or trip.

---

### TD-4204 — Interactive verify after writes
**Size:** 5 · **Depends on:** TD-303, TD-508

The unused validator call for *interactive* mode (spec §12.6 first
sentence). Not the autonomy supervisor.

**Acceptance criteria:**
- [x] Config `verify: off | after_write | ask` (default `after_write`)
- [x] A write turn may enqueue one validator call on the diff + tests
- [x] Timeline `verify_result`; not a second bubble unless asked
- [x] Write-less turns never verify
- [x] No charter, no auto-revert

---

## Epic E43 — Isolation and wake-up

TD-101: hard-required container. No unsandboxed autonomy.

---

### TD-4301 — Rootless container
**Size:** 8 · **Depends on:** TD-4001

**Acceptance criteria:**
- [x] Autonomous runs exec in rootless Podman (or documented
      equivalent) with only the workspace mounted
- [x] Missing runtime refuses start with install copy
- [x] Interactive mode does not require a container
- [x] Size 8 — Firecracker/EZER is a follow-up, not this story

---

### TD-4302 — No credentials in the mount
**Size:** 3 · **Depends on:** TD-4301

**Acceptance criteria:**
- [x] Host keychain, `~/.ssh`, cloud creds, and the user-data-dir key
      are not visible in the container
- [x] A test that a well-known cred path is absent
- [x] Network inside the container follows the charter wall

**Completed (2026-08-26):** `container_argv` still bind-mounts only the
workspace; there is no extra-mount API. A well-known cred path
(`~/.ssh` / `$HOME/.ssh` / `/root/.ssh`) is absent from argv and from
the fake runtime's container view. `network: deny` keeps
`--network=none`; a host allowlist omits that flag (Podman slirp) and
never uses `--network=host`. Unexpected values fail closed as deny.

---

### TD-4303 — Wake-up summary
**Size:** 5 · **Depends on:** TD-4101, TD-3801

**Acceptance criteria:**
- [x] On complete / stop / breaker: a summary (what changed, ledger
      excerpt, refusals, branch name) lands in the window and the
      notify channel
- [x] The user can open the branch and the ledger in one click

**Completed (2026-08-26):** `advance_autonomy` (and the loop cap path)
emits session-scoped `autonomy_summary` and sends the same body on
slack/ntfy. `should_notify` accepts `breaker:` so TD-4203 can plug in.
One click opens the ledger in the OS editor and copies the auto-branch
name. Interactive sessions never emit the event.

---

### TD-4304 — M9 exit
**Size:** 3 · **Depends on:** TD-4101, TD-4203, TD-4301

**Acceptance criteria:**
- [x] Headless: mock provider + fake container, a charter with a
      two-step DoD, assert branch commits, one Class A ledger line,
      a tripped breaker, and no `main` commit
- [x] **This harness is the M9 exit criterion**

Done (2026-08-26): `tstd.e2e_m9` + `core/scripts/e2e_m9.py`,
pinned in CI as `tests/test_e2e_m9.py`. Headless: `MockProvider` +
fake rootless Podman, a two-step `$` DoD, Class A write on
`tst/auto/<slug>`, `breaker:no_dod_progress`, `main` SHA unchanged.
Not `e2e_harness.run`. Not marked `live`.

---

# MILESTONE M10 — Extensibility (v0.8)

MCP, skills, a plugin surface. Every new tool still goes through the
classifier. No bypass.

**Do not start until M6** if the first consumer is computer-use MCP;
slash/skills can start after M4.

**M10 exit:** TD-4604.

---

## Epic E44 — MCP loading

---

### TD-4401 — Load MCP servers from config
**Size:** 8 · **Depends on:** TD-601

**Acceptance criteria:**
- [x] User-data-dir config lists stdio (and loopback HTTP) servers
- [x] Tools appear in the registry with the server as provenance
- [x] A dead server is a doctor row, not a dead daemon
- [x] Destination traces to config (`test_outbound_hosts`)
- [x] Size 8 — split at client vs registry if needed

Done (2026-08-26): `tstd.mcp` (stdio + loopback HTTP JSON-RPC) loads
`mcp.servers` from user-data-dir config, registers `{id}__{name}` with
provenance `mcp:<id>` on the existing dispatcher. Dead servers are
`mcp:<id>` doctor rows; empty config adds none. Split: client vs loader.

---

### TD-4402 — MCP tools hit the classifier
**Size:** 3 · **Depends on:** TD-4401, TD-702

**Acceptance criteria:**
- [x] No MCP tool bypasses TD-702
- [x] Missing `host_fields` / path fields fail toward B, never A
- [x] A test attempts a bypass and gets `UnclassifiedToolCall`

Done (2026-08-26): MCP provenance with no declared `path_fields` /
`host_fields` / `host_resolver` is static Class B (`mcp-undeclared-fields`),
ahead of every A grant. Dispatch with `classifier=None` still raises
`UnclassifiedToolCall`. Loader keeps empty field lists and `ask`.

---

### TD-4403 — Settings: MCP servers
**Size:** 3 · **Depends on:** TD-4401, TD-1703

**Acceptance criteria:**
- [x] Add / disable / remove a server without editing YAML by hand
- [x] Command + args only; no free-form env that could smuggle a key
      into a file (paste-a-token stays keychain)

**Done:** Settings MCP section plus `set_mcp_server` / `delete_mcp_server`.
Surgical persist of `mcp.servers` (no `env`). HTTP non-loopback refused
before write. Supervisor `reload` so doctor / next session see the list;
live sessions keep the old tool set.

---

## Epic E45 — Slash commands and skills

Complement steering. Human-written. Agent cannot write them.

---

### TD-4501 — Slash commands
**Size:** 5 · **Depends on:** TD-1004, TD-501

**Acceptance criteria:**
- [x] `.tst/commands/*.md` and `~/.tstdesk/commands/*.md` (user-global
      wins on name)
- [x] `/` in the composer lists them; insert or send (default insert)
- [x] Not steering — not in the cache prefix unless invoked
- [x] Agent writes to those trees are Class C
- [x] Fallback: `.claude/commands/` when ours is empty

**Done:** Discovery in `tstd.context.commands`. Human-path `list_commands` /
`command_list`. Composer `/` palette inserts by default. Assembler
unchanged. Writes to command trees reuse the steering-file Class C
refusal.

---

### TD-4502 — `SKILL.md`
**Size:** 5 · **Depends on:** TD-4501, TD-508

**Acceptance criteria:**
- [x] `.tst/skills/<name>/SKILL.md` (+ user-global). Frontmatter:
      `description`, `whenToUse` only
- [x] Brain gets a name+description catalog; bodies load on
      `load_skill` or slash, after the cache prefix
- [x] Over-budget skill is refused, not truncated
- [x] Agent cannot write `**/SKILL.md`
- [x] Fallback: `.claude/skills/` when ours is empty
- [x] Inspector lists loaded skills separately from steering

**Done:** Discovery in `tstd.context.skills`. Brain catalog after the
cache prefix; bodies attach on `load_skill` or `/name` (commands win on
the same stem). Over-budget bodies are refused at 4k heuristic tokens.
Any `SKILL.md` write is Class C. Inspector `instruction_stack.skills`
is a separate list, not a steering source.

---

## Epic E46 — Plugins, subagent, plan lock

---

### TD-4601 — Custom tool packages
**Size:** 5 · **Depends on:** TD-4402

**Acceptance criteria:**
- [x] A documented in-process plugin (Python entry point) can register
      tools the same way builtins do
- [x] License must be permissive; a non-permissive plugin is a Class C
      product decision, not a silent load
- [x] Architecture guide walkthrough updated (TD-1504)

**Done:** `tstd.tools` entry points call `register(registry, dispatcher)`.
License is fail-closed (MIT/Apache-2.0/BSD/ISC/Unlicense/0BSD/CC0-1.0).
GPL/empty/unknown log `plugin_license` / `non_permissive` and register
nothing. Builtin names are not replaced. Broken plugins are skipped;
`create_registry` still returns builtins. Architecture guide §5 plugin
walkthrough is executed by the doc test; dispatch still hits the
classifier.

---

### TD-4602 — One-level worker subagent
**Size:** 8 · **Depends on:** TD-402, TD-702

**Acceptance criteria:**
- [x] `delegate` runs a worker child with the parent's wall and cards
- [x] No grandchildren. Child finish is a capped summary
- [x] Cost rolls into the parent, tagged worker
- [x] Caps are the parent's
- [x] Spec §8 still stands: this is not Hermes delegation

Done (2026-08-26): Thin one-level `delegate` tool. Transient child
`Session` (`delegate_depth=1`) is not registered, persisted, or a
second window. Child tools are fs + shell only (no `delegate`).
Finish is a 2k-char capped summary. Child provider calls record on
the parent `CostTracker` with `source="worker"`. Caps are the
parent's (spend / wall-clock / remaining iterations) plus an
internal 8-iteration child bound. Not Hermes: no mesh, mailbox, or
recursive swarm.

---

### TD-4603 — Plan mode (brain lock)
**Size:** 3 · **Depends on:** TD-303, TD-1006

**Acceptance criteria:**
- [x] A Plan flag forces `brain` on every completion until cleared
- [x] `set_tier` to worker/validator is refused while on
- [x] Tools still dispatch; the meter is honest (this is expensive)
- [x] Not a plan document. Not accept-to-execute

**Done:** Plan lock lives on `TierRouter`. `set_plan {on}` acks with
`tier_state.plan`. Worker/validator `set_tier` returns `plan_mode`.
`set_tier("brain")` is a no-op so autonomy revert stays legal. In-memory
only — revive does not restore override or plan. Title-bar Plan chip
paints from the event; no plan document.

---

### TD-4604 — M10 exit
**Size:** 3 · **Depends on:** TD-4402, TD-4502

**Acceptance criteria:**
- [x] Headless: a mock MCP server contributes one tool; a slash
      command and a skill appear in the recorded prompt only when
      invoked; the MCP tool cannot skip the classifier
- [x] **This harness is the M10 exit criterion**

Done (2026-08-26): `tstd.e2e_m10` + `core/scripts/e2e_m10.py`,
pinned in CI as `tests/test_e2e_m10.py`. Headless: `MockProvider` +
a fake stdio MCP speaker (`harness__echo`), slash body and skill
body appear in recorded prompts only after invoke, MCP call carries
a decision class. Not `e2e_harness.run`. Not marked `live`.

---

# Later — Flourishes (E47)

Unversioned on purpose. Do not pull these into a milestone to "finish
the product." Web search is already TD-609/TD-610.

---

## Epic E47 — Platform furniture

---

### TD-4701 — Voice / dictation
**Size:** 8 · **Depends on:** TD-1004

**Acceptance criteria:**
- [ ] A hold-to-talk control transcribes into the composer locally or
      via a user-configured speech endpoint (config-sourced host)
- [ ] No always-on mic. No cloud default
- [ ] Off by default

---

### TD-4702 — macOS quick-entry overlay
**Size:** 5 · **Depends on:** TD-2902

**Acceptance criteria:**
- [ ] A global shortcut opens a small composer bound to the last
      workspace
- [ ] Permission copy for Accessibility if the OS requires it
- [ ] Linux/Windows are out unless cheap

---

### TD-4703 — Tray and multi-window
**Size:** 5 · **Depends on:** TD-2902

**Acceptance criteria:**
- [ ] Tray icon with running-count; Quit lives here too
- [ ] A second window can attach to a different session
- [ ] One daemon

---

### TD-4704 — Auto-updater
**Size:** 5 · **Depends on:** TD-1303

**Acceptance criteria:**
- [ ] Opt-in check against GitHub releases (user-initiated or a
      stated interval)
- [ ] No telemetry. Signature story recorded (may still be unsigned)
- [ ] Off by default until signing exists

---

### TD-4705 — Image / vision attachments
**Size:** 5 · **Depends on:** TD-1709

**Acceptance criteria:**
- [ ] Images attach when the active brain/worker advertises vision
- [ ] Capability detected, not assumed; a text-only model refuses
      with copy
- [ ] Caps apply. No silent downscale that hides a secret

---

### TD-4706 — Mermaid, LaTeX, user-bubble markdown
**Size:** 3 · **Depends on:** TD-1603, TD-3202

**Acceptance criteria:**
- [ ] Vendored mermaid + KaTeX; no CDN
- [ ] Failed parse falls back to the fence
- [ ] Bundle delta in `DECISIONS.md`
- [ ] Images in markdown wait for TD-4705

---

### TD-4707 — Extra notify channels
**Size:** 3 · **Depends on:** TD-3801

**Acceptance criteria:**
- [ ] Discord and/or Telegram as the same `send` module
- [ ] Slack remains the default
- [ ] 20-platform gateway stays refused (spec §8)

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
| R9 | **Ollama embed evicts the chat model** | 20–48s stalls, "memory is broken"** | Embeddings are a sidecar (`/v1/embeddings`). Heading-match is the M4 floor. TD-2701 does not require the sidecar |
| R10 | **A decomposed later phase gets built mid-M4** | Never ships v0.2 | Stories exist so they can be sequenced, not started. `AGENTS.md` §3 still wins. Each later milestone has an exit harness |

---

# Summary

| Milestone | Epics | Stories | Points |
|---|---|---|---|
| M0 Foundation | E1 | 7 | 15 |
| M1 Headless core | E2–E9 | 55 | 162 |
| M1.5 Local models | E18 | 15 | 36 |
| M2 The window | E10–E12 | 24 | 68 |
| M3 Shippable | E13–E17, E19 | 47 | 136 |
| **Total v0.1** | **19** | **148** | **417** |
| M4 Memory (v0.2) | E21–E28 | 32 | 90 |
| **Total v0.1 + v0.2** | **27** | **180** | **507** |
| M5 Cowork (v0.3) | E29–E32, E48 | 31 | 84 |
| M6 Computer use (v0.4) | E20, E33–E34 | 12 | 63 |
| M7 Remote (v0.5) | E36–E38 | 11 | 43 |
| M8 Local remainder (v0.6) | E39 | 4 | 19 |
| M9 Autonomy (v0.7) | E40–E43 | 14 | 68 |
| M10 Extensibility (v0.8) | E44–E46 | 9 | 43 |
| Later | E47 | 7 | 34 |
| **Total planned** | **47** | **268** | **861** |

Points are relative sizing for sequencing and splitting decisions, not a schedule. Do not
convert them to dates.

Recompute this table from the story headings and their `**Size:**` fields when you add or split
a story; do not increment the total by hand. Hand-incrementing is how it drifted to 110/338
against an actual 115/347.

**The single most important sequencing rule in this document: M1 exits when TD-1401 passes.**
A correct, tested, headless core is what makes the UI a presentation problem instead of a
debugging problem.

---


## Epic E20 — Linux support for the computer-use MCP server

**Milestone: M6.** Counted in the M6 totals. Still cannot be verified off a Linux box.

**Goal:** `mcp/tst-cu-mcp` runs on Linux, or says clearly and early that it cannot.

**Filed as a new epic, not folded into E17.** E17's TD-1710 is the *product's* computer-use
feature — the Screen pane and the `tst-cua` drivers under `core/`. This epic is the **developer
tooling** under `mcp/`, which had no backlog home at all until now. That gap was flagged twice
while shipping v0.2.0 of the server and is closed here. If the two ever merge, TD-1710 is the
place they meet.

**This work cannot be done or verified on Windows or macOS.** It needs a Linux session in front
of it. Filed so the reconnaissance below is not repeated, not so it can be started remotely.

### Current behaviour on Linux

`backends/__init__.py` includes `"linux"`. Under a native X11 session `health` reports
`supported: true` and `session_type: "x11"`. A Wayland session reports `supported: false`
with `session_type: "wayland"` (TD-2002). The Desk live path accepts Linux; first-run
onboarding is platform `"linux"`.

**One rough edge worth fixing regardless of whether the backend gets built:** the wheel
*installs* on Linux. It is pure-Python `py3-none-any`, the PyObjC dependencies are gated behind
`sys_platform == 'darwin'`, and the Windows backend adds no dependency at all. So `pip install`
succeeds and the first tool call is where the user finds out. Platform classifiers or an
import-time check would move that discovery to install time.

### Why this is two backends, not one

**X11 is tractable** — comparable in shape and size to the Windows backend (~700 lines):

| Protocol method | X11 mechanism |
|---|---|
| `list_displays` | XRandR |
| `capture_png` | `XGetImage`, or MSS |
| `move_mouse` / `click` / `scroll` | XTest (`XTestFakeMotionEvent`, `XTestFakeButtonEvent`) |
| `type_text` / `press_keys` | XTest (`XTestFakeKeyEvent`) plus keysym mapping |
| `cursor_position` | `XQueryPointer` |
| `foreground_window` | `_NET_ACTIVE_WINDOW` via EWMH |
| `check_permissions` | No gate exists — X11 has essentially no security boundary here |

**Wayland is a different problem, not more of the same.** Wayland deliberately forbids what this
server does: no unprivileged client may read the screen or synthesise global input. Capture
requires `xdg-desktop-portal` ScreenCast over PipeWire with per-session consent; input requires
the portal RemoteDesktop interface or libei.

The hard part is `foreground_window`. **No general Wayland protocol exposes the active window to
an unprivileged client.** That is the method the `expect_window` guard depends on — the mechanism
that stops a click landing in the wrong application after the UI moves. On Wayland that guard is
likely unimplementable through Wayland alone, which means a Linux port either accepts a weaker
safety guarantee there or finds the answer elsewhere.

This matters because modern distributions default to Wayland: Ubuntu since 21.04, Fedora, RHEL 9.
X11-only support covers X11 sessions, older distributions, VNC and remote setups, and Xvfb
containers — real, but shrinking.

### Prior art already in the tree

TD-102's `docs/REUSE.md` classifies an **AT-SPI driver** from `tst-cua` as intact but deferred to
v0.4. AT-SPI is the Linux accessibility bus, works under both X11 and Wayland, and could supply
element-level targeting *and* possibly active-window information — which would answer the Wayland
`foreground_window` problem above.

That is the same accessibility-tree approach identified during the Windows port as the real fix
for coordinate brittleness, rather than a third copy of the fragile approach. **Read the existing
AT-SPI driver before writing anything.** Linux may be where the robust design lands first.

---

### TD-2001 — X11 backend for `tst-cu-mcp`
**Size:** 8 · **Depends on:** TD-102

Implement the `Backend` protocol for X11 and add `"linux"` to `SUPPORTED_PLATFORMS`. Follow the
structure the Windows backend established: platform code confined to the backend, policy
(kill-switch, argument validation, focus guard) staying in `input_control` where a new platform
cannot skip it.

**Acceptance criteria:**
- [x] `docs/REUSE.md`'s AT-SPI driver read and assessed first, with a written statement of what
      it can supply that raw X11 cannot — before any code is written
- [x] Every `Backend` protocol method implemented; `mypy --strict` clean
- [x] Display enumeration reports true pixel bounds on a multi-monitor layout, including a
      display at a negative origin
- [x] Capture returns a valid PNG for a full display and for a sub-region, without writing to disk
- [x] Coordinate round-trip verified: `move_mouse` then `cursor_position` agree within tolerance
      at several points across every display
- [x] `foreground_window` returns a title and a process name for a real window
- [x] `expect_window` refuses on a mismatch **without actuating** — the same assertion the
      Windows suite makes, since a guard that refuses after acting is worse than none
- [x] Kill-switch halts actuation while capture and state reads keep working
- [x] A `test_desktop_linux.py` tier mirroring `test_desktop_windows.py`, marked `desktop`, that
      moves the pointer and restores it and **does not type**
- [x] Keystroke tests live in their own module behind `TST_CU_MCP_ALLOW_INTRUSIVE_TESTS`, never
      reachable by marker selection alone (see the 2026-08-18 correction in `DECISIONS.md`)
- [x] `health` reports `supported: true` under X11
- [x] Behaviour under Wayland is explicit, not accidental — either a clean refusal or whatever
      TD-2002 decides

**Notes:** The Windows port's two live-use findings are likely to have X11 analogues worth
checking early: a key that only works with the right flag set (`VK_LWIN` needed the extended
flag), and input silently discarded by a window that has focus but is not yet ready. Neither was
found by review.

**Completed (2026-08-24):** AT-SPI assessment recorded in `DECISIONS.md` first.
`LinuxBackend` implements the full `Backend` protocol via ctypes against
libX11/libXrandr/libXtst and Pillow `ImageGrab`. Negative-origin layouts are
pinned in `test_linux_geometry.py`; live capture/cursor/foreground ran on an
X11 XFCE host (`pytest -m desktop`). Wayland is an explicit health refusal.
The Desk live path accepts Linux; `cu_permissions.platform` is `"linux"`.

---

### TD-2002 — Decide the Wayland strategy
**Size:** 3 · **Depends on:** TD-2001

A decision story, not an implementation one. Wayland cannot be served by extending the X11
backend, and the choice affects what safety guarantees the server can honestly claim.

**Acceptance criteria:**
- [x] Portal-based capture (`xdg-desktop-portal` ScreenCast over PipeWire) and input (portal
      RemoteDesktop or libei) each assessed for feasibility, with the consent flow described
- [x] A definite answer on whether `foreground_window` is obtainable under Wayland — via AT-SPI,
      via a compositor-specific protocol, or not at all
- [x] If it is not obtainable: a stated position on whether `expect_window` degrades, refuses, or
      is unavailable there, since silently weakening a safety guard is not an option
- [x] Recommendation recorded in `DECISIONS.md` as a Class B or C decision with rationale
- [x] Scope and size for the implementation work, or an explicit decision not to support Wayland
      and what `health` should then report

**Notes:** Resist implementing while investigating. The output of this story is a decision and a
size, and the honest answer may be "X11 only, Wayland reports unsupported".

**Completed (2026-08-24):** Class B in `DECISIONS.md`. Portals are
feasible on GNOME/KDE (ScreenCast + PipeWire; RemoteDesktop + libei);
wlr ScreenCast is common and RemoteDesktop is not. `foreground_window`
is not compositor-neutral. `expect_window` refuses when focus cannot
be read — never degrades. Wayland stays unsupported;
`health` reports `supported: false` / `session_type: "wayland"`. A
later epic, if any, is size 13 and is not filed now.

---


## Epic E48 — Security-review hardening

**Milestone: M5.** Counted in the M5 totals. Filed as a new epic, not scattered across
E6/E7/E9/E10/E14: every story here came out of one pass — the full-repo security and
consistency review of 2026-08-21 — and the stories read best with that provenance kept in
one place (the E20 precedent). No existing epic owns "defects found by auditing what already
shipped."

Several are defects against promises the README makes in public: redaction that misses the
key formats this app actually stores, a steering boundary that a case-insensitive filesystem
walks around, a policy file the agent can rewrite to promote its own autonomy. Per §7 a gap
in boundary enforcement is a defect, not a missing nice-to-have. TD-4802 through TD-4806
should land before any v0.3 tag; that gate is advisory and the sequencing call is the
maintainer's — M5's exit condition is unchanged.

TD-4801 is the cheap batch — one-line defects with tests — and ships with the filing.

**Goal:** every promise the project makes in its README, spec, and prime directives is
enforced in code at the strength the prose claims.

### TD-4801 — Review quick fixes: key-shape redaction, token comparison, notification redaction, backoff precedence
**Size:** 2 · **Depends on:** TD-1405

**Acceptance criteria:**
- [x] `SECRET_PATTERNS` matches the dashed key forms the shipped presets can hold
      (`sk-or-v1-…`, `sk-proj-…`, `sk-ant-…`), proved by fakes in the security suite's
      table that the old pattern misses
- [x] The credential-hygiene canary is reshaped to a dashed OpenRouter-style key, so the
      end-to-end test exercises the format the app actually stores
- [x] The pre-commit hook's `sk-` pattern covers the same dashed forms
- [x] `validate_token` compares with `hmac.compare_digest`
- [x] OS notification bodies pass through the UI `redact()` belt, pinned by a test
- [x] Reconnect backoff honors a custom `baseBackoffMs` exponentially —
      `(base ?? 500) * 2 ** attempt` — pinned by a test that goes red under the old
      operator precedence

Found 2026-08-21. The UI's own `redact.ts` already documented the core gap in a comment —
"core requires 20+ alphanumerics with no dashes, which misses those" — and compensated
locally. The belt knew about the suspenders' hole. The backoff bug is a precedence slip:
`baseBackoffMs ?? 500 * 2 ** attempt` parses as `base ?? (500 * 2**attempt)`, so any
configured base is applied flat and the exponential growth only exists for the default.
The existing tests never assert the second retry's delay, so the suite stayed green.

**Completed (2026-08-21):** pattern widened to `sk-[a-zA-Z0-9][a-zA-Z0-9_-]{15,}` matching
the UI belt's shape; three dashed fakes added to the security-suite table; canary reshaped;
hook pattern updated; `compare_digest` on UTF-8 bytes; `redact()` wraps notification copy;
backoff parenthesized and pinned by a second-interval test.

### TD-4802 — Redaction chokepoint skips three event types, extra fields, and tracebacks
**Size:** 3 · **Depends on:** TD-1405

**Acceptance criteria:**
- [x] `ApprovalRequest.summary`, `DecisionLogged`, and assistant text deltas pass through
      the redactor before the event log — or each gets a documented, test-enforced argument
      for why its shape cannot carry a secret
- [x] `AuditStore.append_decision` scrubs its payload the way `append` does
- [x] `JSONFormatter` redacts `extra_fields` values and formatted tracebacks, not just the
      message and args
- [x] A key-shaped string planted in each path above never reaches disk or the wire raw,
      proved by tests

TD-1405 redacts at event-log insertion but only for the event shapes it enumerated.
`ApprovalRequest.summary` is model-adjacent free text that also lands in OS notifications;
`extra_fields` are merged into the log record after the redaction filter has run;
tracebacks are formatted straight into the JSON line. Each is a narrow hole, but the
README's claim is categorical, so the holes are defects.

**Completed (2026-08-21):** the per-type field list became a whole-event scrub —
`_redact_event` runs every string field of every event type through `redact_structure`,
with the path-not-bytes contract documented for future payload-carrying events. The
connection-scoped reply path (`session_list` titles, `memory_files` contents — user text
that never rode the log) got its own chokepoint: `_handle_message` funnels every reply
through `scrub_wire_json`. `append_decision` and the `JSONFormatter` last mile
(`extra_fields`, tracebacks) scrub too. The drift guard is an enumeration test that plants
a canary in every string field validation accepts on every `DaemonEvent` subclass — a new
event type that skips the scrub fails red the day it is added.

### TD-4803 — `.tst/config.yaml` is agent-writable, so the agent can rewrite its own guardrails
**Size:** 2 · **Depends on:** TD-706

**Acceptance criteria:**
- [x] The fs write tool refuses `.tst/config.yaml` the way it refuses steering files
- [x] The daemon's own config-write path (user-initiated from the UI) still works
- [x] A test drives a policy-poisoning write (e.g. setting `class_c_default: auto`) through
      the fs tool and asserts refusal

The approval policy lives in `.tst/config.yaml`. `is_steering_write` covers `AGENTS.md`,
`CLAUDE.md`, and `.tst/rules/`, but not the policy file, and `writable_paths` defaults to
`**`. A session that can write its own policy can promote Class C to `auto` — the
self-escalation the steering-file rule exists to prevent, through the side door. Spec §6
has the UI owning config writes, so the refusal targets the agent's fs tool, not the
daemon's config path.

**Completed (2026-08-21):** `is_steering_write` refuses `_POLICY_FILE_PARTS`
(`.tst/config.yaml`, case-folded) alongside the rules dir — one chokepoint feeds both the
classifier (Class C, `steering-file-write`) and the guard (`steering_file` refusal). The
daemon's scaffold path writes directly and is pinned unaffected; a dispatcher-level test
drives the `class_c_default: auto` poisoning write and asserts the file survives.

### TD-4804 — Steering-directory check is case-sensitive on case-insensitive filesystems
**Size:** 2 · **Depends on:** TD-602

**Acceptance criteria:**
- [x] The `.tst/rules/` prefix comparison case-folds, so `.tst/RULES/…` (any case variant)
      is refused as a steering write
- [x] Over-refusal on case-sensitive filesystems (a literal `.tst/RULES/` directory is
      treated as steering there too) is accepted and documented in the code
- [x] Tests drive the comparison with case variants; the memory carve-out folds the same
      way so `.tst/MEMORY/…` stays memory, not steering

`is_steering_write` upper-cases basenames but compares the rules-dir tuple verbatim. On
APFS and NTFS — both default case-insensitive — a write to `.tst/RULES/evil.md` lands in
`.tst/rules/` while the guard sees a different path and allows it. The primary dev platform
is macOS, so the shipped-default filesystem is the vulnerable one.

**Completed (2026-08-21):** `_fold()` case-folds the guard's directory comparisons
unconditionally, documented in the code. Parametrized variants cover `RULES`/`Rules`/`rUlEs`
and `MEMORY`/`Memory`; the basename check still wins under a case-variant memory dir; the
classifier and guard both refuse `.tst/RULES/evil.md`.

### TD-4805 — Shell tool hardening: steering-aware classification, env filter gaps, allowlist honesty
**Size:** 3 · **Depends on:** TD-605, TD-703

**Acceptance criteria:**
- [x] A shell command that writes a steering path (`echo … > AGENTS.md`,
      `tee .tst/rules/x`) classifies Class C statically, not via model judgment
- [x] `sanitized_env` also drops the missed secret carriers (`DOCKER_AUTH_CONFIG`,
      `MYSQL_PWD`, `*_AUTH`, credential-URL forms), with a test per form
- [x] The `allowed_commands` docstring and user docs name the known escape hatches
      (`find -exec`, `xargs`, `sh -c`) instead of implying containment
- [x] Whether worker-tier shell calls get a static Class B floor is decided and recorded in
      `DECISIONS.md` either way

The fs tools get path-boundary enforcement; the shell tool gets none, so
`echo rule > .tst/rules/x.md` is only as safe as the classifier's reading of the string.
The allowlist checks each segment's leading binary, which `find / -exec …` walks through.
And the env filter's name list predates several common secret carriers. None of these is
a sandbox break — the docstring already says the allowlist is a policy rail — but the
docs and the classifier should say and do exactly what is true.

**Completed (2026-08-21):** two static rules — `shell-steering-write` (C: redirection or
`tee` into a steering path, extracted with `shlex` punctuation tokenization) and
`shell-floor` (B: every shell call asks; the worker tier is never consulted for shell, so
no model-granted A can auto-run an opaque command — recorded in DECISIONS.md). The env net
gained `PWD` carriers, `AUTH` as a word (`GIT_AUTHOR_*` and `SSH_AUTH_SOCK` survive, with
the reasoning in the code), and a value-shape check for credential-embedded URLs. The
allowlist docs name the re-exec hatches in `shell.py` and `configuration.md`.

### TD-4806 — Rendered markdown links navigate the webview away from the app
**Size:** 2 · **Depends on:** TD-1004

**Acceptance criteria:**
- [x] Links in rendered assistant markdown are rewritten (`target="_blank"`,
      `rel="noreferrer"`) and clicks route through the Tauri opener, never webview
      navigation
- [x] Non-http(s) schemes (`javascript:`, `data:`, `file:`) are dropped by the renderer
- [x] A UI test renders a markdown link and asserts the click reaches the opener bridge,
      not `window.location`

`Markdown.svelte` wires copy buttons in its click handler but does nothing for anchors, so
a model-emitted link navigates the app webview to an arbitrary external site — the app
window becomes a browser with no chrome, and the session UI is gone. DOMPurify already
sanitizes the HTML; the gap is navigation, not injection.

**Completed (2026-08-21):** a DOMPurify `afterSanitizeAttributes` hook rewrites anchors —
http(s) gets `target="_blank" rel="noreferrer noopener"`; every other scheme and relative
hrefs lose `href` and render as inert text. The delegated click handler in
`Markdown.svelte` preventDefaults anchor clicks and routes them through the opener
plugin's `openUrl`. Tests pin the rewrite, the per-scheme drops, the click reaching the
opener mock with default prevented, and that copy buttons are undisturbed.

### TD-4807 — The webview ships with no CSP and form tags survive sanitization
**Size:** 2 · **Depends on:** TD-1002

**Acceptance criteria:**
- [x] `tauri.conf.json` sets a content security policy locked to what the app uses
      (self, the localhost WS, inline styles as needed) and the app still renders —
      highlight.js, fonts, and the socket all verified — **mechanism amended:** the
      policy lives in `ui/vite.config.ts` as `kit.csp` (hash mode), emitted as a
      build-time meta tag; `tauri.conf.json` stays `csp: null`. See DECISIONS
      2026-08-21. Render verified by dev smoke (screenshot: chrome, highlight.js,
      serif fonts, copy control, socket connected).
- [x] The DOMPurify config forbids `form`, `input`, and `button` tags so assistant text
      cannot render a fake approval form — plus `textarea`, `select`, `option`,
      `optgroup`; the code-block copy control became a `<span>` so the forbid list
      is categorical
- [x] Tests pin the tag list; the CSP is verified by a manual smoke pass and noted —
      `csp-config.test.ts` pins every directive and the `csp: null` guard; the
      emitted meta's sha256 was verified against the built bootstrap; dev-mode smoke
      passed. A full packaged-.app smoke remains a pre-release manual step.

`csp: null` in the Tauri config means any successful injection runs with full webview
privileges. DOMPurify's default profile allows form elements, and the app renders
model-controlled markdown next to real approval cards — a lookalike form is a phishing
surface inside the trust boundary.

**Completed (2026-08-21):** `kit.csp` hash mode emits
`default-src 'self'; script-src 'self' + per-build bootstrap hash; style-src 'self'
'unsafe-inline'; connect-src 'self' ws://127.0.0.1:* ipc://localhost (+ ws://localhost:*
in dev); img-src 'self' data:; font-src 'self'; object-src/base-uri/form-action 'none'`.
Tauri's bridge is unaffected (native init scripts; verified in tauri-2.11.5 source).
DOMPurify `FORBID_TAGS` drops the form family categorically; tests pin each tag, the
fake-approval-form degradation, copy-control survival, and every CSP directive.

### TD-4808 — `network: deny` never reaches the web tools; `side_effect_class` is dead metadata
**Size:** 2 · **Depends on:** TD-609, TD-610

**Acceptance criteria:**
- [x] Either the web tools declare host fields the classifier enforces `network: deny`
      against, or the policy surface stops advertising a rail that is not wired —
      wired: `web_fetch` declares `host_fields=("url",)` with URL→host reduction;
      `web_search` declares a `host_resolver` reading `search.base_url` at
      classification time
- [x] `side_effect_class` is enforced in classification or removed from the schema —
      enforced as a floor: `never` → C ahead of every grant, `ask` → terminal B
      floor (DECISIONS 2026-08-21)
- [x] Tests for whichever way each goes — floor placement and precedence in
      test_classifier.py; end-to-end wiring, host reduction, config-host resolution,
      and a declaration drift guard in test_security_suite.py

A policy rule can say `network: deny` and the web tools will still fetch — nothing carries
the rule to the tool. Dead rails are worse than absent ones: the config reads as though
a guarantee exists. Same shape as TD-1410's argument: a promise that holds by construction
until the day it silently doesn't.

**Completed (2026-08-21):** both rails wired. `network: deny` now refuses `web_fetch`
and `web_search` (Class C via `network-new-host`); an allowlisted host asks (Class B via
the new floor). `side_effect_class` is enforced as a floor — specific grants
(in-workspace edit, memory write) keep their deliberate Class A. Shell egress remains
ungated by `network` (the classifier cannot see inside a command string) — documented in
configuration.md §4.6. Browser CU tools keep their `actuates` rail unchanged.

### TD-4809 — Shell host: release builds honor TSTD_PATH, Windows open_path is cmd-injectable, externalBin lives in an npm flag
**Size:** 2 · **Depends on:** TD-1301

**Acceptance criteria:**
- [x] `TSTD_PATH` is honored in debug builds only (or renamed `TSTD_DEV_…`) — a release
      binary must run its bundled sidecar, not whatever the environment names
- [x] Windows `open_path` no longer builds a `cmd /C start` command line from an
      unsanitized path (opener plugin's path API, or validate and refuse metacharacters)
- [x] `externalBin` is declared in `tauri.conf.json` so the sidecar bundling cannot drift
      between the npm script and CI
- [x] A Rust test pins the release-build refusal; the Windows change is verified by review
      and noted (no Windows CI)

An environment variable that swaps the daemon binary is a dev convenience; honored in a
release build it is a local-privilege hook into every session. The `cmd /C start` path
concatenation is injectable with `&`-class metacharacters. The `externalBin` injection
works today but lives in a quoted npm flag — invisible to anyone reading the Tauri config.

**Completed (2026-08-22).** `resolve_with` gained a `dev_overrides` flag
(`resolve_command` passes `cfg!(debug_assertions)`); dev-only resolution is also compiled
out of release via cfg. Dev order: `$TSTD_PATH` → core venv → `uv run` (TD-1304 kept:
dev always resolves) and deliberately *ahead* of the sidecar, so once externalBin ships a
packaged binary next to `target/debug`, `tauri dev` still tracks the checkout instead of
latching onto a stale sidecar. Release order: sidecar → `tstd` on PATH → refuse, with the
error no longer advertising `TSTD_PATH`. Tests: five existing call sites pass the flag;
new pins are the release refusal with `TSTD_PATH` set, sidecar-over-env precedence in
release, and the release PATH fallback (absorbing old `which_wins_over_uv_fallback`,
whose ordering no longer exists in dev).
`open_path` now calls the opener plugin already registered on the app
(`app.opener().open_path(path, None::<&str>)`); the hand-rolled three-platform spawn,
including Windows' injectable `cmd /C start "" <path>`, is deleted. Review-verified only
(no Windows CI): the plugin's path API hands the OS opener the path as a single argument,
never through a shell.
`bundle.externalBin: ["binaries/tstd"]` now lives in shell/tauri.conf.json; tauri:build
dropped its `--config` override. Discovery worth keeping: tauri-build validates
externalBin paths at compile time, so **every** `cargo` build of the shell crate needs
`shell/binaries/tstd-<host triple>` to exist — that is why the declaration could only live
in a flag `tauri build` saw before. Coverage: beforeBuildCommand builds it;
beforeDevCommand runs `build_sidecar.py --if-missing` (first `tauri:dev` pays a one-time
PyInstaller build); ci.yml's rust job creates an empty sentinel in `binaries/` (the job's
working-directory is already `shell/`) before clippy/test; bare
`cargo` in a fresh clone needs one sidecar build or the same sentinel (gitignored path,
never dirties the tree). `build_sidecar.py` grew `--if-missing` plus a `target_path()`
helper. Owed: a human `npm run tauri:dev` and packaged-launch pass.

### TD-4810 — daemon_supervision tests race on process-wide TSTD_PATH
**Size:** 1 · **Depends on:** TD-1301

**Acceptance criteria:**
- [x] The env-mutating test serializes (or receives the path explicitly), so parallel
      `cargo test` cannot interleave the mutation with another test's `spawn_daemon`
- [x] The flake signature — clean-shutdown test fails because the port file survives —
      is reproduced by forcing the interleaving, then fixed
- [x] 10 consecutive full Rust suite runs with no failure

Observed 2026-08-21: `spawns_daemon_connects_via_port_file_and_shuts_down_cleanly` failed
on a full-suite run and passed in isolation. `onefile_shape_attaches_and_group_kill_reaps_grandchild`
mutates process-wide `TSTD_PATH`; under parallel test threads that mutation can land inside
another test's daemon spawn. Same disease TD-1409 treated: a test whose outcome depends on
what else is running.

**Completed (2026-08-22).** Two defects, and the one behind the observed flake turned out
not to be the one the entry named. The env mutation is gone regardless:
`spawn_daemon_with(data_dir, argv)` is the new test seam (`spawn_daemon` delegates to it
with `resolve_command()`), and the onefile-shape test passes its wrapper argv explicitly
instead of pointing process-wide `TSTD_PATH` at it around the spawn. Forcing that
interleaving deterministically did contaminate a sibling — an instrumented victim asked
for the venv daemon and got `/bin/sh` — yet still passed: process groups are per-spawn
(`process_group(0)`), so the neighbor's group kill never crosses, which meant the flake
needed something else. Instrumented autopsies over looped suite runs (9 failures in ~55,
every one a `wait_for_port_file` timeout while the daemon sat alive and healthy and its
watched directory was *gone*) found it: `make_dir()` built names from `pid + nanos`, the
macOS clock resolves to exactly 1 µs (verified — consecutive samples step by 1000 ns),
so co-starting tests computed the same data-dir path, two daemons then fought over one
`port.json` ("stale port file detected, replacing"), and whichever test finished first
deleted the shared dir out from under the other. A survivor's stale port file in the
shared dir is precisely the originally reported "port file survives" shape; TSTD_PATH
was guilt by participation, not cause. Fix: dir names carry a per-process `AtomicU64`
counter, so collision is impossible regardless of clock resolution;
`embeddings_supervision` already used unique per-test tags and needed nothing.
Criterion 3: ten consecutive green full-suite runs post-fix.

### TD-4811 — README Status denies shipped M4 features
**Size:** 1 · **Depends on:** TD-1501

**Acceptance criteria:**
- [x] The Status section reflects M4's exit (memory, session revive) and M5's in-flight
      state
- [x] Every "not yet" claim in the README is audited against the backlog's completed
      stories
- [x] The docs test that pins README numbers also pins the status claims that can be
      machine-checked

The README still says memory and detached sessions are not built. M4 exited; the brain
prompt carries a memory subset and sessions revive from disk. The README is the project's
public face — the one document whose claims a stranger acts on — and it currently
under-sells what the tests prove.

**Completed (2026-08-22):** reality had outrun the story text — M5–M8 and TD-4001
are on this tip, not in-flight. Status now claims memory + session revive, cowork /
`tst run` / close-vs-quit, computer-use + kill switch, Tailscale remote attach +
notify/scheduler, the `vllm` preset + UI-TARS grounding, and `CHARTER.md`
validation. The not-built list is the M9 remainder (TD-4002+), M10 (MCP loading /
slash / `SKILL.md` / plan lock), Linux desktop CU (TD-2001/2002), desktop Design
AX (TD-3406), and green installers (TD-1302/1303 already named in the release
sentence) — not remote attach and not vLLM. `test_docs_readme_numbers.py` pins
works vs not-built to *this checkout's* exit-harness boxes so a frozen fork table
cannot reintroduce a false M7/M8 gap.

### TD-4812 — Process debt: unrecorded M4-gate call, stale spec sections, release-tag collision, stale kickoff prompt
**Size:** 2 · **Depends on:** none

**Acceptance criteria:**
- [x] `DECISIONS.md` records who authorized starting M4 with M3's gate open, and why —
      marked as a retroactive record
- [x] Spec §10's open decisions are marked answered with pointers to their `DECISIONS.md`
      entries, and §9's phasing reflects the re-plan
- [x] The `v0.2.0` tag collision is closed — rename the `tst-cu-mcp` tag to
      `tst-cu-mcp-v0.2.0`, or narrow `release.yml`'s trigger pattern
- [x] `tst-desk-kickoff-prompt.md` is updated to the `docs/` paths or deleted; `AGENTS.md`
      §11's layout is refreshed (maintainer edit — steering files are read-only to agents)

**Completed (2026-08-22):** The M4-gate record went in as a retroactive `DECISIONS.md`
entry that states plainly what the evidence supports: no contemporaneous approval note
exists anywhere, the call survives only in the backlog's own 2026-08-19 re-plan of the M4
header (no gating language on M4; the earliest "do not start until" is M5's), attribution
to the product owner follows from that document being his planning surface, and the entry
asks to be corrected if that inference is wrong. The why is evidenced: every blocked box
needs hardware this environment lacks, so feature lanes ran while the manual boxes stay
visibly unticked. Spec §10 turned out to need no archaeology — all seven items were
answered at kickoff by DECISIONS' TD-101 entry, and each now carries its inline pointer;
§9 gained a re-plan note naming the three divergences (M1.5/M8 split, extensibility added
as backlog M10/v0.8, project home joining M4). For the tag collision this lane took the
narrow-trigger horn and left the published tag alone: renaming a pushed tag that a GitHub
Release references is remote surgery on shared state, while namespacing app releases to
`tstdesk-v*` costs nothing (no app tag exists yet) and closes the hazard class — package
tags can never fire the app workflow again. release.yml's trigger and its version-strip,
changelog.py's default range (which would otherwise diff across a package tag), TD-1303's
note, and the README sentence all moved together. The kickoff prompt kept its provenance
with a dated banner and corrected paths rather than deletion, and AGENTS.md §11's layout
now shows docs/'s actual contents and tstd's load-bearing additions since kickoff.

M4 started while M3's exit condition was open. That may have been the right call, but the
decision is not in the log, and the log is the project's memory for exactly this kind of
call. Separately: `tst-cu-mcp` tagged `v0.2.0` in a repo whose `release.yml` fires on
`v*` — a namespace collision that can trigger a TST Desk release from the server's tag.

### TD-4813 — macOS keychain write exposes the API key in the process list
**Size:** 2 · **Depends on:** TD-1102

**Acceptance criteria:**
- [x] The macOS backend stops passing the secret as `security -w <argv>` (Security
      framework bindings, or another argv-free path)
- [x] A test or documented manual check asserts the secret does not appear in `ps` during
      `set_api_key`
- [x] Linux and Windows backends audited for the same exposure and cleared or fixed

`security add-generic-password -w <secret>` puts the key in the process's argument list,
readable by any local process via `ps` for the lifetime of the call. Short window, real
exposure — and prime directive §2.2's "no secret is ever written to a log" spirit covers
the process table too. The Windows backend already uses proper bindings; macOS should too.

Closed with `tstd/keychain_macos.py`: the write goes through `SecItemAdd` via raw ctypes
against the system frameworks (the Windows backend's zero-dependency precedent), passing
the secret as in-memory CFData — no subprocess on the write path at all. Reads and deletes
stay on the CLI, whose argv carries only account/service names. The audit cleared both
other backends: Linux pipes the secret to `secret-tool` over stdin, Windows hands a blob
to `CredWriteW`; neither ever places it in argv. `test_no_spawned_argv_carries_the_secret`
records the argv of every process the store flow spawns and fails if the key shows up —
reverting to the CLI add trips it exactly where `ps` would have read.

### TD-4814 — web_fetch's SSRF check is a DNS-rebinding TOCTOU
**Size:** 2 · **Depends on:** TD-610

**Acceptance criteria:**
- [x] The resolved address is pinned into the connection (resolve-and-connect or a custom
      transport), so the check-then-fetch window closes — or the limitation is documented
      and the private-address check re-runs at connect time
- [x] A test with a double-flip DNS stub proves the fix, or pins the documented limitation

`web_fetch` resolves the host, checks the address is not private, then connects — and the
second resolution is free to answer differently. The guard is real against honest mistakes
and bypassable by a hostile DNS answer. Worth closing properly or labeling honestly.

**Completed (2026-08-22):** closed properly — both horns of the criterion at once, since
the fix IS resolve-and-connect *and* a connect-time check. `_GuardedBackend`, an
`httpcore.AnyIOBackend` subclass wired in through a rebuilt `httpcore.AsyncConnectionPool`
inside an `httpx.AsyncHTTPTransport` subclass (httpx exposes no network-backend parameter,
so the pool built by the stock constructor is swapped for one behind the guarded backend),
resolves the host itself, refuses any loopback/link-local/multicast/unspecified answer via
the same `_addr_refusal` policy function the handler's pre-check uses (one statement of
the wall; the two checks cannot drift), and pins the dial to an address it validated.
There is no second resolution left to poison: the backend's resolution is the only one,
and TLS still verifies the URL's hostname because httpcore applies SNI and certificate
checking above the stream it hands back. The handler keeps its per-hop pre-check for
fast, friendly refusals and literal-IP/scheme/hostname screening; redirects were already
re-checked per hop and now each hop's connection passes the guarded backend too. The
double-flip stub stages the real attack end to end — `socket.getaddrinfo` fed a clean
public lie while the injected resolver flips to loopback, against a live HTTP server on
127.0.0.1 counting hits — and asserts zero hits, an error result, and exactly one
resolution. Both tests were probe-verified against the unfixed transport (fetch reaches
the rogue server) before being accepted as green with it.

### TD-4815 — tst-cu-mcp: actuation switch coercion, unbounded click count, trust-boundary docs
**Size:** 2 · **Depends on:** none

**Acceptance criteria:**
- [x] The actuation-enabled config parses quoted YAML booleans (`"true"`/`"false"` strings)
      fail-closed, not fail-open
- [x] `click` count is bounded; absurd counts are refused
- [x] The README documents the no-approval-gate trust model — any local process that can
      reach the server can drive input — as a stated posture, not an oversight

The actuation kill-switch reads a config value that YAML can deliver as a string, and the
comparison fails open: a quoted `"false"` still enables input actuation. The server is a
developer tool with OS-level permissions; its safety switches have to survive config
dialects. Filed here rather than in E20, which owns the server's Linux port.

Closed in `config._actuation_flag`: real booleans pass through, recognized quoted
spellings coerce to their literal meaning (a quoted `"false"` now disables), and an
unrecognizable value stops startup instead of guessing at a safety setting.
`MAX_CLICK_COUNT = 100` joins the text/scroll bounds; the README Safety section states
the trust model — anything that can reach this server can drive your machine — as the
design, with the gates listed as what you set up around it.

### TD-4816 — Low-severity review follow-ups
**Size:** 3 · **Depends on:** none

**Acceptance criteria:**
- [x] `events.jsonl` is created with restrictive permissions (no create-then-chmod window)
- [x] The git tool sanitizes its child environment the way the shell tool does
- [x] The audit schema docstring stops claiming `DROP TABLE` is impossible, or makes it true
- [x] Tauri `opener:allow-open-path` is scoped, or documented as intentionally unscoped
- [x] The UI validates inbound daemon events against the union before dispatch, or documents
      the trust it places in the socket
- [x] `serde_yaml` (deprecated) and `tokio-tungstenite` are replaced or bumped
- [x] MCP lows: astral-plane typing guard, DPI fallback documented, screenshot temp-file
      lifecycle
- [x] `conversation.json`'s plaintext-at-rest posture is stated in the README's security
      section

The grab-bag rule applies: each box is small, independently verifiable, and none deserves
its own number. If any grows teeth in the doing, split it out per the sizing rules.

Closed across core, shell, ui, mcp, and README in one pass. Both transcript writers —
`events.jsonl` and `conversation.json`'s temp file — are now created 0600 in the same
syscall; the reviewer caught that `_write_json` still had the window after the first fix.
Checkpointer and MemoryCommitter reuse the shell tool's `sanitized_env()` for every git
child (identity layered on after). The opener grant is documented-intentionally-unscoped:
three surfaces need arbitrary user paths, static globs would break them all, and the
host's own `open_path` command sits outside the capability system anyway. The UI already
dropped unknown event types at the discriminant level; it now counts them
(`unknownEventCount`) with the trust boundary written down at the parse site. serde_yaml
migrated to its successor `serde_yml` 0.0.13 (single-maintainer republish of the archived
crate — worth revisiting if it stalls) and tokio-tungstenite bumped 0.24 → 0.30. The mcp
type-text cap and CGEvent length both count UTF-16 units now, so astral-plane text can't
double-dip the cap or half-send emoji. Flagged for later, deliberately not done here:
the remaining git spawns on dev/diagnostic paths (e2e_checks, e2e_harness, benchmarks,
context manifest) still inherit full env, and the UI's drop counter has no surface yet.
### TD-4817 — Shell steering guard misses `>&` and `>|` redirect operators
**Size:** 1 · **Depends on:** TD-4805

**Acceptance criteria:**
- [x] `_shell_write_targets` extracts targets of `>&` and `>|` redirects, so
      `echo x >& AGENTS.md` and `echo x >| .tst/config.yaml` classify static C
- [x] A leading `!` on the target (zsh `>&!`) is stripped before the steering check
- [x] `2>&1`, `>&2`, and `exec 3>&1` still classify B (descriptor dups are not writes)
- [x] Both new shapes join the static-C parametrized test; the descriptor forms pin B

Completed 2026-08-21 (branch `td/4817-review-repairs`): both operators joined
`_REDIRECT_TOKENS`; the extractor strips a leading `!` (attached or spaced zsh
clobber). Descriptor dups extract numeric non-targets, which never match steering —
pinned in the B-floor parametrization.

Found by the 2026-08-21 ox-alpha review (Vuln 1, reproduced end-to-end): shlex emits
`>&` and `>|` as single punctuation tokens, the extractor sees nothing, and the command
lands on the B floor — which skip-all then promotes to silent execution.

### TD-4818 — Skip-all approvals must not promote the shell floor
**Size:** 2 · **Depends on:** TD-804, TD-4805

**Acceptance criteria:**
- [x] `resolve_explained` does not convert ask→auto under skip-all when the gate came
      from the `shell-floor` rule; the approval card still appears
- [x] Other B-class gates (CU actuation, allowlisted web fetch) keep their TD-804
      promotion — the exemption is scoped to shell
- [x] `docs/configuration.md` stops saying steering redirects are "Class C outright"
      and describes what the static table actually catches
- [x] Tests: skip-all + shell-floor still asks; skip-all + ordinary B still auto-runs

Completed 2026-08-21 (branch `td/4817-review-repairs`): the exemption keys on the
tool name rather than the rule id — every shell B is the floor, and the name
survives rule renames. Explicit `shell: auto` workspace rules still win (they
resolve before the skip-all block); that escape hatch is pinned by test. Class B
decision recorded in DECISIONS.md.

Found by the 2026-08-21 ox-alpha review (Vuln 2). The B floor exists because the
classifier cannot see inside a command string; promoting it under skip-all makes every
unparsed write form (`eval`, `sh -c`, `cp`, command substitution) a silent steering
write. This also falsifies the TD-4805 DECISIONS rationale — repaired, not relitigated.

**Superseded in part by TD-805 (2026-08-22):** the skip-all exemption for
`shell` is removed. The configuration.md wording for what the static
table actually catches still stands.

### TD-4819 — Credential-URL env filter misses password-only form
**Size:** 1 · **Depends on:** TD-4805

**Acceptance criteria:**
- [x] `redis://:pw@host` (empty username) and friends are stripped by `sanitized_env()`
- [x] Existing vectors (`user:pass@`, benign URLs without credentials) behave as before
- [x] Tests cover the password-only form for redis/postgresql/mongodb shapes

Completed 2026-08-21 (branch `td/4817-review-repairs`): the user class relaxed from
`+` to `*`; a match still requires the `:…@` tail, so benign URLs are untouched.
Parametrized over redis/rediss/postgresql/mongodb shapes.

Found by the 2026-08-21 ox-alpha review (Vuln 3): `_CREDENTIAL_URL_VALUE_RE` requires a
non-empty username, so Heroku-style password-only URLs survive into `printenv` output
and the persisted timeline.

### TD-4820 — Steering guard admits trailing dot/space aliases
**Size:** 1 · **Depends on:** TD-4803

**Acceptance criteria:**
- [x] `check_write` refuses any path component ending in `.` or space, mirroring the
      8.3/ADS refusals — `.tst/config.yaml.`, `AGENTS.md.`, `.tst./config.yaml`,
      `.tst/rules./x.md` all refuse
- [x] Ordinary dotted filenames (`notes.md`, `.tst/config.yaml`) are unaffected
- [x] Tests pin both the refusals and the unaffected forms

Completed 2026-08-21 (branch `td/4817-review-repairs`): the check lives in
`windows_unsafe_reason`, so the guard and the classifier's `boundary-unsafe-path`
rule both gained it in one move. `.`/`..` navigation components are excluded; the
bare policy file still refuses as `steering_file`, not `windows_unsafe` (pinned).

Found by the 2026-08-21 ox-alpha review (Bug 1). Creation-only and Windows-only today
(Win32 strips trailing dots/spaces at open time, aliasing the real steering file), but
the guard's contract is fail-closed on any shipped platform's unsafe forms.

### TD-4821 — Repo self-hosting gates: secrets-hook canaries, svelte-check red
**Size:** 1 · **Depends on:** TD-4801

**Acceptance criteria:**
- [x] Tracked test canaries matching the widened `sk-` pattern carry the
      `tst-secret-ok` marker the hook already honors (`test_setup_state.py`,
      `test_shell_tools.py`); staging those files no longer blocks the commit
- [x] `svelte-check` is green: `DesignLayer.svelte` `clientWidth`/`clientHeight`
      `never` errors fixed
- [x] Hook still rejects an unmarked canary (negative test by hand, noted)

Completed 2026-08-21 (branch `td/4817-review-repairs`): five canary lines marked;
hook verified clean on both files and still exit-1 on an unmarked canary.
`DesignLayer.svelte`'s `overlaySize` moved to `$derived.by` — the expression form
is analyzed inline, where TS narrows the `bind:this` target to `null` and reports
`never`; the closure form gets the declared type. svelte-check: 0 errors
(the `MemoryProposalCard` warning is pre-existing and left alone).

Found by the 2026-08-21 ox-alpha review (Bugs 2 and 4). The hook already knows the
`tst-secret-ok` convention — the canaries simply lacked it.

### TD-4822 — tst-cu-mcp: `tools/list` hang on batched stdin
**Size:** 2 · **Depends on:** none

**Acceptance criteria:**
- [x] initialize + `tools/list` written to stdin together both get answers; the server
      does not exit silently at EOF
- [x] The four failing mcp tests pass
- [x] Regression test: batched stdin request completes

Found by the 2026-08-21 ox-alpha review (Bug 3, reproduced 3/3 outside pytest).
Accepted on the report's reproduction; the fix belongs to the package's protocol loop
with its own test pass.

Closed by `DrainingStdioServer` (`src/tst_cu_mcp/stdio_transport.py`): the stock
mcp 2.0.0 stdio plumbing cancels the serving task group the instant stdin hits EOF,
so a request spawned into a handler in the same scheduling quantum dies before its
first step — pipelined clients get answers only to the earlier requests and the
process exits 0. The subclass interposes relay streams, counts requests forwarded
against answers written, and holds EOF until every pre-EOF request settles (wire
answer, or the dispatcher's `on_request_unanswered` hook for peer-cancelled work).
`mcp==2.0.0` is still the newest upstream release, so there was nothing to bump to.

### TD-4823 — tst-cu-mcp: mypy is platform-dependent
**Size:** 1 · **Depends on:** none

**Acceptance criteria:**
- [x] `mypy` on `mcp/tst-cu-mcp` passes on macOS (per-module overrides for the
      Windows-only ctypes names in `backends/windows.py`, or equivalent gating)
- [x] The override does not weaken checking on Windows itself

Found by the 2026-08-21 ox-alpha review (Bug 5 / Enhancement 3).

Closed with a `[[tool.mypy.overrides]]` disabling only `attr-defined` for
`tst_cu_mcp.backends.windows`: those ctypes names resolve only when mypy itself
runs on Windows, where the override is a no-op and full checking still applies.

