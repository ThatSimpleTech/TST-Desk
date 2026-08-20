# TST Desk — Decisions

This file records architectural, scope, and taste decisions that are not covered by the spec
or that resolve open questions in the spec. See `AGENTS.md` §5 (Decision protocol).

---

## 2026-08-12 — TD-101: Seven open decisions

Resolved during project kickoff. All seven are recorded here as binding.

### 1. Repo layout

**Decision:** Standalone `ThatSimpleTech/tst-desk` repository.

**Rationale:** The v0.1 success criterion requires "a stranger clones the repo and runs one
command." A standalone repo is the simplest path. Embedding inside TSTOS couples the release
cycle to a larger project and adds friction for external contributors.

### 2. `tst-cua` reuse strategy

**Decision:** Vendor into the repo.

**Rationale:** The agent loop must be adapted for the 3-tier router. Vendoring gives full
control to modify without waiting for upstream or dealing with pre-release package fragility.
The Mock driver is expected to be valuable as the v0.1 test harness.

### 3. Goose's role

**Decision:** Fully replace with `tstd`.

**Rationale:** Goose is the tool building this project, not a dependency of it. The daemon
architecture already provides a CLI (spec §2), so an optional Goose backend would add
maintenance surface without adding capability.

### 4. License

**Decision:** Apache-2.0, with a `NOTICE` file.

**Rationale:** The patent grant makes it safely adoptable inside companies, which is the
audience that matters most. MIT is simpler but Apache-2.0 removes the adoption friction
for the corporate use cases TST Desk targets.

### 5. Frontend framework

**Decision:** SvelteKit inside Tauri (as spec'd).

**Rationale:** Confirmed. The spec and `AGENTS.md` §6 are written around Svelte 5 runes,
design tokens, and typed stores. Changing to React would require rewriting the frontend
standards documents.

### 6. Product name

**Decision:** "TST Desk" — locked.

**Rationale:** Used consistently across the spec, backlog, `AGENTS.md`, and kickoff prompt.
Changing now would require updating all four documents.

### 7. Autonomy isolation policy

**Decision:** Hard-required container. No unsandboxed autonomy, no confirmation-dialog escape
hatch.

**Rationale:** A scary confirmation is a UX band-aid, not a real safety boundary. Containers
provide actual isolation — the "wall" the spec describes in §12. v0.1 defers autonomy mode
to a later milestone, but the architecture must be built to require this.

---

## 2026-08-12 — TD-204: Protocol schema

Decisions made during the protocol schema design.

### 1. Field naming convention

**Decision:** `snake_case` in both Python and TypeScript.

**Rationale:** Pydantic serializes to snake_case by default. The TypeScript side consumes the
JSON directly — no translation layer, no camelCase conversion. TypeScript interfaces use
snake_case field names to match the wire format exactly. This is simpler and less error-prone
than a camelCase convention with a transform.

### 2. `seq` type

**Decision:** Monotonic integer per session, starting at 1.

**Rationale:** The spec says "monotonic `seq` scoped to the session." An integer is simpler
than a UUID — testable, sortable, and the attach/replay protocol uses `from_seq` (integer
comparison). UUIDs are meaningless for ordering.

### 3. Event model

**Decision:** Pydantic discriminated union with `type: Literal["..."]`.

**Rationale:** Pydantic v2 supports discriminated unions via `typing.Union` with `Literal`
type discriminators. `BaseModel.model_validate(data, strict=True)` routes to the correct
subclass. This is simpler than a manual registry or a visitor pattern, and the exhaustive
of check via `typing.assert_never()` ensures all cases are handled.

### 4. TypeScript sync strategy

**Decision:** Hand-written types in TypeScript, asserted by a test that round-trips every
event type through both Python and TypeScript serialization.

**Rationale:** Codegen from Pydantic to TypeScript is fragile (edge cases for Literal types,
discriminated unions, `Optional` fields). A test that constructs an event, serializes it
through Python, and parses the same JSON through TypeScript `zod` or `io-ts` is more
reliable. The types are small enough that hand-writing them is cheaper than maintaining
a codegen pipeline.

### 5. `attach`/`detach` shape

**Decision:** Full shape defined, behavior trivial in v0.1.

**Rationale:** `attach{session_id, from_seq}` reconnects to an existing session and replays
events from `from_seq`. `detach` stops the stream. The daemon accepts the messages and
validates the session_id, but since v0.1 has no detached sessions, the "replay" is always
empty and the "live stream" is the session's current event log. The shape must be right so
the UI can be built against it; the behavior is trivial until v0.3.

### 6. Unknown message handling

**Decision:** Unknown message types produce a typed `error` event with `code: "unknown_message"`,
never a crash or silent ignore.

**Rationale:** Forward compatibility: a future client may send message types the current
daemon doesn't understand. The daemon must reject them gracefully with a diagnostic message
naming the unknown type, so the client can degrade.

---

## 2026-08-13 — TD-305: Track merge strategy

### 1. Merge of E3/E4/E6 track

**Decision:** Merged `td/402-tool-dispatch` (E3/E4/E6 track tip) into the E5 context track at
`8f8f0c5` using `git merge --no-ff`. Auto-resolved cleanly by the `ort` strategy with no
manual conflict resolution needed.

**Rationale:** The two tracks were largely disjoint — the E3/E4/E6 track contributed ~30 files
(provider, session, loop, tools, config, cost, keychain, mock, tests) while the E5 track
contributed the `context/` package. The shared files that both tracks touched were:

| File | Resolution |
|------|-----------|
| `core/tstd/context/__init__.py` | E5 full package vs. E3 empty stub → auto-kept E5 |
| `core/tstd/protocol.py` | Both tracks added messages; auto-union succeeded |
| `core/tstd/daemon.py` | Both modified; auto-merged cleanly |
| `core/tstd/ws.py` | Both modified; auto-merged cleanly |
| `core/tstd/router.py` | Content-identical (git cherry-pick + original) → auto-kept |
| `core/tstd/tools/__init__.py` | E5 empty stub vs. E3 full module → auto-kept E3 |
| `core/pyproject.toml` | Both added deps; auto-union succeeded |
| `DECISIONS.md` | Both added entries; auto-union succeeded |

**Post-merge fix:** Added `types-jsonschema` to dev dependencies (pre-existing gap in the
E3/E4/E6 track — `jsonschema` is used by `dispatch.py` but lacked type stubs, breaking
`mypy --strict`).

Decisions made during the per-tier context assembly design.

### 1. TD-303 cherry-pick vs. full track merge

**Decision:** Cherry-picked `66ffa83` (TD-303: tier routing policy) onto the E5 context track
branch rather than merging the E3/E4/E6 track.

**Rationale:** The E3/E4/E6 track is 9.5k lines across 36 files; importing all of it would
violate scope discipline (AGENTS.md §3) and introduce conflicts on `protocol.py`, `ws.py`,
`daemon.py`, and `uv.lock`. The single self-contained commit adds exactly `core/tstd/router.py`
(177 lines) and `core/tests/test_router.py` (276 lines), both absent on this branch — the
minimal dependency needed for `TierName` reuse. The full track merge is deferred as a
separate integration task (required before TD-305 session wiring).

### 2. Tier name type: reuse `TierName` from `tstd.router`

**Decision:** The tier-aware context assembly (`core/tstd/context/tier.py`) imports and uses
`TierName` from `tstd.router` rather than redefining a `Tier` literal.

**Rationale:** `tstd.router` (TD-303) is the canonical definition of `TierName = Literal["brain",
"worker", "validator"]`. Redefining it in the context package would create a second source of
truth. The dependency is clean (no cycles — `router.py` depends only on `typing`).

### 3. Default validator steering subset

**Decision:** The `DEFAULT_VALIDATOR_SUBSET` is `("AGENTS.md", "standards*.md",
"conventions*.md", "style*.md")`, matched against the steering file's absolute path using
the same basename-at-any-depth glob semantics as `appliesTo` (TD-503).

**Rationale:** Spec §4.6 says "standards/conventions subset" without defining which files
those are. The default selects the workspace conventions doc (AGENTS.md) plus rules whose
names carry conventional standards/conventions/style labels. This is configurable via
`TierContextConfig.validator_subset` (replace semantics, not extend). The default is
documented in `DEFAULT_VALIDATOR_SUBSET`'s docstring.

### 4. Config infra deferred

**Decision:** No `.tst/config.yaml` wiring for the validator subset. `TierContextConfig` is
a plain dataclass; the daemon will read it from config when that module lands (other track).

**Rationale:** Config file reading lives on the E3/E4/E6 track. The `TierContextConfig` API
is designed so wiring it to config later is a one-line change at the call site.

### 5. Memory block deferred

**Decision:** The `assemble_for_tier(memory=...)` parameter exists and is wired for the brain
tier, but defaults to `None` (no memory block). No memory module exists on this track.

**Rationale:** The memory module is a future story (TD-5xx). The parameter is a hook so the
daemon passes it once memory exists. No behavioral change is needed in the tier assembler.

### 6. `source_filter` added to `ContextAssembler`

**Decision:** `ContextAssembler.assemble`/`assemble_sync` gained an optional `source_filter:
Callable[[SteeringSource], bool] | None` parameter that skips filtered sources entirely
(no import processing, no rendering, absent from the `sources` list).

**Rationale:** The validator's steering subset requires filtering sources before rendering
the block. Adding the filter as a parameter to the assembler keeps provenance-comment
rendering in one place (avoids duplicating `ContextAssembler._render` in `tier.py`). The
default `None` preserves existing behavior.

### 7. `_path_matches_glob` reused from `assembler.py`

**Decision:** `tier.py` imports `_path_matches_glob` from `tstd.context.assembler`
(module-private, same package).

**Rationale:** The glob semantics (bowlderized `fnmatch` with `**` support, basename-at-any-depth
for slash-less patterns) are the same as TD-503's `appliesTo` matching. Duplicating them
would be a maintenance hazard. Cross-module reference within the same package is acceptable.

**Decision:** Hard-required container. No unsandboxed autonomy, no confirmation-dialog escape
hatch.

**Rationale:** A scary confirmation is a UX band-aid, not a real safety boundary. Containers
provide actual isolation — the "wall" the spec describes in §12. v0.1 defers autonomy mode
to a later milestone, but the architecture must be built to require this.
## 2026-08-12 — TD-401: Agent loop

Decisions made during the loop port from tst-cua.

### 1. Provider factory pattern

**Decision:** The loop accepts a `Callable[[], Awaitable[ProviderLike]]` (provider
factory) instead of a `ProviderLike` instance.

**Rationale:** Sessions can be opened and attached without any provider being
available. The provider is created lazily on first user message, so the daemon
does not need a keychain entry at startup. This also makes the loop fully
testable — tests pass a simple factory that returns a `MockProvider`.

### 2. ProviderLike protocol

**Decision:** A structural `Protocol` (not an ABC) defines the provider interface
the loop talks to.

**Rationale:** Both `ProviderClient` (network) and `MockProvider` (offline) satisfy
the protocol structurally without inheritance. This enforces the "provider-agnostic"
requirement in the acceptance criteria without coupling the loop to either
implementation.

### 3. User message queue on Session

**Decision:** Added an `asyncio.Queue[str]` to the `Session` class, with
`add_user_message()` and `wait_for_user_message()` methods.

**Rationale:** Decouples the WebSocket connection from the agent loop. The daemon
writes to the queue; the loop reads from it. This is the mechanism that makes
prime directive §2.5 ("the daemon owns sessions; the window is only a viewer")
concrete — the loop never blocks on a socket.

### 4. Message list copy in provider requests

**Decision:** `_stream_turn` copies the messages list before passing it to the
provider.

**Rationale:** The loop mutates the messages list after the provider call
(appending the assistant response). Without a copy, the provider would see
post-stream mutations in its stored request, producing confusing aliasing bugs
in the audit trail and tests.

### 5. Deviation from REUSE.md

The tst-cua loop structure is `plan → gate → act → verify → reconcile`. TD-401
implements `plan → act → record` with placeholders for `gate` (decision
classifier, E7) and `verify` (validator tier, E8). This is not a deviation from
the intent — the REUSE.md explicitly identifies these as later stories and the
current structure is a correct subset. The full structure is assembled
incrementally across TD-402, TD-403, E7, and E8.



## 2026-08-12 — TD-306: Resilience

Decisions made during resilience implementation.

### 1. Retry strategy

**Decision:** Exponential backoff with jitter, bounded attempts. Retry on 429, 5xx, and
connection errors. Non-retryable errors (auth, context-length, parse) never retried.
`Retry-After` header overrides the computed delay (max with exponential base).

**Rationale:** Standard industry practice. The backlog explicitly specifies this pattern.

### 2. Default-on retry

**Decision:** `ProviderClient` retries by default (`RetryConfig` with `max_retries=3`).
Callers can disable by passing `RetryConfig(max_retries=0)`.

**Rationale:** Resilience is on by default so the loop (TD-401) gets it for free. Existing
single-shot tests opt out with `max_retries=0`.

### 3. Retry scope for streaming

**Decision:** Retry on initial connection only (before any content chunk). Mid-stream
failures produce a clean `stream_interrupted` error without retrying.

**Rationale:** Retrying a mid-stream drop would re-send the entire request and re-stream
from scratch, producing duplicate content chunks that the consumer cannot deduplicate.

### 4. Stream interruption detection

**Decision:** `_stream_once` tracks in-flight tool-call state. If the stream ends without
a `finish_reason` while a tool call is in progress, a `stream_interrupted` `ProviderError`
is yielded. The consumer can detect this and discard the partial tool call.

**Rationale:** The consumer (loop) must never dispatch a half-parsed tool call. The
`ProviderClient` guarantees this by emitting a typed error at the protocol level.

### 5. Error message strategy

**Decision:** Auth failures (401) and context-length errors (413) get actionable messages
replacing the provider's generic error text. The provider's original detail is appended to
context-length messages (safe, no secrets) but excluded from auth messages (risk of key
echo in provider error body).

**Rationale:** Users should see a fix instruction, not a raw provider error. The
distinction between safe-to-include (context length) and not-safe-to-include (auth) follows
the security principle of not echoing credentials. Auth/context-length error codes are
normalized to `auth_failed` / `context_length_exceeded` regardless of the provider's
internal code, giving the UI a stable branch target.

---

## 2026-08-13 — TD-305: Cache-aware prompt assembly

### 1. Stable-prefix order implemented in `PromptAssembler`

**Decision:** Added `core/tstd/context/prompt.py` with `PromptAssembler` and `AssembledPrompt`.
The system prompt is assembled in the stable-prefix order from spec §4.5: base prompt →
steering → memory placeholder → workspace manifest, with the conversation appended per turn
by the loop.

**Rationale:** Blocks 1-2 (base + steering) are the provider prefix-cache target. The
assembler returns the full text, the prefix (blocks 1-2), its SHA-256 hash, and its token
count so tests and logs can assert byte-identity across turns.

### 2. Memory placeholder

**Decision:** The brain tier's memory slot is occupied by `MEMORY_PLACEHOLDER`
(`<!-- memory: none loaded for this session -->`) until the memory story lands (spec §5).

**Rationale:** Keeps the memory block positionally stable in the prompt so a real memory
block can slot in without reordering the cache prefix (spec §4.5 calls this block "memory
placeholder"). Worker/validator never carry the memory slot (spec §4.6).

### 3. Loop wiring

**Decision:** `agent_loop` accepts an optional `PromptAssembler` (built from
`session.workspace_path` when absent) and re-assembles the system message at the start of
each turn, replacing the placeholder system prompt.

**Rationale:** The tier changes per turn (brain → worker), and per-tier context differs
(TD-508), so the system message must be re-assembled when the active tier changes. The
steering block bytes stay identical when files don't change, preserving the cache prefix.

### 4. Cache observability

**Decision:** The turn-start log carries `cache_prefix_hash` and `cache_prefix_tokens`; the
turn-complete log carries `cache_prefix_hash` and `cache_ratio`. `CostTracker` gained
`turn_cache_ratio()`, `turn_cached_tokens()`, `turn_uncached_tokens()`, and
`turn_cache_ratio` in `summary()`.

**Rationale:** Criterion 3 requires the cache hit rate to be observable in logs and in the
cost breakdown. The provider's `Usage.cached_prompt_tokens` was already tracked (TD-304);
the new helpers surface the ratio derived from it.

---

## 2026-08-13 — TD-509: Hot reload

### 1. Change detection strategy

**Decision:** No filesystem watcher. The loop already re-resolves steering every turn via
`PromptAssembler` (which re-reads files), so detection is done by comparing the assembled
steering prefix hash (`prefix_hash`) against the previous turn's hash. On change, the loop
emits a new `SteeringReloaded` protocol event plus a fresh `InstructionStack` event to the
session event log.

**Rationale:** The session event log is already broadcast to the UI by the daemon, so
emitting there announces the reload in the timeline (criterion 2) and pushes the updated
stack to the inspector. A filesystem watcher (e.g. watchdog) would add a dependency and
thread to solve a problem the per-turn re-resolution already handles.

### 2. Debounce

**Decision:** Turn-boundary hash comparison IS the debounce. Rapid successive saves between
turns collapse into one reload event (the loop only sees the final state when it assembles
the next turn's prompt).

**Rationale:** Criterion 4 requires debouncing against rapid saves. Since there is no
filesystem notification, there is no event flood — one `SteeringReloaded` per changed
state. Verified by `test_rapid_saves_single_reload`.

### 3. `TierContext`/`AssembledPrompt` carry `AssembledSteering`

**Decision:** `TierContext` and `AssembledPrompt` now carry the underlying
`AssembledSteering` (previously discarded after assembly). `SteeringReloaded.source_count`
and the pushed `InstructionStack` event are built from it.

**Rationale:** The loop needs the resolved sources to build the inspector event; discarding
them forced a duplicate re-resolution. Carrying them through is the minimal change.

### 4. Protocol addition

**Decision:** Added `SteeringReloaded` (type `steering_reloaded`) to the daemon event
union, carrying `prefix_hash`, `prefix_tokens`, and `source_count`.

**Rationale:** The timeline needs a typed, distinct announcement of a reload (vs. a generic
log line). It complements the existing `InstructionStack` event, which carries the full
resolved stack for the inspector.

---

## 2026-08-13 — TD-901: Audit store

Decisions made while building the append-only SQLite audit store.

### 1. Shared redaction helper extracted from the logging filter

**Decision:** `redact_secrets(text)` now lives as a module-level function in
`tstd/logging.py`; `SecretsRedactionFilter` delegates to it, and the audit store scrubs
tool arguments with it before insert.

**Rationale:** Criterion 4 requires arguments scrubbed "through the same redaction filter
as logs." A shared *function* over the same `SECRET_PATTERNS` list is that requirement made
structural: a pattern added once protects logs and the audit database at the same time.

### 2. `costs` is a VIEW over `model_calls`, not a table

**Decision:** The sixth schema object is a view aggregating `model_calls` by
(session, turn, tier, day), with classifier cost kept on its own column.

**Rationale:** Rollup rows would duplicate truth in a store that can never UPDATE them;
any later-scrubbed insert would leave the aggregate permanently wrong. A view computes on
read, keeping one source of truth. Indices on `model_calls(session_id)` and
`model_calls(ts)` keep the view's group-bys off full scans for the queries the UI makes
(timeline per session, cost meter per session/day, decisions panel per session).

### 3. One row per *completed* tool call — never insert-then-patch

**Decision:** `tool_calls` rows are inserted after the call resolves, carrying status and
result hash together. There is no "pending" row that would need an UPDATE later.

**Rationale:** Preserves append-only as a physical fact. The TD-902 writer pairs
`ToolCall`/`ToolResult` events by `tool_call_id` before inserting; a call that never
resolves is recorded as `status='error'` (or `'refused'` for boundary refusals) with a
NULL result hash.

### 4. Timestamps are UTC epoch seconds (REAL)

**Decision:** All `ts` columns are seconds since the Unix epoch. Day buckets derive via
`datetime(ts, 'unixepoch', 'localtime')`.

**Rationale:** Sortable, comparable, and timezone-safe at the storage layer; local-day
grouping (needed by per-day cost queries) happens at query time, matching how
`CostTracker` reports "today" in local time.

### 5. Migration steps carry their version stamp inside their transaction

**Decision:** `MIGRATIONS` is an ordered tuple of SQL scripts; each step runs as
`BEGIN; <schema>; INSERT INTO schema_migrations; COMMIT;` via `executescript`.

**Rationale:** `executescript` autocommits per statement, so the transaction must live in
the script text for a failed step to roll back atomically with its version stamp. Tested
by `test_failed_migration_rolls_back_atomically`.

---

## 2026-08-13 — TD-902: Audit writer

Decisions made while wiring the audit store to live sessions.

### 1. Event-sourced recording — no new protocol events

**Decision:** The writer subscribes to each session's append-only event log and records
turns, tool calls/results, and decisions from `TurnComplete` / `ToolCall` / `ToolResult` /
`DecisionLogged` events. No event type was added to the protocol.

**Rationale:** TD-204's daemon→client event set is deliberately closed for v0.1 (the shell
is built against it). The event log already carries everything the audit trail needs except
per-call model detail (see §2). The event-driven seam also means the loop never knows the
store exists — recording cannot change loop behavior, and failure in the audit path cannot
fail a turn.

### 2. Model calls flow through a `CostTracker` listener, not events

**Decision:** `CostTracker` gained `add_listener(callback)` — invoked with
`(record, is_classifier)` after every recorded call; the loop attaches a forwarding closure
when the daemon passes an `audit_sink` (new optional `agent_loop` parameter).

**Rationale:** Every provider call already passes through `CostTracker.record()` (and, once
the E7 track merges, `record_classifier()`), so one listener there observes all spend without
touching the provider or the protocol. Note for the merge: `record_classifier()` must call
`_notify(record, is_classifier=True)` when TD-703 lands here — the flag and API already exist.

### 3. Turn model calls are buffered, then inserted with their turn row

**Decision:** `record_model_call` buffers non-classifier calls per session; the `TurnComplete`
handler inserts the turn row and its model calls in one store op so `turn_id` is set at insert
time. Buffered calls flush unlinked (`turn_id=NULL`) on terminal session state or writer close.

**Rationale:** The store is append-only — no backfill UPDATE is possible. Buffering is safe
because both producer (cost listener) and consumer (event subscriber) run on the same event
loop thread; a cancelled or failed turn emits no `TurnComplete`, so the terminal-state and
shutdown flushes keep the spend record complete (a lost cost record is an audit defect).

### 4. Refusals are stored as `status='refused'`

**Decision:** A `ToolResult` with `status='error'` whose call carried `decision_class='C'` is
stored with status `refused`.

**Rationale:** TD-602 forces class C onto boundary refusals; mapping them apart from ordinary
tool errors makes Class C refusals directly queryable (TD-902 criterion 4) and matches the
store's `CHECK` constraint vocabulary. On this branch `decision_class` is still always NULL
(TD-702 lands on the E7 track) — the mapping activates when that merges, no further change.

### 5. Failure reporting is edge-triggered

**Decision:** On a store write failure the writer logs the exception and emits one typed
`error` event (`audit_write_failed`) into the affected session's timeline; it reports again
only after a write has succeeded. Writes never stop.

**Rationale:** "Degrades loudly" is a user-experience requirement, not a log volume. One
banner per failure burst tells the user the trail is incomplete; a per-write notification
would drown the timeline during a persistent disk fault.

---

## TD-903 — Cost aggregation and export

### 1. Aggregates return raw SUM precision

**Decision:** `cost_by_*` return the SQLite SUM unrounded. Presentation formatting is the
consumer's job.

**Rationale:** The story's property test requires aggregates to equal the sum of the
individual records; rounding to 6 decimals inside the store broke that equality by up to
5e-7 per bucket. An audit number that doesn't match its own receipts is worse than an
ugly float.

### 2. Query/export layer is a sibling module, not more of `audit.py`

**Decision:** `audit_queries.py` holds the read side (aggregations, exports); `audit.py`
stays the write-only append path plus schema.

**Rationale:** The append-only write path is the audited surface — keeping it small keeps it
reviewable. The read side will grow (dashboards, CLIs, rollups) without touching it. The
queries read `store._conn` in-package rather than growing a public read API on the store:
same-module-package access is the pragmatic seam; the writer already serializes access.

### 3. Exports carry individual records, not aggregates

**Decision:** JSONL/CSV export one row per `model_calls` record (with `is_classifier` and
ISO-8601 UTC timestamp), not pre-aggregated buckets.

**Rationale:** Aggregates are always recomputable from the records; the reverse isn't.
The export exists for reconciliation against provider billing — which is per-call. The
`model` slug is the user's own configured data, included so bills reconcile (prime
directive §7 restricts slugs in code, not in the user's records).
## 2026-08-13 — TD-405: Context window management

### 1. Compaction announced via new `ContextCompacted` protocol event

**Decision:** Compact is never silent (criterion 4), and the closed daemon-event set had
nothing that fits. Added one event, `ContextCompacted` (`context_compacted`), carrying
`dropped_messages`, `kept_messages`, `tokens_before`, and `tokens_after` — enough for the
timeline to show exactly what compaction did.

**Rationale:** Reusing a generic log or an `InstructionStack` restyle would bury the most
important fact for the user ("your older turns were replaced by a summary") in noise. One
typed event keeps the timeline honest and the TS mirror addition trivial.

### 2. Deterministic extractive summary, no model call

**Decision:** Dropped turns collapse into a truncated line-per-message summary
(`User:`, `Assistant:`, `Assistant called tools:`, `Tool result:`), capped at 200 chars per
message and 2,000 chars overall. No model synthesizes the summary.

**Rationale:** Spending tokens to save tokens inverts the story's economics (the class B /
cost-tracking rules make spend visible by design). A deterministic summary is also what the
test suite can pin exactly, and it keeps compaction O(messages) instead of
O(request). The trade-off — no semantic compression — is accepted: the two most recent
user turns survive verbatim, which is the context the model actually needs to stay coherent.

### 3. Threshold model and token counting

**Decision:** Compaction triggers when the estimated prompt tokens exceed
`0.8 × (context_window − max_output_tokens)`. Estimation reuses the tokens module via
`make_token_counter(slug)`; counters are created once per slug in the loop (encoders load
lazily and are not cheap), so a module-style per-loop dict caches them.

**Rationale:** The model must still be able to answer, so the output reservation comes off
the window first. The 0.8 headroom absorbs estimation error (heuristic counting on
unknown slugs) before the provider's hard cut. Per-slug counting only second-guesses an
estimator, not the provider, which is the accuracy that stopping at the window edge needs.

### 4. Inner-loop placement, cuts at user boundaries

**Decision:** The guard runs at step 2b.2 of the agent loop — after the fresh system
assembly, before the provider call. `find_compaction_point` returns the oldest user message
to keep: the cut never splits an assistant `tool_calls` message from its `tool` results,
and the in-flight turn (plus one prior turn) always survives. The compacted list replaces
`messages` in place, so the fresh-assembled system prompt stays at index 0.

**Rationale:** Compacting only between turns lets a mid-turn tool storm (long `fs_read`
outputs, TD-603's numbered lines) blow past the window before the user ever sends again;
the guard pays off exactly where it lives. User-boundary cuts are the only structural
invariant the delta-based providers don't enforce themselves — splitting a call/result pair
produces provider 400s, splitting the in-flight turn produces nonsense.

### 5. Prior summaries fold into the next summary

**Decision:** `_render_summary` treats a dropped system message as a prior compaction
summary and folds its lines into the new one (still under the 2,000-char cap), pinned by
`test_second_compaction_folds_prior_summary_in`.

**Rationale:** Otherwise the second compaction silently deletes the first summary and the
oldest context vanishes in one step instead of degrading. Folding is deterministic and
cheap; the cap keeps runaway growth impossible, so long sessions compress toward a bounded
"old context" block.
## 2026-08-13 — TD-701: Decision classifier

Decisions made during the decision-class model and rule table design.

### 1. Classifier consumes a reduced `DecisionRequest`, not raw tool args

**Decision:** `DecisionClassifier.classify(DecisionRequest)` where
`DecisionRequest` carries already-resolved signals — `reads`, `writes`,
`hosts`, `is_mutation` — rather than parsing raw tool arguments internally.

**Rationale:** Path and host extraction from tool arguments is tool-specific
and belongs with the dispatch chokepoint (TD-702) that knows each tool's
schema. Keeping the classifier purely over resolved signals makes the rule
table a pure table and trivially testable case-by-case; TD-702 builds the
`DecisionRequest` from a validated tool call.

### 2. Reads outside the workspace are Class C, matching the criterion verbatim

**Decision:** The `path-outside-workspace` rule fires on any referenced path
— reads included — not just writes.

**Rationale:** TD-701's criterion says "any path outside the workspace → C",
unqualified. Reads outside `writable_paths` are already refused by TD-602's
boundary enforcement; mirroring that at the classifier keeps the wall
single-sourced.

### 3. Writable glob semantics reuse TD-503 `appliesTo` semantics

**Decision:** `writable_patterns` match relative paths with `**` (zero-or-more
segments) and slash-less patterns match the basename at any depth, the same
semantics as TD-503 path-scoped rules.

**Rationale:** One consistent glob dialect across steering scoping and the
autonomy boundary avoids two subtly-different path-matching behaviours.

### 4. Rule table is a declarative tuple, first-match-wins, C-before-A

**Decision:** `RULE_TABLE` is a tuple of `Rule(id, description, class, match)`
evaluated in priority order; irreversible Class C rules are declared before
the reversible Class A rule so a write that is both an in-workspace edit and
a steering-file write lands on C.

**Rationale:** "Rule table is data, not scattered conditionals" (TD-701).
First-match-wins with C-first makes irreversibility outrank reversibility
deterministically, and every classification records the firing rule's id for
explainability.

---

## 2026-08-13 — TD-702: Classifier chokepoint

Decisions made while making the decision classifier a mandatory gate on tool
execution (prime directive §2.6).

### 1. Guard at the execution boundary, not the call site

**Decision:** `ToolDispatcher.dispatch` raises `UnclassifiedToolCall` when a
tool reaches its handler without a classifier attached. The loop is the only
production dispatch path; it attaches a `DecisionClassifier` built from the
session's workspace boundary and classifies every call before dispatch.

**Rationale:** Enforcing the chokepoint inside `dispatch()` — where the tool
handler is about to run — makes "no bypass path" a runtime guarantee rather
than a convention. Any future dispatch call site inherits the guard: a tool
cannot execute without a classifier. Criterion 4 is the same guard: reaching
execution unclassified (classifier absent) raises immediately.

### 2. `Tool` declares classifier-relevant metadata

**Decision:** `Tool` gained `path_fields`, `host_fields`, and `mutates`
(backward-compatible defaults), and `build_decision_request()` reduces a tool
call to a `DecisionRequest` using that metadata — never heuristics over raw
argument text.

**Rationale:** The rule table (TD-701) classifies over resolved signals
(reads/writes/hosts/mutation). Per-tool explicit declaration is the only
reliable way to extract those signals; guessing from argument shapes would
both miss cases and misfire on opaque ones. Built-ins were annotated
(`fs_read` read+path, `fs_write` write+path, `shell` mutates).

### 3. Ambiguous is not unclassified

**Decision:** A call the static table cannot decide (`decision_class is None`)
still executes with the class recorded as `None`; the guard raises only when
no classifier ran at all.

**Rationale:** Ambiguous cases are TD-703's job (worker-tier model call,
defaulting to B). Raising on them now would break every shell-style call
before TD-703 exists. The chokepoint's job in TD-702 is to guarantee
classification *happened* — the class being `None` is a legitimate
classification outcome until TD-703 lands.

### 4. Audit attachment is the `tool_call` event

**Decision:** The decision class is attached to the `ToolCall` event (which
already carried `decision_class`, previously always `None`), emitted into the
session's append-only event log — the v0.1 audit trail — before execution.

**Rationale:** Criterion 3 requires the class on the audit record and the
`tool_call` event; in v0.1 these are the same append-only log. E9 (TD-901)
will persist the log as the dedicated audit store; no new protocol surface
was needed.

---

## 2026-08-13 — TD-703: Ambiguous-case classifier call

Decisions made while wiring the worker-tier fallback for cases the static
rule table cannot decide.

### 1. `AmbiguousClassifier` wraps the static table; `classify` is async

**Decision:** A new `AmbiguousClassifier(static, call_worker)` consults the
static `DecisionClassifier` first and short-circuits when a rule fires; only
ambiguous results go to the worker tier. Its `classify` is async because the
fallback makes a model call; the dispatcher and loop await it.

**Rationale:** Keeps the static rule table (TD-701) pure and synchronous for
its table-driven tests while the chokepoint gains the worker fallback without
an alternate dispatch path. Static cases never pay for a model call.

### 2. Cache key is (tool, canonical arguments) — the safe reading of "argument-shape"

**Decision:** The session cache is keyed by `(tool_name, canonical JSON of
the argument dict)`, bounded at 256 entries with oldest-first eviction.

**Rationale:** "Argument-shape" is read as the canonical form of the
arguments: identical tool calls (the common repeat) hit the cache; calls with
different arguments never share an entry. Caching by key/types alone could
misclassify a different shell command from a cached one — unsafe for a
security classifier. Bound prevents unbounded session growth.

### 3. The worker call bypasses `TierRouter` accounting

**Decision:** The classifier call is a single-shot, non-streaming completion
on the worker tier's configured model, made directly against the provider —
it never calls `TierRouter.record_turn_start/success/failure`.

**Rationale:** Router turn accounting drives main-loop tier rotation and
failure escalation; classifier calls are auxiliary and must not consume lead
turns or trigger escalation. `ProviderLike` was extended with the
non-streaming `chat_completion` method both providers already implemented.

### 4. Fail toward B, never A

**Decision:** Any worker exception, empty response, or unparseable response
classifies as **B**, and the outcome is cached.

**Rationale:** Spec §12.2: fail toward asking, not toward acting. The B
default is a "surface in the summary" class, so a broken classifier degrades
to review, never to silent action.

### 5. Classifier cost is separate accounting

**Decision:** `CostTracker` gained `record_classifier()`/`classifier_cost()`/
`classifier_call_count()`; classifier calls are stored in their own list and
never folded into turn/session/day totals. `summary()` and the `CostUpdate`
event (new `classifier_cost` field, default 0) expose it.

**Rationale:** Criterion 4 requires the cost visible in the breakdown but
separately — folding it into main-loop cost would hide the price of the
ambiguity fallback and misattribute spend to the agent's own turns.

---

## 2026-08-13 — TD-602: Path boundary enforcement

Decisions made while building the path boundary guard.

### 1. Enforcement lives in the dispatch execution path, not per-handler

**Decision:** A `PathGuard` is attached to the `ToolDispatcher` (like the
classifier chokepoint); `dispatch()` refuses any path-bearing tool call
before the handler runs. A path-bearing tool reaching the handler without a
guard attached raises (no bypass for the boundary, mirroring prime §2.6).

**Rationale:** "Enforced in the tool itself" (criterion 5) means the tool
layer — and dispatch is the tool layer. Handlers (TD-604) receive
already-canonicalized, already-checked paths, so enforcement can never be
skipped by a handler that forgets its own check.

### 2. Shared path primitives live in `autonomy/classifier.py`; `tools/boundary.py` owns enforcement

**Decision:** Canonicalization, workspace membership, writable-glob
matching, and steering-file detection stay in the classifier (promoted to
public names); the guard imports them and adds the enforcement semantics
(RefusalError, refusal order, hardlink and Windows-unsafe checks).

**Rationale:** One source of truth for the primitives the classifier and the
guard both need; importing them from the classifier into `tools/boundary.py`
avoids an import cycle (the classifier must not import tools at module
load).

### 3. Hardlinks: refuse in-place writes to nlink > 1 files

**Decision:** `check_write` refuses any write whose target already exists
with `st_nlink > 1`.

**Rationale:** A hardlink inside the workspace can alias a file outside it,
and an in-place write would modify the shared inode — realpath cannot see
this. The sanctioned path is atomic temp-file + rename (TD-604), which
replaces the directory entry and never touches the external inode. Fresh
files (no stat) are unaffected.

### 4. Windows-unsafe forms are refused fail-closed on every platform

**Decision:** Drive-relative/absolute (`C:foo`), UNC (`\\server\share`),
8.3 short names (`PROGRA~1`), and ADS (`file:stream`) are refused on all
operating systems, not just Windows.

**Rationale:** A workspace may be shared or moved across OSes; a path that is
harmless on macOS can alias a different file on Windows. Native Windows
semantics are exercised by the existing `windows-latest` CI runner
(skipped locally).

### 5. Refusals are Class C everywhere

**Decision:** The classifier gained two rules — `boundary-unsafe-path` (C)
for Windows-unsafe forms and `path-outside-writable` (C) for in-workspace
writes outside `writable_paths` — and `dispatch()` forces any guard refusal
to record `decision_class=C` on the result.

**Rationale:** Criterion 6: refusals log as Class C on the audit trail
(`tool_call` event + result). The classifier cannot stat files (hardlinks)
or see the guard's refusal semantics, so the enforcement layer overrides the
class to C when it refuses — a boundary refusal is definitionally "anything
the charter forbids" (§12.2).

---

## 2026-08-13 — TD-603: Filesystem read tools

Decisions made while building the read handlers.

### 1. Handlers are thin async wrappers over sync cores

**Decision:** `fs_read`/`fs_list` are async handlers that delegate the
blocking filesystem work to sync helpers via `asyncio.to_thread`.

**Rationale:** AGENTS.md §6 forbids blocking calls in the event loop; the
dispatch chokepoint awaits handlers, so any blocking I/O there would stall
the session. The sync cores stay unit-testable without an event loop.

### 2. `fs_list` reuses the manifest's ignore set

**Decision:** Directory listing prunes the same `_FALLBACK_IGNORE` dirs the
workspace manifest prunes (`node_modules`, `__pycache__`, `.venv`, `.git`,
`.tst`).

**Rationale:** One ignore set for the workspace keeps listings and the
manifest consistent. Gitignore-native listing (via `git ls-files
--exclude-standard`) exists in the manifest and can be reused later if
subdirectory listings need it; the fallback set covers the practical junk
dirs for v0.1.

### 3. Truncation marker states totals, and only for cap-truncation

**Decision:** The read handler caps formatted output at 2000 lines; when the
cap cuts a file, a marker states total lines and bytes. An explicit
`limit`/`offset` window is a window — no marker.

**Rationale:** "Large files truncated with explicit markers and a stated
total size" (criterion 4) refers to the cap; labelling an explicit window
"truncated" would mislead the model about the file's length.

### 4. Binary and encoding refusal lives at the handler

**Decision:** NUL-bytes-in-head detection and UTF-8 decode failure both
refuse with an explanatory message instead of dumping bytes.

**Rationale:** The model should never receive raw binary or mojibake bytes —
an explanatory refusal lets it pick another path (e.g., hash the file,
inspect via shell once TD-605 lands).

---

## 2026-08-13 — TD-705: Checkpoint commits

Decisions made while building the session-branch checkpointer.

### 1. Plumbing-only snapshots over a throwaway index

**Decision:** Checkpoints are built with `read-tree` + `add` + `write-tree`
+ `commit-tree` under a temporary `GIT_INDEX_FILE`, then published with a
single `update-ref` of `refs/heads/tst/session/<id>`. No command the module
runs ever reads or writes HEAD, the user's index, or the working tree.

**Rationale:** "Pre-existing uncommitted user changes are never clobbered"
has to be a structural guarantee, not a careful-usage one. Porcelain
(`add`/`commit` against the real index) can stash, refresh, or conflict with
user state; plumbing against a private index cannot.

### 2. Parent chain is session tip → HEAD → none

**Decision:** Each checkpoint's parent is the previous checkpoint if the
session branch exists, else HEAD, else no parent (unborn HEAD repos).
Dirty working-tree changes outside the written paths are neither captured
nor disturbed.

**Rationale:** The session branch is the undo stack (spec §12.8), so its
history must be a clean chain of the agent's writes on top of whatever the
user had committed. Seeding from HEAD keeps the first checkpoint reviewable
as a normal diff against the user's last commit.

### 3. Dirty baselines are reported once, then checkpointing continues

**Decision:** At the first checkpoint in a repo with uncommitted changes,
the user gets one `dirty_baseline` notice; checkpointing proceeds anyway.
A rebase in progress is different: checkpoints are *skipped* (not disabled)
until it ends, with one `rebase_in_progress` notice.

**Rationale:** Dirty state is common and harmless to snapshot from (we only
capture the committed tree plus our own writes), so it warrants a notice,
not a stop. Mid-rebase, the repo's state is genuinely in flux and the user
is driving — writing refs then would be noise at best, so we stand down and
resume.

### 4. Degradation is informed-once, sticky per kind, never fatal

**Decision:** A non-git workspace disables checkpointing for the session
with one `no_git` notice. Every notice code is delivered at most once per
session via the `checkpoint_notice` daemon event. A checkpoint failure can
never fail the write that triggered it.

**Rationale:** The feature is an enhancement, not a precondition — the
acceptance criteria require everything else to keep working. Repeating the
same notice on every write would be spam; the event log keeps the first one
for the audit trail.

### 5. Fixed agent identity, independent of user git config

**Decision:** Checkpoint commits are authored as `TST Desk
<tstdesk@localhost>` via per-invocation environment variables, never the
user's configured identity.

**Rationale:** Checkpoints must work in repos with no user config (fresh
clones, CI checkouts) and must be visibly agent-authored in `git log` so a
human reviewing the branch can tell the undo stack from their own history.

### 6. The seam lives in the dispatcher, after handler success

**Decision:** `ToolDispatcher.dispatch` checkpoints after a successful
handler, using the canonical write paths captured by the boundary guard,
only for mutating tools with path fields and an attached session. The
result carries `checkpoint_commit` / `checkpoint_notice`; the loop mirrors
the notice into the event log.

**Rationale:** Same shape as the classifier chokepoint and the boundary
guard — enforcement in the tool layer, not per-handler, so a future write
tool cannot forget to checkpoint. Canonical paths (not raw model arguments)
guarantee the snapshot covers exactly what the guard approved.

---

## 2026-08-13 — TD-604: Filesystem write tools

Decisions made while building the write handlers.

### 1. Atomicity is temp file in the target directory plus `os.replace`

**Decision:** Every write goes to a hidden temp file created in the
target's own directory (`mkstemp` with a dotted prefix), is flushed and
fsynced, then `os.replace`d over the target. On any failure the temp file
is removed and the previous target is untouched.

**Rationale:** A rename is atomic only within a filesystem, so the temp
file must live beside the target. "No partial file on failure" then holds
structurally: the target is either the old content or the new content,
never a prefix of the new.

### 2. Write handlers raise; read handlers return error strings

**Decision:** `fs_write`/`fs_edit` raise on failure (missing file,
ambiguous target, decode error) and dispatch converts the exception into a
`handler_error` result; the read handlers keep returning `"Error: …"`
strings.

**Rationale:** A failed write must be `status="error"` so it is not
checkpointed — silent success-with-error-message would put a commit on the
undo stack for work that did not happen. Reads have nothing to checkpoint,
and an explanatory string lets the model self-correct without burning the
turn.

### 3. `fs_edit` is exact single-occurrence replacement, no `replace_all`

**Decision:** `fs_edit` fails loudly when the target occurs zero times
("not found") or more than once ("ambiguous: N occurrences"). There is no
bulk-replace flag.

**Rationale:** "Failing loudly if the target is absent or ambiguous" is the
acceptance criterion, and the fix for ambiguity is more context in
`old_string` — something the model can do reliably. A `replace_all` flag
would let an under-specified edit touch every match, which is exactly the
silent-corruption mode the loud failure exists to prevent.

### 4. The diff is computed at the dispatcher seam, not in the handlers

**Decision:** Dispatch snapshots the canonical write targets (best-effort
UTF-8, capped) before the handler runs and after it succeeds, renders a
unified diff, and attaches it to the `tool_result` event's new `diff`
field. A write that changed nothing carries no diff; a non-diffable target
(binary, oversized) writes fine with `diff: null`.

**Rationale:** "Every write produces a diff" is guaranteed in exactly one
place — the same seam that checkpoints — so a future mutating tool gets
diffs without implementing them. Handlers stay free to return whatever
summary helps the model.

### 5. `fs_write` keeps the pre-registered `append` flag

**Decision:** The registry (TD-601) shipped `fs_write` with an
`append: bool` option; the handler implements it as read + concatenate +
atomic write rather than dropping the flag.

**Rationale:** The schema is already promised to the model; removing a
declared argument would be a protocol regression. Append reuses the same
atomic path, so it costs nothing extra.

---

## 2026-08-13 — TD-605: Shell execution

Decisions made while building the shell handler.

### 1. Every handler receives `tool_call_id` as a keyword argument

**Decision:** Dispatch calls `handler(session=session, tool_call_id=tool_call_id,
**arguments)`; all handlers accept a trailing `tool_call_id: str = ""`
parameter even when they ignore it.

**Rationale:** `shell_output` timeline events must carry the invocation id
so the UI can attach streamed output to the tool call that produced it.
Alternatives — positional correlation, signature inspection, side channels —
all made the UI derive truth it was never given. A uniform kwarg is boring,
explicit, and keeps direct test calls (`fs_read(None, path)`) working.

### 2. Streamed output is a first-class daemon event

**Decision:** New `shell_output` protocol event (`session_id`,
`tool_call_id`, `stream: "stdout" | "stderr"`, `chunk`) appended to the
session event log per chunk as output arrives.

**Rationale:** "Streamed to the timeline as they arrived, not buffered to
the end" (criterion 3) means the timeline — the event log — must receive
chunks live. Reusing `tool_result` or `assistant_delta` would either buffer
to the end or mislabel command output as model speech.

### 3. Timeout and cancel are results; refusals are errors

**Decision:** A timed-out or cancelled command returns `status="success"`
with a leading marker line (`timed out after Ns — process group killed` /
`cancelled — process group killed`) and whatever output was captured; the
process group is SIGKILLed with no SIGTERM grace. `status="error"` is
reserved for refusals and infrastructure failures (allowlist, bad timeout,
missing workspace, spawn failure).

**Rationale:** Criterion 5 establishes that execution facts are results the
model reasons about; criterion 2 wants the command dead on timeout, and a
grace period turns a deadline into a suggestion. A child killed by SIGKILL
reports `-9` as its exit code — consistent with shell convention.

### 4. Allowlist matches the resolved binary, fail-closed

**Decision:** With `allowed_commands` set, each top-level segment's leading
binary is resolved with `shutil.which` and matched by basename;
unresolvable binaries, unparseable segments, and backtick substitution are
refused. Wrapper binaries (`sh -c`, `sudo`, `env`) match as themselves.

**Rationale:** Matching the resolved basename lets `git` and `/usr/bin/git`
both match `git` while defeating PATH-shadowing; refusing the unparseable
keeps the check fail-closed. Wrappers are deliberately not pierced — the
allowlist is a policy rail, not a sandbox, and spec §12.5's container is
the real isolation boundary for autonomous runs.

---

## 2026-08-13 — TD-1001: Application shell

Decisions made during the application shell build.

### 1. Window-state persistence plugin

**Decision:** Use the official `tauri-plugin-window-state` (v2) to persist window
size/position state across launches.

**Rationale:** Persisting window geometry is a solved problem in Tauri v2 — the official
plugin writes the state to disk and restores it on launch. A custom mechanism (manual
save/load of bounds) would reimplement the same logic with more surface area and platform
edge cases (multi-display, maximized state). The plugin is first-party and permissively
licensed. The window keeps native chrome; geometry persistence is the only behavior
added by the host.

### 2. Divider position via `localStorage`

**Decision:** The split-pane divider position persists to the webview's `localStorage`,
not to a Rust-side file or the window-state plugin.

**Rationale:** The divider is a frontend layout concern with no daemon or host contract.
`localStorage` inside the Tauri webview outlives relaunches and is the simplest correct
place for a per-workspace UI preference. Reading it is synchronous and avoids a Rust/Tauri
round trip. This keeps the host thin (AGENTS.md §6).

### 3. Native window chrome

**Decision:** Keep the default native title bar / window decorations in v0.1; no custom
frameless titlebar.

**Rationale:** Native chrome is the most robust cross-platform choice with zero additional
dragging/hit-testing code. A custom titlebar is a stylistic enhancement that adds
platform-specific complexity (macOS traffic lights, Windows caption buttons) with no
v0.1 acceptance criterion requiring it. Deferred; revisit if a later story mandates it.

### 4. Theme via `prefers-color-scheme`

**Decision:** Light/dark theme is driven by the CSS `prefers-color-scheme` media query
over semantic token variables; the daemon/host sends no theme signal.

**Rationale:** TD-1001's criterion 5 is "themes following the OS preference." A media-query
approach needs no JavaScript state and no host wiring, and automatically follows the OS
setting at runtime. Semantic tokens mean components never reference raw colors, so the
theme switch needs no component changes. A user-overridable theme is a future, optional
enhancement.

### 5. Static adapter for Tauri packaging

**Decision:** Use `@sveltejs/adapter-static` (SPA mode: `ssr = false`,
`prerender = true`, `fallback: '200.html'`) instead of `@sveltejs/adapter-auto`.

**Rationale:** `adapter-auto` detects no production environment for an embedded
Tauri build and emits nothing, leaving `tauri build` without its `frontendDist`
input (`ui/build`). adapter-static emits a prerendered `index.html` plus a
fallback shell — all the webview needs. The UI has no server runtime by design
(AGENTS.md §1), so nothing is lost by dropping SSR. adapter-static is
first-party and MIT-licensed.

---

## 2026-08-13 — TD-1002: Daemon supervision

Class B — recorded per AGENTS.md §5.

### 1. Spawn resolution order

**Decision:** `$TSTD_PATH` env → `tstd` on PATH (`which` crate) → dev/`debug_assertions`
fallback. The fallback prefers the core venv's `tstd` script directly
(`../core/.venv/bin/tstd`), and only falls back to `uv run --directory ../core tstd` if that
is absent.

**Rationale:** `uv run` inserts a wrapper process: the daemon becomes a *grandchild*, so its
port-file `pid` never matches the host's direct child pid, which breaks pid-keyed stale-file
detection. Spawning the shebang script directly keeps the daemon pid = the host's child. This
was caught by the `daemon_supervision` integration test (port file pid 3983 vs uv pid 3981).

### 2. Port-file hygiene

**Decision:** `port.json` carries `{port, token, pid}`, written atomically (tmp + rename, mode
0600), deleted on clean stop. The host waits for a file whose embedded `pid` equals the child
it spawned.

**Rationale:** After a SIGKILL crash the old port file lingers; mere existence is not "the new
daemon is up." Keying on pid (or better, the fresh token) closes that race. Mirrors the Python
integration test's pid-keyed wait.

### 3. Graceful shutdown is one cross-platform path

**Decision:** A new client message `{"type":"shutdown"}` makes the daemon stop cleanly (exit
0, port file removed). The host falls back to `SIGKILL` after a 5 s grace.

**Rationale:** A single WS shutdown path avoids SIGTERM-works-only-on-Unix and stdin-EOF
platform splits. The host does not await a WS close-ack after sending `shutdown` — the daemon
tears down the connection during its own shutdown, so a close handshake can race into a hang.

### 4. Host quit paths (macOS)

**Decision:** Intercept the X-button via `CloseRequested` + `api.prevent_close()` → async
clean shutdown → `app.exit(0)`, with a `TODO(v0.3)` marker for detached sessions. The catch-all
is `RunEvent::Exit` (best-effort synchronous kill by pid), NOT `ExitRequested`, which macOS
raising semantics make unreliable.

**Rationale:** Tauri 2 does not reliably raise `ExitRequested` on Cmd+Q/dock quit
(tauri#13778/#9198). The daemon's own `--parent-pid` watchdog is the real no-orphan guarantee
for force-quit; the host kill is best-effort window-closing.

### 5. Orphan prevention is daemon-side

**Decision:** `--parent-pid` flag; a watchdog task polls host liveness (POSIX `os.kill(pid,0)`,
Windows `ctypes` OpenProcess) 1×/s and shuts down if the host dies.

**Rationale:** Only a daemon-side watchdog survives `kill -9` of the host; no destructor or
`RunEvent` on the host does.

### 6. Session persistence for restart

**Decision:** `sessions.json` snapshot in the data dir (atomic, 0600). Non-terminal sessions
rehydrate as new state `interrupted`. New protocol pair `list_sessions` → `session_list`.

**Rationale:** UI rule "never derives truth it wasn't given" forces daemon-side persistence.
Durable event log is explicitly v0.3 (TD-205); v0.1 persists only the registry list, and
rehydrated live sessions are tombstones.

### 7. Platform + packaging scope

**Decision:** macOS-only for this story. Windows/Linux build, packaging, and manual
no-orphan verification are deferred to the packaging milestone.

**Rationale:** User decision: develop now, package later. The watchdog and shutdown code are
written cross-platform, so the packaging pass needs to confirm, not redesign.

### 8. Verification documentation

**Decision:** No new verification `.md`. TD-1002's acceptance checkboxes are ticked in
`docs/tst-desk-backlog.md` with a completion note.

**Rationale:** User instruction: the backlog is where completed-story notes go.

---

## TD-1403 — Context assembler cross-cutting suite

### 1. Goldens are regenerated via an env var, not a pytest flag

**Decision:** `_assert_golden` rewrites its golden file when `TSTD_UPDATE_GOLDEN=1` is set;
otherwise it compares. No `--update-goldens` conftest option was added.

**Rationale:** The repo had no golden-file convention before this suite. An env var keeps the
machinery inside the suite's own file (no shared conftest state, no new pytest surface) and
forces golden regeneration to be a deliberate, visible act followed by a diffs review. If a
second suite needs goldens, promote the helper to a shared conftest then — not before.

### 2. Goldens are machine-independent via path stripping

**Decision:** Absolute fixture paths inside golden output are replaced with `<ROOT>` before
comparison; the golden drop-in point is the test's `tmp_path`.

**Rationale:** Assembled steering blocks embed absolute source paths (provenance comments).
Without stripping, goldens would differ per machine and per test-run directory; with it, a
golden diff means behavior changed, not that the tmp directory moved.

### 3. Sharp edges are pinned, not fixed

**Decision:** Two discovered behaviors are asserted as-is with a comment naming the edge: a
root file that `@`-imports a nested steering source emits the content twice (distinct
provenance), and an unclosed code fence in an imported file protects @-directives to EOF.

**Rationale:** A test suite that silently "fixes" behavior by asserting changed expectations
hides the change. Pinning documents intent for the reviewer; changing either behavior is a
separate story with its own decision trail.

---

## 2026-08-13 — TD-1402: Security suite

### 1. Redaction filter argument bug fixed, not accommodated

**Decision:** `SecretsRedactionFilter` redacted `%s`-style log args by re-substituting from
the *original* argument for each pattern (`args[i] = pattern.sub(..., arg)`), so any
pattern after the matching one overwrote the redaction. Only entries matching the last
pattern in `SECRET_PATTERNS` survived. Fixed to accumulate on the narrowed value
(`arg = pattern.sub(...); args[i] = arg`).

**Rationale:** The suite found a live leak: any credential logged as a positional arg
(`logger.info("key %s", key)`) was redacted only if it was a private-key header. A security
suite that documents known-broken behavior instead of pinning the fix would be security
theater. One-line fix, no contract change.

### 2. Unimplemented surfaces are skip-marked, not quietly absent

**Decision:** Criteria with no implementation surface to test — tool-level steering refusal
(TD-604 pending), environment sanitization (TD-605 pending), and redaction of audit/event
surfaces and error strings (no story exists) — are `pytest.skip` markers with reasons that
name the dependency, so the gaps print on every run (`rs` lines) instead of vanishing from
the count.

**Rationale:** "Every test in this suite is a release blocker" only holds if the gaps stay
visible. Skips with reasons are the honest midpoint between absent tests (invisible) and
`xfail` (implies the code exists and misbehaves — it doesn't exist). The skip bodies state
the target invariant so unskipping is mechanical. The audit/event redaction gap needs a new
backlog story; flagged in the story report.

### 3. Suite asserts structural invariants, not just behaviors

**Decision:** Three tests pin structure rather than behavior: C-rule precedence in
`RULE_TABLE` (an A rule evaluated before a C rule is an auto-approval bypass), dispatch
call-site confinement (a new `.dispatch(`/`dispatch_many(` caller is a new chokepoint risk),
and the absence of a `host` parameter on `WebSocketServer.start` (loopback is hardcoded; a
host knob must route through `validate_interface`).

**Rationale:** Behavior tests prove today's code is safe; structural tests force the
conversation when tomorrow's code changes the preconditions. In a suite whose every test is
a release blocker, shape-level guards are the cheap half of the defense.

### 4. Branch base

**Decision:** Branched from `da1f2db` (the TD-602/702/603 integration tip) rather than
main; the suite tests machinery that does not exist on main. Adds one test file plus the
one-line logging fix. Merge order: after the TD-605 chain lands on main.

**Rationale:** Same chained-branch pattern as TD-903 on TD-901. New-file-only diffs rebase
trivially; the logging touch is byte-compatible with both parents.

---

## 2026-08-13 — TD-706: Boundary configuration

Decisions made while adding `.tst/config.yaml`.

### 1. Config shape mirrors the spec §12.4 charter

**Decision:** `.tst/config.yaml` uses nested `boundary:` (`writable_paths`,
`allowed_commands`, `network`) and `caps:` (`spend_usd`, `wall_clock_hours`,
`max_iterations`) sections, validated by pydantic with errors that name the
offending key.

**Rationale:** Same shape as the charter contract in the spec, so a charter
section maps onto config one-to-one; TD-801 later adds `policy:` beside
`boundary:`/`caps:`.

### 2. Defaults: workspace-only writes, no network, conservative cap

**Decision:** Absent config → `writable_paths: ["**"]`, `network: "deny"`,
`spend_usd: 25.00`, `wall_clock_hours: 8.0`, `max_iterations: 200`.

**Rationale:** Criterion 2. "Deny" maps to an empty `allowed_hosts` so any
network call is Class C; `["**"]` keeps writes inside the workspace.

### 3. The boundary resolves once, at workspace open

**Decision:** The daemon loads the config on `open_workspace`, stamps it on
the session, emits a new `boundary_update` event (after `session_state`), and
the loop builds the classifier/guard `Boundary` from it — `writable_paths` →
`writable_patterns`, `network` → `allowed_hosts`. A bad config falls back to
defaults with the actionable error logged and surfaced in the event
`source`; the workspace still opens.

**Rationale:** One resolution point keeps classifier, guard, and UI agreeing
on the same wall. Refusing to open on a bad config would brick the workspace
over a typo; defaults-plus-surfaced-error is friendlier and still loud.

### 4. `.tst/config.yaml` is not steering-refused (for now)

**Decision:** The config file stays outside the PD §2.4 steering list
(AGENTS.md/CLAUDE.md/.tst/rules), matching the TD-1402 security suite's
explicit contract.

**Rationale:** PD §2.4's list is literal and the config only takes effect at
open — a mid-session write cannot move the running wall. **Open security
question:** a poisoned config *would* widen the boundary on the next
workspace open (the agent writing its own future wall). Closing that hole
means making `.tst/config.yaml` steering-refused, which overrides the
TD-1402 test — flagging for the user rather than silently changing another
story's security contract.

### 5. New event shifts replay seq expectations

**Decision:** `boundary_update` is a logged event (seq 2 after
`session_state`), so attach/replay tests' hardcoded seq numbers were updated.

**Rationale:** Events are seq-numbered and append-only; any new open-time
event shifts subsequent seqs. The UI round-trip fixture gained a
`boundary_update` sample.

---

## 2026-08-13 — M1 cross-track integration

Class B — recorded per AGENTS.md §5.

### 1. Merge everything into a scratch branch, fast-forward main at the end

**Decision:** Ten tracks were merged onto `integrate/m1-tracks` (from main `c8baabb`)
in dependency order — TD-901, TD-903, TD-405, the TD-602/603/702/703/705/604 chain,
TD-605, TD-1001, TD-1002, TD-1403, TD-1402, TD-706 — with full gates
(pytest/mypy/ruff/svelte-check/vitest, plus cargo test where the shell changed)
after each merge. Main receives only the green tip via fast-forward.

**Rationale:** Story branches were developed in parallel worktrees against a moving
main; integrating in one scratch branch keeps main continuous and leaves every
conflict resolution on the record as its own merge commit.

### 2. Resolutions of note

- **Tool-handler contract collision (TD-605 x TD-604):** dispatch now always passes
  `tool_call_id`; fs_write/fs_edit signature gained the parameter and every test
  stub handler was updated. The synthetic `ghost_probe` test replaces the old
  "real tool without handler" trick now that every builtin has a handler.
- **Redaction kept at one chokepoint:** TD-1402 fixed the per-arg redaction bug in
  the logging filter; TD-901 had already factored the same loop into
  `redact_secrets()`. The shared helper won — one implementation covers logs and
  the audit store, which is also the answer to the TD-1402 finding that redaction
  of audit/event surfaces had no home.
- **Cargo lockfile regenerated** after unioning TD-1001 (window-state plugin) and
  TD-1002 (tokio/tokio-tungstenite et al.) dependency sets; `cargo check`/`test`
  verified the union compiles.
- **TD-1402 env-sanitization skip released** the moment TD-605 landed:
  `sanitized_env()` + an end-to-end `run_shell` environment-dump test.

---

## 2026-08-13 — TD-1003: Protocol client

### 1. WebSocket transport is injected, not imported.

**Decision:** `ProtocolClient` takes a `socketFactory` and a `getDaemonInfo` provider (
`ClientOptions` in `ui/src/lib/client.ts`); it never imports `WebSocket` or Tauri APIs.

**Rationale:** This is what makes the reconnect/backoff/gap logic unit-testable under vitest's
node environment with a fake socket, and it keeps the client pure. Tauri wiring lives one layer
up in `connection-status.ts`. TD-1003-1.

### 2. No gaps / no duplicates is enforced client-side by sequence gating.

**Decision:** Per session, accept an event only if its `seq` advances `lastSeq` by exactly one.
`seq <= lastSeq` is a duplicate and is dropped; `seq > lastSeq+1` is a gap and triggers a forced
re-attach at `from_seq = lastSeq+1`. Unknown event types still advance the seen `seq` so a
future type never fires a false gap on the next known event.

**Rationale:** The daemon's per-session log is the source of truth and its `seq` advances for
every event it emits (including types the TS mirror may not know). Advancing seen-seq on
unknowns and gating on the known stream is the only way to honor "no gaps, no duplicates"
without a durable server-side cursor (which is v0.3, TD-205).

### 3. Reconnect re-attaches every known session rather than re-sending a batch cursor.

**Decision:** On `hello_ack` of a reconnection, the client sends `attach{session_id,
from_seq: lastSeq+1}` for each attached session, letting the daemon's existing `events_from`
replay replay exactly the missed-tail.

**Rationale:** Reuses the attach/replay mechanism the daemon already implements
(`Daemon._handle_attach`), rather than inventing a new bulk-catchup message. Only sessions the
client is actively following are re-attached.

### 4. Connection state combines the real socket state with the host's supervision event.

**Decision:** The `connection-status.ts` store exposes `ws` (socket state: disconnected /
connecting / connected / reconnecting / stopped, from actual socket transitions) and `daemon`
(read only from the host's `daemon-status` Tauri event: starting / connected / crashed /
stopping / stopped plus port/restart). The banner renders from both.

**Rationale:** AGENTS §6 — "the UI never derives truth it wasn't given." `ws` is derived from
the live socket; `daemon` is forwarded verbatim from the supervisor. The UI infers nothing.

### 5. `protocol.ts` stays a hand-mirror kept in sync by the fixture generator.

**Decision:** The TD-1003 additions to `ui/src/lib/protocol.ts` (Shutdown, ListSessions,
HelloAck, SteeringReloaded, InstructionStack/Entry, SessionList/Summary, `interrupted`) are
added to `core/scripts/generate_protocol_fixtures.py` and re-generated, and asserted by
`protocol.test.ts`.

**Rationale:** Continues the existing TD-204 §4 cross-language sync strategy — one generator,
one shared JSON snapshot, asserts both directions, rather than two drifting copies.

### 6. Unknown event types dropped with a warning, never forwarded or fatal.

**Decision:** `dispatch` checks `type` against `KNOWN_EVENT_TYPES`; any unknown type logs a
`console.warn` and is not forwarded to `onEvent`.

**Rationale:** Meets the "never a crash" criterion against unknown future event types, while
still advancing the sequence cursor (see 2) so downstream gap detection stays honest.

---

## 2026-08-13 — TD-1401: End-to-end headless harness

Class B — recorded per AGENTS.md §5.

### 1. In-process daemon over the real WebSocket protocol

**Decision:** The harness runs `Daemon` in-process (no subprocess) but drives it
through a real `websockets` client: port file, hello token, open_workspace,
attach, user_message, approve, shutdown. The mock provider is injected via the
`Daemon(provider=...)` seam, whose type was widened to the loop's existing
`ProviderLike` protocol.

**Rationale:** A subprocess daemon adds process-lifecycle flake to a CI gate
without exercising anything more — the socket, protocol JSON, session loop,
tools, checkpoint, and audit paths are all the real ones either way. The
seam for injection already existed; widening its annotation to the protocol
the loop actually consumes is the honest type.

### 2. This story wires the tool stack into the daemon

**Decision:** `open_workspace` now builds the builtin registry + dispatcher
(`register_builtin_handlers` with the boundary's `allowed_commands`) and
hands both to `agent_loop`. Until this story, daemon sessions had no tool
dispatcher at all — tools were only wired in tests.

**Rationale:** The harness is the first client that needs a daemon session to
actually execute a tool call; the gap only surfaced because of it. This is
the M1-exit pattern working as intended. The empty `allowed_commands`
default stays fail-closed (shell unusable until allowlisted, TD-706/605).

### 3. Approval is asserted at the protocol surface, not gated

**Decision:** The harness sends `approve` when the `tool_call` event arrives
and asserts the classification on that event (`decision_class`). No
execution gate is built here — that is TD-802.

**Rationale:** Criterion's chain is classification → approval → execution;
today the classifier annotates and dispatch proceeds. Faking a gate inside
the harness would test code that ships nowhere. When TD-802 lands, this
harness's approve message drives the real gate unchanged.

### 4. Steering proof is the mock's recorded request

**Decision:** "Steering resolved" asserts the workspace `AGENTS.md` content
appears in the system prompt of the mock's first recorded request.
`steering_reloaded` (TD-509) is a hot-reload event and never fires on a
fresh session's first turn (`_last_prefix_hash` is None), so the event
stream cannot prove initial resolution.

**Rationale:** The criterion is that steering reached the model. The recorded
request is the ground truth of that, and the mock already records it.

---

## 2026-08-13 — TD-1405: Audit and event surface redaction

**Context:** TD-1402 found that `redact_secrets` guarded only the log
stream: tool-call arguments and results flowed into the event log — and
from there to client replay, the WebSocket broadcast, and the audit
writer — unredacted. Two section-4 tests were skip-marked against this
story.

### 1. The event log's `add()` is the redaction chokepoint

**Decision:** `SessionEventLog.add()` stores and broadcasts a redacted
*copy* of every event (`_redact_event`): `ToolCall.arguments` (recursive
`redact_structure`, promoted from audit.py to logging.py so both
pipelines share one implementation), `ToolResult.output`/`diff`,
`ShellOutput.chunk`, `Error.message`, and `SessionState.reason`.

**Rationale:** Every consumer — replay, broadcast, the TD-902 audit
writer — reads events out of `add()`. Redacting at insertion covers all
three with one transform. The loop builds the model's tool messages from
dispatch results, not from logged events, so execution and the file on
disk keep the exact bytes (asserted in the suite).

### 2. `build_error` scrubs at construction

**Decision:** `build_error()` passes its message through
`redact_secrets` directly.

**Rationale:** The error envelope is written straight to the socket and
never passes through the event log, so the chokepoint cannot see it.
This is the only bypass of `add()` among client-visible surfaces.

### 3. The audit store keeps its own scrub

**Decision:** `AuditStore.append_tool_call` still scrubs arguments
itself (now via the shared `redact_structure`), even though the writer's
feed is already redacted upstream. Result output remains a SHA-256 hash
— now of the redacted text.

**Rationale:** Defense in depth at the persistence boundary costs one
idempotent pass. Hashing redacted output also closes a side channel: a
stored hash can no longer be rainbow-tabled back to a known secret
shape.

### 4. What is deliberately not redacted

**Decision:** User messages and assistant deltas pass through
unredacted.

**Rationale:** The user's own message goes to the model verbatim by
design — redacting the stored copy would fork the transcript from what
the model saw. Deltas are the model's own stream; the exfiltration
vector the story closes is tool payloads and errors, and the known-shape
patterns (API keys, PEM headers) do not appear in ordinary prose.
Recorded here so the exclusion is a decision, not an omission.

---

## 2026-08-13 — TD-1404: Performance baselines

### 1. First-token latency is a pipeline measurement

**Decision:** The first-token metric uses the mock provider (instant
responses), so it measures the internal pipeline — context assembly,
routing, event fan-out — and never network or model time.  The baseline
file's `note` field says this explicitly.

**Rationale:** A live-model measurement would conflate provider variance
with our own regressions and could never run in CI.  The mock isolates
the part we control.

### 2. The threshold is 3x baseline or +250 ms

**Decision:** `test_benchmarks.py` fails when a fresh median exceeds
`max(3 * baseline, baseline + 0.25s)`.

**Rationale:** Timing tests on shared CI runners flap; a tight threshold
trains everyone to ignore it.  The slack term keeps sub-100 ms metrics
quiet on loaded machines while the factor still catches the regressions
that matter — an O(n) scan added to `open_workspace`, a steering
assembly that grows superlinear.  Baselines re-record only via
`scripts/benchmarks.py --record`, a deliberate act; the test never
writes the file it checks against.

### 3. Timeline render is recorded as a named gap, not a proxy

**Decision:** `timeline_render_1000` sits in the baseline file as
`null` with `pending: TD-1005`, and a structural test keeps the pending
set honest.

**Rationale:** The timeline component does not exist yet (TD-1005 is in
planning).  A render proxy — protocol ingestion, store update — would
measure the wrong thing and give false comfort under the criterion's
name.  When TD-1005 lands, the metric gets a measurer and a baseline in
one follow-up.
---

## 2026-08-13 — TD-707: Cap enforcement

Decisions made while adding spend/wall-clock/iteration caps.

### 1. Pause is a distinct fault state, not an approval

**Decision:** A new `paused` session state (running → paused → running); the
loop parks in `wait_for_resume()` (an asyncio event — no spinning or
polling) and the `session_state` event carries the fault summary naming the
cap and the numbers. No `approval_request` is emitted.

**Rationale:** Criterion 3. A cap is a hard stop the user must act on (raise
the cap), not a per-action question the model can answer.

### 2. Checked before every model call, measured per session

**Decision:** `_cap_violation()` runs before each model call: spend from
`CostTracker.session_cost()`, wall-clock from the loop's session start, and
an iteration counter incremented per model call. Violation parks the loop;
on resume the caps are re-checked before the next call.

**Rationale:** Criterion 1 + 2. One pre-call chokepoint covers the first
call of a turn and every round-trip call; a single expensive call trips the
cap before the *next* call (the `$0.01` test's "halts on the first call").

### 3. Resume reloads the boundary, then re-checks

**Decision:** A new `resume` client message makes the daemon reload
`.tst/config.yaml` (so raised caps take effect), emit a fresh
`boundary_update`, and signal the parked loop. If the new caps are still
exceeded, the loop re-pauses immediately.

**Rationale:** Criterion 4. Session state (messages, events, tool results)
is never touched — the loop continues the same turn where it parked.

### 4. Caps come from the TD-706 config, enforced in the loop

**Decision:** The loop reads `session.boundary_config.caps` (TD-706); the
classifier's `cap-exceeded` rule (TD-701) stays for decision classification.

**Rationale:** One config, one enforcement point. TD-707 enforces; TD-706
defines; TD-701 classifies.
---

## 2026-08-13 — TD-801: Policy model

Decisions made during the policy model build.

### 1. Argument-summary semantics

**Decision:** Policy patterns match against a normalized per-call summary: the
first `path_fields` value as a workspace-relative POSIX path (absolute when it
escapes the workspace), the first `host_fields` value for network tools, the
`command` argument verbatim for the shell tool, and canonical JSON otherwise.

**Rationale:** Criterion 1 needs "(tool, argument pattern)" to match something
deterministic. Reusing the tool's declared metadata (`path_fields` /
`host_fields`, TD-702) keeps the reduction honest — no heuristics over raw
text. The `command` convention lets rules read naturally (`npm test*`), the
spec §6 example ("always allow this command in this workspace").

### 2. Specificity order and tie-break

**Decision:** Most-specific rule wins, ordered by: exact tool name over globs,
then fewer wildcards in the argument pattern, then longer literal pattern.
Rules identical on all three axes break toward the most restrictive effect
(never > ask > auto).

**Rationale:** Criterion 4 requires deterministic precedence. Declaration
order is fragile (users append rules over time); a structural order is
testable and stable. Restrictive tie-break fails safe — ambiguity resolves
toward asking, never toward acting.

### 3. Class-C calls never resolve to `auto`

**Decision:** Even when a matching rule says `auto`, a class-C call resolves
to the configured `class_c_default` (`ask` or `never`).

**Rationale:** Criterion 5 ("policy never grants what the boundary forbids")
holds at two layers: dispatch refuses boundary-crossing calls before policy
is consulted (TD-602 ordering), and the resolver refuses to downgrade a C to
silent execution. Policy can tighten the wall, never widen it.

### 4. Policy owns its config section

**Decision:** `tstd/policy.py` loads and saves only the `policy:` key of
`.tst/config.yaml`; saves are section-preserving (boundary/caps carried
through) and atomic (temp file + replace). `boundary_config.py` is untouched.

**Rationale:** TD-706 owns the boundary loader with its own tests; extending
it would entangle two stories' modules. Both loaders read the same file and
ignore foreign keys (pydantic default), so the sections compose without
coupling. Atomic write follows the TD-604 workspace-file pattern.
---


## 2026-08-13 — TD-802: Approval flow

Decisions made during the approval flow build.

### 1. Gate placement: after boundary enforcement, before execution

**Decision:** The policy gate runs in `dispatch()` after the TD-602 boundary
block and before the diff snapshot/handler. `auto` runs; `never` returns a
structured `policy_denied` result; `ask` awaits the injected approval handler.

**Rationale:** Boundary refusals return before the gate, so policy can never
resurrect what the boundary refused (criterion 5, ordering layer). The
resolver adds a second layer — a class-C call never resolves to `auto`
(TD-801). Two independent layers, both tested.

### 2. Missing approval handler is a chokepoint bypass

**Decision:** A call resolving to `ask` with no `approval_handler` attached
raises `UnclassifiedToolCall`, the same exception the classifier chokepoint
uses. A `None` decision class at the gate is treated as B.

**Rationale:** If asking is impossible, executing would fail open and
returning an error would silently misclassify a policy outcome as a tool
failure. Raising makes the misconfiguration loud (prime §2.6's no-bypass
stance extended to the approval gate). None→B mirrors TD-703: fail toward
asking, never toward acting. Test dispatchers attach an explicit
auto-approver (`attach_auto_approver`) so mechanics tests state their intent.

### 3. Approval futures live on the Session

**Decision:** Pending approvals are `asyncio.Future`s in a dict on the
`Session`, resolved by `resolve_approval()` from any attached client. The
park is a bare `await` — no loop, no poll.

**Rationale:** The daemon owns sessions; the window is only a viewer (prime
§2.5). Client disconnect leaves the future parked (criterion 2/5); the
logged `approval_request` + `session_state(awaiting_approval)` events replay
on attach, which is what makes a parked session resumable from any client.

### 4. Timeout lives in policy config and equals denial

**Decision:** `PolicyConfig.approval_timeout_seconds` (the `policy:` section
of `.tst/config.yaml`), default `None` = wait indefinitely. Expiry returns a
structured denial to the model: "Approval timed out after Ns — treated as
denial". The future is shielded so a late approve/deny resolves nothing.

**Rationale:** Criterion 4 demands configurability with an indefinite
default. Timeout-as-denial fails safe: an unanswered gate never silently
executes. The structured message lets the model choose another path rather
than hanging the turn.

### 5. `PolicyEffect` literal re-declared in policy.py

**Decision:** `policy.py` defines `PolicyEffect = Literal["auto","ask","never"]`
instead of importing `SideEffectClass` from `tools.registry`.

**Rationale:** The gate made dispatch import policy; policy importing
tools.registry cycled through tools/__init__ (registry → dispatch → policy).
The literal keeps imports one-directional; mypy sees identical types.

---


## 2026-08-14 — TD-1301: Python runtime bundling (sidecar)

**Decision:** Ship the daemon as a PyInstaller onefile sidecar
(`shell/binaries/tstd-<host triple>`, Tauri `externalBin` convention),
built by `core/scripts/build_sidecar.py`.  Resolution order in
`shell/src/daemon.rs`: `$TSTD_PATH` → bundled sidecar (exe sibling) →
`tstd` on PATH → dev fallback (venv / `uv run`, debug builds only).

**Why onefile over onedir or embed-Python:** Tauri's `externalBin`
expects a single executable next to the app binary; onedir would need a
bundle-layout carve-out per platform, and embedding a Python.framework
by hand is the schedule risk this story exists to burn down.  Onefile
pays an unpack-to-temp cost at boot — measured 2.0 s to `port.json` on
an M-series host, inside the 3 s budget.  Revisit only if the budget
tightens.

**Why the sidecar beats PATH:** the shell and daemon are version-locked
(protocol handshake, port-file contract); a user-installed `tstd` of a
different vintage must not win.  `$TSTD_PATH` stays the explicit escape
hatch for development against a packaged shell.

**Bundle size:** 18.9 MB (aarch64-apple-darwin) — interpreter plus the
daemon's dependency closure.  Documented per the acceptance criterion;
justified against the fallback (requiring system Python + guided
installer), which trades 19 MB of download for a per-user support
surface.

**Class B** — packaging architecture, recorded here; clean-VM launch
verification stays a manual step alongside TD-1302's per-platform
installs.

## 2026-08-14 — TD-1301 addendum: externalBin injected at package time

**Decision:** `shell/tauri.conf.json` does NOT carry `externalBin`.
`tauri-build` validates sidecar paths at compile time for every
`cargo build/test/clippy`, so a committed entry hard-breaks the Rust
lane (CI clippy job, local cargo test) whenever `shell/binaries/` is
absent — which is always, since the artifact is gitignored.  Instead
`ui/package.json`'s `tauri:build` passes
`--config '{"bundle":{"externalBin":["binaries/tstd"]}}'`, and
`beforeBuildCommand` builds the sidecar first.  Dev builds and cargo
tooling never see the entry; the packaged app always does.

**Class B** — corrects the TD-1301 wiring one integration later.

## 2026-08-14 — TD-1302: Ship v0.1 unsigned

**Decision:** v0.1 bundles ship without code signing or notarization.
Per-platform first-run warnings are documented in the README.  Revisit for
v0.2 or the first external-user release, whichever comes first.

**Benefit of signing:** removes the Gatekeeper/SmartScreen first-run
friction; macOS notarization is effectively required for a broad audience
(right-click-Open is a power-user answer).  **Cost:** an Apple Developer
Program membership, a Windows EV/OV certificate, plus notarization and
signing steps wired into every release build — real money and, more to the
point, secrets and infra the release pipeline does not yet have a home
for.  For an internal v0.1 with named users, the warning text is enough.

## 2026-08-14 — TD-1303: Release workflow shape

**Decision:** `package.yml` stays the platform builder (plus a
`workflow_call` trigger); `release.yml` fires on `v*` tags, reuses that
matrix, and only publishes — checksums, changelog, `gh release create`.
Tag validity is gated twice: a release-job step asserts the tag equals all
four component versions, and `core/tests/test_version_consistency.py`
fails CI the moment the files drift.

**Rationale:** one build definition (no divergent packaging between
validation and release), and the changelog is generated from the
`TD-###: subject` convention rather than maintained by hand — the
convention was already universal, so the generator is a regexp, not a new
discipline. TD-1303 found ui/package.json at 0.0.1 while the rest
sat at 0.1.0; the test exists precisely because that drift is invisible
until release day.
---


---

## 2026-08-13 — TD-704: Decisions ledger

Decisions made while building `.tst/autonomy/DECISIONS.md`.

### 1. The ledger hook lives in dispatch, next to the checkpoint

**Decision:** `ToolDispatcher` gained a `ledger`; after a successful Class A/B
execution it appends an entry (what = tool + compact arguments, why = the
classification reason) and emits `decision_logged`. The loop wires a
`DecisionLedger(workspace)` by default.

**Rationale:** Dispatch is where the decision class (TD-702) and the
checkpoint commit SHA (TD-705) are both in scope — exactly the two things a
ledger entry needs. The append is best-effort: it can never fail the tool
result.

### 2. Class A requires a commit — enforced in the ledger, not the hook

**Decision:** `DecisionLedger.append` raises `ValueError` when a Class A
entry has no commit; the dispatcher catches that and skips (nothing is
logged, nothing emitted). Class B may be recorded without a commit (no
`Undo` line).

**Rationale:** AC 3 — "if an action cannot be attributed to a commit, it is
not Class A." Enforcing in the ledger module makes the rule true for every
writer, not just the dispatcher.

### 3. Atomic, locked, append-only

**Decision:** Appends open the file in `O_APPEND` mode under a POSIX
advisory lock (`fcntl.flock`; Windows relies on append-mode atomicity), and
the file I/O runs through `asyncio.to_thread`.

**Rationale:** AC 2 — concurrent sessions must not interleave partial
entries. O_APPEND + flock serializes writers; to_thread keeps the loop
unblocked (§6). A test appends 20 entries concurrently and reads all 20
back.

### 4. `decision_logged.commit` is optional

**Decision:** The event's `commit` field became `str | None` (was required).

**Rationale:** AC 3 allows uncommitted Class B entries; the event must be
able to carry them. Backward compatible (fixtures already pass a commit).

## 2026-08-14 — CI gates must match the gate developers run locally

**Decision:** `ci.yml` typechecks with `uv run mypy tstd` (strict, product
code) instead of `uv run mypy tstd tests`.

**Rationale:** When main was first pushed, the first live CI runs failed
mypy with 76 errors across 12 test files — every one annotation-shaped
(`no-untyped-def`, `type-arg`, `union-attr` on asserted fixtures), with the
suite behaviorally green (970 passed). No lane gates tests with strict
mypy locally; a CI gate stricter than the developer gate guarantees a red
main that trains everyone to ignore CI. Tests keep behavioral enforcement
via pytest; test typing can be tightened opportunistically per story
rather than in one sweep over five lanes' files.

**Also fixed (first-run defects):** rust leg now installs the Linux system
deps package.yml already used (glib-sys build scripts need webkit/appindicator
headers); typescript leg now installs uv before the protocol-fixtures step.

## 2026-08-14 — TD-505: external import approval

Spec §4.2 says imports from outside the workspace prompt for approval, but is
silent on the mechanism. Four structural choices, recorded here.

### 1. Reuse the tool-call approval machinery

**Decision:** `Session.request_import_approval(path)` reuses the TD-802/803
pending-future machinery, emitting an `approval_request` event with
`tool_call_id="external-import:<path>"`, `tool_name="external_import"`,
`decision_class="C"`, and `proposed_always_allow=None`.

**Rationale:** An external import is an untrusted-file-read, i.e. class C, so
it can never be always-allowed. Reusing the existing event + park/approve/deny/
timeout/replay/redaction paths avoids any new protocol message, and TD-1007's
approval card renders it unchanged.

### 2. Allowlist lives in `.tst/config.yaml`

**Decision:** A top-level `approved_external_imports: [...]` key, loaded and
saved section-preservingly (`load_approved_imports`/`save_approved_imports`),
atomic write.

**Rationale:** "Remembered per workspace per file path" implies durable state.
`.tst/config.yaml` is already the per-workspace trust store (boundary TD-706,
policy TD-801), so a separate file/loader adds nothing.

### 3. Approval persists; denial is session-scoped

**Decision:** Approved paths are written to the allowlist; denied paths are
held in memory for the session and omitted with a warning, but not persisted.

**Rationale:** The criteria only require *approval* to be remembered. A denied
import still re-prompts on the next session, which keeps the door open for the
user to change their mind.

### 4. Two-phase assembly with a fixpoint gate

**Decision:** The sync import resolver detects external imports, omits
unapproved ones, and collects them as `pending_imports` on `AssembledSteering`;
the async loop then raises one approval per pending path and re-assembles until
none remain.

**Rationale:** Import resolution runs in a worker thread (`asyncio.to_thread`),
but approval is inherently async (parks the session). The fixpoint loop handles
nested external imports revealed only after an approval, bounded by TD-504's
max depth 4.

### 5. Denial warning surfaces via `import_issues` + structured log

**Decision:** No new timeline event type; a denied import records
`external import denied: <path>` in the assembly's `import_issues` and a
structured warning log line.

**Rationale:** There is no generic "warning" daemon event today; adding one
touches the protocol schema, the TS mirror, and the fixture generator. The
timeline (TD-1005) can render `import_issues` when it consumes assembly
warnings — a follow-up if a dedicated event is wanted.

### 6. Follow-ups from post-integration verification (same day)

Post-integration verification found four gaps; all but one closed in
`td/505-import-approval-followups`:

1. **Sticky approval card.** The import approval carries a synthetic
   `tool_call_id` that never reaches the dispatcher, so no `tool_result`
   ever paired with it — the approval card stuck and the timeline entry
   never resolved. `request_import_approval` now emits a `ToolResult`
   event on resolution: `success` on approve, `error` with
   `error_code="approval_denied"` on deny/timeout. No UI change needed —
   the approval store and timeline already key off that pairing.
2. **Malformed config crashed the loop.** `load_approved_imports` at loop
   start was unguarded, so one bad `.tst/config.yaml` killed every session
   at first message. Load now falls back to an empty allowlist with a
   warning (the daemon's existing tolerate-and-warn pattern), and the
   same guard wraps the save — a failed write keeps the approval
   in-memory for the session instead of crashing mid-turn.
3. **Inspectors ignored the allowlist.** Doctor's steering check and
   `get_instruction_stack` assembled without the durable allowlist, so
   approved imports still showed "awaiting approval" in both views. Both
   now forward it via a shared helper. Denied imports are *not*
   forwarded: denials are session-scoped by design and the inspectors are
   workspace-scoped views — "would prompt on a new session" is the honest
   durable answer.
4. **Comment-destroying config save** (not fixed): `save_approved_imports`
   round-trips through `yaml.safe_load`/`safe_dump`, discarding comments
   and formatting — systemic to `save_policy` too. Deferred as its own
   story.

**Swap-attack tradeoff (documenting, not changing).** The allowlist is
path-only: a file approved once can later be *replaced* (different content,
same path) without re-prompting. A content-hashed allowlist would
re-prompt on any change — safer, but it re-prompts on every legitimate
edit too, which trains users to rubber-stamp. Path-only matches how the
rest of the file-based trust model works (steering files themselves are
read fresh every turn without hashing). Recorded here so the tradeoff is
visible; revisit if threat models change.

## 2026-08-14 — TD-1005: Activity timeline

Class B — recorded per AGENTS.md §5.

### 1. New `tier_switched` daemon event

**Decision:** Added a `TierSwitched` event (`type: "tier_switched"`) to the daemon event
union, carrying `session_id`, `tier`, and `previous`. The `set_tier` message handler
(`Daemon._handle_set_tier`) applies the `TierRouter` override and appends the event to the
session log so the timeline can show manual routing changes.

**Rationale:** Criterion 1 lists "tier switches" as a first-class timeline entry, but the
closed daemon→client event set had nothing that announced one — `set_tier` applied the
router override silently. One typed event keeps the timeline honest (the UI never derives
truth it wasn't given, AGENTS §6) and makes the TS mirror addition trivial.

### 2. Pure store + thin runes wrapper split

**Decision:** `timeline.ts` holds the `Timeline` class and `eventToEntry` mapping with no
runes/DOM/Tauri imports; `timeline-store.ts` is a runes singleton that wraps it and reassigns
`entries = [...timeline.entries]` on every push.

**Rationale:** `timeline.ts` unit-tests under vitest's node environment (which has no Svelte
transform); `timeline-store.ts` mirrors the existing `connection-status.ts` runes pattern and
stays untested, same as the splitpane.ts (pure) / SplitPane.svelte (presentational) split.
Reassigning the list (not mutating) is what makes Svelte reliably invalidate after a
`shell_output` chunk mutates an existing entry's stdout/stderr buffer in place.

### 3. Shell streaming merges into the parent tool call

**Decision:** `shell_output` events (tool `shell`) append their `chunk` to the parent
`tool_call` entry's `stdout`/`stderr` buffer, keyed by `tool_call_id`; orphan chunks (no
matching tool call yet) are dropped rather than buffered.

**Rationale:** The timeline is chronological *entries*, not a raw event dump — a shell command
is one row whose output grows live under it, not a dozen chunk rows. Dropping orphans is the
simple, correct read of "the daemon is the source of truth": a chunk with no call is a gap
the client cannot reconcile, and buffering it would let the UI fabricate ordering the daemon
never sent.

### 4. Virtualization: fixed 32px rows + inline expansion

**Decision:** Collapsed rows are exactly 32px (border-box), so `computeWindow` math needs no
measurement. Expanding a row sets `.row.expanded { height: auto }` and the 5-row overscan
buffer absorbs the single-row height change without re-running the window.

**Rationale:** Virtualizing variable-height rows requires measuring every rendered row; fixed
collapsed heights make the window arithmetic trivial and O(1). One expanded row at a time
(only the focused row) is a deliberate product simplification — the overscan buffer is sized
to absorb one growth spike, which is all the single-expanded-row interaction model produces.

### 5. Backlog completion note, no new verification doc

**Decision:** TD-1005's acceptance checkboxes are ticked in `docs/tst-desk-backlog.md` with a
completion note; no separate verification `.md`.

**Rationale:** User instruction (recorded in TD-1002 §8): the backlog is where completed-story
notes go.
## 2026-08-14 — TD-1004: Chat pane

### 1. Rune-using modules are `*.svelte.ts`, imported as `.svelte.js`

**Decision:** `connection-status.ts` was renamed to
`connection-status.svelte.ts` (a `git mv`, not a rewrite), and every module
that uses runes must carry the `.svelte.ts` suffix. Import sites spell the
specifier as `*.svelte.js` explicitly — extensionless imports do not
resolve `.svelte.ts` under Vite 8/rolldown.

**Rationale:** vite-plugin-svelte only compiles runes in `.svelte` and
`.svelte.ts`/`.svelte.js` files. A `$state` in a plain `.ts` file ships to
the bundle untransformed and throws `ReferenceError` at load — this was
latent on main for `connection-status.ts` and would have bricked the app on
first real render. The naming convention makes rune usage visible in the
file listing and lets the compiler guard it.

### 2. UI modules never touch `ProtocolClient` directly

**Decision:** `client.ts` gained a general `send(msg)` (returns `false`
instead of writing when the socket isn't handshaken-idle), and
`connection-status.svelte.ts` re-exports the chat-facing surface:
`onDaemonEvent` (subscribe fan-out), `sendToDaemon`, `attachToSession`,
`detachFromSession`. Chat store and components import only from there.

**Rationale:** One WebSocket, one handshake, one owner. Fan-out keeps every
pane off its own connection and keeps client lifecycle (reconnect, seq
tracking) in exactly one place, matching the TD-1003 boundary.

### 3. Markdown pipeline: marked + highlight.js + DOMPurify

**Decision:** Assistant text renders through marked (parse) → highlight.js
(fenced code) → DOMPurify (sanitize) before `{@html}`. Code blocks carry a
copy button wired by one delegated click handler. Tests run under jsdom,
not happy-dom.

**Rationale:** Model output is untrusted HTML-adjacent content, so
sanitization is mandatory (prime directives). DOMPurify ≥ 3.4.8 is broken
under happy-dom — happy-dom's `Node.prototype.nodeName` getter defeats the
anti-clobbering fix (cure53/DOMPurify#1496), silently dropping allowed tags
and potentially retaining disallowed ones; upstream explicitly does not
support it. jsdom is DOMPurify's tested environment. The app itself is
unaffected (real WebKit webview).

### 4. Virtualization via @tanstack/svelte-virtual

**Decision:** Long conversations are windowed with `createVirtualizer`
(estimate 96px, overscan 6, `getItemKey` on message id), rows absolutely
positioned inside a full-height sizer, measured dynamically via
`measureElement`. The reactive `count` is driven by `setOptions` inside an
`$effect`, never captured at construction.

**Rationale:** AC — "Conversation history scrollable and virtualized for
long sessions." svelte-virtual is the maintained Svelte binding over
tanstack virtual-core; dynamic measurement handles variable-height markdown
without per-message size bookkeeping.

### 5. Session binding follows `list_sessions`, newest `updated_at` wins

**Decision:** The chat store binds the most recently updated session from
`session_list` events (requested at init), keeps the current session while
the daemon still lists it, and switches (detach old, clear, attach new)
when it disappears. Attach replays full history through the same reducer as
live events — there is no separate history path.

**Rationale:** The daemon owns sessions (prime directives); the UI asks
(`list_sessions` is answered by `daemon._handle_list_sessions`) rather than
inferring liveness. One reducer for replay and live streams guarantees
identical rendering for both.

### 6. Cancel shows for `running` and `awaiting_approval`

**Decision:** `showCancel` returns true for both `running` and
`awaiting_approval` turn states.

**Rationale:** The criterion says "whenever a turn is running"; an approval
wait is still a live turn the user must be able to abort. Deliberate
superset, recorded so a future tightening is a conscious change.

### 7. Streaming appends in place — stable row identity

**Decision:** `assistant_delta` mutates the last assistant message's `text`
in place (same object) when the turn is incomplete; only a new turn pushes
a new message object. Rows render in a keyed each-block; the scrollbar
gutter is reserved with `scrollbar-gutter: stable`.

**Rationale:** AC — "Streaming assistant output rendered smoothly, without
layout jump." Stable identity means the keyed list never re-mounts a row
mid-stream; the reserved gutter keeps the scrollbar's arrival from
reflowing the conversation.

## 2026-08-14 — `tier_state` event and live `cost_update` on the wire (TD-1006)

**Decision:** A new `tier_state` event carries `{tier, override, model_slugs}`
and is emitted at exactly three points: after `boundary_update` at
`open_workspace`, as the ack of a `set_tier` message, and at turn start only
when the tier actually changed (midtier handoffs/escalations).

**Rationale:** AC 2 (tier chips showing active tier and slugs, clickable to
switch) needs the tier before any turn runs and needs an ack when the user
pins one. Emitting only on change in the loop keeps repetitions off the wire;
clients that attach mid-session already get the open-time snapshot replayed.
`override` rides the same event so the pin marker needs no second channel.

**Decision:** `cost_update` is emitted per model call (and per classifier
call) from inside the loop, and gains `cost_by_tier`; classifier spend is
excluded from the per-tier map because it already rides `classifier_cost`.

**Rationale:** AC 3 — "live cost meter updating as costs accrue" — means per
call, not per turn; the tracker already aggregates, so emission is an
`event_log.add` at the point of `record`, keeping one source of truth.

**Decision:** The shell grants only `dialog:allow-open` for the workspace
picker; read/write filesystem permissions are refused outright.

**Rationale:** Least privilege — the picked path goes to the daemon over the
protocol, where TD-706's boundary validation applies; the UI process never
needs filesystem access itself.

**Decision:** Session state is a Svelte 5 rune module
(`session-status.svelte.ts`), and `vitest.config.ts` now loads the
`@sveltejs/vite-plugin-svelte` plugin so `.svelte.ts` modules resolve in the
test pipeline; imports use the documented `./x.svelte.js` specifier.

**Rationale:** Runes in module scope only compile in `.svelte.ts` files, and
vitest (plain node env) resolves/compiles them only with the plugin loaded;
the sibling `connection-status.ts` store relies on the same pattern with no
test import, which this makes one consistent idiom.

## 2026-08-14 — `set_tier` unification at the TD-1005/TD-1006 seam

**Decision:** TD-1005 and TD-1006 both grew a `set_tier` handler in parallel.
The merged daemon keeps one handler (TD-1006's richer error model:
`session_not_found` / `session_not_live` / `bad_request`) that emits both
events — `tier_switched` (transition, `previous` recorded) for the timeline,
then the `tier_state` snapshot ack for the title bar. The router reference
lives on the session (`sess.router`); the parallel daemon-side
`_tier_routers` dict was dropped.

**Rationale:** One owner of the mutation keeps the two events consistent by
construction, and the session is the natural lifetime for the router (the
dict needed its own cleanup and duplicated what the registry already keys).
The events stay distinct: the timeline wants a compact transition row, the
title bar wants the full state (override + slugs) — each consumer reads only
what it needs.

## 2026-08-14 — TD-803: Always-allow

Decisions made during the always-allow build.

### 1. Rule lifecycle is a daemon API, not file editing

**Decision:** "Always allow" writes a rule via an `always_allow` daemon
message; settings lists and revokes rules via `list_policy_rules` and
`revoke_policy_rule` messages.  The settings UI does not edit
`.tst/config.yaml` directly (contrast TD-707's cap-raising flow, where the
user edits the file and sends `resume`).

**Rationale:** "Always allow" originates as a one-click approval-card
action, so it needs a daemon message regardless.  Routing list/revoke
through the same channel keeps the policy file format an implementation
detail of the daemon (the window stays a viewer, prime §2.5) and funnels
every mutation through the atomic, section-preserving `save_policy` write
(TD-801 §4).

### 2. A saved rule's identity is `(tool, args)`

**Decision:** `add_rule` and `remove_rule` key on the `(tool, args)` pair.
`add_rule` replaces any existing rule with the same pair (idempotent), and
`revoke_policy_rule` removes by that pair.

**Rationale:** `PolicyRule` has no unique id; `(tool, args)` is the
narrowest stable identity that distinguishes two rules.  Idempotent add
keeps re-affirming "always allow" for the same call from piling up
duplicate rules in the settings list.

## 2026-08-14 — TD-1007: Approval cards

Decisions made during the approval-card build, completed on top of TD-803's
always-allow daemon support.

### 1. The "Always allow" button is TD-1007's; the rule lifecycle is TD-803's

**Decision:** The card's third action — "Always allow in this workspace" —
lives in TD-1007. It renders only when the daemon's `approval_request`
carries a `proposed_always_allow` rule, and clicking it sends the
`always_allow` client message that TD-803's daemon already handles (narrow
rule generation, `(tool, args)` identity, class-C refusal).

**Rationale:** TD-803 built the whole rule lifecycle but no UI; TD-1007 owns
the card. Splitting the button out would have meant TD-1007 reimplementing
rule generation (a collision), while leaving it out left AC #2 unmet. The
card is a thin client of TD-803's message, so the two lanes never overlap.

### 2. The button is gated on a non-null proposal, not on the class alone

**Decision:** `isAlwaysAllowable` returns true only when
`proposedAlwaysAllow !== null` and `decisionClass !== 'C'`. The class check
is defensive: the daemon never proposes a rule for class C, so a proposal
already implies allowability, but the explicit guard keeps the wall absolute
even if a future proposal leaked a class-C effect.

**Rationale:** The button must never offer to auto-approve a class-C call
(the wall). Deriving the button's presence from the daemon's own proposal
means the UI never invents a rule the policy model didn't already accept.

### 3. The store mutates an exported `$state` array instead of reassigning it

**Decision:** `approval-store.svelte.ts` exports `const pending = $state(...)`
and mutates it with `push`/`splice`. Reassigning (`pending = [...pending]`)
is rejected by the Svelte 5 / rolldown compiler (`state_invalid_export`):
an exported `$state` binding may only be mutated in place.

**Rationale:** The list has to be reactive and exportable to the card/bar.
In-place mutation goes through the deep proxy (same as timeline-store and
chat-store), so reassignment — the old Svelte 4 store idiom — is neither
needed nor allowed.

## 2026-08-14 — TD-1008: Errors and notifications

Decisions made during the errors-and-notifications build.

### 1. A failed turn is machine-visible on the wire, distinct from a failed session

**Decision:** ``turn_complete`` carries ``failed`` + ``error_code`` (the
provider's typed code, e.g. ``auth_failed``, ``rate_limited``).  A turn that
ends on a provider error is a *turn* failure — the session keeps running and
the assistant message with the actionable text is in the transcript — while a
``session_state: failed`` remains reserved for loop-level death.

**Rationale:** Notifications need a typed cause to tailor copy, and the two
lifetimes genuinely differ: a 429 kills the turn, not the conversation.
Explicitly, the taxonomy now lives in two wire channels: daemon *command*
errors ride ``error`` events (bad_request, session_not_found, ...), while
*turn* failures ride the ``turn_complete`` fields — nobody should go looking
for a turn failure in the error channel.  The
alternative — bubbling provider errors into session failure — would strand
recoverable conversations and give the UI nothing actionable.

### 2. A missing keychain key fails the turn, not the session

**Decision:** ``KeychainError`` during lazy provider resolution appends the
actionable assistant message, emits ``error_code: "missing_api_key"``, and
breaks the turn.  The provider is cached only on success, so storing the key
and resending retries the same conversation.

**Rationale:** TD-1001's first-run flow means "no key yet" is a normal state,
not a crash.  Session-death would force reopening the workspace after the
user stores the key — pure friction on the most common first-run failure.

### 3. Notice severity comes from a pure copy table; dedupe is by cause

**Decision:** ``error-copy.ts`` maps each typed cause to
``{severity, title, body}`` — banners only for user-must-act blockers
(missing/rejected key, forbidden, cap pauses, session failure), toasts for
transient or maybe-works-next-time failures (429/5xx, context overflow).
The store dedupes by a per-cause key so repeats update in place, toasts
self-expire, and a ``session_state: running`` (resume) clears the blocker
banners — work flowing again is the fix being confirmed.

**Rationale:** Keeping copy in one reviewable table satisfies "actionable,
never raw tracebacks" by construction; severity-by-cause prevents the two
failure modes the AC names — a blocking error that scrolls away as a toast,
 and a transient 429 pinned as a banner crying wolf.

Broader copy rows (interruption, timeouts, transport/parse failures) and a wider
client-side redaction mirror came from the parallel TD-1008 draft built in
``wt-td1008``; the two lanes converged on this single wire design, and those
additions are grafted onto this table rather than shipped as a second system.

### 4. Diagnostics are assembled from states and event metadata, never content

**Decision:** "Copy diagnostics" (on every blocking banner) writes a redacted
report: daemon + protocol versions, ws/daemon/session state, cost totals,
and the last 25 event types with seqs.  It never includes message content,
tool arguments, or filesystem paths.

**Rationale:** The report is meant to be pasted into a bug report — anything
content-bearing would leak the user's code into whatever tracker receives
it.  Event types + seqs carry the diagnostic signal (what happened, in what
order) without the payload.

## 2026-08-14 — TD-1008 follow-up: copy and diagnostics grafts

Grafts onto the merged notifications base from the parallel ``wt-td1008``
draft (lane converged per the note in TD-1008 §3).

### 1. Transport/parse copy rows are live, not future-proofing

**Decision:** ``timeout``, ``connection_error``, ``request_error``,
``parse_error``, and ``stream_interrupted`` got tailored rows in
``TURN_ERROR_COPY``.

**Rationale:** These codes come from provider.py's exception paths (httpx
timeout/connect/HTTP, response JSON parse, chunk-stream drops) and flow
straight onto ``turn_complete.error_code``, so the rows are reachable today.
Checking ``_STATUS_CODE_MAP`` alone misses them — it only covers
HTTP-status-derived codes.

### 2. Interrupted sessions get a tombstone banner

**Decision:** ``sessionStateCopy`` maps ``interrupted`` to a blocking banner
saying the session is a tombstone and to start a new one, instead of staying
silent.

**Rationale:** An interrupted session looks identical to a resumable pause
in the session list; silence is the one state where the user most needs to
be told work cannot continue.

### 3. Cap-pause copy names the field to change

**Decision:** The three cap banners name the setting — ``spend_usd`` /
``wall_clock_hours`` / ``max_iterations`` under ``caps:`` in
``.tst/config.yaml`` — rather than "the cap" generically.

### 4. Diagnostics gain live notifications, UI version, and a client-side redact pass

**Decision:** ``buildDiagnostics`` adds the UI version (imported from
``package.json`` so it cannot drift) and the live notification list, and
passes wire-derived text (session reason, notification bodies) through
``redact.ts`` — a client-side mirror of core's ``SECRET_PATTERNS``, widened
for Bearer headers and dashed key forms (``sk-ant-…``). The content
exclusions stand: no message content, tool arguments, or filesystem paths.

**Rationale:** The report is meant to be pasted into a tracker. TD-1405
redacts at event-log insertion; this pass at the report boundary means the
UI never has to trust that every future daemon path remembered — the belt
to its suspenders.

## 2026-08-14 — TD-1101: First-run wizard

### 1. The first-run signal is a keychain probe, not a disk artifact

**Decision:** ``get_setup_state`` answers ``has_api_key`` by probing the OS
keychain; there is no "setup complete" flag file anywhere.

**Rationale:** A flag file lies in both directions — it survives a key being
deleted (says set up when it is not) and a key hand-stored before first
launch (says first run when it is not). The keychain is the source of truth
for credentials, so it is the source of truth for "has this machine ever
been set up".

### 2. Setup events ride the connection-scoped channel

**Decision:** ``setup_state`` and ``api_key_validated`` carry ``seq: 1``
and no ``session_id``, the same convention as ``session_list`` and
``policy_rules``. ``set_api_key`` and ``set_preset`` are acked with a fresh
``setup_state`` so one message type keeps the wizard consistent.

### 3. The wizard probes on the client's `connected` transition — `ready` is never emitted

**Decision:** The onboarding store sends ``get_setup_state`` when the
protocol client reports ``connected``, via a small state fan-out on
connection-status. The ``ready`` event declared in the wire schema ("Sent
after a successful handshake") is not emitted by the daemon and stays that
way: adding a frame after every handshake would rewrite recv-ordering
assumptions in six daemon test suites to deliver a signal the client
already has. ``notifications.svelte.ts``'s ``ready`` arm (daemon version
for diagnostics) remains inert until someone needs it enough to pay that
cost.

**Rationale:** Occam. The handshake-complete moment is observable
client-side; the probe is idempotent and reconnect-safe either way.

### 4. Key validation is one live call, one token

**Decision:** ``validate_api_key`` builds the provider client from the
keychain against the active brain's base URL and asks for ``max_tokens=1``
on a throwaway "ok" message. A 401 maps to keychain-fix copy; anything else
surfaces the provider's own error text. The success reply never names the
key.

**Rationale:** Cheapest possible proof the key works end-to-end; validating
a fake endpoint would teach the user nothing about their actual route.

### 5. The daemon writes config surgically and never mutates the cached copy

**Decision:** ``set_preset`` persists by rewriting (or appending) the single
top-level ``active_preset:`` line — atomically, via a same-directory temp
file — instead of a YAML round-trip that would strip the shipped file's
comments. The live daemon applies the change on ``model_copy`` so the
``lru_cache``-backed shared config instance is never mutated under other
consumers.

## 2026-08-14 — TD-1104: Diagnostics (doctor view)

### 1. Key validity and provider reachability share one probe

**Decision:** ``run_diagnostics`` makes at most one live call — the same
one-token probe TD-1101's ``validate_api_key`` performs — and derives both
the ``api_key`` and ``provider`` rows from its outcome. ``auth_failed``
(401) proves reachability while invalidating the key, so the provider row
reads *ok* in the same report that fails the key. A transport failure
proves nothing about the key, so the key row reads *skip* with detail
pointing at the connectivity problem instead.

**Rationale:** Two probes would double the cost and the failure modes
without buying information. The single-probe reading of each error code is
exact: 401 can only come from a reached provider.

### 2. The report rides the connection-scoped ``seq=1`` channel

**Decision:** ``diagnostics_report`` is connection-scoped (no
``session_id``, fixed ``seq=1``), same as ``setup_state`` and
``policy_rules``. Diagnostics describe the daemon and the user's
environment, not any session; the workspace and steering rows resolve
"which workspace" from the most-recent session record, and are *skip*
rows when no session exists.

**Rationale:** A session-scoped report would force the pane to pick a
session before asking, and the checks aren't about one. ``seq=1`` keeps
the client's gap-detector out of a stream the session log doesn't own.

### 3. Fix text speaks UI, not CLI

**Decision:** every failure row's ``fix`` names a thing in the app the
user can do — "open the wizard from the gear in the title bar", "open
Doctor again after reconnecting" — never a shell command. The ``skip``
rows still explain themselves ("not checked — no API key").

**Rationale:** The doctor view is the fallback when the app itself is the
nearest thing the user trusts; a user who came to fix the desktop app
should not be sent to a terminal to fix it.

### 4. Paths scrubbed at the rows; secrets scrubbed at the copy boundary

**Decision:** rows that would otherwise embed absolute local paths — the
workspace row's "{workspace} is writable", steering import issues — are
scrubbed where the row is built: the workspace is named by basename, and
import-issue paths are relativized against the workspace (home paths
become ``~/…``). ``Copy report`` additionally passes the whole assembled
text through ``redact()`` — the same pass TD-1008's diagnostics copy
uses — so secret-shaped tokens inside any row's ``detail`` or ``fix``
never reach the clipboard. The pane shows the same scrubbed rows; no
absolute local path ever touches the wire.

**Rationale:** TD-1008's diagnostics keep local paths out of pasteable
output by exclusion; here the rows *want* to name folders, so they get
just-enough-to-locate forms instead. Redaction at the copy boundary is
the belt to that: a future check that embeds a key-shaped string can't
leak in the pasteable artifact even if its row forgot to scrub.

## 2026-08-14 — TD-1404: Timeline render baseline

### 1. The timeline gate lives in vitest, not pytest

**Decision:** ``timeline_render_1000`` is measured and gated by
``ui/src/lib/timeline-bench.test.ts`` under jsdom — pytest cannot mount a
Svelte component. Both sides read the same committed
``core/tests/perf_baselines.json`` with the same threshold (3x baseline or
baseline + 250 ms). Core keeps the row honest from its side: the metric
moved from ``PENDING`` to a new ``UI_MEASURED`` map, and
``test_ui_measured_metrics_have_baselines`` fails if the row is missing
or null.

**Rationale:** One baselines file, one threshold, whichever runner can
actually exercise the surface owns the measurement. A deleted row now
fails in both suites.

### 2. What the number measures

**Decision:** the bench pushes 1000 synthesized daemon events (a
session-realistic mix: deltas, tool calls with their shell_output chunks,
results, decisions, errors) through the real ``push()`` store path, then
mounts ``ActivityTimeline`` and flushes one frame. The push loop is the
term that scales with entry count — each shell chunk linear-scans for its
parent row — while the render is windowed by TD-1005's virtualization
(~30 rows for the stubbed 640px viewport in jsdom), i.e. constant vs
entry count by design. The JSON metadata states that jsdom measures
Svelte DOM work, not browser layout or paint.

**Rationale:** A regression someone introduces in the per-event path is
what a 1000-entry live session would feel as sag; windowed paint staying
flat is the property the virtualizer was bought for.

### 3. Recording is an env-flagged vitest run

**Decision:** ``BENCH_RECORD=1 npx vitest run src/lib/timeline-bench.test.ts``
writes the fresh median into the shared JSON (clearing the pending marker
and declaring the row ui-measured) instead of asserting. Core's
``scripts/benchmarks.py --record`` conversely preserves the UI-measured
row when re-baselining the four core metrics — each runner owns its own
rows and must never clobber the other's.

**Rationale:** Only the vitest pipeline can compile and mount the
component, and CI never sets the flag, so gate mode is the default. The
app tree stays browser-typed; the recorder's node fs access sits behind a
four-line ambient shim (``node-test-shims.d.ts``) rather than pulling
@types/node into the project for one script-like test.

## 2026-08-14 — TD-1202: Decisions ledger panel

### 1. No new wire — the stream already carries everything

**Decision:** the panel filters ``decision_logged`` (TD-704) out of the
existing session stream on the client; per-session scoping, class
filtering, and the revert command are all computed client-side. The undo
command is derived — ``git revert <sha>`` — exactly as the ledger writer
computes it in ``core/tstd/autonomy/ledger.py``, so the copy matches the
markdown.

**Rationale:** TD-704 put class/what/why/commit on the event precisely so
reviewers would not need a second channel. A ``list_decisions`` verb would
re-implement replay the client already gets from attach.

### 2. Density is the interface

**Decision:** one line per decision — class chip, ``what``, short SHA —
with click-to-expand for the ``why`` and the revert button. Class filter
chips (A/B/C/All) sit in the header. The AC's "under a minute for a full
session of Class A" is met structurally: the A filter is one click and
each row costs a glance.

**Rationale:** Any design that shows rationale by default turns the
scannable list back into prose. The A/B/C classes exist to be a filter,
not a label.

### 3. Link-out via a zero-dep shell command, with a path fallback

**Decision:** the panel opens ``<workspace>/.tst/autonomy/DECISIONS.md``
through a new ``open_path`` Tauri command (``open`` / ``cmd /c start`` /
``xdg-open`` — no plugin added, no capability change). In a bare browser
(dev), the click falls back to copying the path. The relative ledger
location is a house constant mirrored from ``autonomy/ledger.py`` and
pinned by a test on the value.

**Rationale:** ``@tauri-apps/plugin-opener`` would drag a Rust + JS
dependency and a capabilities row through packaging for one click; the
std-command spelling is fifteen lines and covers the three CI platforms.
The path-always-visible footer means the fallback never leaves the user
without the destination.

## 2026-08-14 — TD-1103: Workspace management

### 1. Refuse, don't silently succeed

**Decision:** ``open_workspace`` now validates ``is_dir()`` before creating
anything and returns a typed ``workspace_not_found`` error through the
existing ``build_error`` dispatch idiom. Previously a nonexistent path
silently produced a session, one the UI could never render correctly.

**Rationale:** the failure was invisible — an absent workspace yielded a
live session id and a store row pointing at nothing. A hard refusal at
dispatch is where the UI can recover: the recents-menu toast explains the
folder moved or was deleted and points at both remedies (re-pick the new
location, remove the stale entry). Seven daemon tests used ``/tmp/test``
as a magic path and now open real tmp dirs — that was the tell that the
absence of validation had leaked into the test suite's assumptions.

### 2. Scaffolding is documentation, not configuration

**Decision:** first open of a workspace plants
``.tst/config.yaml`` as a fully commented template — every line a
comment, so ``yaml.safe_load`` yields ``None`` and the loaders map
``None`` to defaults (one-line change in each of ``load_workspace_boundary``
and ``load_policy``; ``save_policy`` treats ``None`` as an empty section
map). The scaffold never overwrites an existing file.

**Rationale:** a template with real values pins them at scaffold time —
every knob later changes its default and stale workspaces would silently
diverge. Comments-as-documentation means "absent" and "scaffolded"
behave identically, and ``boundary_source`` keeps reporting the real
file path (which now exists and is worth showing the user). The cost was
three ``None`` branches in loaders; the alternative — parse comments to
distinguish template from user content — is machinery for no behavior
difference.

### 3. No new wire — the daemon already knows the recents

**Decision:** the recents menu derives from the ``session_list`` event the
UI already receives (dedupe by ``workspace_path``, newest first, cap 12),
and per-entry removal is a UI-side hide-list persisted to localStorage
under ``tstdesk.hiddenRecentWorkspaces``. The daemon's session history is
untouched by removal.

**Rationale:** added a ``remove_recent`` command to the protocol would
put UI preference state (what the user wants to see) into the daemon's
session record (what happened), and would need its own ack + tests +
fixtures for zero behavior the user can tell apart. localStorage is
where UI-only preferences live; the hide-list survives restarts, and a
moved workspace stays discoverable by re-picking it — a fresh session
on the new path appends a new recents entry, which is the desired
understanding, not a bug to prevent.

### 4. AC4 is a property of the loop, so pin it at the loop

**Decision:** "switching workspaces re-resolves steering" is guaranteed
by construction (each session's loop builds its own ``PromptAssembler``
from ``session.workspace_path`` at loop.py), so the pin is a loop-level
test: two workspaces with distinct marker AGENTS.md files, one turn
each, and the system messages the mock provider received must carry the
right marker — never the other's.

**Rationale:** the cheapest place to break this property in the future
is exactly the seam the test guards: a shared assembler, a cached
prompt keyed on the wrong thing, a workspace mutation mid-session.
Asserting on the content the provider received pins the user-visible
outcome (the model sees this workspace's rules), which survives any
internal refactor of how the assembler is built.

## 2026-08-14 — TD-1201: Resolved stack panel

### 1. Stack queries assemble on demand; cache state rides the payload

**Decision:** ``get_instruction_stack`` re-assembles steering in the daemon
handler instead of serving a cached stack, and ``InstructionStack`` carries
``last_cached_tokens`` — the provider-observed cached prompt tokens of the
most recent main-loop call (classifier calls excluded), ``None`` before the
first turn.

**Rationale:** Assembly is cheap and the on-demand answer can never be
stale relative to the filesystem. Cache state is a provider-side fact the
UI cannot infer: zero cached tokens is a miss, but before any turn the
honest answer is "unknown" — so the field is nullable and the panel says
so rather than implying a miss.

**Path-scope caveat (corrected at integration):** no production call site
passes ``matched_paths`` — TD-503's touch-tracking was never plumbed — so
path-scoped rules assemble active in every response and push, and the
"unmatched" state is unreachable today. The panel therefore labels scoped
rules by prompt membership ("in prompt" / "not in prompt"), not by a match
verdict; "matched/unmatched" and TD-1201's third acceptance criterion land
with the touch-tracking story.

### 2. Inspector fields are additive-optional; protocol version unchanged

**Decision:** ``InstructionStackEntry`` gained ``shadowed_path``,
``applies_to``, and ``imports`` (flattened with nesting ``depth``);
``InstructionStack`` gained ``last_cached_tokens``. All are optional with
defaults, so older clients parse new payloads and vice versa — no
``PROTOCOL_VERSION`` bump.

### 3. Open-in-editor via tauri-plugin-opener, paths from the daemon only

**Decision:** New dependency ``tauri-plugin-opener`` (npm
``@tauri-apps/plugin-opener`` + crate), capability scoped to
``opener:allow-open-path`` — no ``open_url``. ``open-file.ts`` no-ops
outside the Tauri shell.

**Rationale:** It is the only open-in-editor mechanism available; the shell
had dialog/log/window-state plugins only. Every path handed to it comes
from the daemon's resolved stack, never from free-text input, so opening
the OS default handler on one cannot be aimed anywhere the steering
resolver didn't already read from.

### 4. Stack is a right-pane tab, not a third pane

**Decision:** The right pane grew an Activity | Stack tab strip instead of
splitting into three panes.

**Rationale:** The stack is a reference view — opened to answer "what is
the model actually running on?", then left. It does not need permanent
screen share with the timeline, and two panes stays the shell's layout
invariant.

---

## 2026-08-14 — TD-1601/1602/1608/1609: Familiarity, visual foundation

Class B — recorded per AGENTS.md §5.

### 1. Palette is family, not copy — our own hex throughout

**Decision:** The warm-paper palette ships with the backlog's pinned values:
light ground `#F8F6F1`, lifted `#FFFFFF`, ink `#191817`, hairline `#E4E0D8`,
rust accent `#B4532A` (hover `#9A4523`); dark charcoal `#232320` with the
accent lifted to `#D0794F`. Values the backlog didn't pin were derived and are
documented inline in `tokens.css`: sunken washes (`#F1EEE6` / `#2A2A26`),
muted ink (`#8A8579` / `#6E6A61`), dark accent-hover (`#DC8A64`), and lifted
dark status hues.

**Rationale:** The reference product's identity is `#FAF9F5` + `#D97757`;
ours is deliberately darker and earthier — a deeper ground, a rust (not
coral) accent — so the window reads as the same warm, quiet family without
lifting the palette. Accent stays scarce: send, active states, links, key
actions only; `--color-info` aliases the accent so "running" never
introduces a cool hue into the warm field.

### 2. Canonical token names with legacy aliases, not a flag-day rename

**Decision:** `tokens.css` defines a canonical semantic set
(`--color-ground`/`--color-lifted`/`--color-sunken`, `--color-ink` ramp,
`--color-hairline`, `--color-user-bubble`, `--color-accent`,
`--color-ok`/`--color-warn`/`--color-err`, `--font-display`/`--font-sans`/
`--font-mono`, `--tracking-display`, `--syn-*` code hues) and keeps every
pre-TD-1601 name (`--color-bg`, `--color-text`, `--color-success`,
`--font-family`, …) as a var()-to-var() alias.

**Rationale:** TD-1601's criterion confines the change to tokens + global
CSS; aliases made that true while still giving the E16 second pass clean
names to build on. Aliases cost one indirection per lookup and no runtime
work; the header comment marks them legacy so new code converges on the
canonical set.

### 3. Source Serif 4 vendored, latin 400/500 only — 41,616 bytes

**Decision:** Two woff2 files (latin, weights 400 and 500, Fontsource
packages fetched via jsDelivr) vendored into `ui/static/fonts/` with the
SIL OFL 1.1 text as `OFL.txt`; two `@font-face` blocks with
`font-display: swap`. Verified as real fonts (`wOF2` magic bytes, `file(1)`
identification) before committing. Bundle impact: **+41,616 bytes**
(20,088 + 21,528) of static font payload, fetched only when a
`--font-display` element renders, never blocking text (swap).

**Rationale:** Vendoring keeps the no-runtime-fetch property (prime
directive §2.3 — zero network calls the user did not initiate) and makes
the build hermetic. Latin-only, two weights is the smallest set that
carries the greeting/heading voice; full family + italics would be ~4x
the bytes for surfaces we don't have. Georgia is the documented fallback
so the swap window still reads serif.

### 4. Icons: one map, one component, stroke-only

**Decision:** All chrome glyphs live in `ui/src/lib/icons.ts` (10 entries)
and render through `Icon.svelte`: 24px viewBox, 1.5px `currentColor`
stroke, round caps/joins, `fill` reserved for active states, default size
1em. The doctor pane's *copied text report* deliberately keeps its ASCII
✓/✗/– marks (`STATUS_MARK`) — plain text is the right medium for a
clipboard artifact, and its tests pin those strings. The favicon is an
original mark (rust rounded square, minimal desk outline), not a borrowed
glyph.

**Rationale:** A single map kills per-call-site SVG drift and makes the
stroke/size discipline enforceable in one place. Emoji in chrome were the
loudest "hack project" tell (backlog's words); emoji in generated
plain-text artifacts are fine and cheaper than icon font machinery.

### 5. First shortcuts: Esc peels layers, ⌘, reopens the wizard

**Decision:** `resolveShortcut()` (pure, in `shortcuts.ts`) maps keydowns
through a context of what's open: Esc closes the workspace menu if open,
is eaten by an open modal (wizard/doctor/decisions), else cancels the
live turn via the chat store's existing `cancel` message; ⌘, (Ctrl+,
off-mac) reopens the wizard from anywhere. Discoverability is via `title`
attributes ("Cancel turn (Esc)", "Setup wizard (⌘,)").

**Rationale:** One key with two simultaneous visible effects (close a menu
*and* kill a turn) is how shortcuts earn a reputation for eating work;
the layer order makes Esc deterministic. Esc does not close modals yet —
that behavior doesn't exist today and adding it is a separate question
per pane; the mapping's `modalOpen` branch keeps today's behavior instead
of letting Esc reach through a dialog to the turn behind it. A pure
mapping function keeps all of this testable in node vitest (9 cases)
without a DOM.

## 2026-08-14 — TD-1406: Windows CI parity (first pass)

The Windows CI leg was red at mypy for a stretch, which hid the pytest leg
entirely — when mypy went green, ~110 tests failed at once. This pass makes
the suite platform-honest without touching the guard's security posture.

### 1. The boundary guard stays strict; the tests go relative

**Decision:** No change to `windows_unsafe` semantics — drive-letter, UNC,
8.3 short-name, and ADS forms stay refused fail-closed on every platform.
Guard tests that fed absolute `tmp_path` paths now `monkeypatch.chdir` into
the workspace and pass workspace-relative paths, so refusal codes
(`outside_workspace`, `steering_file`, `hardlink`, `outside_writable_paths`)
pin on BOTH platforms with no `skipif`.

**Rationale:** Loosening a security guard to make a CI leg pass is the wrong
direction of fit. Whether absolute in-workspace paths should be LEGAL on
Windows (today they are refused everywhere, so the model must speak
workspace-relative — defensible, but a product decision) is deferred to
TD-1406's remaining boxes. Relative-path tests pin the security semantics
on both platforms meanwhile, which is strictly more coverage than skipping.

### 2. Prompt text is POSIX-separated everywhere

**Decision:** Manifest walk entries and assembler provenance comments render
paths with `as_posix()` on every platform.

**Rationale:** These strings land in the system prompt. OS-native separators
would make the same workspace produce different prompts on different
machines, breaking cache-prefix stability and golden tests for no product
benefit. The model reads `src/main.py` fine on any host.

### 3. Real Windows product bugs fixed, not papered over

**Decision:** `daemon.run` registers signal handlers inside
`contextlib.suppress(NotImplementedError)` (asyncio signal handlers are
POSIX-only; shutdown still arrives via the shutdown message and parent
watchdog). `_win_parent_alive` now checks `GetExitCodeProcess` for
STILL_ACTIVE — plain OpenProcess reports a dead process alive while any
handle to it is open. The decision ledger takes an `msvcrt.locking`
byte-range lock on win32 (the previous code had no lock at all there, and
MSVCRT append-mode writes are not atomic). The shell tool's allowlist
normalizes PATHEXT extensions and case on win32 (`echo.EXE` ≡ `echo`), and
`_kill_process_group` actually kills the child on win32 (it was a no-op
with a comment claiming a caller's `proc.kill()` that did not exist).

**Rationale:** Each of these was a latent production defect on Windows that
the red CI leg had been hiding; the CI excavation paid for itself.

### 4. Platform-divergent semantics are skipped and pointed, not faked

**Decision:** chmod-based `restricted_mode` tests (session store, port
file), POSIX env-assignment shell syntax, and POSIX process-group kill
semantics carry `skipif(win32, reason="TD-1406: ...")` with the reason
naming the deferred work.

**Rationale:** Windows ACLs, Job Objects, and cmd.exe syntax are genuinely
different semantics that deserve their own implementation pass (they are
TD-1406's remaining acceptance boxes). A skip with a pointer is honest; a
test asserting POSIX behavior on Windows is fiction.

## 2026-08-14 — TD-503: Touch-tracking plumbs matched_paths into production

### 1. Touches are recorded only on handler success

**Decision:** `Session.record_touched` is called from dispatch after the
handler returns.  Boundary refusals, policy refusals, denials, and handler
errors return earlier and never reach the hook.

**Rationale:** A refused or failed call touched nothing.  Recording failed
attempts would let a probing caller — or a confused model — activate
path-scoped rules by name-dropping paths it never accessed, and be
rewarded with more prompt content for the probe.

### 2. Touches are stored workspace-relative POSIX; outside paths are dropped

**Decision:** Relative tool arguments are stored as-given (the fs tools
resolve them against the workspace root); absolute arguments are
relativized against the resolved workspace path, and anything outside it
is discarded.

**Rationale:** `appliesTo` globs speak workspace-relative paths, so the
match set must too.  A path outside the workspace can never match a scoped
rule legitimately — the boundary guard has already refused it — and
keeping it would leak absolute host paths into prompt assembly.

### 3. The first assembly is the baseline, not an activation

**Decision:** `RuleActivated` fires only when a rule becomes active at a
subsequent assembly; the first assembly's active set is recorded silently.

**Rationale:** A session that begins with a rule already active (touches
recorded before the loop) is initial state, not a change.  Announcing the
baseline would train the user to ignore the event.

### 4. Activations are announced once per rule per session

**Decision:** The loop diffs the active scoped-rule set across assemblies
and emits one `RuleActivated` per newly active rule with its
workspace-relative path; the timeline renders it as a steering entry titled
"Rule activated".

**Rationale:** Re-announcing on every turn would drown the signal.  Once is
the honest unit: from that assembly onward, the rule is in the prompt.

## 2026-08-14 — TD-1406: Windows CI parity (second pass)

### 1. Drive-absolute paths are legal on Windows hosts; the refuse list is otherwise unchanged

**Decision:** `boundary.py` no longer refuses drive-*absolute* paths
(`C:\foo`, `C:/foo`) as a form problem on `sys.platform == "win32"`: they
canonicalize and face the normal workspace / writable-paths / steering
checks, so an escape still refuses as `outside_workspace`.  Drive-relative
(`C:foo`), UNC, 8.3 short names, and ADS stay refused on every platform,
and every drive-letter path stays refused off Windows.

**Rationale:** TD-1402's blanket refusal was fail-closed but unworkable on
Windows, where every absolute path carries a drive letter: the natural
idiom (`C:\ws\src\app.py`) made every path tool unusable on the platform,
which the e2e harness demonstrated on the runner.  Drive-relative paths
(resolve against a drive's current directory) and UNC/8.3/ADS forms
(unresolvable or aliasing) remain genuinely unsafe.  This implements
TD-1406's first acceptance box; the box ticks when the Windows leg proves
it green.

### 2. Tests must await daemon shutdown — POSIX hides open handles

**Decision:** Daemon-driving tests stop the daemon by setting the shutdown
event and awaiting the task (cancel as fallback), so the audit sqlite
connection closes before tempdir cleanup.

**Rationale:** `TemporaryDirectory` teardown unlinks `audit.db`; with an
open handle that is `WinError 32` on Windows.  Fire-and-forget
`task.cancel()` raced `_shutdown()`'s drain-and-close on every platform —
POSIX just unlinks open files silently.  22 failing tests shared this
signature.

### 3. Spawn `python -m tstd.daemon`, not the console script, when a test needs the daemon's pid

**Decision:** The restart integration test launches the module directly;
on win32 the clean-shutdown leg drives the protocol `shutdown` message
(the graceful path a host uses there) instead of `terminate()`.

**Rationale:** On Windows, uv's console-script wrappers are trampoline
exes that spawn a child python: the harness waited on the trampoline's pid
while the port file carried the child's, and killing the trampoline
orphaned the real daemon (its watchdog watches the still-alive pytest
process).  POSIX keeps SIGTERM.

### 4. Connection-refusal tests pin via a bound-then-closed loopback port

**Decision:** `test_connection_refused` binds and closes a loopback socket
and targets literal `127.0.0.1:<port>` with a real (2 s) connect deadline.

**Rationale:** On the Windows runner, dual-stack `getaddrinfo("localhost")`
plus fallback outlasted the 0.1 s deadline and surfaced as a timeout — the
provider's error mapping was correct; the test's traffic engineering was
not.

### 5. Import provenance and golden normalization speak POSIX

**Decision:** `context/imports.py` provenance comments render `as_posix()`
(the assembler's earlier gap), and the golden harness normalizes the
fixture root in both its native and posix spellings.

**Rationale:** Same rule as the first pass (prompt text is POSIX-separated
everywhere) — these were the stragglers that only surface when the
separator differs from the golden's.

## 2026-08-14 — TD-1406: Windows CI parity (path surfaces)

### 1. Every model-facing path surface speaks POSIX

**Decision:** Discovery subtree labels (`discover.py`), `fs_list` output
(`handlers.py`), and validator-subset glob matching (`tier.py`) all render
`as_posix()` instead of the OS-native string. The test workarounds that
compared separator-insensitively are removed — the suite now pins forward
slashes directly.

**Rationale:** The second pass established the rule for prompt text and
goldens; these were the remaining producers. The `tier.py` case was a real
break, not cosmetic: subset patterns compile to `/`-separated regexes, so
on Windows even a basename pattern (`AGENTS.md` → `**/AGENTS.md`) never
matched a backslash path and the validator tier silently assembled with no
steering at all. `fs_list` output feeds the model paths it quotes back
into later tool calls, and subtree labels land in the steering block —
both must be stable across platforms.

## 2026-08-14 — TD-605: Kill honesty under OS veto (TestCancel flake family)

### 1. Report kill refusals instead of claiming the group died

**Decision:** `_kill_process_group` returns a note when the OS refuses
the kill; cancel/timeout result headers carry it ("cancelled — group kill
refused by the OS (EPERM); …"), and the two `CancelledError` paths — which
have no result to carry it — log a warning instead. The shielded-spawn fix
stays: cancellation landing mid-spawn still settles the spawn and kills
the group before propagating.

**Rationale:** The flake family traced past the spawn window to a macOS
veto: the kernel intermittently refuses same-uid kills of a spawned
process group with EPERM. Probes established the refusal attaches to the
group itself (fresh groups stay killable during another group's window),
no userspace vector breaks it (`killpg`, per-pid `kill`, and
`/bin/kill -9` all fail for the group's remaining life), and the command
always runs to completion. Claiming "process group killed" while the
group runs out is a lie the model would reason from; the refusal is now
surfaced the same way as every other outcome.

### 2. Tests key OS-veto tolerance on the product's report

**Decision:** Group-death assertions in the cancel/timeout tests are
skipped only when the refusal appears in the result header or the log;
every other round keeps the hard assertion. Deterministic refusal tests
monkeypatch `killpg` to raise `PermissionError` and pin the honest
header/log behavior.

**Rationale:** On a veto round the marker file is written no matter what
userspace does — asserting its absence would test the kernel, not the
product. An environmental probe (spawn a fresh group, try to kill it)
was measured and rejected: fresh groups are killable during another
group's refusal window, so a probe cannot excuse a real product failure.
Keying on the product's own refusal report keeps the pin exact on every
round where the kill was delivered.

### 3. A test that performs its own kill skips on PermissionError

**Decision:** Where the test itself delivers the signal (parent-death
watchdog's `sleeper.kill()`; daemon-restart's crash-simulating
`daemon.kill()` and clean-shutdown `daemon2.terminate()`), a vetoed
delivery raises `PermissionError` in the test process and no product
code was driven, so the test skips — guarded, so a skip never masks a
real failure from the scenario body. Cleanup-only kills suppress instead
and leave the child to its `--parent-pid` watchdog; the leak is bounded
by the pytest process.

**Rationale:** Same veto, other side of the seam: the product's refusal
report never reaches these tests because no product code ran. Keying on
the delivery error itself is exact — `PermissionError` fires only when
the signal demonstrably never left the test process, so every round
where delivery succeeded keeps the hard assertions.

## 2026-08-14 — TD-1102: Credential storage

### 1. Delete rides the setup_state ack — no new event

**Decision:** ``delete_api_key {provider}`` is answered with a fresh
``setup_state`` (``has_api_key`` flips false), exactly the ack pattern
``set_api_key`` already uses (TD-1101). Failures are a typed ``error``
event with code ``key_delete_failed``. No ``api_key_status`` event, no
new panel: the wizard's key step is the settings surface, and ``has_api_key``
is already the client's single source of truth.

**Rationale:** Two verbs with one ack keeps the wire minimal and the UI
free of a second state channel to reconcile. The one-off alternative
(``api_key_status`` push after mutations) duplicates what the connection
already re-probes on every reconnect.

### 2. Windows keychain via ctypes, not the keyring package

**Decision:** the Windows backend is ~170 lines of stdlib ``ctypes`` over
advapi32 ``CredReadW``/``CredWriteW``/``CredDeleteW`` (generic credentials,
target ``{service}:{account}``, UTF-16-LE blob). No ``keyring`` dependency.
All Win32 API access sits behind ``_cred_*`` seams and a platform guard, so
the module imports and is unit-tested off Windows.

**Rationale:** The macOS (``security``) and Linux (``secret-tool``) backends
are already zero-dependency subprocess wrappers; adding one third-party
package for the third platform would make it the only keychain backend that
ships code we don't read. ``keyring`` also drags ``jaraco.*`` and
``pywin32-ctypes`` into the frozen binary for three API calls.

### 3. All key-fix copy points at the title-bar gear

**Decision:** every "re-enter your key" path — the daemon's
``auth_failure_message``, the doctor's ``fix_key`` hint, and the UI's
``missing_api_key``/``auth_failed`` banners — names one destination:
title-bar gear → Provider API key. The previously shipped
``tstd keychain set <provider>`` copy was removed: no such CLI exists.

**Rationale:** Copy that names a command that doesn't exist is worse than
no copy — it sends the user hunting. One destination means the fix
instructions can never disagree with each other.

### 4. The websockets frame logger is capped at INFO in production

**Decision:** ``setup_logging`` forces ``logging.getLogger("websockets")``
to at most INFO even under ``--log-level debug``. The library logs raw
frame contents at DEBUG, and ``set_api_key`` frames carry the key itself.

**Rationale:** The secrets filter is pattern-based (``sk-…``, ``ghp_…``);
an unusually-shaped key would pass straight through it into ``tstd.log``.
The hygiene canary test caught exactly this leak — the AC's "never logged"
needed a production chokepoint, not just a wider test. Capping the frame
logger removes the class of leak instead of one instance.

## 2026-08-14 — TD-1106: Validate works on the entered key

### 1. Validation takes the typed key directly

**Decision:** `validate_api_key` gains an optional `api_key` field. When
present, the daemon probes with that key (`ProviderClient` constructed
inline) instead of consulting the keychain; the wizard's Validate button
is enabled whenever there is something to check — a typed key or a stored
one — and passes the field's content.

**Rationale:** Validation was gated on `hasApiKey`, so any keychain
failure dead-ended the step — Store unreachable while typing, Validate
unreachable without a store. Validation is a property of the string in
the field, not of storage state; the probe path now matches that.

### 2. A successful typed-key verdict never flips `has_api_key`

**Decision:** The client tracks whether the in-flight validate carried a
typed key; `api_key_validated` with `ok` only sets `hasApiKey` for
stored-key probes. `setup_state` remains the sole source of truth for
"a key is stored".

**Rationale:** A typed key that validates but was never stored would
otherwise mark onboarding complete while every real chat call still fails
on the missing keychain entry. Honesty about storage state outranks
convenience.

## 2026-08-14 — TD-1105: Keychain locked/drift error surface

### 1. Lock classification happens at the keychain layer

**Decision:** All six raw-stderr raise sites (macOS `security` and Linux
`secret-tool` get/store/delete) route through `_classify_cli_failure`.
Locked-family markers (passphrase-drift, interaction-barred, locked
collection) raise `KeychainLockedError` carrying unlock guidance; anything
else keeps the raw stderr in a plain `KeychainError`. The daemon maps the
locked type to a distinct `keychain_locked` error code; the UI renders it
as a banner whose copy names the retry (`Store key again` — the wizard
keeps the typed key).

**Rationale:** A locked login keychain after a macOS password change is a
real first-run killer (observed on an AD-bound Mac), and the raw
`security` stderr ("user name or passphrase not correct") reads like the
user mistyped something. Classification belongs at the CLI boundary —
that's where the stderr is — and in one function, so a new marker is one
line. The retry path is the wizard's own field retention plus named copy,
not new plumbing: the banner body tells the user their key is still typed
and which button retries.

## 2026-08-16 — TD-1801: Keyless local provider

### 1. A loopback `base_url` is the keyless signal

**Decision:** `config.is_loopback_url` classifies a provider endpoint as
on-box (127.0.0.0/8, `::1`, `localhost`) and the daemon's `_brain_client`
skips the keychain entirely for one. Anything unclassifiable — no scheme,
an unparseable host, a name that merely contains "localhost" — is treated
as remote and keeps the key requirement.

**Rationale:** The alternative was to catch `KeychainError` and continue
without a key, which is wrong in the one case that matters: `KeychainLockedError`
subclasses `KeychainError`, so a locked login keychain would silently
degrade a *remote* call into an unauthenticated one and swallow TD-1105's
unlock guidance. Keying off the URL means the keyless branch is chosen
before the keychain is ever consulted, so a keychain fault on a remote tier
still surfaces as a keychain fault. The predicate is deliberately separate
from `ws.validate_interface`: that one guards which interface we *bind*
(§2.1) and is a membership test over a bare host, this one classifies an
endpoint we *call*.

### 2. `ProviderClient.api_key` accepts `None`, and no key means no header

**Decision:** `api_key` widens from `str` to `str | None` (still a required
positional — keyless must be written out, never defaulted into) and
`_headers` omits `Authorization` altogether when it is `None`.

**Rationale:** The cheaper option was passing `""`, but that puts a literal
`Authorization: Bearer ` on the wire. Ollama ignores it; a stricter
OpenAI-compatible server (llama.cpp, LM Studio, vLLM with `--api-key`) can
reject the malformed value, and "we sent an empty credential" is not the
same statement as "we sent no credential". An absent header makes keyless
provable on the wire, which is what the test asserts. §2.2 is untouched in
both directions: this removes a keychain read, it adds no place a secret is
stored or logged.

### 3. `setup_state` gains `key_required`; the UI stops guessing

**Decision:** `SetupState` carries `key_required: bool = True`, computed
from the active preset. The wizard's auto-open gate becomes
`!has_api_key && key_required`. The field is additive with a safe default,
so `PROTOCOL_VERSION` does not move — a client that ignores it behaves
exactly as before.

**Rationale:** `has_api_key` is an honest probe of stored-key presence and
should stay one; making it report `true` for a local preset would be a lie
told to skip a modal. But the UI cannot derive "this preset needs no key"
from `active_preset` without hardcoding preset names, which §6 forbids
("the UI never derives truth it wasn't given"). A new field is the only
shape that keeps both invariants. `requires_api_key()` is conservative —
false only when *every* tier is loopback — so a mixed preset never reports
keyless.

### 4. The doctor skips the key row instead of failing it

**Decision:** When the active preset needs no key, the `api_key` diagnostic
row is `skip` ("not needed — the active preset runs on a local endpoint")
rather than `fail`. The `provider` row is still a real probe.

**Rationale:** Not named in an acceptance criterion, but making local-only
a supported path turns the existing behaviour into a false failure: a
working local setup would read "no API key stored — open the setup wizard".
A diagnostic that lies about a healthy system is worse than no row. The
provider row is unchanged because a keyless endpoint is still reachable or
not, and that verdict is still worth having.

### 5. Known limitation: resolution is brain-tier-wide, not per-tier

**Decision:** `_brain_client` decides keyless-ness from the **brain** tier's
`base_url`, matching the daemon's existing one-client-per-process shape.
`requires_api_key()` looks at all three tiers. A mixed preset (loopback
brain, remote worker) therefore resolves keyless while reporting
`key_required: true`.

**Rationale:** The daemon has always built a single client from the brain
tier and shared it with the worker-tier classifier call, so per-tier
`base_url` is decorative today. Making it genuinely per-tier means multiple
clients keyed by endpoint — a scope change well beyond this story, raised
rather than built (§5 Class C). The conservative `requires_api_key()` means
the mismatch fails safe: a mixed preset still prompts for a key.

---

## 2026-08-16 — TD-1802: Local preset and zero-cost accounting

### 1. The `local` preset ships a context window below the model's ceiling

**Decision:** All three `local` tiers keep `context_window: 32768` even
though the model's architectural ceiling is 262144, with the reasoning
written into `config.yaml` next to the value.

**Rationale:** `context_window` has exactly one consumer —
`compaction.budget_threshold` — so it is a compaction budget, not a
request parameter (nothing sends it to the provider). It must therefore be
no larger than what the *server* will actually serve, and Ollama caps
context at `num_ctx` independent of the model, defaulting far below the
ceiling. Shipping 262144 would keep compaction from ever firing and let the
server silently truncate the oldest messages — and message zero is the
steering block, so the failure mode is losing AGENTS.md without a word.
The two errors are not symmetric: too low compacts earlier than necessary,
too high loses steering silently. A user who has raised `num_ctx` raises
this to match, which is a config edit with no release.

### 2. The cost meter renders exact zero as `$0.00`

**Decision:** `formatUsd` moves out of `TitleBar.svelte` into
`ui/src/lib/cost-format.ts` and returns `$0.00` for exactly `0`, keeping
four decimals for everything below a dollar.

**Rationale:** The acceptance criterion says the meter reads `$0.00`; it
read `$0.0000`, which on a free preset looks like a value too small to
display rather than a deliberate zero. The guard is exact equality, never
a rounding window — real spend of `0.00001` still renders `$0.0000`, so
the meter can never show "free" for money actually spent. That property is
the reason this is worth a line here rather than a silent edit. Extraction
to a `.ts` module is what makes it testable at all: the repo has no Svelte
component-test harness, and §6 already wants logic out of presentational
components.

### 3. Known defect, not fixed: usage-only trailing chunks are never recorded

> **Corrected 2026-08-16 by TD-1804 — read this before the entry below.**
> The rationale as written claims "Ollama co-emits usage with
> `finish_reason`, so the shipped `local` preset — and this story's ledger
> test — exercise the working path." That claim is false, and the deferral
> rested on it. Measured against Ollama 0.32.13 on
> `http://127.0.0.1:11434/v1` with `stream_options: {"include_usage":
> true}`, the two fields arrive on two chunks:
>
>     {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}
>     {"choices":[],"usage":{"prompt_tokens":16,"completion_tokens":39,...}}
>
> giving `finish_reason='length' usage=None` and then `finish_reason=None
> usage=present`. So the shipped `local` preset exercised the *broken*
> path, and this story's third criterion — "the ledger still records real
> token counts for a zero-price tier" — was proven only against
> `MockProvider`, which does co-emit. The defect was real and the deferral
> was defensible; the premise given for it was not. The original text is
> left intact below rather than edited, because the reasoning error is the
> part worth keeping. Fixed in TD-1804.

**Decision:** Left alone and raised (§5 Class C). `loop.py` records usage
only when a chunk carries **both** `finish_reason` and `usage`, but
`provider.py` returns the usage-only trailing chunk with
`finish_reason=None`. `stream_options: {"include_usage": true}` is sent
unconditionally, and OpenAI and vLLM answer it with exactly that separate
chunk — against those servers no `model_call` row is written and no
`cost_update` fires, at any price.

**Rationale:** Price-independent, so it is not this story's defect, and the
fix changes the recording contract for every preset with a real
double-counting risk on providers that send usage twice. Ollama co-emits
usage with `finish_reason`, so the shipped `local` preset — and this
story's ledger test — exercise the working path. `MockProvider` also
co-emits, which is why the whole suite is blind to it. Wants its own story.

---

## 2026-08-16 — TD-1803: Live-provider end-to-end harness

### 1. `run()` takes a `HarnessPlan`, not a provider factory

**Decision:** `e2e_harness.run(workspace, data_dir, plan=None)` where
`HarnessPlan` carries the provider, the steering text, the prompt, the
event that approval is granted on, the written-content predicate, whether
spend is expected, and the two timeouts. `plan=None` builds `mock_plan()`,
so TD-1401's call shape and output are unchanged.

**Rationale:** Swapping the provider alone is not enough to describe a live
pass. A local model needs a fifteen-minute budget instead of forty-five
seconds, bills nothing where the mock bills real money, cannot be held to
byte-exact file content, and must approve on `approval_request` rather than
`tool_call`. Those five differences would otherwise become five `if live:`
branches through the run body — and a branch the mock never takes is a
branch that rots. As data, there is exactly one code path and the mock pass
executes every line the live pass does.

### 2. Live tests are deselected by default via `addopts`

**Decision:** `pyproject.toml` registers a `live` marker and sets
`addopts = ["-m", "not live"]`. Opt in with `uv run pytest -m live`.

**Rationale:** The story requires live runs excluded from the default CI
leg. `ci.yml` runs a bare `uv run pytest -q --tb=short`, so the exclusion
has to live in the config rather than the invocation — and putting it there
means a developer's local `pytest` behaves identically to CI, which is the
property worth having. The cost is that `addopts` now silently filters
every run; the marker description and this entry are the compensating
signal. A `skipif` on an environment variable was the alternative and was
rejected: it reports "skipped" for a test that was never meant to run here,
which trains people to ignore skips.

### 3. A live endpoint must be loopback

**Decision:** `live_preflight` refuses a non-loopback `--live-endpoint`.

**Rationale:** TD-1801 made loopback tiers keyless, so an on-box endpoint
needs no credential and the harness never touches the keychain. Allowing a
remote endpoint would mean a test harness that can spend the user's money
and would need an async, key-bearing construction path for no gain the
story asks for. Refusing is one line and makes "a live run cannot bill
anyone" a property rather than a hope.

### 4. Class C raised, not fixed: the live ledger is empty, and TD-1802's premise was wrong

**Decision:** Left alone and reported (§5 Class C). The live pass fails its
`ledger` and `cost accounting` checks. `loop.py` records usage only when a
chunk carries **both** `finish_reason` and `usage`; Ollama sends them on
separate chunks, so no `model_call` row is written and the turn reports
`tokens=0, cost=0.0`.

**Rationale:** This is the defect TD-1802 recorded under "usage-only
trailing chunks are never recorded" — but that entry claimed Ollama
co-emits usage with `finish_reason` and therefore exercised the working
path. That claim is false. Verified against the running server:

    {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}
    {"choices":[],"usage":{"prompt_tokens":16,"completion_tokens":39,...}}

Two chunks, and the `and` short-circuits on both. So the shipped `local`
preset exercises the *broken* path, and TD-1802's third criterion — "the
ledger still records real token counts for a zero-price tier" — is proven
only against `MockProvider`, which co-emits. The correction matters more
than the bug: a deferred defect was deferred on a false premise.

Still not fixed here. It is a change to the recording contract for every
provider and every preset, with the double-counting risk TD-1802 named, and
it is outside this story's files. Fixing it inside a harness story would
also destroy the evidence: the live harness caught a real defect on its
first run, which is the clearest possible demonstration of why the story
exists.

Worth noting what the failure did *not* do. `provider.errors` is empty and
no `ProviderContractError` was raised — Ollama's chunk shape is exactly
what `stream_options: {"include_usage": true}` specifies, so the endpoint
kept its contract and the loop dropped the usage. The harness attributed
the failure to the right side without being told, which is criterion four
demonstrated on a real fault rather than a simulated one.

---

## 2026-08-16 — TD-1804: Record usage independently of `finish_reason`

### 1. Usage is reconciled once at stream end, last-wins

**Decision:** `_stream_and_parse` keeps the usage from every chunk that
carries one, overwriting as it goes, and records exactly once after the
stream closes:

    if chunk.usage is not None:
        usage = chunk.usage
    ...
    if usage is not None:
        tracker.record(tier, usage, tier_cfg)
        await session.event_log.add(tracker.emit_cost_update(session.id))

replacing `if chunk.finish_reason and chunk.usage:` inside the loop. One
`CallRecord` and one `cost_update` per provider call, whatever shape the
stream arrives in.

**Rationale:** Three provider shapes have to land on the same invariant.
Ollama sends `usage` on the chunk *after* `finish_reason`; OpenAI and vLLM
send a trailing usage-only chunk with `finish_reason=None`; `MockProvider`
co-emits both on one chunk. The old `and` fired only for the third, which
is exactly why the whole suite was blind to the defect.

Two alternatives were considered and rejected:

- **Record on any chunk carrying usage.** Fixes Ollama and triple-bills a
  provider asked for continuous usage stats, which repeats a *cumulative*
  figure on every chunk. Verified as a mutation: the repeated-usage test
  writes three ledger rows — `(1200, 1)`, `(1200, 240)`, `(1200, 480)` —
  instead of one.
- **Record-once latch, first-wins.** Idempotent, but on that same provider
  it records `completion_tokens=1` and calls the call finished. Cheapest to
  write and the most expensive to trust.

Last-wins is the only one of the three that is both idempotent and
complete, because OpenAI-compatible usage is cumulative for the call: the
last figure seen is the whole figure. Deferring to stream end costs
nothing in event ordering — the co-emitting mock's usage already rides the
final chunk, so `cost_update` lands in the same place it always did,
after the last `assistant_delta` and before any `tool_call`. TD-1401's
event sequence is unchanged, and TD-1006's "one `cost_update` per recorded
call, not one per turn" still holds: a tool round-trip is two calls and
still emits two.

### 2. A failed or cancelled call still records the usage it saw

**Decision:** The provider-error and cancellation exits `break` out of the
chunk loop instead of returning from inside it, so the single recording
point runs on every path.

**Rationale:** Those tokens were spent whether or not the call finished,
and an audit ledger that understates real consumption is worse than one
that reports an aborted call. It is also the smaller change: with one exit
path there is one place recording can happen, so the idempotence property
is structural rather than something three `return` statements have to
agree about. The returned tuple is byte-identical to before on both paths —
cancellation still yields `(…, True, "cancelled", None)` and a provider
error still yields `(…, True, message, code)`.

In practice nothing new fires today: usage rides at or after the end of a
stream on every provider we have seen, so a stream that is cut short has no
usage to record and the ledger stays empty exactly as it did.

### 3. Raised, not fixed: `turn_complete.tokens` reports the last call only

`agent_loop` calls `tracker.begin_turn()` inside the tool-call round-trip
loop, not once per user turn, so the turn accumulator resets on every
provider call. A two-call turn reports the second call's tokens in
`turn_complete` while the ledger and `cost_update.session_cost` correctly
carry both. Surfaced by TD-1804's per-call test and left alone: it is
pre-existing, outside this story, and a change to what `turn_complete`
means on the wire (§3). The test asserts session spend rather than turn
tokens so it neither depends on the quirk nor pretends it is absent.

*Resolved by TD-1806 (below); this entry stands as the record of the
deferral, not of current behaviour.*

---

## 2026-08-16 — TD-1805: Resolve the local model from the endpoint

### 1. "Unset" is `null`; the empty string is a validation error

**Decision:** `TierConfig.slug` becomes
`Annotated[str, Field(min_length=1)] | None = None`. An omitted key and an
explicit `slug:` (which YAML parses as `None`) mean the same thing —
discover it. `slug: ""` fails validation naming the key.

**Rationale:** Omitted and `null` cannot be allowed to diverge: a bare
`slug:` line *is* `null`, so splitting them would make trailing whitespace
carry meaning, and a user commenting a value out would land on whichever
one we did not handle. That leaves the empty string, which is never a
statement of intent — it is a half-finished edit, a template variable that
did not expand, or a key deleted by hand. Accepting it as a third spelling
of "unset" would silently paper over the typo; rejecting it costs one line
and names the key.

The constraint sits inside the `Annotated` rather than on the union, so
`min_length` applies to the `str` branch instead of to a nullable schema
pydantic cannot constrain.

### 2. Exactly one served model resolves; zero or several are refused by name

**Decision:** `discover_model` resolves only when `/v1/models` returns
exactly one id. Zero raises. Two or more raises, listing every id it saw
and pointing at the tier's `slug:` key.

**Rationale:** The alternative worth taking seriously is "take the first",
which keeps a fresh install working on a machine with several models
pulled. It was rejected because `/v1/models` is not a list of chat models —
Ollama lists embedding models (`nomic-embed-text` and friends) beside
them, in an order the caller does not control. Picking first would
routinely bind the agent to a model that cannot answer a chat completion
at all, and the failure would surface three layers down as a provider
error about a malformed response rather than as "you have five models,
name one".

"Refuse unless exactly one" trades a rare one-time edit for never guessing.
The error is a two-second fix — it prints the ids, and setting `slug:`
takes precedence forever after — while a wrong silent guess is a debugging
session. This is the same conservative shape as TD-1801's
`requires_api_key()`: when the situation is ambiguous, fail toward the
user's explicit statement rather than toward convenience.

### 3. Discovery runs on the first turn, and a failure fails the turn

**Decision:** `agent_loop` calls `resolve_tier_slugs(config)` after
dequeuing a user message and before the round-trip loop. A
`ModelDiscoveryError` there appends the error to the transcript, emits
`turn_complete{failed: true, error_code: "model_unresolved"}`, and
continues the loop.

**Rationale:** Four placements were possible and three are worse. Import
time is a network call on `import tstd` (§2.3). Daemon start would make
the daemon's own startup depend on a model server. Session open would keep
a workspace from opening at all when Ollama happens to be down — a user
could not even browse files. The first turn is where the model is actually
needed, and TD-1008 already set the precedent for exactly this shape with a
missing keychain entry: the conversation survives, the user fixes the
environment, and the next message goes through. `test_a_dead_endpoint_
fails_the_turn_not_the_session` pins the whole cycle including recovery.

The call is idempotent and returns after a dict scan once every slug is
set, so it is invoked per turn without a round-trip per turn — no
`_resolved` flag to keep in sync with the config it describes.

### 4. Resolution mutates the loaded config in memory, and writes nothing

**Decision:** `resolve_tier_slugs` assigns `tier_cfg.slug` on the live
`ModelConfig` rather than returning a resolved copy. Nothing is written to
`config.yaml`, ever.

**Rationale:** The slug is read from six places, some of them synchronous
and deep inside the call graph — `CostTracker._build_record` stamps every
ledger row with it, and the compaction token counter is keyed on it.
Threading a resolved copy through all of them would mean either an async
signature change in the cost path or two configs in flight, one of which
would eventually be the stale one. One object, settled once, is the
smaller and less surprising design.

Not persisting is a §2.2/§2.7 requirement and also the better behaviour:
the next process re-reads the endpoint, so a model swapped on the server is
picked up without a stale tag left behind in a file the user never edited.
`test_it_writes_nothing_to_disk` asserts the config file is byte-identical
after a resolution.

### 5. A remote tier with no slug is a validation error, not a discovery one

**Decision:** `TierConfig` refuses an unset slug on a non-loopback
`base_url` in a `model_validator`, so `load_config` reports it as a
`ConfigError` naming the tier. `ModelDiscoveryError` is a sibling of
`ConfigError`, not a subclass.

**Rationale:** This makes "remote tiers never trigger discovery" structural
rather than a branch that has to be remembered — there is no reachable
state in which discovery is asked about an off-box endpoint, so no request
can leave the machine and no credential question can arise. Catching it at
load also puts the error where the user can act on it, at the file, rather
than mid-turn.

The two errors stay siblings because they mean opposite things: a
`ConfigError` says the file is wrong and editing it is the fix, while a
`ModelDiscoveryError` says the file is right and the machine is not ready —
the identical config succeeds once the server is up. Subclassing would let
the daemon's existing `except ConfigError` fallbacks (workspace boundary,
policy) swallow a discovery failure into a "using defaults" warning.

### 6. `TierConfig.require_slug()` is the narrowing accessor

**Decision:** Every place that sends a model to a provider calls
`require_slug()` instead of reading `.slug`. It raises
`ModelDiscoveryError` when the slug is `None`.

**Rationale:** `str | None` has to be narrowed somewhere for `mypy
--strict`, and the choice is between narrowing at each of a dozen call
sites or once behind a name. Making it raise rather than fall back to a
default turns a missed resolution into a loud typed failure instead of
`"model": null` on the wire, where the provider's reply would be some
generic 400 about a malformed request. It should never fire; that is the
point of putting it where it would.

### 7. `tier_state` omits a tier whose slug is unresolved

**Decision:** `_tier_state_event` builds `model_slugs` from tiers with a
resolved slug only. The loop re-emits `tier_state` on the first turn, once
discovery has landed.

**Rationale:** The session-open event fires before any turn, so on a local
preset there is genuinely no model to report yet. A placeholder string
would be the daemon inventing a truth the UI would then display (§6, "the
UI never derives truth it wasn't given"), and widening `model_slugs` to
`dict[str, str | null]` would be a wire change for a state that lasts one
turn. `TitleBar.svelte` already guards each slug lookup for truthiness, so
an absent key renders as no tooltip and then fills in — no frontend change
was needed.

### 8. Class C raised, not decided: the spec's model table is now stale

`docs/tst-desk-spec.md` §7 documents the tier defaults and states "Slugs
and prices live in `config.yaml`, not code". That is still true, but the
spec does not say a loopback tier may omit `slug` and have it resolved from
`/v1/models`, which is now part of the user-facing configuration contract —
AGENTS.md §10 requires the docs to follow. Editing the spec is a scope
boundary (§5 Class C), so it is raised here rather than taken: the spec
needs a sentence in §7 covering optional slugs on loopback endpoints, the
exactly-one rule, and the fact that config always wins. The backlog's
TD-1805 acceptance boxes are likewise left unticked pending that call.

---

## 2026-08-16 — TD-1806: Count a whole turn's tokens and duration

### 1. The turn accumulator opens at the turn, not at the provider call

**Decision:** `tracker.begin_turn()` and `turn_start = time.time()` move out
of the tool-call round-trip loop and up to the top of the turn loop, right
after the user message is appended. The round-trip loop keeps everything
else it had.

**Rationale:** They measure a turn, and the loop they sat in runs once per
*provider call*. A turn that called a tool therefore reported its final
leg's tokens and its final leg's duration as the whole turn's. Measured on
the live harness against Ollama 0.32.13, same task, both sides of the
change:

    before   ledger rows (1212, 190) + (1373, 28) = 2803   turn reported 1401
    after    ledger rows (1214, 177) + (1377,  21) = 2789   turn reported 2789

1401 is exactly `1373 + 28` — the last call, to the token.

Alternatives rejected:

- **Sum the ledger at `turn_complete` instead.** `CostTracker` already owns
  a turn accumulator and every `turn_*` aggregate reads it; a second,
  parallel notion of "this turn" computed in the loop would be two answers
  to one question, and the cache-ratio and cached-token aggregates would
  still report the last leg.
- **Reset per call but keep a separate turn stopwatch.** Fixes the duration
  and leaves the token defect exactly where it was.

Only `_turn_calls` is reset by `begin_turn`, verified before moving it: the
session ledger (`_calls`), the classifier ledger (`_classifier_calls`) and
`last_cached_prompt_tokens` all read `_calls` and are indifferent to where
`begin_turn` is called. `_cap_violation` measures `session_cost()`, not
turn cost, so cap enforcement is unchanged. The docstring on `begin_turn`
now states the once-per-turn contract so the next reader does not have to
re-derive it.

### 2. `cost_update.turn_cost` now accrues across the turn (Class B)

**Decision:** Accepted as an entailed consequence and pinned by a test
rather than left to be discovered: mid-turn `cost_update` events now carry
the turn's spend *so far* instead of the last call's spend alone. TD-1804's
guarantee is about the *count* — one ledger row and one `cost_update` per
provider call — and that count is unchanged, asserted in the same tests.

**Rationale:** `turn_cost` is a field named for the turn; reporting one leg
of it was the same defect as `turn_complete.tokens`, just on the meter
instead of the receipt. The alternative — freezing `turn_cost` at per-call
semantics to avoid touching a shipped value — would leave the UI's running
meter disagreeing with the `turn_complete` it lands on at the end of every
turn that used a tool. No wire shape changed, so no client needs updating;
`session_cost` and `cost_by_tier` are untouched.

### 3. The turn clock starts before tier-slug resolution

**Decision:** `turn_start` is taken before TD-1805's `resolve_tier_slugs`
call, so a turn that fails discovery reports the time it spent trying, and
that path's own `tracker.begin_turn()` (added when the reset lived in the
inner loop, which the failure path never reaches) is deleted as redundant.

**Rationale:** Discovery is work the turn does; a user waiting on an
unreachable endpoint waited for real. A failed discovery still bills zero
tokens because `begin_turn` cleared the accumulator and no call was made —
`test_a_failed_discovery_bills_nothing` continues to pass unchanged.

### 4. Noted, not changed: `router.record_turn_start()` also runs per call

`record_turn_start` sits in the same round-trip loop at `loop.py:677`, so
`router.turn_count` counts provider calls rather than turns, and a
tool-using turn advances the lead-turn rotation faster than a plain one.
That is the same shape of defect this story fixed, on a different counter,
and no acceptance criterion here covers it — moving it would change tier
selection, which is TD-303's contract and its tests. Raised for a story of
its own rather than folded in.

---

## 2026-08-16 — TD-1807: The live harness must not assume tool-call ordering

### 1. The checked call is selected by name, then followed by its id (Class B)

**Decision:** The harness picks the tool call under test as the **first
`fs_write` in the transcript**, and matches its approval and its result on
that call's `tool_call_id`. Position is never used again. The three checks
that used `calls[0]`, `gate[0]` and `results[0]` now talk about one call by
construction rather than by coincidence.

**Rationale:** The OpenAI tool-call contract makes no ordering promise, and
a local model reading a file before writing one is doing nothing wrong — it
cost one failure in five consecutive live runs of the M1.5 exit criterion.
Measured again while validating this story: `qwen3.8:27b` made *two*
`fs_write` calls in one live pass, which the old code would also have judged
by whichever landed first.

**First, not "whichever one passes."** If the model's first write is refused
and a later one succeeds, this run fails — and the note lists the later call
and its status, so the report says exactly that. Selecting the call that
satisfies the checks would turn the exit criterion into a search for its own
green, which §7 forbids more strongly than it dislikes a false negative.
Revisit only if a real transcript produces one.

### 2. Extra tool calls are reported, never judged (Class B)

**Decision:** `HarnessResult` grows `notes: list[str]`, printed under the
checks as `NOTE` lines and excluded from `ok`. Calls other than the selected
one land there as `name#id → status`; the checked call's own line states its
position (`call 1 of 2`) when there was more than one.

**Rationale:** The story requires that extras neither fail the run nor pass
it silently. A check that always passes would have satisfied "reports them"
on paper while adding a line to the verdict that can never fail — the exact
tautology §7 warns about. A note carries the information without pretending
to be a judgement. Nothing else observed a check's pass/fail count, so the
addition is additive: the mock plan makes one tool call, emits no note, and
its report is unchanged.

### 3. `e2e_checks.py` split out of `e2e_harness.py` (Class B)

**Decision:** The verdict half — `HarnessResult` and every check — moves to
`tstd/e2e_checks.py`; `e2e_harness.py` keeps workspace preparation, the mock
plan, the protocol client, and the CLI. `run()` and `main()` keep their
signatures, so `scripts/e2e_headless.py` and both tests are untouched.

**Rationale:** §6's ~400-line limit was breached (422), and the split had to
be by responsibility rather than by line count. Driving a session and
judging its transcript are separate jobs with separate reasons to change:
this story changed only the second, and TD-1401's drive path did not move a
line. 292 + 307 lines.

### 4. The offline regression uses a scripted provider, not `MockProvider`

`MockProvider` emits one tool call per script and reuses the id
`call_mock_1` for every one of them, so it cannot express a transcript with
two distinguishable calls — and it must stay byte-identical for TD-1401.
`tests/test_e2e_ordering.py` therefore replays chunk sequences through the
`ScriptedProvider` already written for TD-1804. The daemon, classifier,
policy gate and dispatcher are all real; only the model is scripted. Two of
its five runs must fail, which is what keeps the new selection rule from
being a rule that passes everything.

An in-workspace `fs_read` matches no static rule, so it classifies B and
parks on the approval gate — the runs about ordering give it an explicit
`fs_read → auto` policy so the only approval in the transcript is the one
the run is about. Found by writing the tests, not assumed.

---

## 2026-08-16 — TD-1805 (finish): the resolution had to happen before the requirement

TD-1805 shipped an optional slug, a discovery path, and a narrowing
accessor that assumed discovery had already run. Two shipped call sites had
not run it. This entry closes the story and **corrects decision 6 of the
2026-08-16 TD-1805 entry above**, which claimed of `require_slug()`: "It
should never fire; that is the point of putting it where it would." That
claim was false when it was written. `tstd/e2e_harness.py` and
`tstd/benchmarks.py` both called it on the brain tier before anything
resolved that tier, so under `active_preset: local` — the only shipped
preset that leaves the slug unset — it fired every time and neither the
headless harness nor the perf baselines could be run at all. Reproduced
against the pre-fix branch: both raised `ModelDiscoveryError: no model has
been resolved for http://127.0.0.1:11434/v1`, with a fix line addressed to
a programmer ("Resolve the tier's slug before using it (tstd.discovery)")
rather than to the user in front of it.

The suite never saw it because every test ran under the default remote
preset, where all three slugs are pinned in config. That is the general
lesson worth keeping: an accessor whose contract is "the caller must have
done X first" is only as good as the coverage of the callers, and the
preset that exercises the branch has to be in the suite, not just in the
story.

### 1. Resolve before requiring — the two paths do what the daemon does

**Decision:** `mock_plan` and `measure_first_token_latency` call
`resolve_tier_slugs(cached_config())` and then `require_slug()`.
`require_slug()` is unchanged and still raises.

**Rationale:** Three options were real. *Tolerate an unresolved slug* was
rejected outright: both call sites use the slug as the key of a
`MockProvider` script map, so a missing or invented key falls through to the
`(unused)` default, the scripted tool call never happens, and the pass fails
as a broken agent loop instead of an absent model server — a silent
substitution that produces a lie rather than an error. *Pin a synthetic slug
for the mock leg* would keep that leg fully offline, but it would mean the
harness rewrites the user's active configuration for the duration of the
run and then reports a pass about a config they do not have; the M1.5 exit
is supposed to prove the shipped chain works on this machine.

*Resolve first* adds no dependency the run does not already have: under a
local preset `agent_loop` resolves at the top of every turn anyway, so the
endpoint was always going to be needed. `cached_config()` is `lru_cache`d,
so the harness, the benchmark and the daemon share one `ModelConfig` object
— resolving in the caller means the loop's own call is the dict scan it was
designed to be, and the pass costs one `/v1/models` round-trip, not two.
Verified against Ollama 0.32.13: the mock harness passes under
`active_preset: local` in 0.8s, and `first_token_latency` measures 41 ms,
unchanged against the committed baseline (0.0414 s) — resolution happens
before the timed section.

`mock_plan` becoming `async` is the only signature change. It is internal
(`run()` is its only caller; `scripts/e2e_headless.py` and all three harness
tests go through `run()`/`main()`), so TD-1401's call shape is untouched.

### 2. The mock plan's spend expectation follows the preset's prices (Class B)

**Decision:** `HarnessPlan.expect_spend` for the mock plan is derived —
`max(input_price, output_price, cache_read_price) > 0` on the brain tier —
instead of the literal `True`.

**Rationale:** Fixing the crash was not enough to let a local-preset user
*run* the harness: the `local` preset prices every tier at zero, so
`cost > 0` failed a check for behaving correctly. TD-1803 had already drawn
exactly this distinction when it set `expect_spend=False` on the live plan
("a zero-price preset bills nothing, and nothing is the honest answer");
the mock plan simply never faced it, because nothing had ever run the mock
leg under a zero-price preset. Deriving it applies one rule to both plans
rather than two hardcoded answers. Under any priced preset the derived
value is `True`, so TD-1401's assertion is bit-for-bit what it was — pinned
by `test_a_priced_preset_still_demands_a_bill`. The `ledger` check is what
proves free work is still tracked: the local pass reports `cost=0.0
tokens=3360`.

### 3. The off-box refusal moves ahead of discovery (§2-adjacent)

**Decision:** `is_loopback_url` on `--live-endpoint` is extracted from
`live_preflight` into `off_box_refusal()` and called first in `_run_live`,
before the slug is resolved.

**Rationale:** TD-1805 inserted `discover_model(endpoint)` above
`live_preflight`, which is where the loopback check lived. So
`--live-endpoint https://…` sent a request to that host and *then* refused
the run. Measured on the pre-fix branch against an unroutable off-box
address: 10.3 s — a full `DISCOVERY_TIMEOUT` spent trying to connect —
reported as "no model server answered", which is the harness telling the
user it failed to reach a host it was never allowed to contact. Post-fix:
0.23 s, refused with no connection attempted. This is not a §2.1 violation
(that governs what we *bind*) and the request carried no credential, but it
directly contradicts TD-1803's acceptance note that a non-loopback endpoint
"is refused before any request leaves the box", and §2.3's zero-unrequested-
network posture. Fixed rather than raised as Class C because it is a
reordering inside the path this story changed, with no scope change: the
refusal, its wording and its exit code are all as they were.

### 4. Discovery's fix text is chosen by what actually failed

**Decision:** `_START_SERVER_FIX` no longer answers every failure. A
transport error keeps it; a 5xx gets `_SERVER_UNHEALTHY_FIX` (check the
logs); any other non-200, and a 200 whose body is not an OpenAI model list,
get `_WRONG_PATH_FIX` (point `base_url` at the API root). `_model_ids`
returns `None` for "not a model list" so that an empty `data: []` — a
correct server with nothing loaded — keeps its own "load a model" fix.

**Rationale:** A reply is proof the address is live, so "start a server
there" sends the user to check a process that is already running. The
common real case is a `base_url` missing `/v1`: against the live Ollama,
`http://127.0.0.1:11434` now reads "Something is listening there but it is
not serving the OpenAI model list. Point the tier's base_url at the API
root…" where it previously told the user to start the server that was
answering it.

### 5. `model_unresolved` gets UI copy; the endpoint stays off the wire (Class B)

**Decision:** `TURN_ERROR_COPY` gains a `model_unresolved` banner. It names
the two fixes (start the server, or set `slug:`) and points at diagnostics
for the endpoint. `TurnComplete` is **not** widened to carry the endpoint.

**Rationale:** The code reached the wire with no entry in the table, so
`turnFailureCopy` fell through to "The turn ended with an unrecognised error
(model_unresolved)" — a raw code, which is the generic provider error AC-3
exists to prevent. A banner, not a toast, on the same reasoning as
`missing_api_key`: nothing works until the user acts, and the conversation
survives so they can act and resend.

Carrying the endpoint would mean a new optional field on `TurnComplete`
used by exactly one code path — protocol message design, Class B, and the
spec is silent, so §5 says ask rather than decide. It is not needed to make
the copy actionable: on a local preset the endpoint is a line in the user's
own `config.yaml`, and `run_diagnostics` already reports it in the failing
`provider` row (pinned by `test_the_provider_row_carries_the_endpoint_and_
the_fix`). Recorded here as the open question if a future story wants the
endpoint in the banner itself.

### 6. Decision 8's Class C is answered: the spec now documents the contract

The earlier entry raised the spec update as a scope boundary and left it.
The story owner has since directed it, so `docs/tst-desk-spec.md` §7 gains
"The `local` preset: the endpoint is ours to guess, the model tag is not" —
optional-only-on-loopback, resolved on first turn, config always wins,
exactly-one-resolves, and the no-key/no-write guarantee. §10 is satisfied:
this story changed the user-facing configuration contract and the docs now
say so.

---

## 2026-08-17 — TD-1808: Scope the execution check to the call it is checking

### 1. The call's own effect is read from its `tool_result`, not from the workspace (Class B)

**Decision:** The execution check judges the checked `fs_write` from two facts on its own
`tool_result` — `status == "success"`, and a non-empty `diff` — plus the `content` argument of
the `tool_call` that produced it. The workspace's final file content no longer decides
anything; it survives only in the detail line.

**Rationale:** `diff` is rendered by the dispatcher from a snapshot taken either side of that
one handler (TD-604), so it is evidence about one call and cannot be moved by a later one.
Deriving `wrote_file` from the file's final content conflated one call's outcome with the
transcript's aggregate state — the same defect TD-1807 removed from `calls[0]`, `gate[0]` and
`results[0]`, left behind on the content half. Two independent facts are required rather than
one so that neither the handler's self-report nor the model's stated intent passes alone: the
call must ask for content the plan accepts *and* the diff must prove it landed.

### 2. Content compares as line lists, and `HarnessPlan` gained no second callable (Class B)

**Decision:** Rejected adding a `wrote_ok` field to `HarnessPlan` for per-call content. The
check reuses `plan.content_ok` against the call's `content` argument, and separately asserts
`added == asked.splitlines()`.

**Rationale:** The obvious design — rebuild the post-image from the diff and feed it to
`content_ok` — cannot work. `render_diff` builds from `splitlines()` with `lineterm=""`, which
discards line terminators, and `difflib` never emits the `\ No newline at end of file` marker
because it never saw them. A rebuilt image can therefore never compare equal to the mock plan's
`"hello from M1\n"`. Putting both sides through the same `splitlines()` transformation is the
only lossless match available, and it avoided widening a shared dataclass that
`e2e_harness`, `e2e_live` and the tests all name.

### 3. Both stories' runs share one test module (Class A)

**Decision:** The four new runs went into `core/tests/test_e2e_ordering.py` rather than a new
module, and its docstring now covers TD-1807 and TD-1808.

**Rationale:** Same defect, two halves. A separate module would either duplicate the chunk
scripting helpers or import private names across test modules. 325 lines, inside §6.

---

## 2026-08-17 — TD-1809, and the pattern behind TD-1407 and TD-1408

### 1. The doctor's probe test resolves slugs itself (Class A)

**Decision:** `TestDoctorRows` patches `tstd.daemon.resolve_tier_slugs` with a stand-in that
fills unset slugs the way a single-model endpoint would. No product code changed.

**Rationale:** `_provider_probe` resolves slugs before it builds a client, and the shipped
`local` preset sets none, so the row's verdict was decided by how many models the developer
happened to have pulled. `discover_model` refusing to guess among several is correct and is
what TD-1805 shipped — the doctor was telling the truth. The test was what assumed a machine.

### 2. A pattern, not three incidents (recorded so it is not re-learned)

Three defects this session share one shape: a run whose verdict depends on the developer's
machine rather than on the code. The doctor probe read the live endpoint's model list
(TD-1809). `test_cancelled_error_path_kills_group` times a race with fixed sleeps and loses
under suite load (TD-1407). `test_config` and `test_cost` read the real user `config.yaml` and
assert shipped defaults (TD-1408). The repo already knows the answer — `test_e2e_live` and
`test_local_preset_paths` redirect `HOME` before touching preset state, on the stated grounds
that changing the developer's preset from a test "would be a rude side effect that survives the
run." New tests should reach for a fixture or a redirect before they reach for the machine.

---

## 2026-08-17 — TD-1703: Settings screen v1

### 1. Slug edits land in the user config, never the packaged one (Class B)

**Decision:** `save_tier_slug` writes `user_data_dir()/config.yaml`. Directed by the story
owner when asked.

**Rationale:** `core/tstd/config.yaml` is packaged (`pyproject.toml` `include`), so an upgrade
overwrites it and any edit written there is silently lost. The user copy is the one
`ensure_user_config` already creates and `load_config` already reads.

### 2. `set_tier_slug` is narrow, not a general `update_config` (Class B)

**Decision:** The message carries exactly `preset`, `tier`, `slug`. No general config-write
message exists.

**Rationale:** §2.2 forbids a secret reaching a config file. A message that can only carry a
tier name and a slug cannot smuggle one in by construction, so the guarantee is structural
rather than a validation rule someone can forget. Widening this later is easy; narrowing a
shipped message is not.

### 3. `setup_state` gained `tier_slugs` rather than a new event (Class B)

**Decision:** An additive field with a default, following `key_required`'s precedent, acked on
the same event that already answers `set_preset`.

**Rationale:** The settings screen needs the slugs at exactly the moments it already needs
`presets` and `active_preset`. A separate event would need its own request, its own reducer and
its own reconnect handling for data that always travels with this one. A client that ignores
the field behaves as it did, so this is not a version bump.

### 4. Reported slugs are the file's, captured by snapshot (Class B)

**Decision:** The daemon snapshots every preset's slugs when it adopts a config, and reports
from that snapshot. It does not re-read the file, and it does not read `self.config`.

**Rationale:** `resolve_tier_slugs` fills unset slugs *in place* (TD-1805), so after one probe a
loopback tier carries a tag that was never in the file. Reporting that would make the settings
field look configured and leave the user one save from pinning a model they deliberately left
for the endpoint to choose. Re-reading from disk would also have been correct, but
`load_config()` with no path reads the real user config — reintroducing exactly the trap
TD-1809 had just removed, into every test that calls `_setup_state_event`. The snapshot needs no
I/O. Note `model_copy` is shallow and shares tier objects, so keeping a copy of the config is
*not* a snapshot; a test pins that, because the obvious implementation silently does not work.

### 5. The nested YAML edit is surgical; `ruamel.yaml` was declined (Class B)

**Decision:** `save_tier_slug` locates the `presets → <preset> → <tier>` block by scanning
indentation, replaces the shipped `# slug:` placeholder in place, and writes atomically. The
value is JSON-encoded. No new dependency.

**Rationale:** The same reason `save_active_preset` avoids a dump: the shipped config is mostly
teaching, and a PyYAML round-trip drops every comment. A round-trip-preserving library
(`ruamel.yaml`) would solve it but adding a dependency is Class C and would need sign-off for a
2-point story. JSON encoding is not cosmetic — model tags carry colons (`qwen3.8:27b`) and a raw
newline in the value would inject arbitrary YAML into a user's config; a test pins that.

Known limit, documented on the function: it loads before it writes, so it cannot repair a config
that a missing slug already made invalid. Not reachable from the settings screen, which only
edits a config the daemon already loaded.

### 6. `config_write.py` split out of `config.py` (Class B)

**Decision:** The write half — `save_active_preset`, `save_tier_slug` and their helpers — moved
to `core/tstd/config_write.py`. `save_active_preset` now shares the atomic-write helper instead
of carrying its own copy.

**Rationale:** `config.py` reached 409 lines, past §6, and the two halves are different jobs:
one parses and validates, the other edits a file a human also edits. Same split rationale as
`e2e_checks.py` out of `e2e_harness.py` in TD-1807.

### 7. The dark palette is duplicated, and a test keeps the copies honest (Class B)

**Decision:** `tokens.css` scopes the OS rule to `:root:not([data-theme="light"])` and repeats
the same palette under `:root[data-theme="dark"]`. `ui/src/lib/tokens.test.ts` compares the two
token-by-token.

**Rationale:** A theme the user can force needs the dark tokens reachable from a plain selector,
and CSS cannot share one declaration block between a media query and an attribute selector. The
alternatives were an indirection layer (`--dark-*` variables, three mentions per token) or
`light-dark()`, which is too new to rely on across the webviews TD-1302 targets. Duplication's
only real risk is drift, and drift is machine-checkable: the test fails naming whichever value
moved. The `:not()` is what makes an explicit light choice beat a dark OS —
`:root[data-theme="dark"]` outranks the media rule, so the choice wins in both directions.

### 8. "System" clears the attribute instead of resolving to light or dark (Class B)

**Decision:** `setTheme("system")` *removes* `data-theme` from `<html>`. It does not read
`prefers-color-scheme` and write back the resolved value.

**Rationale:** Resolving in JS freezes the choice at whatever the OS was when the store started.
The media query in `tokens.css` already answers the question and re-answers it when the OS flips,
so removing the attribute hands the decision back to the layer that tracks it. The alternative
would need a `matchMedia` listener to stay correct — more moving parts for a worse answer.

### 9. The key section drives the onboarding store's flows (Class B)

**Decision:** The settings Key section calls TD-1102's `storeKey`, `validateKey` and `removeKey`
from `onboarding.svelte.ts` rather than adding equivalents to the settings store.

**Rationale:** One code path for a credential, not two. A second path is a second place for §2.2
to be violated, and the flows are identical — the settings screen is a different doorway to the
same behavior, not different behavior. The settings store holds presence (`hasApiKey`) only; a
test asserts no credential appears anywhere in its state.

### 10. `PolicyRuleList` split out of `SettingsPane` (Class A)

**Decision:** The policy section's markup and styles moved to their own component.

**Rationale:** `SettingsPane` reached 405 lines, past §6. The policy list's styles are used by
nothing else, so moving it reduced total lines rather than duplicating scoped CSS — which
extracting the key section would have done, since that section shares `.field`/`.input`/`.btn`
with the model section. It also isolates the three-state rendering: no session is a different
claim from no rules.
## 2026-08-17 — TD-1407: The kill battery waits on conditions, not wall-clock

### 1. Spawn-ack and group-gone replace the 0.3s/2.5s sleeps

**Decision:** The escape probe command now names itself:
`echo $$ > pgid.txt; { sleep 30; touch kicked.txt; } & wait` — the group
leader writes its own pgid (the test's spawn-ack), then parks a subshell
whose marker window (30s) is far wider than any scheduling stall the
suite has measured. Cancel/timeout tests wait for `pgid.txt` before
cancelling (the cancel provably lands mid-run, never mid-spawn) and,
after the kill settles, probe `killpg(pgid, 0)` until the group is gone
instead of sleeping past a marker deadline. A kill that lands before the
leader's first write leaves no pgid file — nothing was forked, so
nothing could escape, and the assertion passes by waiting the file out.

**Rationale:** The old arithmetic (`sleep 0.3` → cancel → `sleep 2.5` →
assert no marker) only holds when the loop schedules the kill within
~1.7s of the escapee's start; measured stalls under full-suite load ran
3–14s, so the subshell won for reasons unrelated to the product. The
group-gone probe keys on the condition the marker approximated — a
surviving group IS the escaped grandchild — so the test still fails on a
genuine escape (AC: the fix must not become a test that cannot fail),
and the 30s window makes the environmental race unwinnable for the
escapee instead of merely unlikely. The `kill refused` escape hatch is
unchanged and still short-circuits before any probing: a vetoed group
outlives the test, so veto rounds never probe.

## 2026-08-17 — TD-1408: Config tests read the shipped default, not the developer's

### 1. The fixture is the shipped default, loaded by explicit path

**Decision:** test_config.py's eight no-arg `load_config()` calls and
test_cost.py's tracker fixture now load `default_config_yaml()` written
into tmp_path. `test_cached_config` redirects `tstd.config.user_data_dir`
to tmp_path and clears the lru cache around the call. No test in
core/tests/ reads configuration from `user_data_dir()` — the remaining
no-arg `cached_config()` consumers (test_setup_state, test_e2e_live,
test_local_preset_paths) redirect HOME first, as they already did.

**Rationale:** A no-arg load resolves the developer's real file, so the
suite was green only while that file byte-matched the shipped default —
and the documented remedy for a multi-model endpoint (pin a slug,
TD-1805) is exactly what breaks it. Because the fixture IS the shipped
default, the pinned values (slugs, 16384 max_output, prices) keep their
bite: a shipped config that drifts from its documented tags still fails
these tests, on any machine.

## 2026-08-17 — TD-1707: The command palette is a doorway, not a second implementation

### 1. The right pane's tab moved out of AppShell into a store (Class B)

**Decision:** The Activity | Stack choice, until now `let rightTab = $state(...)`
inside `AppShell.svelte`, lives in `ui/src/lib/right-pane.svelte.ts` as
`rightPane.tab` with a `showRightPane(tab)` setter. AppShell renders it and no
longer owns it.

**Rationale:** "Open stack" is one of the acceptance criteria's commands, and a
command can only drive state that something other than the markup owns. The
alternatives were worse: exporting a setter from a component, or having the
palette store poke a prop through the tree. It also matches how every other
commandable surface in the app already works — doctor, decisions and settings
each own their `open` flag in a store, and the palette calls the same function
their header buttons call.

### 2. Escape gains a palette layer, and `ShortcutContext` gains a field (Class B)

**Decision:** `resolveShortcut` peels menu → palette → modal → turn, returning
the new `"close-palette"` action, and `ShortcutContext` grows a `paletteOpen`
flag. AppShell also counts the palette in `modalOpen`.

**Rationale:** The palette is a modal layer, so Escape must dismiss it and must
not reach through to cancel a running turn — the exact failure TD-1609's layer
order exists to prevent. Two ways to spell that were available: handle Escape
inside `CommandPalette.svelte`, or extend the one pure mapping. A local handler
would be a second opinion about what Escape means, and the order between two
listeners is not something the tests could pin. Counting the palette in
`modalOpen` as well is belt and braces: if the palette layer is ever removed
from the chain, the turn still doesn't get cancelled by an Escape aimed at the
palette. The added field is why `shortcuts.test.ts` contexts changed shape.

### 3. "Toggle theme" swings between light and dark; "system" is a way in, not a stop (Class A)

**Decision:** The palette's theme command sets light when the current theme is
dark, and dark otherwise — so from "system" the first press lands on dark and
the next on light. It never selects "system".

**Rationale:** A two-state toggle is what a palette entry named "Toggle theme"
promises; cycling three states makes the same keystroke mean different things on
consecutive presses. "System" stays reachable where it is chosen deliberately —
the settings Appearance section, which is one palette entry away.

### 4. Greedy subsequence matching, not optimal placement (Class A)

**Decision:** `fuzzyScore` walks the query left to right taking the first
available character, scoring word-start hits (+10) and adjacent runs (+6)
against a capped gap penalty. A title hit always outranks a hit in an entry's
subtitle or keywords.

**Rationale:** Greedy placement is not always the best-scoring placement, but
the corpus is six actions plus the listed sessions. An optimal matcher would
cost more to read and maintain than the ranking it buys at that size.
## 2026-08-17 — TD-1704: The steerable queue is the client's, not the daemon's

### 1. Queued rows are held in the UI and drained one per turn end (Class B)

**Decision:** A message composed while `showCancel(chat.turnState)` is true is
parked in `chat.queued` and is not written to the wire. The store hands the head
of that queue to the daemon on `turn_complete` — one row per turn end, in order —
and `sendQueuedNow` hands one over immediately, ahead of the rows before it.
Nothing flushes on a terminal `session_state`. Switching sessions or disposing
drops the queue.

**Rationale:** The daemon does already queue mid-turn user messages — the handler
enqueues into `Session._user_message_queue` whenever the session is live, and the
loop drains one at each turn boundary — so this story needed no core change. But
that queue is write-only from the client's side: the protocol has no message that
edits or withdraws a `user_message` once sent, and the only way to void one is to
kill the session. Sending on submit and *also* drawing rows would give the user a
send-now, an edit box and a remove button that could not affect what the daemon
would run, which is worse than not offering them. Holding the rows client-side is
what makes all three real. Draining one per turn end rather than the whole queue
keeps that property for the tail: everything not yet running stays editable.

**Consequence noted:** the queue gate is `showCancel` alone, deliberately not a
second notion of running (AGENTS §6 — the UI does not derive truth it wasn't
given). In the short window after a local send where `awaitingFirstToken` is set
but no delta has yet raised `turnState` to `running` (TD-1714), a second send goes
straight to the wire and the daemon queues it, as it does today — correct, just
without a row.

### 2. The composer's send→stop morph stands; Enter is the queue's way in (Class A)

**Decision:** `Composer.submit()` no longer refuses while `running`, but the
circular button still morphs to stop (TD-1604/TD-1609). While a turn is live,
Enter queues and the button cancels.

**Rationale:** Two live buttons in the card would need a second affordance and a
layout for it, to duplicate a key that already submits. The queued row appearing
directly above the card is the confirmation the send landed.
## 2026-08-17 — TD-1503: The configuration reference is executable, not prose

### 1. Every YAML example carries a verify marker and is run through the real loader

**Decision:** `docs/configuration.md` annotates each fenced YAML block with
an HTML comment — `<!-- verify: model -->`, `tier`, `workspace`, and the
`-invalid` variants — and `core/tests/test_docs_config_reference.py` runs
each one through `load_config` or the three `.tst/config.yaml` readers.
A `yaml` block with no marker fails collection, so an example cannot be
added without also being checked. The test additionally pins three claims
the prose makes: every key used in an example exists in the schema, every
field of every config model appears in the document, and no slug from the
shipped `config.yaml` appears in the text.

**Rationale:** Both config models inherit pydantic's `extra="ignore"`, so a
stale example validates cleanly while documenting a key that no longer
exists — "does it parse" is not enough on its own. The undocumented-key
check is what makes the reference stay complete: adding a field to
`TierConfig` or `CapsSection` now fails the suite until someone writes it
down, rather than leaving a gap nobody audits. The slug check enforces §7
of the spec — the landscape moves weekly, so the shipped config is the one
place slugs live, and a slug in prose would be a second one to forget.
The marker sits in an HTML comment so it is invisible in rendered markdown.

### 2. The reference documents enforced behaviour, not the shipped comments

**Decision:** Where the shipped `.tst/config.yaml` template disagrees with
the code, §4.5 documents what the code does and names the disagreement.
Two cases: `allowed_commands: []` refuses every command (the template says
"empty means any command"), and `network` is not enforced by any shipping
tool (the template says `deny` blocks all network). Both are reported as
defects; neither is fixed in this story.

**Rationale:** A reference that repeats a comment the code contradicts is
worse than no reference — the reader configures against it and gets the
opposite of what they asked for, which for `allowed_commands` means an
unusable shell tool on the default settings. Scope discipline (§3) says a
docs story does not change product code, and describing the gap keeps the
reader correct today while leaving the fix to be scheduled on its own.
## 2026-08-17 — TD-1712: Rail information architecture

### 1. Rail entries carry a three-way state, not a landed/unlanded flag (Class B)

**Decision:** `RAIL_FUNCTIONS` (ui/src/lib/rail.ts) gives each surface `state:
"current" | "ready" | "planned"`. Only `ready` activates. Home is `current`,
Projects is `ready`, Scheduled is `planned` with the note `v0.5`.

**Rationale:** The AC forbids a dead click, and a boolean can't tell the two
non-clickable rows apart: "the pane is already showing this" and "this epic
lands in v0.5" are different claims, and rendering them alike would lie about
one of them. Home began as a `landed` entry that dismissed the stacked
overlays; the preview killed it — the settings pane covers the rail with a
full-viewport overlay, so the one situation that action existed for is the one
situation in which it can't be clicked. Navigating home for real would mean
unbinding the pane from its session, which is TD-1711/TD-1713's attach
machinery, not a size-2 IA story. So Home renders selected (`aria-current`,
accent edge) and the dispatcher refuses it.

### 2. The account row's identity is the active preset + key presence (Class B)

**Decision:** `accountRow(activePreset, hasApiKey)` labels the row with the
daemon's `setup_state.active_preset` verbatim (falling back to "No provider"),
takes the avatar initial from it, and notes "Key stored" / "No key".

**Rationale:** §2 says there is no account, so the row can only anchor the
identity the app actually has: which provider preset is active and whether a
key exists. Both are daemon truth from an event the settings store already
reduces — §6's "never derive truth it wasn't given". The preset name shows
verbatim because capitalizing `tst-default` would invent a name the config
never used. The note is presence only; the key itself never reaches UI state
(§2.2), which TD-1703's tests already pin.

### 3. Settings left the header for the rail's account anchor (Class B)

**Decision:** AppShell's settings gear is gone. Settings is reached from the
account row or ⌘, (`resolveShortcut` unchanged). Decisions and doctor keep
their header buttons.

**Rationale:** The AC places the settings entry in the rail "not buried in the
title bar". Keeping both would leave two doorways to one pane and the buried
one would stay the muscle memory.

### 4. Projects raises TD-1103's recents menu (Class A)

**Decision:** The Projects row calls `toggleWorkspaceMenu()` rather than
growing a rail-side picker.

**Rationale:** One workspace-switching surface, not two. Known seam: the menu
still anchors under the title bar, so the popover opens away from the row that
raised it. Re-anchoring belongs with the Projects surface proper (TD-1103's
epic), not here.

### 5. `RailFunctions` and `RailAccount` split out of `SessionRail` (Class A)

**Decision:** The function group and the account anchor are their own
components, both taking a `compact` prop for the collapsed strip.

**Rationale:** SessionRail reached 586 lines with the new markup and styles,
past §6. Neither block shares scoped CSS with what stayed behind, so the split
removed lines instead of duplicating them — the same test decision #10 applied
to `PolicyRuleList`.

## 2026-08-17 — TD-1705: The files pane reads the diffs, not the arguments

### 1. A written file's identity comes from the diff header (Class B)

**Decision:** `foldFileWrites` (ui/src/lib/files.ts) keys the file list on the
path in each diff section's `+++ b/<path>` header. It never reads the
`tool_call`'s `path` argument, and it keeps no list of which tool names write.

**Rationale:** The header is the only place the daemon states what it actually
wrote: `dispatch.py` labels the diff with the *canonical* path
(`path_guard.check_write`'s resolved, absolute result), while the argument is
whatever the model typed and may be relative or a symlink alias. It is also the
only source that survives a call with two write targets — a move renders two
sections in one `tool_result`, and the arguments would have to be un-mapped
field by field to tell which is which. Keying on the header is §6's "the UI
never derives truth it wasn't given" applied literally, and it means the pane
needs no table of write tools to fall out of date with the registry. The cost
is that a write the daemon could not diff (binary, oversized, unreadable —
`snapshot_text` returns `None`) does not appear in the pane. That is the right
failure: listing a file with no diff would be the UI asserting a write it was
not told about.

### 2. The fold is a pure function over the timeline, not a second store (Class B)

**Decision:** There is no `files.svelte.ts`. `FilesPanel.svelte` derives its
whole model with `$derived(foldFileWrites(entries))` over the existing
`timeline-store` array.

**Rationale:** The diffs are already in the timeline store — a second store
subscribed to the same event stream would keep a second copy of every write in
step for no gain, and would need its own clearing rule. Recomputing costs an
O(total diff bytes) pass, and only while the Files tab is the visible one,
because `$derived` does not run when nothing reads it. The fold being pure is
also what makes it testable at all: this repo tests stores and pure functions,
not `.svelte` components (files.test.ts drives it through the real `Timeline`).

### 3. Most-recently-written first (Class A)

**Decision:** `FilesSummary.files` sorts by the seq of each path's latest
write, newest first; a file written again moves back to the top. Within a file,
its writes stay oldest-first.

**Rationale:** The pane is watched live while an agent works, and what it just
touched is what the reader is looking for. Discovery order would bury the
active file under whatever was written first. The per-file write list keeps
stream order because it reads as a history of that one file.
---

## 2026-08-17 — TD-1716: Resume healing

### 1. `ping` is a frame, not a `DaemonEvent` (Class B)

**Decision:** The liveness frame is `Ping` — `{"type": "ping"}`, no `session_id`,
no `seq` — and it does **not** inherit `DaemonEvent`, whose
`seq: int = Field(gt=0)` is the per-session event log's contract. It joins
`DaemonEventT` and `_KNOWN_EVENT_TYPES` so `parse_daemon_event` accepts it, and
`build_ping()` sits beside `build_hello_ack()`. In the TypeScript mirror `Ping`
is its own interface and is deliberately **absent** from `DaemonEventUnion`; the
client consumes it in `handleMessage` and it never reaches a store.

**Rationale:** A ping has nothing to sequence. Giving it a seq would mean either
inventing a session to charge it to or minting a second, connection-scoped
counter that no replay could ever use — and the moment it carries a seq, the
client's gap detection is entitled to reason about it, which is precisely the
machinery a liveness frame must stay out of. The two unions have different jobs
and the asymmetry follows from that: Python's `DaemonEventT` is the wire's parse
union (a frame the daemon really sends must parse, or a strict client chokes on
its own daemon), while TypeScript's `DaemonEventUnion` is the set of events
stores reduce. `hello_ack` already sets the precedent on both sides — sent by
the daemon, defined in `protocol.ts`, absent from the union, handled in the
transport. `ping` is the same kind of thing.

**Rejected:** making `seq` optional on the `DaemonEvent` base. That weakens the
guarantee every sequenced event depends on, to describe one frame that isn't
sequenced at all.

### 2. The zombie test runs on resume, never on a timer (Class B)

**Decision:** The client stamps `lastFrameAt` on every inbound frame and
compares it against `ZOMBIE_SILENCE_MS` (30s, two ping intervals) **only** in
`resume()`. There is no client-side heartbeat watchdog.

**Rationale:** A heartbeat timer would be defeated by exactly the condition it
exists to detect — a suspended webview's timers do not fire, so the check would
sleep through the outage and then run late against a clock that had already
healed. It would also be wrong in the other direction: a legitimately
backgrounded window is silent and healthy, and tearing its socket down on a
timer would be fighting App Nap, which the story explicitly rules out. The
resume edge is the one moment the page is provably running and the evidence
(a stale `lastFrameAt`) is still on the floor where the suspension left it.

**Consequence:** ping cadence and the silence threshold are coupled —
`PING_INTERVAL_SECONDS * 2 <= 30` is asserted in `test_resume_healing.py` so a
future cadence change cannot silently make one dropped ping look like a death.

### 3. Re-attach had to become repeatable on the wire (Class A)

**Decision:** Two fixes the unconditional re-attach forced into the open:
`_handle_attach` cancels any stream the same connection already had for that
session before replaying (with an `owner` guard in `_cleanup_attach` so the
superseded task's teardown cannot tear down its replacement), and
`ProtocolClient.forceReconnect` disowns a socket before closing it, with
`onclose` ignoring a socket that is no longer current.

**Rationale:** Before this story a second attach on one connection was a wiring
mistake; now it happens every time the user comes back to the window. Left
alone, each resume added a streaming task that duplicated every event to the
same socket, and each zombie close scheduled a retry on top of the reconnect it
had just started — one suspension, two sockets, then four. Both are the same
shape of bug: a superseded thing that never learned it was superseded.
## 2026-08-17 — TD-1715: Archive, delete, and re-project sessions

### 1. One complete `session_list`, with an `archived` flag per row (Class B)

**Decision:** `SessionSummary` gained `archived: bool` (default false) and the
daemon keeps listing every session. "Hidden from the default list" is rendered
from the flag — the rail shows one shelf or the other — rather than by scoping
the request.

**Rationale:** The alternative was an `include_archived` flag on
`list_sessions`, which reads cleaner right up until you count the consumers.
Three stores reduce the same `session_list` event: the rail, the recents menu
(`workspaces.svelte.ts`), and the chat pane's auto-bind. Making the event's
*contents* depend on who asked would mean a rail click on "Archived" silently
rewrites the recents menu and unbinds the chat pane, because to those two
stores a narrowed list is indistinguishable from sessions having disappeared.
Every consumer would then need to know the scope of a list it did not request.
One complete list with a flag keeps `session_list` meaning exactly what it has
always meant, and makes each consumer's filter its own business.

### 2. In-flight turn is counted on the session, not read off `session_state` (Class B)

**Decision:** `Session` tracks `_open_turns` — raised in `add_user_message`,
lowered on the `turn_complete` event via a subscription to its own event log —
and exposes `turn_in_flight`. Delete and Move refuse on it with
`session_busy`; Archive ignores it.

**Rationale:** `session_state "running"` cannot answer this. TD-1714 established
that it means the session's *loop* is alive: set once at open, spanning the
whole session, replayed at the head of every attach. Refusing Delete on
"running" would refuse it for every healthy session forever. `turn_complete` is
the only frame on the wire that proves a turn ended, so it is what lowers the
count. Terminal states force `turn_in_flight` false regardless, so a session
whose loop died mid-turn can still be deleted rather than being wedged by a
count nothing will ever decrement.

### 3. Move keeps the pane bound; Archive and Delete rebind it (Class B)

**Decision:** All three verbs answer with a refreshed `session_list`, and the
chat store's existing binding rule resolves the pane: keep the current session
while it is listed *and unarchived*, else bind the newest live unarchived one,
else empty state. A moved session stays listed and unarchived, so the pane
stays on it and the title bar follows it to the new project.

**Rationale:** The criterion pairs "archiving, deleting, or moving" with "never
a stranded composer", and a rebind is what prevents stranding when the session
leaves the shelf. A move doesn't remove it — that is the point of criterion 3,
where the event log moves *with* the session — so evicting the user from a
conversation that survived intact would be the worse reading of the same
sentence. One rule covers all three cases and lives in one place, so no verb
can grow its own rebinding logic later.

### 4. Move validates that the target is a directory, not that it is "known" (Class B)

**Decision:** `move_session` accepts any path and refuses one that is not a
directory (`workspace_not_found`), mirroring `open_workspace`'s own check. The
picker offers only workspaces the recents store already knows.

**Rationale:** "Another known workspace" is a UI affordance, not a new
capability boundary: `open_workspace` has always accepted an arbitrary path
from the client, so restricting this verb further would guard nothing while
blocking the obvious case of moving into a project opened moments ago. The
substantive validation — it exists, it is a directory — is the same one the
open path performs, and the boundary and policy are re-resolved from the new
root so the next turn runs under the target's wall, not the origin's.

### 5. `session_lifecycle.py` and `session-actions.svelte.ts` split out (Class A)

**Decision:** The three daemon verbs live in `core/tstd/session_lifecycle.py`
rather than `daemon.py`; the rail's row commands live in
`ui/src/lib/session-actions.svelte.ts` rather than `sessions.svelte.ts`. The
row markup moved to `RailSessionRow.svelte`.

**Rationale:** §6's file-size rule, applied before the fact in each case.
`daemon.py` is already the largest module in the package and gains three lines
of dispatch instead of a hundred of logic; `sessions.svelte.ts` crossed 400
lines with the commands inline and came back to 341 without them. The store
keeps `closeRowMenus` because its own reducer calls it on every refreshed list
— imports run store → actions only, never back.

### 6. Delete leaves the audit database alone (Class A)

**Decision:** Deleting a session drops its registry entry, its runner, its
in-memory event log, and its durable record. The append-only audit log is not
touched.

**Rationale:** The criterion says the event log; the audit database is the
forensic record §2 requires to be append-only. A delete that could rewrite it
would make it not that.

---

## 2026-08-17 — TD-606: An empty `allowed_commands` means unrestricted

### 1. Empty or absent means any command (Class B)

**Decision:** `BoundarySection.shell_allowlist()` returns `None` — `ShellPolicy`'s unrestricted
sentinel — for both an omitted and an explicitly empty `allowed_commands`. Rejected the
alternative where absent means unrestricted and an explicit `[]` means deny-all.

**Rationale:** The shipped template has always said "empty means any command"; the code disagreed
and the code was wrong. The alternative needs a schema change to `list[str] | None`, silently
flips the meaning of any config already carrying `[]`, and invents expressive power nobody asked
for. If deny-all-shell is wanted it should be its own switch rather than an overloaded empty
list — noted as a deliberate non-goal.

The obvious objection is that this is a security boundary and fail-closed is normally right —
TD-1402 chose exactly that for Windows paths. It does not apply here, because the allowlist is
not the gate. No rule in `RULE_TABLE` matches on tool name, so a plain shell call falls through
the static rules to the ambiguous classifier and defaults to class B, which is an approval the
user answers (§2.6). Permissive-by-default restores a second wall to its documented shape; it
does not remove the first one.

### 2. The translation lives on the boundary, not at the call site (Class B)

**Decision:** the empty-means-unrestricted rule is a method on `BoundarySection`. Not
`daemon.py`, and not `ShellPolicy`.

**Rationale:** fixing the call site repairs one caller and leaves the trap armed for the next.
Teaching `ShellPolicy` that `()` means unrestricted makes `()` and `None` synonyms inside the
tool, which buries config semantics in the wrong layer and forecloses ever expressing deny-all.
The boundary object is what owns what the config *means* — §6's "validate at the boundary, trust
internally".

Note the two other `allowed_commands` readers in `daemon.py` were deliberately left alone: both
build `BoundaryUpdateEvent`, which reports the configured wall to the UI. They should show the
raw list, because an empty list there means "no allowlist configured", not "the sentinel".

### 3. The regression test had to watch the daemon, not imitate it (Class A)

First attempt mirrored the daemon's wiring in the test and passed with `daemon.py` reverted —
the same shape of gap that let the defect ship, reproduced in the fix. The behavioural runs stay
(they pin the semantics), but `TestTheDaemonsOwnWiring` now spies on `register_builtin_handlers`
while calling `_start_session`, and two of its runs go red on revert. Verified by reverting.

---

## 2026-08-17 — TD-1706: Usage and cost view

The story's note said "aggregation queries exist (TD-903); verify which export
affordances are already wired before adding UI." Verified first: TD-903 landed
`core/tstd/audit_queries.py` — `cost_by_turn/session/day/tier`, `export_jsonl`,
`export_csv` — and **none of it is reachable from the client**. Its only
non-test caller is `e2e_checks.py`. The live `cost_update` event carries the
running session totals the title-bar meter renders, not audit-store history,
and no message triggers an export. So the UI could not be added over the
existing protocol, and the four decisions below follow from that.

### 1. Two new client verbs rather than one general "query the audit store" (Class B)

**Decision:** `get_usage` → `usage_report` and `export_usage` → `usage_exported`.
Both connection-scoped with `seq: 1`, like `diagnostics_report`.

**Rationale:** The audit database spans every session, so a session-scoped
message would answer a different question than the one asked. Two narrow verbs
beat one parameterised query verb for the same reason `set_tier_slug` beat a
general `update_config`: a message that can only ask for rollups cannot later
be talked into reading rows it should not. Widening is easy; narrowing a
shipped message is not.

Registered in all four places — `ClientMessageT`, `_KNOWN_CLIENT_TYPES`,
`DaemonEventT`, `_KNOWN_EVENT_TYPES` — plus both TypeScript unions. A type in
the union but not the known-type set parses as `unknown_message` at runtime
rather than failing at import, so the round-trip tests in `test_protocol.py`
go through `parse_client_message` specifically to catch that.

### 2. `export_usage` carries a format, never a destination path (Class B)

**Decision:** The daemon writes to `<data_dir>/exports/usage-<UTC stamp>.<ext>`
and returns the path on `usage_exported`. The client cannot name the file.

**Rationale:** A client-supplied path would make this message a general
"write a file anywhere the daemon can reach" verb reachable from the socket —
a wider hole than an export button needs, and one no acceptance criterion
asks for. The criterion is that the buttons *reuse* the existing exporters,
which they do: the handler calls TD-903's `export_jsonl`/`export_csv`
unchanged. The UI shows the returned path and opens it with the same
`openInEditor` the stack panel uses on daemon-supplied paths.

The cost is that the user does not get a native save dialog. Accepted: adding
one later is additive (a new optional field, or a host-side reveal), whereas
un-shipping a path parameter is not.

### 3. Reads open their own connection to `audit.db` (Class B)

**Decision:** `_usage_report` and `_usage_export` construct a short-lived
`AuditStore` inside `asyncio.to_thread` rather than borrowing the
`AuditWriter`'s.

**Rationale:** What makes the writer's single sqlite connection safe under
`check_same_thread=False` is that its one drain task serializes every call —
`audit.py` says so explicitly. A query sharing that connection would run beside
a write on it from a different pool thread and lose exactly that guarantee.
WAL is already set in `AuditStore.__init__`, and a second connection is what
WAL is *for*, so this is the supported way to ask rather than a workaround.
The migration step a fresh handle runs is idempotent and writes nothing at the
current version. `test_daemon_usage.py` queries a database a live daemon holds
open, so the arrangement is tested rather than assumed.

### 4. Weeks open on Monday, and the rollup groups on bucket *and* tier (Class A)

**Decision:** `usage_rollup(store, bucket, limit)` is a new query beside the
TD-903 four, not a replacement. Week keys are `date(day, 'weekday 0', '-6 days')`.

**Rationale:** The existing queries answer one scope at a time, which is what
the totals needed; the view needs "what did Tuesday cost, and how much of that
was the brain" — a bucket and its tier split at once. It reads the same `costs`
view, so it is another slice of identical rows and cannot disagree with them.

The week expression steps forward to the coming Sunday, then back six days.
SQLite leaves a date that already is Sunday where it stands, so Sunday resolves
to the Monday that opened its own week rather than the next one — the boundary
this gets wrong if `weekday 0` is used without the step back, and the case
`test_week_bucket_keys_on_the_monday_that_opened_it` pins.

`limit` caps *buckets*, not rows. A row cap would truncate a day mid-tier and
report it as costing less than it did; `test_limit_never_truncates_a_bucket_mid_tier`
is the test that would catch that regression.
## 2026-08-17 — TD-1709: Attachments v1

### 1. The cap lives on the workspace boundary, in its own section (Class B)

**Decision:** attachment caps are a new top-level `attachments` section in
`.tst/config.yaml` — `max_file_bytes`, `max_total_bytes`, `max_count` — modelled by
`AttachmentLimits` in the new `tstd/attachments.py`. Not `config.yaml` (the model plane), and
not a fourth key under `caps`.

**Rationale:** the model plane was the tempting alternative, because attachment size does
relate to how much context a model can take, and `context_window` already lives there. It is
the wrong home. A tier is a per-turn routing decision — the router moves a session from brain
to worker between turns — so a cap sourced from the model plane would accept a file on one turn
and refuse the identical file on the next, with nothing the user did explaining the difference.
A client-facing refusal has to be stable for as long as the composer is open. The workspace is
also the unit a team shares: `.tst/config.yaml` is git-tracked, so a repo full of large
generated files can tighten this for everyone who opens it, and the file is re-read on open and
on resume rather than needing a daemon restart.

Its own section rather than a fourth key under `caps` because §4.2's whole sentence is "checked
before every model call, and hitting one pauses the run". These are checked when a client
message arrives and they pause nothing — they refuse. Filing them together would cost that
section the one description that makes it legible.

### 2. Attachments travel as base64 bytes, not as decoded text (Class B)

**Decision:** `Attachment` carries `name` and `content_b64`. Not a `content: str` the client
decoded first, and no client-declared size or mime type.

**Rationale:** this is what makes "the daemon refuses a binary attachment" true rather than
decorative. Given bytes, the daemon runs strict UTF-8 plus a NUL scan itself and a PNG fails.
Given a string, the client has already decoded — a client that reads a PNG with `readAsText`
hands us replacement characters, which are indistinguishable from prose that legitimately
contains them, and the gate degrades into trusting the sender. The costs are real and small:
base64 adds a third to the frame, and the transport ceiling that implies is documented in
§4.3 of the configuration reference. Declared size and mime are omitted for the same reason —
the daemon has to measure the real bytes anyway, and two sources for one fact is how they start
disagreeing.

### 3. A refused attachment refuses the whole message (Class B)

**Decision:** one bad file fails the send; nothing partial is delivered, and every refusal's
copy ends by saying nothing was sent.

**Rationale:** the alternative — drop the offending file, deliver the rest — leaves the user
watching a turn run against a message they believe carried three files when it carried two,
with no signal about which. Silent partial delivery is the failure mode §6 forbids. The cost is
that the typed text is dropped too, which is why the copy says so explicitly rather than
leaving the user to discover it.

### 4. The sent row keeps chips, not bytes — so retry refuses (Class A)

**Decision:** `ChatMessage.attachments` carries names and sizes only. `retryLastUserMessage`
returns false when the last user row carried attachments.

**Rationale:** holding the bytes would keep every file a session ever attached in memory for
the life of a transcript that only ever renders the label. The consequence is that retry cannot
faithfully resend such a message, and a resend without the files would be a different message
wearing the same label — so it refuses instead. Re-attaching is a few seconds; a silently
different retry is a bug report.

### 5. No `accept` filter on the file picker (Class A)

**Decision:** the picker accepts any file and the refusal explains.

**Rationale:** the third acceptance criterion is that an oversize or binary attempt fails *with
actionable copy*. A picker that greys out `.png` produces no copy at all — the user learns
nothing about why, or that text files are the deliberate scope of v1.
## 2026-08-17 — TD-1810: Tell the model the workspace root

### 1. The root goes inside the cache prefix, ahead of steering (Class B)

**Decision:** A new block `[1b]` states the absolute workspace root once,
between the base system prompt and the steering block, and it is part of
the TD-305 prefix that `AssembledPrompt.prefix_hash` covers. Spec §4.5's
order becomes base → **workspace root** → steering → memory → manifest →
conversation. Every tier gets it, at the same offset.

**Rationale:** "Does not disturb the cached prefix" is a statement about
stability, not about staying out of it. The root is constant for the life
of a session, so the two positions that keep it out of the prefix — after
memory, or after the manifest — would re-bill bytes that never change on
every turn, which is the exact cost TD-305 exists to avoid. Inside the
prefix it is paid for once per session and read from cache after that.

Ahead of steering rather than behind it, because a prefix cache is
invalidated from the first changed byte onward: steering is the earliest
block that can change mid-session (a TD-509 reload), and a root that
followed it would be re-tokenised every time someone edited `AGENTS.md`.
Ordering stable-est-first is the whole mechanism.

All three tiers, because the worker calls the same `fs_*` tools the brain
does, and because a per-tier position would split the `base + root` head
that brain, worker, and validator currently share.

Cost, once. **Corrected 2026-08-17** (this read "~80–125 heuristic tokens,
once", which was wrong at both ends and wrong in kind): the block is fixed
prose plus the root written twice, so its size scales with the root and no
single range describes it. Measured through `tstd.context.tokens`
(`make_token_counter` returns the heuristic here — tiktoken is not a
dependency, so every slug falls back to `ceil(chars / 4)`): the block as
first written was 345 + 2·len(root) characters, i.e. 87 tokens for a
two-character root, 108 for this repository's own 44-character root, and
136 for a 100-character pytest temp root — over the top of the stated range,
not inside it. The block as reworded below is 284 + 2·len(root) characters:
`71 + ceil(len(root) / 2)` tokens, so **72 / 93 / 121** for those same three
roots. The existing TD-305 invariant is unchanged —
`test_prefix_still_byte_identical_across_calls` and
`test_cache_prefix_stable_across_turns` both hold.

### 2. The `fs_*` schema descriptions are left alone (Class B, rejected)

**Decision:** `fs_read`, `fs_list`, `fs_write`, and `fs_edit` keep
"Absolute path to the file to …". The root is not interpolated into any
tool description.

**Rationale:** Considered, because a parameter description is the text
closest to the argument being filled. Rejected on three counts. It would
put the root on the wire four times per request instead of once, which is
the opposite of the story's design constraint. It would make the registry
workspace-dependent — today `create_registry()` is a module-level factory
with static text, and parameterising it by workspace changes a public API
shape for every provider and every caller. And it is unnecessary: the
prompt block alone moved a real model from 0/12 to 12/12 on absolute-path
emission, so the schema change would buy nothing measurable at a real cost.

**Corrected 2026-08-17.** The reading of that A/B which this third count
leaned on — written out in the backlog's Done note as "the BEFORE prompt
already contained the root *substring* … the bytes were never the problem,
stating them as the root was" — does not survive checking. Two errors.

The BEFORE prompt did not contain the root. It contained `<root>/AGENTS.md`,
inside a steering provenance comment, and only when the workspace carried
its own steering file. Reconstructing the pre-TD-1810 prompt over the
story's own fixture: with a workspace `AGENTS.md` the root occurs exactly
once on all three tiers, always as the head of that longer path; with no
workspace steering file it occurs zero times on all three. Recovering the
root from `<root>/AGENTS.md` means stripping a filename — an inference,
which is the step the story exists to remove — and a workspace steered only
from `~/.tstdesk` has no bytes to strip in the first place.

And the A/B cannot attribute the delta to naming the root, because it never
varied that alone. The block went in as one unit: the labelled statement,
the worked join, and the imperative "never pass a relative path to a tool".
0/12 → 12/12 is evidence the block works. It is not evidence about which of
those three sentences did the work; no arm isolated one.

What survives is the count this section actually needs: on the suite as run,
the prompt block took absolute-path emission to 12/12, leaving no headroom
for the schema change to occupy. The rejection stands. Its third reason is
narrower than it was written.

### 3. The root is rendered resolved, with POSIX separators (Class A)

`Path(workspace_path).resolve().as_posix()`. Resolved because the path
guard canonicalises before comparing, so stating a symlinked root would
hand the model a prefix that only accidentally matches the boundary it is
checked against. POSIX separators for the reason the manifest already
renders its entries that way (TD-1406): the model concatenates the two, and
on Windows a backslash root would additionally have to survive JSON string
escaping inside a tool-call argument.

### 4. The root is not a secret, and nothing new logs it

§2.2 covers credentials — API keys in the keychain, redacted on every
output path. A filesystem path the user chose when they opened a workspace
is not one, and it was already the least-secret value in the system:
`Session.workspace_path`, the boundary config, the steering provenance
comments (`<!-- from: /abs/ws/AGENTS.md (workspace) -->`), and every
`fs_*` tool result already carry it. This story adds no new sink — the
audit database stores token counts and cost for a model call, never prompt
text, so the assembled prompt does not reach it, and no log line was added.

### 5. Spec §4.5 now understates the assembled order (flagged, not fixed)

The story's instructions limited markdown edits to `DECISIONS.md` and the
backlog tick, so `docs/tst-desk-spec.md` §4.5 still shows the five-block
order without `[1b]`. §10 wants docs to match behaviour; recording the drift
here rather than leaving it unremarked. The one-line diagram fix is the
whole of it.

**Closed 2026-08-17.** Spec §4.5's diagram now carries the `[1b]` line. The
"Blocks 1–2 are the cache target" sentence below it was left alone: it is
still true, since `[1b]` sits inside that span.

## 2026-08-17 — TD-1810 defect pass: what the block may claim, and who pays

Six defects logged against TD-1810 by its verifiers. Three needed a
decision; the other three were a wording fix, a test that did not test its
own name, and the record corrections filed above and in the backlog.

### 1. Block [1b] states a rule, not a claim about the rest of the prompt (Class B)

**Decision:** The block's middle sentence changes from "Workspace files are
listed relative to this root, so the absolute path of a listed file is the
root joined with its listed path" to "Any path written relative to the
workspace resolves against it". The worked example (`"src/app.py"` →
`"<root>/src/app.py"`) and the closing imperative are unchanged.

**Rationale:** The old sentence asserted that a listing exists. Only the
brain tier is given the workspace manifest (`context/tier.py`); the worker
gets task and relevant files, the validator gets diff and test output, and
neither is handed a file listing at all. The same bytes go to all three
tiers on purpose — a per-tier wording would split the `base + root` head
they share, which is the reason for the block's position — so the sentence
has to hold without knowing which blocks follow it. A rule about how
relative paths resolve does; a claim about what the prompt contains does
not, and a model told it saw a listing has a reason to behave as though it
did. Scoping the sentence per tier was the alternative and was rejected on
the shared-prefix cost. Side effect: 15 tokens cheaper per session.

### 2. A root that cannot be stated verbatim is refused, loudly (Class B)

**Decision:** `workspace_root_block()` raises `ValueError` when the resolved
root contains a C0 control character (tab included), DEL, or U+2028/U+2029,
naming the offending codepoints. Session assembly fails at that point rather
than emitting a block.

**Rationale:** Block [1b] is one labelled line. A newline in the workspace
path ends that line early, so the stated root becomes the text before the
newline and the remainder reads as prose — the model then joins a real but
*wrong* absolute path and the path guard is handed something outside the
workspace. Nothing about that is visible: no exception, no wrong-looking
output, a prompt that still parses. It is the worst failure shape available,
so it gets the loudest handling. The two alternatives lose on the same
ground: escaping puts a string in the prompt that is not the path, and the
model has no way to know which of the two it is being shown; stripping
silently states a root that does not exist. Tab and the other non-printing
characters go with newline rather than being curated out, because the
invariant is "the root as read equals the root the guard enforces", and a
character that a model, a log line, or the inspector may render, strip, or
normalise differently cannot be shown to satisfy it. A uniform rule is also
one rule. NUL never reaches the guard — `pathlib` rejects it first — and is
pinned by its own test so a future change that stops resolving the path does
not quietly let one through. §6's "no silent failure" is the governing line;
a workspace directory with a newline in its name can be renamed.

Refusing does not crash the daemon: `SessionRunner._run` already catches the
exception, logs it, and moves the session to `failed` carrying the message,
so the user is told which codepoint and what to do about it. The message
names the codepoints, not the path, though §4 above holds either way.

### 3. `SteeringReloaded` reports steering separately from the prefix (Class B)

**Decision:** `AssembledPrompt` gains `steering_tokens` — the steering block
alone, counted directly rather than derived by subtracting — and the
`steering_reloaded` event gains a `steering_tokens` field beside
`prefix_tokens`. The UI type, the timeline detail row, and the protocol
fixture follow. `prefix_tokens` keeps its meaning and its value.

**Rationale:** `prefix_tokens` is the size of the whole cache prefix, which
is the right number for "what will the provider re-bill" and the wrong
number for "what do the user's steering files cost" — it also carries the
base prompt and, since TD-1810, block [1b]. Under a heading that says
*Steering reloaded*, the second reading is the one a person takes, and this
story made the overstatement bigger. Renaming or shrinking `prefix_tokens`
was rejected: the cache figure is real and is what TD-305 exists to expose.
Two honest numbers beat one number doing two jobs. This is deliberately not
a fix for TD-1811 — that story is about reporting prefix *reuse* that did
not happen, and nothing here touches the reuse figure — but the field it
adds is the one TD-1811 will need in order to say what actually got billed.
Counted, not subtracted, because the prefix is joined with separators and a
figure that is nearly right is the failure this split exists to remove.

---

## 2026-08-17 — TD-1811: Do not report prefix reuse that did not happen

### 1. `Usage.cached_prompt_tokens` becomes `int | None`; absent is not zero (Class B)

**Decision:** `Usage.cached_prompt_tokens` and `CallRecord.cached_prompt_tokens`
are `int | None`. `None` means the response carried no cached-token figure;
an integer — including `0` — means the provider reported one. The parse is
`usage.prompt_tokens_details.cached_tokens` and nothing else, and a missing
or non-dict `prompt_tokens_details` yields `None` rather than `0`.

**Rationale:** Measured 2026-08-17 against the live endpoint at
`127.0.0.1:11434`: `qwen3.8:27b` and `gemma4:26b-a4b-it-q4_K_M` both return
exactly `prompt_tokens`, `completion_tokens`, `total_tokens`, on the
streaming path and the blocking one. No `prompt_tokens_details`, no
`cached_tokens`. The old parse folded that silence into `0`, and `0` was then
surfaced by `last_cached_prompt_tokens` and rendered by `StackPanel` as
**cache miss** — a claim about the provider's cache the provider never made.
The story is about not overstating reuse; asserting a miss is the same defect
pointed the other way, and it is the one the local path actually hits.

A companion boolean on `CallRecord` (`cached_prompt_tokens: int` plus
`cache_reported: bool`) was considered and rejected. It leaves
`record.cached_prompt_tokens == 0` readable by anyone who forgets the flag,
which is precisely the mistake being removed. `None` is unrepresentable as a
token count, so `mypy --strict` makes every reader decide what unknown means.
That is §2.4's "enforced in the tool, not by convention" applied to a number.

Six call sites now route through `cost.billable_cached_tokens`, which is
documented as a *pricing* fallback only: an unreported figure bills as zero
cached tokens, i.e. the whole prompt at the input rate. That errs toward
overstating spend, which is the safe direction — a provider that never
confirms reuse is not one whose invoice we may assume was discounted.

### 2. The audit column stores reported reuse, and stays `NOT NULL` (Class B)

**Decision:** `model_calls.cached_prompt_tokens` keeps its `INTEGER NOT NULL`
shape. `audit_writer` coerces an unreported figure to `0` on the way in. No
migration, no nullable column, no second column.

**Rationale:** The column answers "how many tokens did providers report as
reused", and a silent provider contributes none — so every `SUM` over it
stays a sum of real claims and none of the `audit_queries` rollups change
meaning. A nullable column would buy the ability to distinguish silence
retrospectively, at the cost of a schema migration and `NULL`-handling in
every aggregate, to answer a question no surface asks of the ledger. The live
distinction is carried where it is actually consumed — the tracker and the
protocol event. Prompt tokens are stored in full either way, so TD-1802's
"free is not untracked" holds on a silent provider too.

### 3. `InstructionStack` gains `cache_observed` rather than an enum (Class B)

**Decision:** The event carries `last_cached_tokens: int | None` (unchanged
shape, meaning tightened to "the figure the provider reported") plus an
additive `cache_observed: bool` defaulting to `False`. No `PROTOCOL_VERSION`
bump — same additive precedent as TD-1801's `key_required`. The UI derives
four badge states from the pair: *unobserved*, *unreported*, *miss*, *hit*.

**Rationale:** TD-1810 §3 split `steering_tokens` out of `prefix_tokens` so a
figure stopped meaning two things; this is the same move. `last_cached_tokens
= null` meant both "no turn yet" and "the provider said nothing", and those
are different sentences to show a user — the second tells them their engine
will never report reuse, which is the actionable half of this story. A
three-valued enum field was the alternative; two orthogonal fields, each
meaning exactly one thing, is smaller and keeps the count and the
observed-ness independent. §6.3's "the UI never derives truth it wasn't
given" rules out having the viewer guess between them.

### 4. The turn log gains `cache_reported` beside `cache_ratio` (Class A)

`cache_ratio` stays `0.0` in both the reported-miss and the reported-nothing
case, because a ratio is a claim about a saving and there is no saving to
claim in either. Log-side, `cache_reported` says which `0.0` this is.
Grepping a day of logs for cache behaviour is how this story started; the
field is what makes that grep answer the question.

---

## 2026-08-17 — TD-607: `dispatch_many` order and failure isolation

### 1. Input list position is the ordering key, not `tool_call_id` (Class B)

**Decision:** Each call carries its index in `tool_calls` through the
partition; the parallel and sequential halves write into one `dict` keyed on
that index, and the method returns `[by_index[i] for i in range(len(...))]`.

**Rationale:** The obvious alternative — reassemble by matching each result's
`tool_call_id` back to the request list — reads more naturally and is wrong.
`tool_call_id` comes from the provider's tool-call payload, not from us. The
schema does not promise uniqueness within a batch, a mock or a local model
can repeat one, and `""` is a representable value. An ordering key that a
remote party controls is a key that a remote party can collide. Position in
the caller's own list is ours, is total, and is unique by construction, so
the reassembly cannot silently drop or double a result. A test in
`TestDispatchManyOrdering` dispatches three calls all sharing the id `dup`
to keep that property honest.

Sorting the concatenated results by a recovered index was rejected for the
same family of reasons: it reintroduces a derived key and a comparison where
direct placement needs neither.

### 2. A chokepoint bypass stays loud; ordinary failures become results (Class B)

**Decision:** Reading `gather`'s returned list makes the `concurrent_error`
branch reachable, but two things are re-raised out of it rather than
converted: `UnclassifiedToolCall`, and any `BaseException` that is not an
`Exception`.

**Rationale:** §6 says errors "propagate as typed results or raise", and the
two categories differ in who can act on them. A tool that failed is news for
the model — it can retry, or route around, and one failing call in a batch
has no business cancelling its siblings. `UnclassifiedToolCall` is not news
for the model: it means the dispatcher was built without a classifier, a path
guard, or an approval handler, and §2.6 makes that a bypass rather than an
error. Folding it into a `concurrent_error` string would turn a misconfigured
daemon into a stream of ordinary-looking tool failures the model would
cheerfully retry against — loud in the transcript, silent in the way that
matters. It already propagates from the sequential half of the same method,
so re-raising keeps one rule for both halves. Non-`Exception` bases are
re-raised for the usual reason: `CancelledError` is the session cancel path,
and a batch that answers cancellation with an error result is a batch that
does not cancel.

**Not changed:** the sequential half still lets other exceptions propagate.
An exception there already aborts nothing concurrent, `dispatch` catches
handler failures itself, and widening the guard is scope this story did not
buy. If a sequential call ever needs the same isolation, it is the same four
lines.
## 2026-08-17 — TD-510: Frontmatter is stripped everywhere, honoured only in `.tst/rules/`

### 1. Every steering level parses frontmatter; none of it reaches the model (Class B)

**Decision:** `assemble_sync` calls `parse_frontmatter` for every source
regardless of precedence, not only `Precedence.RULES`. The body it returns is
what gets imported, counted, rendered and sent. A steering file that opens
with a `---` block therefore never ships its delimiters or its YAML to the
model, at any level.

**Rationale:** The old gate parsed only rule files, so an `AGENTS.md` or
`CLAUDE.md` opening with frontmatter sent the raw YAML as though it were an
instruction. That is worst for exactly the users the migration section tells
that nothing needs porting: files written for Claude Code, Cursor and the rest
routinely open with a block, and every one of them was arriving as prose. The
parser already degrades to `({}, content)` on absent, empty, malformed and
non-dict frontmatter, so widening the call site adds no new failure mode — a
file with no frontmatter is byte-identical through it.

Stripping now happens *before* import resolution rather than after, so an
`@path` on the first line under a frontmatter block is seen as a directive.
Previously the block sat above it and the directive still resolved, so this is
order-preserving rather than behaviour-changing, but it is the order that
makes sense: the block is not content.

### 2. `appliesTo` outside `.tst/rules/` is stripped, never honoured (Class B)

**Decision:** Only `.tst/rules/` gives `appliesTo` meaning. At every other
level the key is removed with the rest of the block and has no effect on
whether the file loads. `ResolvedSource.applies_to` stays `None` and `active`
stays `True` for non-rule sources.

**Rationale:** This is the half of TD-510 that was a real choice, and the
conservative reading wins on three counts.

A steering file's *location* is already its scope. Root `AGENTS.md` is the
workspace-wide agreement; `src/api/AGENTS.md` is the agreement for that
subtree, scoped by where it sits. Honouring frontmatter would give one file
two scoping mechanisms that can disagree, with the narrower silently winning
and nothing in the file naming the conflict.

Honouring it would also make the working agreement conditional. A
workspace-root `AGENTS.md` carrying `appliesTo: ["src/**"]` would be absent
from every turn that had not yet touched `src/` — including turn one, where
`matched_paths` is empty and nothing has been touched at all. `.tst/rules/` is
built for absence: a rule that does not fire is the expected case, the
inspector greys it, and the author went looking for scoping when they put it
there. `AGENTS.md` is not built for absence, and the failure is silent and
turn-dependent in the direction that loses instructions rather than the
direction that costs tokens.

The frontmatter in arriving files is also not ours. Those blocks carry
`description`, `globs`, `alwaysApply`, `name` — foreign keys with foreign
semantics. Honouring `appliesTo` alone would interpret one key as a scope
while discarding the rest, which is a half-migration: the user gets partial
semantics they cannot predict from either tool's documentation.

Finally the asymmetry. Stripping-only is forward-compatible — if path-scoped
`AGENTS.md` is later wanted, files already stripped keep working and gain
scoping, and nothing that worked stops working. The reverse is not true:
un-honouring later silently widens every scoped file in every workspace that
adopted it. Where one direction is reversible and the other is not, and the
spec is silent, take the reversible one.

Argued against, honestly: a path-scoped `AGENTS.md` is a coherent idea, and a
user arriving from Cursor — where `globs:` in frontmatter is the normal way to
scope — will expect it to work. The answer is that `.tst/rules/` is that
feature, is documented in the migration section as the destination, and
carries the inspector affordance for "why did this not fire" that a scoped
`AGENTS.md` would need built from scratch.

### 3. The dropped scope is flagged, not silently swallowed (Class A)

**Decision:** A non-rule source whose frontmatter carried an `appliesTo` gains
an inspector warning beside the existing over-limit one. Any other frontmatter
key is stripped without comment.

**Rationale:** The story title is "neither honoured nor stripped"; stripping
in silence would have answered only half of it and left the other half a
quieter version of the same defect — the author writes a scope, sees the YAML
disappear, and has no way to learn it did nothing. Warning only on `appliesTo`
keeps the signal about a *dropped intent* rather than about frontmatter in
general, so the common migration case of a `description:` block passes without
nagging about a key we were never going to act on.

`ResolvedSource.warnings` was already a tuple surfaced through
`InstructionStack`, so this is additive with no protocol change. The guide
quotes the warning verbatim and the doc suite provokes it out of the real
assembler, on the same footing as the 200-line warning.
## 2026-08-17 — TD-1009: The activity timeline is scoped to one session

The store fed by `AppShell`'s `onEvent(push)` had no session at all. One
connection carries every session the window follows, so attaching to a second
session showed the first session's activity underneath it, and the Files pane
(TD-1705), which folds the same entries, inherited the symptom. `clear()`
existed and nothing but the TD-1404 benchmark ever called it.

### 1. Clear and re-hydrate from the replay, rather than tagging entries (Class B)

**Decision:** `Timeline` gains a bound session. `bind(sessionId)` drops what
the previous session left and adopts the new one; `push` folds an event only
when it names the bound session. The chat store declares the binding through
a new optional `ChatDeps.onBind`, called from `switchSession` immediately
before the attach that fetches the replay, and from `dispose` with `null`.

**Rationale:** The two candidates were per-entry session tags with a filtered
read, or a single scoped list rebuilt on bind. The second keeps `entries` the
one list the components already read, so `ActivityTimeline` and `FilesPanel`
scope without a line of markup changing — the Files criterion falls out of
the timeline fix rather than needing its own. It also keeps the daemon the
source of truth: the window holds one session's view and re-derives it from
the log instead of caching every session it has visited and then owing
someone the job of keeping those copies in step (§6, "the UI never derives
truth it wasn't given").

`switchSession` is the only place the pane's session changes — the rail's
`selectRow`, the `session_list` auto-bind, and teardown all route through it —
so it is the only place the binding is declared. `onBind` is optional because
a chat pane with no activity lane beside it is still a chat pane; every
existing `ChatDeps` construction keeps working unchanged.

### 2. Re-hydration rests on the detach that precedes every bind (Class B)

**Decision:** No new attach primitive. `switchSession` already detaches the
outgoing session before attaching the incoming one, and `ProtocolClient.detach`
drops that session's `lastSeq` — so the next `attach` asks `from_seq = 1` and
the daemon replays the whole log. That is what refills the cleared list.

**Rationale:** A `rebind`-from-the-top method on the client was written and
rejected: every reachable bind already produces a full replay, so it would
have bought a guarantee the existing pairing gives for free while adding a
public method, a new export in `connection-status`, and a second meaning for
"attach". The invariant is recorded here rather than defended in code because
that is what it is — an invariant, not a mechanism. If a future story ever
binds a pane to a session the connection is already following mid-log, the
replay will be partial and the pane will re-hydrate short; that is the line
to come back to.

### 3. Idempotence is the store's own, keyed on the log position (Class B)

**Decision:** `push` drops any event whose `seq` is at or below the highest
the store has already folded, and `clear` resets that position along with the
entries. Events with no numeric `seq` are exempt.

**Rationale:** The double-count criterion is the one most easily got wrong,
and the client's duplicate detection is not enough to rely on: `detach`
deliberately forgets a session's `lastSeq`, so the very replay that re-hydrates
a bind arrives with every frame looking new. Anchoring on the log position
makes the store correct on its own terms — a re-attach at `lastSeq + 1`
(TD-1716's resume, a reconnect's `hello_ack`) and a re-attach at 1 both land
exactly once, whatever the client above did. The exemption is not a loophole:
a daemon error is written straight to the socket outside the event log
(`core/tstd/protocol.py build_error`), so it carries no seq and nothing can
replay it.

### 4. An event that names no session is not this session's activity (Class A)

An `error` may arrive with no `session_id`, and those no longer render in the
timeline. They are daemon-level failures — a bad frame, a refused handshake —
and TD-1008's notification lane already gives them tailored copy as a toast or
a banner. Attributing one to whichever session happened to be bound would be
inventing a fact the daemon did not send.

### 5. Re-binding the session already shown is a no-op (Class A)

`bind` compares before it clears. A bind that emptied the pane without a full
replay behind it would leave it blank for good, which is a worse defect than
the one being fixed; the guard means no caller can cause it by calling twice.

---

## 2026-08-17 — TD-1813 (finish): the session-binding cut

The first-token wait left `chat-store.ts` at 422 and TD-1009's `onBind` put it
back to 431, so the ~400-line criterion needed a second cut. The commit that
made the first one named this one: `switchSession` plus the `session_list`
case.

### 1. Cut along the pure/effectful line, not around `switchSession` (Class B)

**Decision:** `session-binding.ts` holds two things. `chooseBoundSession
(summaries, currentId)` is pure and returns `{action:"keep"|"unbind"|"bind"}`;
`applyBind(state, ctx, sessionId, turnState)` is the teardown-and-attach that
was `switchSession`'s body. The store's `session_list` case reads the choice
and calls the effect; `switchSession` is a one-line forward.

**Rationale:** `switchSession` on its own is a weak seam. It writes four state
fields and calls into the queue, the wait and all three deps — extracting it
alone hands the new module nearly the whole store and buys a line count, not a
boundary. The rules are the part that keeps attracting defects (TD-1711's
adopted tombstone, TD-1715's adopted archive, TD-1714's session-liveness read
as turn evidence), and every one of them is decidable from the list and the
bound id. Making that half pure puts the policy table under direct test with no
transport in the way, which is where the 12 new cases live.

### 2. The context is collaborators, not a state slice (Class B)

**Decision:** `applyBind` takes the `ChatState` it mutates plus a `BindContext`
of `{ queue, wait, deps }`, each narrowed with `Pick<>` to the calls a bind is
allowed to make — `queue.clear`, `wait.end`, `deps.attach/detach/onBind`.

**Rationale:** `first-token-wait.ts` takes a structural slice
(`FirstTokenWaitState`) and `chat-queue.ts` takes `{ queued }`, because each
owns a few fields and nothing else. Binding owns no fields: it resets most of
the pane and its real dependencies are the store's other collaborators. So the
shape differs, on the same principle — `ChatState` goes on declaring all its
own fields, and no module inherits or re-declares them. The `Pick<>`s keep the
house habit of narrow surfaces: a bind cannot grow a `deps.send` or a
`queue.flushHead` without the signature saying so.

The type import of `ChatState` and `ChatDeps` runs back to `chat-store.ts`. It
is `import type`, erased at build, so there is no runtime cycle — and it keeps
`ChatState` declared in exactly one place, which the alternative (a fourth
structural slice, this one spanning `messages`) would not.

### 3. `keep` carries the turn state it would stamp (Class A)

The keep branch is not a no-op: a refresh may bring a newer turn state for the
session already bound. Carrying it on the result keeps the TD-1714 rule — a
summary's `running` is session-liveness and must never overwrite local turn
evidence — inside the pure function with the rest of the policy, expressed as
`turnState: null`. The store's remaining share is one guarded assignment.

### 4. `isTerminal` moves with the policy that needs it (Class A)

It was module-private in `chat-store.ts` and is now exported from
`session-binding.ts`, which is where the liveness filter reads it; the store
imports it back for the one other use, sealing an in-flight assistant message
on a terminal `session_state`. One definition, and it sits with the rule that
gives it its meaning.

---

## 2026-08-17 — TD-1406: What Windows parity actually costs

Three of the four open criteria were about the same thing wearing different
clothes: a fail-closed rule written on a POSIX host, refusing a form that is
ordinary on Windows and unresolvable anywhere else. Criterion 1 turned out to
be already implemented — `dfe1ab7` carved drive-absolute paths out of
`windows_unsafe_reason` 45 minutes *before* the skip complaining about it was
written — but its companion security test was never made platform-honest, so
the Windows leg would have gone red on the fix. See §4.

### 1. On Windows the 8.3 rule reads the canonical path (Class B)

**Decision:** `windows_unsafe_reason` gains an optional `canonical` argument.
On win32 the 8.3 short-name check runs against the canonical path instead of
the raw string; off win32 it runs against the raw string exactly as before.
`PathGuard.check_read`/`check_write` pass the canonical form they already
hold; the classifier passes nothing and the function resolves for itself when
it reaches that rule.

**Rationale:** the check ran against the raw string on every platform, and
`%TEMP%` on a GitHub Windows runner is `C:\Users\RUNNER~1\AppData\Local\Temp`.
Every `tmp_path` on that runner therefore carried a short-named ancestor, and
an absolute in-workspace path was refused `windows_unsafe` for a segment that
belonged to the machine rather than to anything the model wrote. That is
collateral, not enforcement.

Windows canonicalization is what dissolves it. `os.path.realpath` reaches
`GetFinalPathNameByHandle`, which returns the long form, so by the time the
guard checks containment the alias is already gone — there is nothing left to
point somewhere else. A segment that *survives* resolution named nothing the
filesystem could expand, and stays refused. Off Windows no filesystem knows
the short→long mapping, so the canonical form is not evidence of anything and
the fail-closed refusal is unchanged. Both branches are proven in
`TestShortNameCanonicalization` by patching `sys.platform`, so the win32 half
is not a claim only the Windows leg can check.

The rejected alternative was expanding short names ourselves off Windows.
There is nothing to expand with: the mapping lives in the filesystem, and any
table we invented would be a guess with a security boundary resting on it.

Two limits worth writing down. The expansion needs the path, or an ancestor,
to exist and be openable — a short name naming nothing is not expanded and
stays refused, which is fail-closed and fine. And a directory whose real long
name happens to look like an alias (`my~1`) resolves to itself and is refused
on Windows too; narrow, and refusing is the safe side of it.

The classifier deliberately passes no canonical form. The rule runs on every
classification and the cheap string checks short-circuit ahead of the 8.3 one,
so a UNC vector is judged without first asking the OS to resolve
`\\server\share` — which is a network round trip, or a stall, for a path we
were always going to refuse.

### 2. Windows file permissions are a documented no-op, asserted (Class B)

**Decision:** the session store and the port file keep their `chmod(0o600)`
and it stays a no-op on Windows. No `icacls`, no `pywin32`. The two
`restricted_mode` tests stop skipping on win32 and assert the no-op instead:
POSIX asserts `0o600`, Windows asserts the file exists, reads back, and
carries the Windows default `0o666` — the observable proof the chmod did
nothing.

**Rationale:** the criterion allowed "real Windows ACLs or a documented
no-op", and the honest answer is that `0o600` was never what secured these
files on Windows. Both live under `%LOCALAPPDATA%`, whose default ACL grants
Full to the user, SYSTEM and Administrators and to nobody else. An `icacls`
call on every port-file write would restate a protection the directory
already provides, at the cost of a subprocess on the startup path — and an
Administrator, who is the only extra principal in scope, can take ownership
of the file regardless.

What matters is that "documented" is load-bearing. A skip is not
documentation; it is the absence of a test wearing an explanation, and this
story exists partly because a stale skip reason hid a live defect for three
days. An assertion fails if a future Python makes `os.chmod` meaningful on
Windows, which is exactly when we would want to revisit the decision.

### 3. Windows gets a process-tree kill, and it is not ticked (Class B)

**Decision:** `CREATE_NEW_PROCESS_GROUP` at the spawn and `taskkill /T /F
/PID` in `_kill_process_group`, after the existing `proc.kill()` so the direct
child dies even if the helper cannot. Criterion 4 stays **unticked** and the
`requires_posix_process_group` skip stays in place.

**Rationale:** `Process.kill` is `TerminateProcess`, which kills one process
and leaves its children running — the docstring admitted grandchildren escape.
`taskkill /T` walks the parent chain the OS already records, which is the
closest Windows has to `killpg`; the creation flag is what gives that walk a
defined edge, since without it the child joins the daemon's own group.
`taskkill` ships with Windows, so this is Class B, not a new dependency.

`taskkill` blocks the event loop, which §6 otherwise forbids — but only on
the two cancellation paths, and that distinction is the decision. There are
three call sites, and they are not alike. The two inside
`except asyncio.CancelledError` keep the blocking call: the kill must have
landed before the handler re-raises, and awaiting there can be cancelled
again, so an offloaded kill might never land at all. The third is the
timeout and cooperative-cancel path — normal async flow, no exception in
flight — where nothing stops `asyncio.to_thread` from working. It is also
the *common* path: an ordinary long-running command hitting its timeout. So
it offloads (`_kill_process_group_offloaded`), and a Windows timeout no
longer stalls the WebSocket for up to 2s.

The 2s cap is deliberately shorter than the 5s used elsewhere because on the
two remaining sites it is a bound on a loop stall, not a grace period;
`taskkill` normally returns in tens of milliseconds. Nothing blocks on POSIX,
where the kill is a syscall and a thread hop would cost more than it saves.

It is not ticked because nothing has run it. A process-tree kill is a claim
about an operating system's behaviour, and the only evidence that counts is a
Windows host performing one. This backlog already records seven "green suite,
dead feature" entries, and ticking a kill path from a macOS run where the code
is `sys.platform`-gated out would be the eighth. The skip reason now says the
implementation landed and names both things a Windows run must settle — the
kill itself, and a cmd/PowerShell equivalent of the POSIX escape probe.

### 4. Criterion 1 was implemented but not finished (Class A)

`test_security_suite.py::test_windows_unsafe_refused_on_every_platform`
parametrized `C:\Windows\system.ini` and asserted `windows_unsafe`
unconditionally — a test whose name became false the moment the carve-out
landed, and which would have failed on the first green Windows leg. The
drive-absolute vector now has its own test asserting `outside_workspace` on
win32 and `windows_unsafe` elsewhere: refused either way, for reasons that
differ per platform, which is the decision stated as a test.

Five other test files carried comments explaining that they feed
workspace-relative paths *because* drive-letter absolutes are refused as
`windows_unsafe`. That reason went half-false on Windows, but the practice it
justifies is still right — relative inputs exercise identical semantics on
every platform — so the comments were corrected in place and the tests left
alone. A test-writing convention that outlives its stated reason is the same
failure as a stale skip, one degree quieter.

---

## 2026-08-18 — TD-1304: port-file pid is the listener, host accepts a descendant

**Decision:** `port.json.pid` remains `os.getpid()` of the process that bound the
socket. The host accepts that pid when it is the child it spawned **or a live
descendant of it**. The sidecar is spawned in its own process group; timeout,
handshake failure, crash, and quit kill the group (Unix `kill -KILL -pgid`,
Windows `taskkill /T`).

**Rationale:** PyInstaller's `--onefile` bootloader is the spawned child; the
real daemon is its grandchild and is what writes the port file. Exact equality
— the TD-1002 rule — never matches, so the packaged app never attaches and
each retry leaves a leftover listener. Switching off `--onefile` would rewrite
TD-1301. Writing the bootloader pid into the port file would lie about who is
listening. Descendant matching is what debug already does (direct child) and
what onefile actually is.

`request_shutdown` no longer clears the stored leader pid: `RunEvent::Exit`
can fire in the same tick and still needs it for the group kill.

**Alternative rejected:** infer the pid from whoever is listening on the port.
That would accept a stale daemon from a previous crash that still held the
port — the case the pid key exists to refuse.

**Windows:** parent walk uses `wmic`; tree kill uses `taskkill /T`. Job-object
process-group kill stays TD-1406.

---

## 2026-08-18 — E19: reasoning visibility pulled into M3 (Class C)

**Decision:** Epic E19 (TD-1901 reasoning passthrough, TD-1902 collapsible thinking) is
scheduled into M3 rather than deferred past v0.1. Filed as a **bugfix**, not a familiarity
story.

**Rationale:** M3's exit condition is "a stranger can install and use it from a fresh
machine." `local` is a shipped preset, and its brain tier is a reasoning model. Measured
against `qwen3.8:27b` on Ollama, the endpoint streams reasoning as `delta.reasoning` with
`delta.content` set to `""`; `provider.py` reads only `content` and `loop.py` gates emission
on its truthiness, so an entire reasoning phase emits nothing. A packaged-app pass on
2026-08-18 reproduced it as Whittling with no thinking UI. A stranger who picks the preset
we ship gets an application that appears hung, which fails the exit condition on its own
terms.

The user asked to file and fix the live-pass findings that were not on this main's backlog.
Silent reasoning was one of them. That is the sign-off.

**Alternative rejected:** dropping `local` from the shipped presets until E19 lands. That
trades a visible defect for a removed capability, and M1.5 exists precisely to make the free
path first-class.

**Also filed from the same pass, as M2 bugfixes in their home epics:** TD-1011 (divider
latch), TD-1012 (virtualizer freeze), TD-1204 (empty stack panel). Hex rail titles stay
TD-1701 / v0.3; title-bar "Running" stays TD-1006 session liveness.

---

## 2026-08-18 — TD-1901: `assistant_reasoning` is its own event (Class B)

**Decision:** a reasoning model's thinking is a distinct `assistant_reasoning` event, not a
flag on `assistant_delta`.

**Rationale:** the transcript has to tell thinking from answer after the stream ends — to
fold one and not the other (TD-1902), and to keep reasoning out of `collected_content` so it
is never replayed to the provider as assistant speech. A flag on a shared type makes that a
runtime check every consumer has to remember.

Both spellings in the wild parse to `Delta.reasoning`: Ollama's `reasoning`, DeepSeek /
OpenRouter's `reasoning_content`. Empty strings normalise to `None`.

---

## 2026-08-18 — `mcp/tst-cu-mcp`: Windows port of the computer-use MCP server

Six decisions taken while porting `tst-cu-mcp` 0.1.0 (macOS-only) to Windows and moving it
into this repository. Recorded together because they only make sense as a set.

### 1. An out-of-milestone MCP area lives in-repo (Class C, user-authorised)

**Decision:** `tst-cu-mcp` lives at `mcp/tst-cu-mcp/` in this repository, detached from the
app.

**Context:** This is a scope decision that `AGENTS.md` §3 and §5 reserve for the user, and
`tst-desk-kickoff-prompt.md` explicitly places MCP in v0.8 and out of scope. It was raised as
such and the user directed the work to proceed here rather than in a separate repository.

**Why detachment is real rather than asserted:** `ci.yml` scopes all three of its jobs with
`defaults.run.working-directory` (`core`, `shell`, `ui`), so a new top-level directory is
invisible to the app's lint, typecheck, test and build legs. Nothing under `mcp/` is imported
by `core/`, `shell/` or `ui/`, and the server ships nothing into them. The pre-commit hook does
lint this area's Python, using this area's own `pyproject.toml` config — deliberately kept, so
standards stay consistent.

**Consequence to resolve:** `AGENTS.md` §11 documents the directory tree and does not list
`mcp/`. Prime directive §2.4 makes `AGENTS.md` read-only to the agent, so **the user must add
that line**. No test pins the tree, so nothing is currently red.

### 2. Missing dependency markers, not missing code, were the blocker (Class B)

**Decision:** The three `pyobjc-framework-*` pins carry `; sys_platform == 'darwin'`.

**Rationale:** They had no environment markers at all, so `uv sync` failed while *resolving* on
Windows — before any platform-specific code was reached. This was the whole reason the server
could not be installed on Windows, and it is one line per dependency. `tests/test_version.py`
asserts the markers are still present, and that no Windows-only runtime dependency has crept
in: the Windows backend is `ctypes` (stdlib) plus Pillow, which macOS already required.

### 3. Windows reports physical pixels; macOS keeps logical points (Class B)

**Decision:** Each backend reports geometry in its own native space, and nothing above the
backend layer knows which.

**Rationale:** macOS has one global logical-point space with a per-display backing scale.
Windows has per-monitor DPI, so there is no single logical space spanning a 150%-scaled laptop
panel and a 100% external monitor. Reporting real pixels keeps one coherent space shared by
display bounds, capture rectangles and input events. This costs nothing above the boundary
because `coordinates.py` maps image pixels to global coordinates *proportionally* over the
captured region — it never needs a scale factor, which is why that module was not touched by
this port.

The Windows backend opts into `PER_MONITOR_AWARE_V2` before reading any geometry. Without it
the OS returns scaled rectangles and every capture and click lands short on a scaled display.

### 4. `cmd` is an alias for Ctrl on Windows; `fn` is refused (Class B)

**Decision:** `cmd`/`command` map to `VK_CONTROL`. `win`/`super` map to the Windows key.
`fn` raises a `ValueError` naming it as macOS-only.

**Rationale:** Models carry a mac-shaped shortcut vocabulary and will ask for `cmd+c`.
Refusing it would fail every copy, paste and save; aliasing it does the thing the model
intended. The alias deliberately does not swallow the Windows key — `win+d` stays distinct
from `cmd+d`, so "show desktop" cannot silently become Ctrl+D. `fn` refuses loudly rather than
being dropped, because a dropped modifier sends a bare arrow key and looks like it worked.
`delete` resolves to Backspace, matching what the key is called on a Mac, with
`forward_delete` for the PC Delete key.

### 5. Windows has no permission gate, so `check_permissions` reports the silent failures

**Decision:** The Windows report says `all_granted: true` **and** names two limits: UIPI and
the secure desktop. `limits_apply` distinguishes which are live, based on whether this process
is elevated.

**Rationale:** An unqualified "granted" would be technically true and practically a lie. Both
Windows failure modes are silent: input aimed at a higher-integrity window is discarded by the
OS, and the secure desktop (UAC prompt, lock screen) can be neither captured nor driven. A
model that believes it clicked something it did not is worse off than one told it cannot.
`_send` also raises when `SendInput` reports fewer events delivered than submitted, so the
UIPI case surfaces as an error rather than a successful no-op.

### 6. Test markers: `desktop` and `intrusive` are separate, and one is unverified

**Decision:** Three tiers. The default run is headless and passes anywhere. `-m desktop`
reads live OS state and restores what it touches. `-m intrusive` synthesizes keystrokes into
whatever holds focus and is excluded from both.

**Rationale:** Backend modules import on every platform and construct without OS calls, so one
host proves both selection branches — the same technique `docs/windows.md` §2 uses, and what
keeps the macOS path from rotting now that development happens on Windows.

**Not verified:** the `intrusive` tier has not been run. Keyboard and scroll actuation are
implemented and unit-tested at the record-building level, but no test has confirmed the OS
accepted a synthesized keystroke. Following the standard TD-1406 set for the shell tree-kill,
that is recorded as unverified rather than assumed. Verifying it needs a deliberate run with
nothing important focused.

### Local note: uv's venv trampoline is blocked on this Windows host

Not a decision, but it will cost the next person an hour. `uv sync` creates
`.venv\Scripts\python.exe` as a small trampoline binary, and on this machine executing it fails
with "Access is denied" — for uv itself as well as for a shell. A copied real `python.exe` in
the same directory runs fine (it fails only for a missing DLL), and Smart App Control is off, so
the block is specific to that binary rather than to the location. The working setup is a stdlib
venv, whose `python.exe` is a copy of the interpreter:

```
& <managed-python> -m venv .venv
uv pip install --python .\.venv\Scripts\python.exe -e . --group dev
```

Also on this host: the registered Python 3.12 at
`%LOCALAPPDATA%\Programs\Python\Python312\python.exe` is a 0-byte file, which makes `uv` fail
during interpreter discovery with os error 193 until that directory is off `PATH`. `uv python
install 3.12` provides a working interpreter under `%APPDATA%\uv\python`.

---

## 2026-08-18 — `mcp/tst-cu-mcp`: focus guard, state read-back, bounded waits

Four additions, all driven by failures observed in a live driving session rather
than by design review. Recorded together because they answer the same question:
*how does a model driving a UI by coordinates know it is aiming at the right
thing?*

### 1. Per-action approval is the wrong gate; the stop-file is the right one

**Decision:** Recommend auto-approving every tool in the MCP client, and treat
the kill-switch stop-file as the session-level gate.

**Rationale:** Reported from live use, and it is a correctness problem rather than
an ergonomics one. Approving a `screenshot` alters the screen before the capture
is taken. Approving a `click` or `type_text` gives the client window focus
immediately before an input event aimed at a different window. The approval
dialog perturbs exactly the state the server exists to observe and drive — the
gate corrupts the thing it gates.

A file-based gate has none of that: creating or deleting `~/.tst-cu-mcp/STOP`
steals no focus and moves no pointer, and it is checked before every actuation on
every platform. Consent moves from per-keystroke to per-session, which is also
the honest granularity — nobody meaningfully evaluates the thirtieth click.

The residual risk is real and is documented rather than mitigated: screen content
is untrusted input, and with actuation auto-approved there is no human checkpoint
between reading it and acting on it. Staying unelevated is load-bearing here,
because UIPI then protects every administrator window for free.

### 2. `expect_window` guards every input tool (Class B)

**Decision:** Every input tool takes an optional `expect_window`. When given, the
foreground window is read and the action refused — before anything is sent —
unless its title or process name contains that string, case-insensitively.

**Rationale:** Two silent misfires in one session. Keystrokes intended for a
just-opened Start menu went nowhere; a click aimed at a Start menu tile landed in
the editor behind it, because the menu had closed between the screenshot and the
click. A coordinate aims at a point, not at a thing, and points change meaning.

Matching is substring-against-title-or-process rather than equality because real
titles carry volatile detail — a browser tab is
`"2026_Engineer Report - Google Docs - Google Chrome"` — and an exact match would
make the guard unusable. A blank expectation matches *nothing* rather than
everything: it almost certainly means an unset variable, and silently disabling a
guard is the opposite of its purpose.

The check lives in `input_control` beside the kill-switch, not in the backends, so
a future platform cannot ship without it. Order is kill-switch → argument
validation → focus check → actuate: "stop" must not depend on anything else being
well-formed, a malformed call should fail on its own merits rather than on
whatever is in front, and the focus check belongs as late as possible so it
reflects the state at the moment of acting.

**Known limit, documented not fixed:** a window can be in front and still not
ready for keystrokes. `expect_window` cannot detect that, and neither can
`SendInput`, which reports success because it genuinely delivered. Only a
screenshot confirms a caret.

### 3. Reads are not gated by the kill-switch (Class B)

**Decision:** `get_foreground_window`, `get_cursor_position`, `get_screen_info`
and `screenshot` work while actuation is halted.

**Rationale:** Halting the hands should not blind the eyes. A caller that has just
been refused an action is precisely the caller that most needs to see where things
stand. Consistent with the pre-existing choice that the stop-file leaves
screenshots working.

### 4. Waits are bounded, and `wait_for_window` waits on a condition (Class B)

**Decision:** `wait` sleeps up to 30 seconds; `wait_for_window` polls the
foreground window until it matches or times out, and *returns* on timeout rather
than raising.

**Rationale:** Without these, sequencing a desktop task required shelling out to
`sleep` — a computer-use server that cannot wait cannot complete a task by
itself. `wait_for_window` is the better tool because it answers the real question
("is the thing I launched ready?") instead of guessing a duration; it checks once
before sleeping, so an already-correct window is free. A timeout returns
`matched: false` plus what is actually in front, because that is information the
caller must act on — the app failed to start, or something stole focus — and an
exception would discard it.

`wait` blocks the server thread. Acceptable only because the stdio transport
serves one client processing one call at a time, so there is no concurrent request
to starve. Both are capped: an unbounded wait in that model is a wedged session
the caller cannot cancel.

### Retired: the `intrusive` marker is no longer entirely unverified

The 2026-08-18 entry above recorded keyboard and scroll actuation as implemented
but unproven. A live session has now exercised `type_text`, `press_keys`, `click`
and `scroll` against a real Windows desktop, including typing into a Google Doc
and opening the Start menu. Still unverified: multi-monitor layouts, UIPI (the
short-delivery raise in `_send` has never fired), the secure desktop, multi-click
`count>=2`, horizontal scroll, and the kill-switch in live use.

### Still open

Not built, and worth naming so they are choices rather than oversights: **drag**
is absent entirely (`click` is down-and-up at one point, so no drag-select, slider
or drag-and-drop), `screenshot` has no frame hash for cheap change-polling, there
is no launch-an-application tool (the shell covers it), and there is no
element-level targeting — an accessibility-tree or text-query layer is what would
make this robust rather than merely careful. Coordinate-driven automation is
brittle by construction, and every guard above manages that rather than removing
it.

---

## 2026-08-18 — Correction: the `intrusive` tests ran, and typed into a live window

Two statements in the entries above were false when written, and are corrected
here rather than edited away.

**What was claimed.** The first 2026-08-18 entry said "the `intrusive` tier has
not been run" and described the tests as "excluded from both the default run and
the `desktop` selection". The second entry then said a live session had exercised
keyboard actuation, without connecting that to the tests.

**What actually happened.** `TestKeyboardIntoARealWindow` lived in
`tests/test_desktop_windows.py`, which applies `pytest.mark.desktop` to every test
in the module via `pytestmark`. The class added `@pytest.mark.intrusive` on top,
so it carried *both* marks. A command-line `-m desktop` **replaces** the
`addopts` filter `-m 'not desktop and not intrusive'` rather than intersecting
with it, so `pytest -m desktop` selected it.

It ran three times. Each run typed `tst-cu-mcp`, then `café 😀`, pressed
`ctrl+f6`, and scrolled up and down into whichever window held focus — which was
the user's chat input. The user found it; the test suite reported 33, 38 and 49
passing desktop tests and gave no indication.

**Root cause.** A pytest marker is a *selector*, not a guard. Marking something
`intrusive` expresses an intention about how it should be selected and enforces
nothing. Any other selection that happens to match will run it, and this one
matched because the mark it was hiding behind was applied module-wide to a file it
did not belong in.

**Fix.** Two independent gates, on the principle that the dangerous thing should
be unreachable by accident rather than merely labelled:

1. `tests/test_intrusive_windows.py` — its own module, with no `desktop` mark
   anywhere in it, so no `desktop` selection can reach it. A test asserts the
   absence of that mark, so reintroducing it fails the suite.
2. `TST_CU_MCP_ALLOW_INTRUSIVE_TESTS` — a `skipif` checked at collection. Even
   `pytest -m intrusive` skips unless it is set, and a test reads the variable
   back so the gate is proven to be the thing gating.

`-m desktop` is now genuinely non-typing: it moves the pointer and restores it,
and nothing else.

**The wider lesson, recorded because it generalises past this bug.** The suite was
green throughout. Every gate reported success. The thing that caught it was a
human noticing text in a chat box. Tests that actuate a shared, stateful resource
— a desktop, a database, a live account — cannot be made safe by naming
conventions, because the harness will happily select them by any matching
criterion. They need a gate that fails closed and is independent of how they were
selected. This is the same standard TD-1406 applied to the shell tree-kill, and it
should have been applied here from the start.

---

## 2026-08-18 — Deviation: committed `mcp/tst-cu-mcp` directly to `main`

**AGENTS.md §8 says "Never commit to `main` directly."** This commit does. Recorded
here because §3 requires a chat instruction that conflicts with the contract to be
surfaced and resolved rather than silently followed — it was raised, and the user
chose `main` over a branch and PR.

Two related irregularities in the same commit, noted so they are not mistaken for
precedent:

- **No story ID.** §8's `td/<story-id>-<slug>` branch and `TD-###:` subject both
  assume a backlog story. This work has none, so the subject is prefixed `mcp:`
  instead. The backlog places computer use in v0.4 (`TD-1710`); this server is
  developer tooling under `mcp/` rather than the product feature under `core/`,
  which is why it was not treated as a v0.4 scope breach — but the distinction is
  a judgement, not something the backlog states, and it should be settled if more
  work lands here.
- **The `.gitignore` fix rides along.** Anchoring `darwin.py` to `/darwin.py` is
  unrelated to the MCP server, but the unanchored pattern silently excluded
  `mcp/tst-cu-mcp/src/tst_cu_mcp/backends/darwin.py` from staging, so the commit
  could not be correct without it.

---

## 2026-08-18 — TD-804: skip-all is a user setting, not a wall bypass (Class C)

**Decision:** Settings → Policy gains a machine-wide **Skip all approvals** toggle. It
promotes Class B `ask` to `auto`. It does not promote Class C, does not override a
`never` rule, and does not run before the boundary. The classifier still runs; the
ledger still logs.

**Rationale:** The user asked for a normal Universal Skip-all setting after a live
Approve failed with `no_pending_approval`. That is the sign-off. A Claude-style
`--dangerously-skip-permissions` that also silences Class C would violate TD-803 and
the spec's wall. Skip-all is the product's version of the feature: skip the *ask*,
not the *wall*.

**Persistence:** `approvals.yaml` in the user data dir, not `.tst/config.yaml`. A
workspace file would be committed and surprise the next clone. `effect: yolo` stays
invalid — skip-all is a separate bit so existing rules keep their meaning.

**Turning it on** approves every currently parked non-C call so a card that is up
does not stay up under a setting that says it should not.

---

## 2026-08-18 — TD-1702: OS notifications via the named plugin (Class B)

**Decision:** `tauri-plugin-notification` is the OS notification path, as the
story names. Permission is requested on the first unfocused qualifying event,
never at startup. Focus is the webview `focus`/`blur` pair.

**Rationale:** In-app toasts (TD-1008) already cover the focused window. The
OS notice is for the coworker-is-waiting case: an approval card or a finished
turn while the user is elsewhere. Clicking the notice focuses the window; the
approval card is already in the footer if one is pending.

---

## 2026-08-18 — TD-1708: fork is a session conversation, not a new session

**Decision:** Editing a past user turn truncates `session.conversation` and
enqueues the new text. Siblings are snapshots of that list. The viewer
learns about a fork through `conversation_reset`. Retry of a finished turn
is the same verb (`fork_from`) so branch chrome covers both.

**Rationale:** The event log is append-only and never stored user rows, so
a new session-per-branch would lose the original transcript on attach
anyway. Keeping both siblings on one session lets `set_branch` restore the
model's context without inventing a second session id. Mid-turn forks are
refused because `_open_turns > 0` already means the loop owes a turn.

---

## 2026-08-18 — TD-1815: chat-store keeps the wiring (Class B)

**Decision:** `applyChatEvent` takes a collaborator context (wait, queue,
forks, bind, id allocator). `createBranchSnaps` owns sibling snapshots.
`ChatState` stays declared on the store.

**Rationale:** TD-1708 pushed `chat-store.ts` to 590 lines. The last split
(TD-1813) already took the wait and the bind policy; what was left was
the reducer and the fork map. Moving either into the store's type file
would make the hub grow again. The store still decides *when* to send
`fork_from`; the fork module decides what a `conversation_reset` is
worth to the transcript.

---

## 2026-08-18 — TD-1814: `cache_reported` is turn-scoped (Class B)

**Decision:** The turn log's `cache_reported` reads `CostTracker.turn_cache_reported()`,
which looks at `_turn_calls`. It does not read `last_cached_prompt_tokens`.

**Rationale:** `turn_cache_ratio()` already sums this turn. `last_cached_prompt_tokens`
is the last *session* call and survives `begin_turn`, because the stack badge is
"currently cached." Pairing a turn-scoped ratio with a session-scoped reported bit
lets a silent turn inherit `true` from the one before it.

**`billable_cached_tokens` in the audit writer stays.** The column is an integer
ledger of billed reuse; silence stores as `0` so `SUM` is a sum of claims. That is
pricing/storage, not cache *state*. The docstring now says so. The turn log and
the stack badge do not call it.

---

## 2026-08-18 — TD-1412: rebuild `decisions` rather than edit v1 (Class B)

**Decision:** Nullability of `decisions.commit_sha` is a new migration
step that copies the table. `_SCHEMA_V1` is not rewritten again.

**Rationale:** The live `audit.db` was created when v1 said `TEXT NOT
NULL`. Editing the v1 string later made new databases honest and left
every existing one broken. SQLite cannot `ALTER COLUMN` to drop NOT
NULL, so the step rebuilds the table. Row data is copied, not updated —
the store's INSERT-only rule is about runtime writes, not schema
evolution.

---

## 2026-08-18 — TD-1014: approval cards bind like the other panes (Class B)

**Decision:** The approval footer is scoped to the bound session and
clears on switch, the same contract as the timeline (TD-1009) and the
decisions pane (TD-1203). Approve dismisses the card when the send
lands, not when `tool_result` arrives.

**Rationale:** `no_pending_approval` on a visible card is a viewer
defect. The daemon is right — that id is gone. A process-global list
plus a full-log replay after detach is how a resolved request becomes a
ghost. Optimistic dismiss is safe because a refused send keeps the card,
and a successful send is exactly the moment the daemon pops the future.

---

## 2026-08-19 — TD-608: relative tool paths join the workspace (Class B)

**Decision:** `PathGuard.canonicalize` joins a relative path to
`workspace_root` before resolving. Dispatch rewrites path arguments to
that form before classification. The workspace-root prompt now says
to pass either form.

**Rationale:** TD-1810 told the model never to pass a relative path
because the guard resolved against cwd. The packaged sidecar's cwd is
`/`, so `docs/foo.md` became `/docs/foo.md` and a file inside the
workspace was refused. Joining to the workspace is the rule the prompt
already stated; the guard now does it. `../` still canonicalises
outside and is still Class C.

---

## 2026-08-19 — TD-609: web_search destination is config (Class B)

**Decision:** `web_search` takes a query, not a URL. The only host it
reaches is `search.base_url` from `config.yaml`. The module is named
in `_OUTBOUND_CAPABLE`. No remote host appears in Python source. Calls
are Class B (ask).

**Rationale:** A second HTTP client is the failure mode TD-1410 exists
to catch. Putting the URL in the shipped YAML (and filling it in when
an older user copy omits `search`) keeps the destination in
configuration. Empty `base_url` disables the tool. The model cannot
point it at an arbitrary host because there is no host argument.

---

## 2026-08-19 — TD-610: fetch is Class B; SSRF is the wall (Class B)

**Decision:** `web_fetch` takes a URL the model chose. It is Class B
(ask) with no `host_fields`. Loopback, link-local, multicast,
unspecified, and metadata names are refused in the handler. Redirects
are followed only after the next hop passes the same check.

**Rationale:** A research loop has to open search hits. Those hosts are
not in config and cannot all live on `allowed_hosts` without turning
the wall into "the internet." The user seeing the URL on the card is
the gate. Fetching the machine (loopback, link-local, metadata) is
not a web read — that is the wall.

The base system prompt now tells the model to fire several searches,
fetch the best pages, and compile. That is the orchestration; it is
not a silent mega-tool.

---

## 2026-08-19 — v0.2 plan: three calls that would be expensive to reopen

Filed with M4 (E21–E27). Spec §5/§9 already named the product. These are
the sequencing choices.

**1. Memory writes are Class A.** Inside `.tst/memory/`, reversible with
`git revert`, not a wall. Steering writes stay Class C. The carve-out is
a classifier rule (TD-2102), not a handler special case.

**2. Distill runs on graceful quit and End session.** v0.1 still kills
the daemon on close; that close is graceful (TD-1002). Crash writes
nothing. v0.3 detached sessions are not a dependency.

**3. Heading-match is the M4 floor; embeddings are in-milestone but not
the exit.** TD-2701 must pass without a sidecar. Ollama `/api/embed` is
already rejected (2026-08-17 measurement).

---

## 2026-08-19 — Project home is a workspace, not a cloud Project (Class B)

**Decision:** Claude's project screen is the IA we copy. The unit is still
a folder. Instructions / Memory / Context are three columns over three
on-disk trees that do not mix (spec §4).

| Column | Files | Writer |
|---|---|---|
| Instructions | `AGENTS.md`, `.tst/rules/**` | Human only |
| Memory | `.tst/memory/**` | Distill + human correct |
| Context | pins in `.tst/context/pins.yaml` | Human pins; assembler reads |

**Rationale:** A second instruction box the agent could write would
collapse §4. Uploading files into a hosted project knowledge base would
need an account and a server. Pins are workspace paths, git-tracked,
behind the same wall as `fs_read`. Rail Projects stops being a recents
alias and becomes this home (TD-2801).

Pins ride the brain only, after memory, after the cache prefix, and drop
first when the capacity meter is full.

---

## 2026-08-19 — TD-2104: memory commits on HEAD; notices reuse the checkpoint rail (Class B)

**Decision:** A `MemoryCommitter` runs after a successful memory write and
commits only those paths on HEAD as `tst: memory update`. It is not
`Checkpointer`. Non-git workspaces still get the write; a one-time
`memory_no_git` notice rides the existing `checkpoint_notice` event.

**Rationale:** Spec §5 wants `git revert` from the user's own history.
The session branch is an undo stack the user never checks out. Mixing
the two would make revert fight checkpoint. A second event type for
"there is no commit" would duplicate the one-time notice rail.

---

## 2026-08-19 — TD-2103: memory line cap is config, distill is the replace path (Class B)

**Decision:** `memory.max_lines` lives in `.tst/config.yaml` (default 200).
`fs_write` / `fs_edit` of `.tst/memory/**` refuse a result over the cap,
and refuse any write to a file already at the cap. Distill calls
`replace_memory_file`, which may replace an at-cap file and still cannot
exceed the cap.

**Rationale:** Spec §5 is distilled, not appended forever. Putting the
number in the write handler would freeze 200 across workspaces. Putting
it under `caps` would pause the agent; this is a write refusal. Shrinking
an at-cap file through the tools would let the agent keep rewriting
instead of distilling, so at-cap replace is distill-only.

---

## 2026-08-19 — TD-2802: Instructions writes are a client verb, not a tool (Class B)

**Decision:** `list_instructions` and `create_rule` are client messages.
They write `.tst/rules/` on the human path. The model cannot send them.
`+` never calls `fs_write`. Editing is the system editor; TD-509 reloads
the stack on the next turn when the bytes change.

**Rationale:** Prime §2.4 is "the agent never writes steering." A second
write path that is a tool would be a bypass. The UI already opens stack
files through `tauri-plugin-opener`; create plants a commented template
the same way memory scaffold does.

---

## 2026-08-19 — TD-2102: memory writes are Class A even under a tight wall (Class B)

**Decision:** `.tst/memory/**` is a named Class A rule (`memory-file-write`).
The guard skips `writable_paths` for those paths. `AGENTS.md` / `CLAUDE.md`
as a basename stay Class C even if dropped under `.tst/memory/`.

**Rationale:** Spec §5 says the agent may write memory. A workspace that
narrowed `writable_paths` to `src/**` would otherwise refuse the tree
the product just scaffolded. Folding memory into `.tst/**` as steering
would refuse it the other way. The carve-out is the table plus the
guard, not a handler `if`.

---

## 2026-08-19 — TD-2801: Projects is a surface, not a recents alias (Class B)

**Decision:** Rail Projects swaps the main pane to a project list / home.
It no longer toggles the title-bar recents menu. Pin persistence stays
TD-2806; the list is the known workspaces from `session_list`. New chat
is `new_session` on a session in that folder, or `open_workspace` when
the folder has none.

**Rationale:** The title-bar menu is a quick switch. A project home
needs a named folder, recents for that path, and room for the three
columns. Reusing the menu would hide that IA. Chat stays mounted
under the project pane so attach/replay is not torn down.

---

## 2026-08-19 — TD-2101: memory scaffold on every session start (Class B)

**Decision:** Plant `.tst/memory/` templates from `_start_session`, not
only `open_workspace`. Config scaffold stays OpenWorkspace-only.

**Rationale:** Config is a wall the user edits before the first turn;
planting it twice would fight their YAML. Memory templates are empty
HTML comments and must appear in workspaces that were opened before
this story. `new_session` is the path those workspaces take. The write
is still per-file and never overwrites.

---

## 2026-08-20 — TD-2201: heading-match is file-level token overlap (Class B)

**Decision:** The loader selects whole files, not heading chunks.
`MEMORY.md` is always included when it exists. Every other
`.tst/memory/*.md` is included only when the alphanumeric tokens of its
ATX headings (fence-excluded, lowercased, length ≥ 3, minus a small
stopword list) intersect the tokens of the user task. Missing or empty
`.tst/memory/` returns nothing so the brain keeps `MEMORY_PLACEHOLDER`.
Symlinks that resolve outside the memory directory are skipped.
Selection is sorted: index first, then remaining names.

**Rationale:** Spec §5 is "MEMORY.md plus any topic file whose heading
matches the task," not a chunker. Chunking would teach later rankers
(TD-2203) the wrong grain. Short tokens and stopwords are dropped so
"the / a / to" cannot match every file. Embeddings stay off this path
on purpose — this is the floor when the sidecar is down.

**Alternative rejected:** Loading every `*.md` and splitting on H1/H2.
That is "everything, every time" with extra steps.

---

## 2026-08-20 — TD-2202: embeddings are search-shaped; down is heading-match (Class B)

**Decision:** `embeddings.base_url` / `model` / `timeout_seconds` live in
`config.yaml` next to `search`. The client POSTs `{base_url}/embeddings`
(OpenAI). Empty `base_url` or `model` disables. A connect/HTTP/parse
failure returns `None`; the turn uses TD-2201. Default shipped URL is
`http://127.0.0.1:8080/v1` with a 2s timeout. Ranking is TD-2203.

**Rationale:** The 2026-08-17 measurement forbids sharing Ollama's
scheduler. Putting the host in Python would fail TD-1410. Failing the
turn when the sidecar is down would make memory worse than heading-match
alone. A short timeout keeps a hung loopback from stalling the brain.

---

## 2026-08-20 — Honest session revive (Class B)

**Decision:** Persist each session's event log (`events.jsonl`) and the
loop's conversation (`conversation.json`) under the data dir. On daemon
start, a session with a conversation snapshot is revived — same id, same
messages, new loop. A session without a snapshot stays `interrupted`.
The UI may replay whatever events were saved; it will not accept a new
message on a tombstone.

This is the Claude-Desktop "open last chat and keep typing" slice, not
v0.3 detached-while-the-window-is-closed. The window may still kill the
daemon. Quit and reopen is enough if the files landed.

**Rationale:** The rail already looked like Claude history. Without a
durable transcript that was a lie: click a row, get a tombstone. The
user asked for Claude's learning curve and forbade faking. Reviving
only when both files exist is the honest cut.

**Alternative rejected:** Show an empty thread under the old id. Also
rejected: marking `interrupted` resumable without the conversation —
the model would start from nothing under a transcript the user can
still see.

---

## 2026-08-20 — TD-2203: cosine rank, top-k, heading-match tie-break (Class B)

**Decision:** When the sidecar returns vectors, every topic file (not
just heading-matches) is scored by cosine similarity to the task.
`MEMORY.md` is always first. Topics sort by `(-score, heading?, name)`.
The loader keeps at most `embeddings.top_k` topics whose TD-506
heuristic counts fit `embeddings.token_budget` after the index.
A topic that does not fit is skipped so a smaller later file can still
land. Embed failure or a disabled client is unchanged TD-2201.

**Rationale:** Spec §5 is a subset, not everything. Ranking all topics
is how a file with no heading overlap can still be relevant. Heading-
match as tie-break keeps the floor visible in the inspector reason.
The 4-chars/token heuristic is the same counter as steering (TD-506),
so a later budget story cannot invent a second ruler.

---

## 2026-08-20 — TD-2502: drop list is token-budget; MEMORY.md last (Class B)

**Decision:** After ranking or heading-match, `enforce_memory_budget`
drops files until the TD-506 total is `<= embeddings.token_budget`.
Topics drop from the tail (lowest rank / last name). `MEMORY.md` is
last. Membership in `MemoryLoad.dropped` means the file was selected
and then cut for budget; the file's `reason` is still why it was
chosen (`always-index` / `heading` / `embedding`). An index that
alone exceeds the cap yields an empty load (placeholder), not an
over-budget block.

**Rationale:** TD-2203 always *tries* `MEMORY.md` first. TD-2502 is the
cap the assembler must honor, including when the index is huge. The
inspector (TD-2604) needs names and both whys without a second API.

**Alternative rejected:** Leaving an oversize `MEMORY.md` in the prompt
and only dropping topics. That matches a literal reading of TD-2203
and blows the cap the user configured.
