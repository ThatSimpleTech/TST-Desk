# AGENTS.md — TST Desk working contract

This file governs how any agent works in this repository. It is the operating contract.
Read it fully before your first action in a session. It outranks your defaults.

Companion documents:

- `tst-desk-spec.md` — what we're building and why. **Source of truth for behavior.**
- `tst-desk-backlog.md` — epics, stories, acceptance criteria. **Source of truth for scope and order.**
- `DECISIONS.md` — running log of choices not covered by the spec. You maintain this.

---

## 1. What this repo is

TST Desk is an open-source, bring-your-own-model desktop agent workspace. It is a local
application that feels like Claude Desktop + Cowork, running on user-selected models through
an OpenAI-compatible API.

**There is no server. There is no hosted backend. There is no account. There is no
subscription.** The only credential is the user's own API key. Any proposal that violates this
is out of scope regardless of how useful it seems.

We are also dogfooding: this file is a TST Desk steering file. The conventions here are the
conventions the product implements.

---

## 2. Prime directives

These are non-negotiable. Violating one is a defect regardless of test results.

1. **Never bind a network socket to `0.0.0.0` or any non-loopback interface.** v0.1 is
   `127.0.0.1` only. Remote access is a later, deliberate, opt-in feature.
2. **No secret is ever written to a config file, a log, or the audit database.** API keys live
   in the OS keychain. Redact in all output paths.
3. **No telemetry, no analytics, no phone-home, no crash reporting to any remote.** Zero
   network calls the user did not initiate.
4. **The agent never writes to steering files.** `AGENTS.md`, `CLAUDE.md`, and `.tst/rules/**`
   are read-only to the filesystem tool. This must be enforced in the tool, not by convention.
5. **The daemon owns sessions; the window is only a viewer.** No code path may make the
   WebSocket connection the owner of a running loop.
6. **Every tool call routes through the decision classifier.** No exceptions, no bypass path.
7. **Model slugs, prices, and provider URLs live in configuration, never in code.**

---

## 3. Scope discipline

**Build only the current milestone.** The backlog defines it. If you believe something outside
the milestone is required, stop and ask — do not build it, and do not scaffold it beyond a
one-line `TODO(TD-###)` comment.

This project has a specific failure mode: it is architecturally interesting, and the temptation
to build the fun later phases early is strong. Resist it. A shipped v0.1 beats an elaborate
half-built v0.4.

If a request in chat conflicts with the backlog, say so and ask which wins. Do not silently
follow the newer instruction.

---

## 4. How to work

For every story:

1. **Read** the story in `tst-desk-backlog.md`, its dependencies, and the spec sections it
   references.
2. **State** in one short paragraph what you're about to do and what files you'll touch.
   For stories sized 5+, propose the approach and wait for sign-off.
3. **Build** the smallest complete increment that satisfies the acceptance criteria.
4. **Test** — write the tests named in the story. Run the full suite.
5. **Verify** each acceptance criterion explicitly. Quote the criterion, state pass/fail.
6. **Commit** with the story ID (see §8).
7. **Report** in the format in §9.

Never mark a story done with a failing test or an unmet criterion. Report it as blocked.

---

## 5. Decision protocol

Classify your own decisions the same way the product classifies its own. This is deliberate.

**Class A — Taste.** Reversible, local, no contract with other code. Naming, file layout within
an established directory, log wording, ordering of independent operations, formatting choices
the linter doesn't cover, internal helper structure.
→ **Decide. Note it in the commit body if non-obvious. Do not ask.**

**Class B — Structural.** Reversible but costly. Public API shape, schema design, a new
dependency, an abstraction boundary, error-handling strategy, protocol message design.
→ **Decide if the spec covers it. Record in `DECISIONS.md` with rationale. Flag in your
report.** If the spec is silent and the choice is hard to unwind, ask.

**Class C — Boundary.** Anything that changes scope, violates §2, adds a dependency with a
non-permissive license, changes the milestone, or touches something the spec explicitly
forbids.
→ **Stop and ask. Always.**

Asking about Class A wastes the user's attention. Deciding Class C unilaterally is a defect.

---

## 6. Code standards

### Python (`tstd` core)

- 3.11+. Type hints on every public function. `mypy --strict` clean.
- `ruff` for lint and format. No manual formatting debates.
- `uv` for dependency management. Pin in `pyproject.toml`.
- Async throughout the I/O path. No blocking calls in the event loop — use `asyncio.to_thread`
  for filesystem and subprocess work.
- Pydantic models for every protocol message and config structure. Validate at the boundary,
  trust internally.
- No bare `except`. No silent failure. Errors propagate as typed results or raise.

### Rust / Tauri host

- Keep it thin. The host manages the window, the daemon process lifecycle, and native dialogs.
  The keychain is not the host's — it is `core/tstd/keychain.py`, reached over the protocol like
  everything else. **Business logic belongs in Python.**
- `clippy` clean.

### Frontend (SvelteKit / Svelte 5)

- Runes. TypeScript strict.
- Design tokens in one place; no hardcoded colors or spacing in components.
- Components are presentational where possible; protocol state lives in typed stores.
- The UI never derives truth it wasn't given — if the daemon didn't send it, don't infer it.

### Universal

- Functions do one thing. Files stay under ~400 lines; split before they grow past it.
- Comments explain **why**, not what. No comment restating the line below it.
- No dead code, no commented-out blocks, no `console.log` left behind.

---

## 7. Testing requirements

Two areas carry disproportionate correctness risk and get disproportionate coverage:

1. **The context assembler** (`E5`). Precedence, fallback, glob matching, import depth, cycle
   detection, code-fence exclusion. Table-driven tests over a fixture workspace tree.
2. **The decision classifier and boundary enforcement** (`E6`, `E7`). Path escape attempts,
   symlink traversal, steering-file write refusal, cap enforcement. These are security tests —
   treat a gap here as a defect, not a missing nice-to-have.

Also required:

- A **mock provider** that returns scripted responses, so the loop is testable without network
  or spend. Every loop test uses it.
- Cost math has unit tests with known token counts and expected dollar figures.
- One end-to-end headless test: workspace → request → tool call → result.

Do not write tests that only assert the implementation back to itself. Test observable
behavior against the acceptance criteria.

---

## 8. Git conventions

- Branch per story: `td/<story-id>-<slug>` (e.g. `td/501-file-discovery`).
- Commit subject: `TD-501: resolve steering file precedence`
- Commit body: what changed, why, and any Class A/B decisions worth recording.
- Never commit to `main` directly.
- Never commit secrets, `.env`, keychain material, or `.tst/` runtime state.
- `.gitignore` covers `.tst/` runtime artifacts but **not** `.tst/rules/`,
  `.tst/memory/`, or `AGENTS.md`, which are meant to be shared.

---

## 9. Reporting format

End every unit of work with exactly this, and nothing more:

```
## TD-### — <title>

**Done:** <2–4 sentences on what now works.>

**Acceptance criteria:**
- [x] <criterion, quoted from the backlog>
- [x] <criterion>
- [ ] <criterion> — <why not, and what's needed>

**Decisions:** <Class B items recorded in DECISIONS.md, or "none">

**Files:** <paths touched>

**Tests:** <what was added; suite result>

**Next:** <the next story ID, or the question blocking you>
```

No preamble, no summary of the summary, no restating the task.

---

## 10. Definition of done

A story is done when **all** of the following hold:

- Every acceptance criterion is met and explicitly verified.
- Tests named in the story exist and pass; full suite is green.
- `ruff`, `mypy --strict`, `clippy`, and `tsc` are clean.
- No prime directive (§2) is violated.
- `DECISIONS.md` updated if any Class B decision was made.
- Docs updated if the story changed user-facing behavior or configuration.
- The commit references the story ID.

---

## 11. Layout

```
tst-desk/
├── AGENTS.md              ← this file
├── DECISIONS.md           ← you maintain
├── docs/
│   ├── tst-desk-spec.md       behavior source of truth
│   ├── tst-desk-backlog.md    scope and order source of truth
│   ├── architecture.md        how the pieces fit together
│   ├── configuration.md       every config key documented
│   ├── steering.md            AGENTS.md and path-scoped rules
│   ├── windows.md             where Windows behaves differently
│   └── images/                screenshots embedded in the README
├── core/                  ← Python daemon (tstd)
│   ├── tstd/
│   │   ├── daemon.py      session lifecycle, WS server
│   │   ├── protocol.py    typed event schema
│   │   ├── session.py     SessionRunner (+ persist/revive)
│   │   ├── loop.py        agent loop (from tst-cua)
│   │   ├── router.py      3-tier model routing
│   │   ├── cost.py        pricing, spend caps, usage export
│   │   ├── memory_*.py    store, prompt slot, distill, commit
│   │   ├── context/       steering + manifest assembly
│   │   ├── tools/         fs, shell, registry
│   │   ├── autonomy/      classifier, ledger, checkpoints
│   │   ├── desktop/       MCP and desktop computer-use
│   │   ├── screen/, browser/  computer-use drivers
│   │   ├── notify/, scheduler/  remote notify and cron
│   │   ├── policy.py      approval policy
│   │   └── audit.py       SQLite append-only log
│   ├── scripts/           sidecar build, changelog
│   └── tests/
├── shell/                 ← Tauri host (Rust)
│   └── src/
├── ui/                    ← SvelteKit frontend
│   └── src/
└── .tst/                  ← per-workspace runtime (gitignored except rules/ and memory/)
```
