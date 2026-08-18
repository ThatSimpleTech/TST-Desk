# TST Desk — Product & Architecture Spec

**An open-source, bring-your-own-model desktop agent workspace.**
Looks and feels like Claude Desktop + Cowork. Runs on your models, your machine, your keys.

Status: spec v1 (2026-08-12)
Owner: That Simple Tech
License target: MIT or Apache-2.0

---

## 1. The thesis

There is no open-source thing that *feels* like Claude Desktop.

- **Odysseus** is a local chat workspace — closer, but it's a chat UI, not a coworker.
- **Hermes Agent** (Nous Research) is a persistent background daemon with a messaging gateway — powerful, but it's a daemon you talk to from Telegram, not a desktop app you work beside.
- **ChatGPT desktop** is a paid subscription and closed.

Nobody has shipped **the desktop agent workspace, open, on your own models.**

TST Desk is that. The pitch in one line:

> Everything you like about working with Claude Desktop and Cowork — the window, the workspace, the watching-it-work, the approvals, the memory — running on models you choose at roughly a third of the cost, with nothing to subscribe to.

Plus the thing none of them have: **autonomy mode** (§12) — hand it a charter and a sprint
document, walk away, come back to a built thing and a ledger of every decision it made.

### Why this wins on cost

The BYO three-tier stack (see §7) lands around **$0.36 per working session**, ~28 sessions per $10.
Claude Max 20x is $200/mo (~$46/week flat). The same workload metered on frontier API rates
runs $88–158 blended. TST Desk users pay per-token for exactly what they use, on a stack
selected for price/performance — and they can see the meter running in the title bar.

### Non-goals (hold this line)

TST Desk is a **local desktop application**. It does **not** require:

- a server, VPS, or any hosted backend
- a phone number, PSTN, or telephony
- an account with TST
- a subscription of any kind

The only credential a user needs is **their own OpenRouter (or other provider) API key.**
Every feature in this spec runs on the user's machine. Anything that would break that rule
belongs in a different document.

---

## 2. What the product actually is

Three layers, bottom to top:

```
┌──────────────────────────────────────────────────────────┐
│  TAURI SHELL  (Rust host + SvelteKit frontend)           │
│  Chat pane · Work pane · Approval cards · Cost meter     │
│  Workspace picker · Instruction inspector · Session list │
└──────────────────────┬───────────────────────────────────┘
                       │ local WebSocket on 127.0.0.1 (+ Tailscale iface, opt-in)
┌──────────────────────▼───────────────────────────────────┐
│  tstd  —  Python core daemon                             │
│  · SessionRunner (survives window close)                 │
│  · agent loop  ← lifted from tst-cua                     │
│  · model router (brain / worker / validator)             │
│  · context assembler (steering + memory + workspace)     │
│  · tool registry + approval gate                         │
│  · audit log (SQLite, append-only)                       │
└──────┬────────────────────────┬──────────────────────────┘
       │                        │
  OpenRouter API          Tools: fs · shell · computer-use
  (or any OpenAI-          (Atspi / Darwin / Browser drivers
   compatible endpoint)     from tst-cua) · MCP extensions
```

### Why the core is a separate daemon, not in-process

This is the single most important architectural decision, and it buys three features at once:

1. **Coworker mode works.** Close the window, the session keeps running. The session owns the
   loop; the socket is just a viewer.
2. **Remote attach works.** Bind the same socket to the Tailscale interface and your phone can
   watch a running session. No new subsystem — it's the same protocol.
3. **The CLI comes free.** `tst attach`, `tst run` — same daemon, different door.

The window is a *view* onto sessions, not the thing that runs them. Design everything this way.

---

## 3. The shell (this is the actual product)

The engine is the easy part — Adam already has it. **The shell is the work, and the shell is
the differentiator.** Budget accordingly: this is bigger than the agent.

### Layout

```
┌───────────────────────────────────────────────────────────────┐
│ ~/code/myproject ▾    brain:kimi-k3  worker:v4-flash   $0.14  │  ← workspace + tier + live cost
├────────────────────────────┬──────────────────────────────────┤
│                            │  ┌ Activity ┬ Files ┬ Screen ┐   │
│   CHAT                     │  │                            │   │
│   (conversation, streamed) │  │  live work view            │   │
│                            │  │  · what it's doing now     │   │
│                            │  │  · files touched (diffs)   │   │
│                            │  │  · screen (computer-use)   │   │
│                            │  │  · command output          │   │
│                            │  └────────────────────────────┘   │
├────────────────────────────┴──────────────────────────────────┤
│  ⚠ Wants to run: `rm -rf build/`   [Approve] [Deny] [Always]  │  ← approval card
└───────────────────────────────────────────────────────────────┘
```

### The four things that make it *feel* like Claude Desktop

1. **It's a window, not a terminal.** Installs like an app. No CLI required to use it.
2. **Workspace-scoped.** You point it at a folder and go. The folder is the unit of work —
   files visible, artifacts delivered back into it. This is the Cowork framing and it's most
   of why it feels like a coworker instead of a chatbot.
3. **You watch it work.** The right pane is not decoration — it's the soul of the product.
   Activity timeline, file diffs, live screen when driving the desktop.
4. **The trust surface is visible.** Approvals are cards, not y/n prompts. Every action lands
   in a timeline you can scroll. This is what makes people comfortable letting it drive.

### Things TST Desk has that Claude Desktop can't

- **Live cost meter.** BYO key means the user owns the spend, so show it — per turn, per
  session, per day. A subscription product structurally cannot do this.
- **Model tier switcher in the title bar.** Swap brain/worker/validator mid-session.
- **The instruction inspector** (§4.4).
- **Memory you can read, edit, and `git revert`** (§5).

### Stack

- **Tauri 2** (Rust host, small binaries, real OS integration) — not Electron.
- **SvelteKit + Svelte 5** frontend. Matches existing TST skill set.
- **Python 3.11+** core daemon, packaged via `uv`.
- Shell↔core over **local WebSocket**, JSON events.

---

## 4. Instruction / steering files

**Design rule: steering is written by the human and read by the agent. Memory is written by
the agent and read by the human. They never share a file, and the agent has no write access
to steering.** When those mix, past decisions get read as current rules and constraints drift.

### 4.1 File resolution

Loaded at session start, in order, lowest → highest precedence:

| Scope | Location | Purpose |
|---|---|---|
| User global | `~/.tstdesk/AGENTS.md` | Personal prefs across all workspaces |
| Workspace | `<workspace>/AGENTS.md` | Team conventions, git-tracked |
| Rules dir | `<workspace>/.tst/rules/*.md` | Modular, path-scoped rules |
| Directory | `<workspace>/**/AGENTS.md` | Subtree-specific rules |

**Fallback for adoption:** if `AGENTS.md` is absent, read `CLAUDE.md` at the same path.
This is deliberate — point TST Desk at any repo already configured for Claude Code and it
works day one, nothing to port. Do not skip this; it is a meaningful adoption unlock.

More specific overrides broader. Files are concatenated into a single steering block with
provenance comments so the model knows where a rule came from.

### 4.2 Imports

`@path/to/file.md` inline imports, relative or absolute, **max depth 4**, cycle-detected.
Not evaluated inside code fences. First import from outside the workspace prompts for
approval (it's an untrusted-file-read).

Imports help *organization*, not context size — imported content still loads in full.

### 4.3 Path-scoped rules (the real token fix)

Rules in `.tst/rules/` may carry frontmatter:

```markdown
---
appliesTo: ["src/api/**/*.py", "tests/api/**"]
---
- All endpoints return Pydantic models, never raw dicts.
- Every route needs a test in tests/api/ before merge.
```

These load **only** when the session touches matching files. This is how a large ruleset
stays affordable. Steering files ride in context on every session and long files measurably
reduce adherence — target **under 200 lines per file** and push detail into path-scoped rules.

### 4.4 The instruction inspector (differentiator)

A panel that shows the **resolved** instruction stack for the current session:

```
ACTIVE INSTRUCTIONS                          1,847 tokens · cached
  ~/.tstdesk/AGENTS.md                 312 tok
  ./AGENTS.md                          640 tok
  .tst/rules/python-style.md           205 tok   [path-scoped: matched src/**]
  .tst/rules/api-conventions.md        418 tok   [path-scoped: matched src/api/**]
  └ @docs/architecture.md              272 tok   [imported]
  .tst/rules/frontend.md                 —       [not matched this session]
```

Click any entry to open it in the editor. In a CLI, "did my rules take effect?" is guesswork.
Here it's a pane. Ship this early — it's cheap and it's the kind of detail that makes people
trust the tool.

### 4.5 Prompt assembly & caching

Order the system prompt so the **stable prefix comes first**:

```
[1] TST Desk base system prompt      ← never changes
[1b] Workspace root (absolute)       ← constant for the session
[2] Steering block (resolved)        ← changes only when files change
[3] Memory block (relevant subset)   ← changes between sessions
[4] Workspace manifest (file tree)   ← changes as files change
[5] Conversation                     ← changes every turn
```

Blocks 1–2 are the cache target. On the locked stack this is the difference between $2.80/M
and $0.30/M on the brain tier — on a long session, that is most of the bill.

### 4.6 Per-tier context routing

Not every tier needs everything:

| Tier | Gets |
|---|---|
| **Brain** (Kimi K3) | Full steering + memory + workspace manifest. It plans. |
| **Worker** (V4 Flash) | Steering + current task + relevant files. No memory block. |
| **Validator** (V4 Pro) | Standards/conventions subset + the diff + test output. Nothing else. |

Validation is input-heavy; keeping the validator's context tight is what keeps its cost at
~$0.08/session instead of dominating the bill.

---

## 5. Memory

**Markdown, in the workspace, in git.** This is a feature, not a compromise: memory you can
open, correct, diff, grep, and revert beats memory you can't see.

### Layout

```
<workspace>/.tst/memory/
  MEMORY.md              ← the index; durable facts about this project
  decisions.md           ← why things are the way they are
  gotchas.md             ← things that bit us
  <topic>.md             ← agent may create topical files
```

### Mechanics

1. **Session start:** the context assembler loads `MEMORY.md` plus any topic file whose
   heading matches the task. Not everything, every time.
2. **Session end:** a **distill step runs on the worker tier** — cheap — which proposes memory
   writes as a diff.
3. **The diff is shown in the UI before it's written.** Accept / edit / reject.
4. **Every accepted write is a git commit** in the workspace (`tst: memory update`). Full
   history. A bad memory is one `git revert` away.

### Hard rules

- The agent may write `.tst/memory/**`. It may **never** write `AGENTS.md` or `.tst/rules/**`.
- Memory is workspace-local by default. A global memory at `~/.tstdesk/memory/` is opt-in.
- Memory files are also capped (~200 lines) and distilled, not appended forever.

---

## 6. Trust surface

Directly ported from the WARDEN design.

- **Approval gates.** Tool calls classify as `auto` / `ask` / `never`. Policy is per-workspace
  in `.tst/config.yaml`, overridable per-session in the UI. "Always allow this command in this
  workspace" writes back to policy. Settings → Policy also has a machine-wide Skip all
  approvals toggle (user data dir, not the workspace file). It skips the ask for Class B;
  Class C and the boundary still win.
- **Append-only audit log** in SQLite: turn, tool, args, result hash, model, tokens, cost,
  timestamp. Never mutated. Exportable.
- **Cost ceilings.** Per-session and per-day spend caps that pause the session and raise a card
  rather than silently burning the user's key.
- **Sandboxing (later phase).** Shell tools default to the workspace directory; opt-in
  container isolation for untrusted work.

---

## 7. Model plane

The opinion is the product. Ship with the stack pre-decided, let people change it.

| Tier | Default | Slug | Role |
|---|---|---|---|
| **Brain** | Kimi K3 | `moonshotai/kimi-k3` | Planning turns only. 1M ctx. ~$2.80/M in, $14/M out, $0.30/M cache-read |
| **Worker** | DeepSeek V4 Flash | `deepseek/deepseek-v4-flash` | Token-heavy edits. ~$0.07/M in, $0.17/M out |
| **Validator** | DeepSeek V4 Pro | `deepseek/deepseek-v4-pro` | Reviews diff + tests. ~$0.44/M in, $0.87/M out |

Documented swaps: GLM-5.2 (`z-ai/glm-5.2`) as a cheaper brain or stronger validator;
V4 Pro as a heavier worker.

**Router requirements:**
- One async `httpx` client, OpenAI-compatible. Works against OpenRouter, any compatible
  endpoint, or a local vLLM server (EZER path) with no code change.
- Slugs and prices live in `config.yaml`, not code. **The landscape moves weekly** — users must
  be able to update model choices without a release.
- First-run wizard: paste key → pick preset (`TST default` / `budget` / `local`) → go.

### The `local` preset: the endpoint is ours to guess, the model tag is not

The shipped `local` preset points every tier at `http://127.0.0.1:11434/v1` and names **no**
model. An OpenAI-compatible endpoint on loopback is a safe convention — Ollama, vLLM, LM Studio
and llama.cpp all serve one — but the model tag belongs to the user's machine, so shipping one
developer's tag as everybody's default is the wrong default for a bring-your-own-model product.

So `slug` is optional, on one condition, and resolved on first use:

- **Optional only on a loopback `base_url`.** A tier with an off-box endpoint and no `slug` is a
  config error at load, naming the tier. Discovery never runs for a remote tier, so no request
  can leave the machine and no credential question arises.
- **Resolved from `GET /v1/models` on the first turn that needs it.** Not at import, not at
  daemon start, not at workspace open — an absent model server fails the *turn*, the way a
  missing API key does, so the conversation survives, the user starts their server, and the next
  message goes through.
- **Config always wins.** A `slug` present in `config.yaml` is never overridden and triggers no
  request at all. `slug:` with no value means the same as omitting it; `slug: ""` is an error.
- **Exactly one served model resolves.** Zero, or several, is refused by name — `/v1/models`
  lists embedding models beside chat models, so "take the first" would routinely bind the agent
  to a model that cannot answer a chat completion. The error prints every id it saw and points
  at the tier's `slug:` key.
- **Nothing is written and nothing is sent.** Discovery reads no API key and sends no
  `Authorization` header (§2.2); the resolved tag lives in memory for the life of the process
  and is never written back to `config.yaml`, so swapping the model on the server is picked up
  by the next run rather than leaving a stale tag in a file the user never edited.

Failure is a typed error naming the endpoint and the fix — a dead endpoint, a wrong `base_url`
and an idle server each say something different — surfaced on the turn as
`turn_complete{failed, error_code: "model_unresolved"}` and in diagnostics as a failing
`provider` row with the endpoint in it.

---

## 8. Coworker mode & remote

Falls out of the daemon architecture almost for free.

- **Detached sessions.** SessionRunner is a background task with a durable event log
  (monotonic `seq`). The window attaches with `attach{session_id, from_seq}` and replays.
  Close the laptop lid — well, don't; but close the *window* and it keeps going.
- **Session list pane.** Everything running, its state, its spend.
- **`awaiting_approval` parks the session** and fires an out-of-band notification.
- **Remote attach** over Tailscale: bind the daemon socket to the Tailscale interface
  (never `0.0.0.0`), attach from a phone browser or the CLI. Read + approve from the road.

### What we borrow from Hermes

Assessed against the real Nous Research repo. The salvage list is small and clean:

| Piece | Verdict |
|---|---|
| **Notification channels** | **Lift.** Each channel is a standalone module with a single `send(config, message)` function. Slack is an incoming webhook. Take the pattern, ship Slack first, Discord/Telegram/ntfy as extras. |
| **Cron scheduler** | **Reference, rebuild.** Good design (natural-language jobs, JSON storage, deliver anywhere) but wired into their agent core. Ours is small: "wake, run instruction, deliver to Slack." |
| **Delegation / subagents** | **Do not lift.** Most entangled with their agent core. This is what `tst-cua` is for. |
| **Messaging gateway (20+ platforms)** | **Skip.** We're a desktop app, not a chat bot. Slack notify is enough. |

Rule of thumb: **borrow the plumbing, not the brain.**

---

## 9. Phased build

Ship the thinnest thing that already feels like Claude Desktop. Earn the rest.

**v0.1 — "It feels like Claude Desktop"** ← the only phase that matters right now
Tauri shell, one window. Chat pane + activity timeline. Workspace picker. `tstd` daemon with
tst-cua loop + 3-tier router. fs + shell tools. Approval cards. Steering file loading +
instruction inspector. Audit log. Cost meter. First-run key wizard.
**Plus autonomy *hooks*** (§12.6): decision classifier, decisions ledger, checkpoint commits.
Interactive mode uses them from day one.
*No computer-use. No coworker. No remote. No memory. No autonomous runner.*

**v0.2 — Memory.** `.tst/memory/`, distill-on-worker, diff-before-write, git commits.

**v0.3 — Cowork parity.** Detached sessions, durable event log, session list, file-diff view,
artifact delivery.

**v0.4 — Computer-use.** Screen pane, tst-cua drivers (Atspi/Darwin/Browser), OS permission
onboarding.

**v0.5 — Remote + notify.** Tailscale bind, phone attach, Slack notifier, small scheduler.

**v0.6 — Local models.** Point the router at vLLM/EZER. UI-TARS for grounding, local worker.

**v0.7 — Autonomy engine.** The charter editor, the autonomous runner loop, validator-as-
supervisor drift checks, circuit breakers, container isolation. Depends on v0.3 (detached
sessions) and v0.5 (notifications). See §12.

---

## 10. Open decisions (answer before building)

1. **Repo:** standalone `ThatSimpleTech/tst-desk`, or a module inside TSTOS?
2. **`tst-cua` reuse:** vendor it into the repo, or depend on it as a package?
3. **Goose:** keep it as an optional headless backend, or fully replace it with `tstd`?
4. **License:** MIT (maximum adoption) or Apache-2.0 (patent grant)?
5. **Frontend:** SvelteKit inside Tauri (matches TST skills) — confirm, or React for
   contributor familiarity?
6. **Name:** "TST Desk" — locked, or still open?
7. **Autonomy isolation:** is unsandboxed autonomy allowed at all with a scary confirmation,
   or hard-required to run in a container? (Recommendation: hard-required. §12.5)

---

## 11. Success criteria for v0.1

A stranger clones the repo, runs one install command, pastes an OpenRouter key, points it at a
folder, types a request, watches it work, approves a shell command, and gets a result —
**without ever opening a terminal after install, and without creating an account anywhere.**

If that's true, the product exists. Everything else is refinement.

---

## 12. Autonomy mode

**The feature nobody else has, and the reason this tool exists for big builds.**

Hand it a charter and a sprint document. Walk away. Come back to a built thing and a ledger of
every decision it made along the way.

This is aimed squarely at projects like EZER and TSTOS — greenfield work measured in weeks,
where the plan already exists in writing and the agent stopping to ask *"how fast should the
window slide?"* is pure friction. That is not a permission question. It is a taste question,
and the agent was fully equipped to answer it.

### 12.1 The core principle: reversibility, not risk

The wrong axis is "how dangerous is this action." The right axis is **how expensive is it to
undo.** A clock position is three seconds of `git revert`. A dropped production table is not.

Autonomy is therefore **a contract, not a switch.** The user grants authority up front, scoped
to a boundary. Inside the boundary the agent has blanket authority and never interrupts.
It stops only when it would step *outside* the wall. The user gets silence; the wall does the
safety work that per-action prompts were doing badly.

### 12.2 Decision classes

Every decision the agent faces is classified before it acts.

| Class | Definition | Examples | Behavior |
|---|---|---|---|
| **A — Taste** | Reversible, inside the workspace, no contract with anything external | Animation timing, spacing, colors, copy wording, file/variable naming, clock placement, transition curves, log format | **Decide. Log. Never ask.** |
| **B — Structural** | Reversible but costly to unwind; affects other code | Schema shape, module boundaries, new dependency, API surface, error-handling strategy | **Decide if the charter covers it. Log with rationale. Surface in the summary for review.** |
| **C — Boundary** | Irreversible, outside the workspace, or over a declared cap | Writes outside workspace, network calls to new hosts, system package installs, credential access, deleting untracked files, force-push, exceeding spend/time cap, anything the charter forbids | **Stop. Always. Notify.** |

Class A is the entire point of the feature. Be aggressive here — an agent that asks about
Class A decisions has failed at autonomy.

Classification is a cheap classifier call on the **worker** tier, not the brain, plus a static
rule table for the obvious cases (path outside workspace → C, always).

### 12.3 The decisions ledger

Class A being aggressive is only safe because it is **decide *and log***, never decide silently.

`<workspace>/.tst/autonomy/DECISIONS.md`, appended as it works:

```markdown
## 2026-08-12T14:22:09Z · Class A · commit 3f9a1c2
**Chose:** Sidebar clock top-right, 240ms ease-out slide.
**Why:** Charter says "unobtrusive status furniture." Top-right keeps it out of
the primary reading path. 240ms matches the existing panel transitions.
**Undo:** `git revert 3f9a1c2`
```

The user reviews forty taste calls in one minute and flips any of them individually.
**They never lose the ability to change their mind — they stop paying for it in
interruptions.** This is the trade that makes the whole feature work.

### 12.4 The charter

Signed before a run starts. Stored at `.tst/autonomy/CHARTER.md`, git-tracked.

```yaml
objective: "Build the TSTOS motion engine per docs/sprint-04.md"
definition_of_done:
  - "cargo test passes"
  - "all six transitions implemented and demoed in examples/"
source_of_truth: ["docs/sprint-04.md", "docs/design-constitution.md"]
boundary:
  writable_paths: ["src/**", "tests/**", "examples/**"]
  allowed_commands: ["cargo", "git", "nix"]
  network: "deny"          # or an explicit allowlist of hosts
caps:
  spend_usd: 25.00
  wall_clock_hours: 8
  max_iterations: 200
stop_conditions:
  - "definition_of_done met"
  - "tests red for 3 consecutive iterations"
  - "same file modified 5+ times without test improvement"
  - "any Class C decision"
```

`source_of_truth` is what makes "it has the entire sprint document" real — those files are
re-read on every drift check, not just at session start.

### 12.5 Isolation — the honest constraint

**A greenfield project is not a sandbox. The *process* is what is or isn't sandboxed.**

The agent will not *decide* to reach for other people's machines — that was never the risk.
The risk is a bad shell command, a hallucinated install script, a runaway delete, or simply
that a developer laptop has cloud credentials and VPN keys sitting on it.

Therefore: **autonomous runs execute in a container with no credentials mounted** — rootless
Podman locally, or a Firecracker microVM on EZER. Only the workspace is bound in.

Frame this correctly in the UI: **the sandbox is what *buys* the autonomy.** Inside a
container with nothing sensitive reachable, Class A and B can open all the way up and mean it.
Isolation is the enabler, not the restriction.

### 12.6 Validator as supervisor

In interactive mode the validator reviews diffs. In autonomy mode its job changes: it becomes
the **intent guardian**, and it is what replaces the human in the loop.

Every N iterations (default 5) or on any Class B decision, the validator tier receives:

- the charter and `source_of_truth` documents
- the diff since the last check
- the new entries in the decisions ledger
- current test output

and answers three questions:

1. **Does the work still serve the objective?**
2. **Have the accumulated Class A decisions drifted from the stated intent?** (Forty
   individually-reasonable taste calls can collectively wander off the brief. This is the
   failure mode that unattended agents actually hit.)
3. **Is progress real, or is it thrashing?**

Drift detected → **auto-revert to the last good checkpoint**, log the reason, re-plan on the
brain tier. Drift twice in a row → stop and notify.

Running this on the validator tier (~$0.44/M in) keeps supervision at pennies per hour.

### 12.7 Circuit breakers

These are not permission requests. They are **fault reports** — the run stops and the user is
notified with a summary.

- spend cap or wall-clock cap reached
- tests red for N consecutive iterations
- same file thrashed N+ times with no test improvement
- no measurable progress against definition-of-done for N iterations
- any Class C decision
- loop detection (repeated identical tool calls)

### 12.8 Checkpointing

Every iteration commits to a dedicated branch (`tst/auto/<charter-slug>`). The branch is the
undo stack, the audit trail, and the review surface. Nothing is ever committed to `main` by an
autonomous run.

The user wakes up to: a branch, a summary of what was built, the decisions ledger, and a list
of anything it refused to touch.

### 12.9 What ships when

**v0.1 (hooks):** decision classifier, `DECISIONS.md` ledger, checkpoint commits, boundary
config in `.tst/config.yaml`. Interactive mode uses all of it immediately — Class A stops
prompting on day one, which is a real quality-of-life win by itself.

**v0.7 (engine):** charter editor UI, the autonomous runner, validator drift checks, circuit
breakers, container isolation, wake-up summary.

Building the hooks first means autonomy is never a retrofit — and the classifier makes
interactive mode better right away.
