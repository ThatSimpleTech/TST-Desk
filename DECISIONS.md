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
