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

## 2026-08-13 — TD-508: Per-tier context routing

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