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
