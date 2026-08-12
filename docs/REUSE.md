# REUSE — tst-cua → TST Desk porting guide

**Status:** TD-102 audit complete. Review before porting begins.

**Source:** `tst-cua` (1,166 lines across 10 files). Package name `cua/`, project
directory `tst-cua`.

**Target:** `tstd` — the Python core daemon for TST Desk.

---

## 1. Decision table

| Module | Lines | Verdict | Rationale |
|--------|-------|---------|-----------|
| `types.py` | 107 | **Reuse as-is** | Core types (`Element`, `Snapshot`, `Action`, `StepResult`, `Tier`). Clean, self-contained, dependency-free. `Tier` enum maps directly to the spec's approval gate. |
| `agent.py` — loop | ≈60 | **Adapt** | `plan → gate → act → verify → reconcile` is the right structure. Needs 3-tier routing, MCP tools, async `httpx` transport, streaming. |
| `agent.py` — `Provider` | 48 | **Rewrite** | Single-model sync `urllib` call. Replace with 3-tier async router. Keep the `BASE_URL`/`model`/`api_key` pattern as config-driven. |
| `agent.py` — `parse_action` | 18 | **Rewrite** | Regex-parsed JSON from model output. Replace with MCP tool-call format. |
| `drivers/base.py` — `Driver` protocol | 15 | **Reuse as-is** | `snapshot() / act() / close()` interface is clean. |
| `drivers/base.py` — `digest()` | 19 | **Adapt** | Semantic hash pattern is excellent. Implementation is a11y-specific. Generalize for TST Desk's state model. |
| `drivers/base.py` — `MockDriver` | 30 | **Reuse as-is** | Directly valuable as the v0.1 test harness. Proven by `test_loop.py`. |
| `drivers/linux.py` | 160 | **Defer v0.4** | `AtspiDriver` + `BrowserDriver`. Computer-use only. Not needed for M1. Marked intact. |
| `darwin.py` | 230 | **Defer v0.4** | `DarwinDriver`. Same as Linux — computer-use only. Intact. |
| `verify.py` — `structural()` | 40 | **Adapt** | Verification concept is the differentiated part of the codebase. Implementation is computer-use-specific (digest comparison, a11y postconditions). Need generalization for fs/shell tools. |
| `verify.py` — `ProgressMonitor` | 40 | **Reuse as-is** | Stall/loop detection is generic. Only needs `MUST_CHANGE` set generalized. |
| `policy.py` — `Governor` | 100 | **Adapt** | Tier escalation, budget enforcement, credential detection patterns are right. Needs SQLite audit (instead of JSONL), WebSocket approver (instead of CLI `input()`), YAML config (instead of dataclass). |
| `policy.py` — `Policy` | 40 | **Adapt** | Guardrail structure is right. Move config to `.tst/config.yaml`. Add per-command and per-path rules from spec. |
| `test_loop.py` | 60 | **Adapt** | MockDriver-based test approach is the v0.1 test harness. Replace scenarios with TST Desk-specific ones (fs ops, shell commands, approval gates). |
| `cli.py` | 20 | **Ignore** | Scaffold entry point. Replaced by the daemon lifecycle. |
| `README.md` | 90 | **Ignore** | tst-cua documentation. Not reused in TST Desk. |

### Summary

| Verdict | Count | Files |
|---------|-------|-------|
| Reuse as-is | 4 | `types.py`, `Driver` protocol, `MockDriver`, `ProgressMonitor` |
| Adapt | 6 | `agent.py` loop, `digest()`, `structural()`, `Governor`, `Policy`, `test_loop.py` |
| Ignore | 2 | `cli.py`, `README.md` |
| Rewrite | 2 | `Provider` class, `parse_action()` |
| Defer v0.4 | 2 | `drivers/linux.py`, `darwin.py` |

---

## 2. Agent loop assessment

### 2.1 Control flow

```
plan → gate → act → verify → reconcile
```

This is the right structure and matches the TST Desk spec's description. The loop
as implemented in `Agent.run()` (≈55 lines) is:

1. **Check budget** — Governor enforces step/spend/time ceilings
2. **Snapshot** — driver captures current state (a11y tree)
3. **Check progress** — ProgressMonitor.advice() for stall/loop detection
4. **Build prompt** — goal + state render + feedback + advice
5. **Call provider** — single model, synchronous, JSON response
6. **Parse action** — regex-extract JSON, build `Action`
7. **Gate** — Governor checks tier, policy, credentials, domain, app
8. **Act** — driver executes the action
9. **After-snapshot** — driver captures state again
10. **Verify** — `structural()` checks digest change + postconditions
11. **Record** — append `StepResult` to transcript, log feedback
12. **Loop** — repeat until `kind: "done"` or budget exhausted

### 2.2 What must change for the 3-tier router

| Step | Current | 3-tier target |
|------|---------|---------------|
| 4–5 | One model call for planning + action | **Brain**: gets full steering + memory + manifest. Plans the action. |
| 8 | Agent executes directly | **Worker**: executes the action. Gets task + relevant files. No memory block. |
| 10 | `structural()` is a cheap function | **Validator**: reviews diff + test output. Gets conventions subset. Called only when verification is ambiguous. |
| All | Single `Provider` object | Three configs (brain/worker/validator) with distinct model slugs, prices, URLs. |

The `Agent` class currently does both planning (brain) and execution (worker) in
one object. For TST Desk these must be split into separate concerns, though the
loop structure itself stays the same.

### 2.3 Tool interface

The current tool interface is a **fixed six-verb JSON vocabulary**:

```json
{"kind": "click|type|key|scroll|focus|navigate|wait|done",
 "target": <element index or null>,
 "value": "<text>",
 "tier": "read|benign|mutating|critical",
 "rationale": "<one short sentence>",
 "postcondition": "<what must be true afterwards>"}
```

This is:
- **Not MCP** — it's a single JSON object parsed from the model's reply, not a
  tool call array
- **Computer-use-specific** — verbs assume a visual surface with a11y elements
- **Deliberately minimal** — designed for verifiability with small models

**For TST Desk:** Replace with MCP tool calls (fs, shell, registry, etc.) routed
through the decision classifier, as specified in AGENTS.md §2 prime directive 6.

### 2.4 Provider assumptions

The `Provider` class assumes:
- **Single model** — one `complete()` call per turn
- **Synchronous transport** — `urllib.request`, no streaming
- **OpenAI-compatible or Anthropic-style** — two code paths selected by `style`
- **Environment variable configuration** — `CUA_BASE_URL`, `CUA_API_KEY`, `CUA_MODEL`
- **No cost tracking** — token counts and pricing are not returned

**For TST Desk:** Need async `httpx` transport, 3-tier routing, streaming,
cost tracking per-turn, and model slugs/prices in config file (not code, per
spec §7 and prime directive 7).

---

## 3. Driver code — defer to v0.4

All three computer-use drivers are **intact** and **deferred**:

| Driver | File | Lines | Status on tst-cua side |
|--------|------|-------|----------------------|
| `AtspiDriver` | `drivers/linux.py` | 80 | Talks to `computer-use-linux` MCP over stdio. **Never executed** against a real AT-SPI surface. |
| `BrowserDriver` | `drivers/linux.py` | 80 | Playwright/CDP with persistent profile. **Never executed** against a real browser. |
| `DarwinDriver` | `darwin.py` | 230 | AXUIElement tree + semantic AXPress. **AST-parsed only** — never executed. |

All three implement the same `Driver` protocol (`snapshot() / act() / close()`)
and share `digest()`, `_flatten()`, and `NOISE_ROLES` patterns. The contract
above `drivers/` is clean — nothing in the agent loop or policy engine depends
on which driver is active.

**When v0.4 arrives:** The drivers are structurally ready. The work is:
1. Run each driver against its real surface (Playwright, AT-SPI bus, macOS AX)
2. Fix the bugs that only surface on real hardware
3. Wire them into the 3-tier router's tool registry

---

## 4. MockDriver — v0.1 test harness assessment

**Verdict: Reuse as-is.** This is the highest-value piece for v0.1.

The `MockDriver` (30 lines in `drivers/base.py`) is a scripted surface that
simulates state transitions:

```python
states = {
    "form":  ("Expense Portal", [
        ("textbox", "Amount", ["enabled","focused"]),
        ("button", "Submit", ["enabled"]),
    ]),
    "done":  ("Expense Portal", [
        ("statictext", "Submitted successfully", ["enabled"]),
    ]),
}
transitions = {("type", "Amount"): "filled", ("click", "Submit"): "done"}
```

The `test_loop.py` (60 lines) proves four failure modes offline:
1. Silent no-op detection (digest doesn't change → verification fails)
2. Postcondition verification on real changes
3. Governor tier escalation + credential blocking
4. Stall + loop detection by `ProgressMonitor`

**For TST Desk:** The mock pattern (states + transitions by action) is reusable.
The test scenarios need to be rewritten for TST Desk's tool set (fs operations,
shell commands, approval gates) instead of computer-use actions. The mock
approach replaces the need for a real desktop or API key in the test suite.

---

## 5. Computer-use coupling concerns

The `Agent` class is tightly coupled to the computer-use paradigm in four ways
that would make later integration painful if not addressed now:

### 5.1 `Snapshot` assumes a visual surface

`Snapshot.elements` is a list of `Element` objects from an accessibility tree.
For TST Desk, the "state snapshot" for a general-purpose agent is a file tree,
a shell environment, and a conversation history — not an a11y tree. The
`Snapshot` type should be generalized or made optional.

### 5.2 `Action` assumes a six-verb vocabulary

`Action.kind` is `click|type|key|scroll|focus|navigate|wait|done`. For TST
Desk, actions are MCP tool calls with arbitrary schemas. The `Action` type
should be replaced with a generic `ToolCall` that carries tool name, arguments,
and tier.

### 5.3 Verification is a11y-specific

`structural()` compares a11y tree digests and checks for text in element
labels. For TST Desk, verification means:
- Did `file.write` produce the expected file?
- Did `shell.run` produce the expected exit code?
- Did the cost stay under the per-turn ceiling?

### 5.4 The system prompt is driver-specific

The `SYSTEM` constant in `agent.py` describes how to operate a computer through
an accessibility tree. For TST Desk, the system prompt describes a general-
purpose agent with fs, shell, and registry tools.

### Mitigation strategy

1. **Keep the `Driver` protocol** as an abstraction layer. Computer-use is a
   specialized driver, not the core loop.
2. **Generalize `Agent`** to work with any `ToolRegistry` (MCP tools), not just
   a single `Driver`. The driver becomes one of many tools.
3. **Use the decision classifier** (spec §12.6, E6) to route between tool types.
   Computer-use actions are a subset of all possible actions.
4. **Port the six-verb vocabulary** as a single MCP tool definition when v0.4
   arrives, not as the core action model.

---

## 6. Detailed file-by-file notes

### types.py — REUSE (107 lines)

- `Element` — idx-based addressing is the core design pattern. Keep.
- `Snapshot` — The `render()` and `find()` methods are useful. The `screenshot_png`
  field is computer-use-specific; make optional in TST Desk.
- `Action` — The six-verb `kind` is computer-use-specific. Replace with `ToolCall`
  in TST Desk. Keep `tier` and `rationale`.
- `StepResult` — Good structure. Keep `before/after` but generalize to
  `state_before/state_after` instead of `Snapshot`.
- `Tier` enum — Directly maps to spec's approval gate. Keep.

### agent.py — ADAPT / REWRITE (205 lines)

**Keep:**
- Loop structure: `plan → gate → act → verify → reconcile`
- Budget check pattern
- StepResult transcript accumulation
- `_say()` logging pattern

**Replace:**
- `Provider` class → 3-tier async router with `httpx`
- `parse_action()` → MCP tool call parser
- `SYSTEM` prompt → TST Desk general-purpose system prompt
- `messages` accumulation → general conversation management
- `urllib.request` → `httpx` async streaming

### drivers/base.py — REUSE (Driver, MockDriver) / ADAPT (digest)

**Keep:**
- `Driver` protocol (snapshot/act/close) — clean abstraction
- `MockDriver` — v0.1 test harness, reuse as-is
- `digest()` pattern — semantic hash for change detection

**Adapt:**
- `digest()` implementation — generalize from a11y elements to TST Desk state
- `MockDriver` states/transitions — replace with TST Desk scenarios when writing tests

### drivers/linux.py — DEFER v0.4 (160 lines)

- `AtspiDriver` — wraps `computer-use-linux` MCP. Intact, unvalidated.
- `BrowserDriver` — Playwright/CDP. Intact, unvalidated.
- `_flatten()` — a11y tree flattener. Useful pattern but deferred.
- `NOISE_ROLES` — Linux-specific roles. Deferred.

### darwin.py — DEFER v0.4 (230 lines)

- `DarwinDriver` — AXUIElement. Intact, unvalidated.
- `_press()` with AXPress fallback pattern — good design.
- `_set_value()` atomic write pattern — macOS-specific win.
- `preflight()` — permission diagnostics pattern. Useful for TST Desk's own
  permission onboarding.

### verify.py — ADAPT (125 lines)

**Keep:**
- `ProgressMonitor` — stall/loop detection. Generic and valuable.
- Verification-first design philosophy — this is the differentiated part.
- Postcondition concept — "what must be true afterwards" is applicable broadly.

**Adapt:**
- `structural()` — replace a11y-specific checks with TST Desk state checks
- `MUST_CHANGE` — add fs/shell equivalents
- `Verdict.needs_model` — the "ask the model" fallback maps to the validator tier

### policy.py — ADAPT (190 lines)

**Keep:**
- `Governor.check_budget()` — step/spend/time ceilings
- `_effective_tier()` — escalation logic (never trust the model)
- `CREDENTIAL_PAT` — regex-based credential detection
- `gate()` — the gating pipeline pattern
- Audit log pattern (append-only record)

**Adapt:**
- Replace JSONL audit with SQLite (per spec §6)
- Replace CLI `input()` approver with WebSocket-based approval cards
- Move `Policy` config to `.tst/config.yaml` (YAML, per spec)
- Add per-command and per-path rules from the spec
- Add per-session and per-day spend caps

### cli.py — IGNORE (20 lines)

Not reusable. TST Desk has its own entry point through the `tstd` daemon.

### test_loop.py — ADAPT (60 lines)

**Keep:**
- MockDriver-based test approach — the v0.1 test harness
- The four failure mode tests (no-op, postcondition, governor, stall/loop)

**Replace:**
- Test scenarios — need TST Desk-specific ones:
  - File write → read verification
  - Shell command → exit code + output verification
  - Approval gate: auto / ask / deny
  - Budget ceiling enforcement
  - Cost calculation accuracy

---

## 7. Risk register

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Computer-use coupling baked into loop before v0.4 | Medium | High | Generalize `Agent` early; keep drivers as one tool type |
| MockDriver diverges from real driver behavior | Medium | Low | Keep `test_loop.py` as integration smoke test; add real driver tests in v0.4 |
| Verifier too specific to a11y to generalize | Low | Medium | The `ProgressMonitor` is generic; `structural()` needs rewrite anyway |
| Provider assumptions (sync, single model) create inertia | Medium | Medium | Rewrite `Provider` as first thing in E4; don't try to adapt it |
| Policy engine tied to JSONL audit | Low | Low | SQLite adapter is straightforward; the `_log()` method is the only touch point |