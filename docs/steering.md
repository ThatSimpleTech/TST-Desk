# Steering authoring guide

Steering is how you tell the agent what your project expects before it ever reads a line of
code. You write it, the agent reads it, and the agent can never write it back — `AGENTS.md`,
`CLAUDE.md` and `.tst/rules/**` are refused by the filesystem tool no matter what your boundary
config says. Memory runs the other way: the agent writes it, you read it. Keeping those two
apart is what stops last month's decisions from being read as this month's rules.

This repository is its own example. The `AGENTS.md` at the root is a real steering file, read
by TST Desk the same way it reads yours.

Every worked example below is a real workspace tree that
`core/tests/test_docs_steering_guide.py` builds on disk and runs through the actual assembler.
The resolved stacks and prompt blocks printed here are its output, not an illustration of it.
If the assembler's behaviour drifts from this page, the suite fails.

---

## 1. The short version

Put an `AGENTS.md` at the root of your repo. Write the things a new teammate would need told
twice. Keep it under 200 lines.

That is the whole requirement. Everything else on this page is for when one file stops being
enough.

If your repo already has a `CLAUDE.md`, you are already done — see §9.

---

## 2. The hierarchy

Four scopes are read at the start of every turn, lowest precedence first:

| Scope | Location | What belongs there |
|---|---|---|
| `user global` | `~/.tstdesk/AGENTS.md` | Your preferences, in every workspace you open |
| `workspace` | `<workspace>/AGENTS.md` | Team conventions. Git-tracked |
| `rules` | `<workspace>/.tst/rules/*.md` | Modular rules, each scopeable to paths |
| `nested` | `<workspace>/**/AGENTS.md` | Rules for one subtree |

They are concatenated in that order into a single block, each file wrapped in a provenance
comment naming where it came from. Nothing is merged or de-duplicated: precedence is a matter
of *ordering*, and when two files disagree the later one wins because the model reads it last.
Say "prefer X" in the workspace file and "never X" in a nested file, and the nested file
carries the subtree.

Here is all four levels at once:

<!-- verify: example hierarchy -->
<!-- verify: file ~/.tstdesk/AGENTS.md -->
````markdown
Prefer small, reviewable commits.
````

<!-- verify: file AGENTS.md -->
````markdown
# Team conventions

Python 3.11+. Type hints on every public function.
````

<!-- verify: file .tst/rules/api.md -->
````markdown
---
appliesTo: ["src/api/**"]
---
Every endpoint returns a Pydantic model, never a raw dict.
````

<!-- verify: file src/api/AGENTS.md -->
````markdown
Routes live in `routes.py`. One module per resource.
````

With the session having touched one file under `src/api/`:

<!-- verify: touched -->
```
src/api/routes.py
```

the resolved stack is:

<!-- verify: stack -->
```
~/.tstdesk/AGENTS.md          (user global)
AGENTS.md                     (workspace)
.tst/rules/api.md             (rules) [scoped]
src/api/AGENTS.md             (nested: src/api)
```

and this is the exact steering block the model receives:

<!-- verify: block -->
````
<!-- from: ~/.tstdesk/AGENTS.md (user global) -->
Prefer small, reviewable commits.
<!-- from: <workspace>/AGENTS.md (workspace) -->
# Team conventions

Python 3.11+. Type hints on every public function.
<!-- from: <workspace>/.tst/rules/api.md (rules) -->
Every endpoint returns a Pydantic model, never a raw dict.
<!-- from: <workspace>/src/api/AGENTS.md (nested: src/api) -->
Routes live in `routes.py`. One module per resource.
````

Note what the provenance comment buys you: when the agent does something you did not expect,
you can ask it which rule it was following, and the answer is in its own context. Note also
that sources are butted together with no blank line between them, and that a file's trailing
newline is dropped — if you end a steering file mid-sentence, the next file's provenance
comment is the very next line.

### Things worth knowing about discovery

- **A missing file is never an error.** Neither is an unreadable one, or one that is not valid
  UTF-8 — it is logged and skipped, and the rest of the stack assembles normally.
- **`.tst/rules/` is not recursive.** Only `*.md` directly inside it is picked up.
  `.tst/rules/python/style.md` is silently invisible.
- **Rules load in byte order of their filename**, which puts capitals first: `Mid.md`,
  `alpha.md`, `zebra.md`. If the order matters, prefix with numbers — `00-`, `10-`, `20-`.
- **Nested files load shallowest first**, so a deeper file overrides a shallower one.
- **The nested walk skips `.git`, `.tst`, `.tstdesk`, `.claude` and `__pycache__`,** and does
  not follow directory symlinks. A steering file inside any of those is not found.
- **Rules come before nested files.** A `src/api/AGENTS.md` therefore outranks every rule in
  `.tst/rules/`, scoped or not.
- **Nested files are not scoped by what the session touches.** Every `AGENTS.md` anywhere in
  the tree loads on every turn, whether or not the agent goes near that subtree. Ten nested
  files means ten files in every prompt. If you want a rule that only costs you when it is
  relevant, that is `.tst/rules/` with `appliesTo` — see §4.

### When an edit takes effect

Steering is re-resolved at the top of every turn. Save the file and the next message picks it
up; there is nothing to restart. The timeline announces the reload and the instruction
inspector refreshes.

The cost of that is a cache miss. Steering is the second block of the stable prompt prefix, so
the turn after an edit cannot reuse the cached prefix and pays full input price for it. Editing
steering mid-session is cheap but not free — batch your edits rather than tuning one sentence
at a time.

That assumes the prefix was being reused in the first place, which is a fact about your
provider, not about the prompt. The inspector's cache badge reports only what the provider
said:

| Badge | What it means |
| --- | --- |
| `cache unknown until a turn runs` | No turn has completed yet. |
| `provider reports no cache figure` | The endpoint returned no cached-token count. Nothing is known about reuse — this is not a miss. |
| `cache miss` | The provider reported zero cached tokens. |
| `cached N tokens` | The provider reported reusing N. |

A local endpoint usually sits on `provider reports no cache figure`. Ollama's
OpenAI-compatible responses carry `prompt_tokens`, `completion_tokens` and `total_tokens` and
nothing else, and llama.cpp discards its context checkpoints on hybrid-attention models, so
the whole prefix is re-processed every turn. The prefix ordering still pays off against cloud
providers that do report and do reuse; on a local model, expect no saving and expect the badge
to say so.

---

## 3. `CLAUDE.md` compatibility

At every path in the hierarchy, the rule is the same: **`AGENTS.md` if it exists, otherwise
`CLAUDE.md`.** The global level checks `~/.tstdesk/AGENTS.md` first and falls back to
`~/.claude/CLAUDE.md`.

A workspace with no `AGENTS.md` anywhere resolves completely:

<!-- verify: example claude-only -->
<!-- verify: file ~/.claude/CLAUDE.md -->
````markdown
Ask before installing anything.
````

<!-- verify: file CLAUDE.md -->
````markdown
Rebase, never merge.
````

<!-- verify: file src/CLAUDE.md -->
````markdown
No new abstractions in `src/` without a second caller.
````

<!-- verify: stack -->
```
~/.claude/CLAUDE.md    (user global) [claude-fallback]
CLAUDE.md              (workspace)   [claude-fallback]
src/CLAUDE.md          (nested: src) [claude-fallback]
```

Every entry is flagged as a fallback in the instruction inspector, so you can always see which
file a rule actually came from.

When both files exist at one path, `AGENTS.md` wins and the `CLAUDE.md` is recorded as
shadowed — the inspector shows you the file that was skipped rather than pretending it is not
there. And note the second half of this example: a `.claude/` directory *inside* the workspace
is skipped by the nested walk, so a `CLAUDE.md` in it is never read.

<!-- verify: example shadowing -->
<!-- verify: file AGENTS.md -->
````markdown
Rebase, never merge.
````

<!-- verify: file CLAUDE.md -->
````markdown
This file is shadowed and contributes nothing.
````

<!-- verify: file .claude/CLAUDE.md -->
````markdown
This file is never discovered at all.
````

<!-- verify: stack -->
```
AGENTS.md    (workspace) [shadows CLAUDE.md]
```

<!-- verify: block -->
````
<!-- from: <workspace>/AGENTS.md (workspace) -->
Rebase, never merge.
````

There is no precedence between the two names beyond that, and no format difference. A
`CLAUDE.md` is parsed exactly like an `AGENTS.md`: same imports, same everything.

---

## 4. Path-scoped rules

Files in `.tst/rules/` may carry YAML frontmatter with an `appliesTo` array of globs. A scoped
rule loads only once the session has touched a file matching one of them. This is the one
mechanism on this page that actually reduces what you pay per turn.

````markdown
---
appliesTo: ["src/api/**", "tests/api/**"]
---
Every endpoint returns a Pydantic model, never a raw dict.
Every route needs a test in `tests/api/` before merge.
````

"Touched" means a path that appeared in a successful tool call — a read, a write, an edit.
The set starts **empty** at session start, which means every scoped rule is inactive on the
first turn and switches on as the agent moves through the tree. When one activates mid-session
the timeline says so, so context never changes silently.

<!-- verify: example scoping -->
<!-- verify: file .tst/rules/api.md -->
````markdown
---
appliesTo: ["src/api/**"]
---
Every endpoint returns a Pydantic model, never a raw dict.
````

<!-- verify: file .tst/rules/style.md -->
````markdown
Line length 100. Trailing commas everywhere.
````

<!-- verify: file .tst/rules/ui.md -->
````markdown
---
appliesTo: ["ui/**/*.svelte"]
---
Runes only. No stores in components.
````

Before the agent has touched anything:

<!-- verify: touched -->
```
(none)
```

<!-- verify: stack -->
```
.tst/rules/api.md      (rules) [scoped inactive]
.tst/rules/style.md    (rules)
.tst/rules/ui.md       (rules) [scoped inactive]
```

After it opens `src/api/routes.py`:

<!-- verify: touched -->
```
src/api/routes.py
```

<!-- verify: stack -->
```
.tst/rules/api.md      (rules) [scoped]
.tst/rules/style.md    (rules)
.tst/rules/ui.md       (rules) [scoped inactive]
```

An inactive rule is still listed in the inspector, greyed, so "why did that rule not fire?"
is a glance rather than an investigation. It contributes nothing to the prompt and nothing to
the token count.

### Glob semantics

Patterns are matched against workspace-relative paths.

| Pattern | Matches | Does not match |
|---|---|---|
| `*.py` | `src/deep/thing.py` | `src/thing.pyi` |
| `Dockerfile` | `Dockerfile`, `infra/Dockerfile`, **`MyDockerfile`** | `Dockerfile.dev` |
| `src/api/*` | `src/api/routes.py` | `src/api/v1/routes.py` |
| `src/api/**` | `src/api/routes.py`, `src/api/v1/routes.py` | `src/api` itself |
| `ui/**/*.svelte` | `ui/App.svelte`, `ui/src/App.svelte` | `src/App.svelte` |
| `**` | everything | — |

A pattern with **no** `/` matches at any depth, the way `.gitignore` does. A pattern with a `/`
is matched against the whole relative path, where `*` stops at a separator and `**` crosses
them. `?` and `[abc]` / `[!abc]` character classes work too.

Note the bolded row. A bare name is matched as a **suffix**, not as a whole basename, so it
also matches filenames that merely *end* with it — `config.py` catches `oldconfig.py`:

<!-- verify: example basename-suffix -->
<!-- verify: file .tst/rules/config.md -->
````markdown
---
appliesTo: ["config.py"]
---
Config keys are sorted and documented in the same commit.
````

<!-- verify: touched -->
```
src/oldconfig.py
```

<!-- verify: stack -->
```
.tst/rules/config.md    (rules) [scoped]
```

Until that is fixed (§10) there is no bare-name form that anchors — `**/config.py` compiles to
the identical pattern. Write the path you actually mean (`src/config.py`), or accept the wider
net. Over-matching activates a rule you did not intend rather than dropping one you did, so it
costs you tokens, not correctness.

### Three ways a rule quietly fails to scope

**1. `appliesTo` outside `.tst/rules/`.** Scope is a rules-directory feature. Frontmatter is
*stripped* from every steering file, so a block at the top of an `AGENTS.md` never reaches the
model — but outside `.tst/rules/` that is all that happens to it. The `appliesTo` is dropped,
not honoured, and the file stays unconditionally on:

<!-- verify: example frontmatter-elsewhere -->
<!-- verify: file AGENTS.md -->
````markdown
---
appliesTo: ["src/**"]
---
Rebase, never merge.
````

<!-- verify: touched -->
```
(none)
```

<!-- verify: stack -->
```
AGENTS.md    (workspace) [appliesTo-ignored]
```

<!-- verify: block -->
````
<!-- from: <workspace>/AGENTS.md (workspace) -->
Rebase, never merge.
````

The delimiters are gone from the prompt, and there is no `scoped` flag — the session touched
nothing, and a rule scoped to `src/**` would have been `inactive` here. This one is not. The
inspector flags the file so the dropped scope is not itself silent:

```
appliesTo only scopes rules in .tst/rules/; it was stripped from this file and had no effect — see the steering authoring guide
```

Why stripped and not honoured: a steering file's *location* is already its scope. Root
`AGENTS.md` is the workspace-wide agreement, `src/api/AGENTS.md` is the agreement for that
subtree. Letting frontmatter re-scope a file on top of that would give one file two scoping
mechanisms that can disagree, and would let the workspace-wide working agreement vanish from a
turn because of which files the session happened to open first. Scope belongs in `.tst/rules/`,
which exists for exactly this and where a missing rule is the expected outcome rather than a
surprise.

**2. An empty or malformed `appliesTo`.** `appliesTo: []`, a string instead of a list, or YAML
that does not parse all degrade to *unscoped*, which means always on. That is the safe
direction, but it is not what you meant, and the inspector will show the rule with no scope
where you expected one.

**3. A pattern anchored where the paths are not.** Touched paths are workspace-relative and
never start with `./` or `/`. `/src/api/**` matches nothing.

### Rule names decide what the validator sees

The validator tier gets a subset of steering rather than all of it — the standards it needs to
review a diff, and nothing else. The subset is chosen by filename: `AGENTS.md`,
`standards*.md`, `conventions*.md`, and `style*.md`. A rule you want applied during review is
worth naming to match; one called `api-notes.md` will not be there. (The validator is
on-demand in v0.1, not scheduled automatically, so this shapes review turns you ask for rather
than every turn.)

---

## 5. Imports

A line consisting of nothing but `@` and a path inlines that file:

````markdown
@.tst/rules/testing.md
@~/notes/house-style.md
@/opt/team/shared.md
````

Relative paths resolve against the directory of the *importing* file, not the workspace root.
`~` expands to your home directory. Imported content carries its own provenance comment, so a
rule that came in through two levels of import still names its real source.

**Imports organise; they do not save tokens.** Imported content is inlined in full, on every
turn, exactly as if you had pasted it. If your goal is a smaller prompt, you want §4.

### The directive has to be the whole line

<!-- verify: example imports -->
<!-- verify: file style.md -->
````markdown
Two-space indents in YAML.
````

<!-- verify: file AGENTS.md -->
````markdown
# Conventions

@style.md

Everything else: see @style.md for the details.

- @style.md

@missing.md
````

<!-- verify: block -->
````
<!-- from: <workspace>/AGENTS.md (workspace) -->
# Conventions

<!-- from: <workspace>/style.md (imported) -->
Two-space indents in YAML.

Everything else: see @style.md for the details.

- @style.md

<!-- import file not found: <workspace>/missing.md -->
````

Three things to take from that. A mention mid-sentence is left alone, which is what lets you
write about imports without triggering them. A list item is *also* left alone — `- @style.md`
looks like an import and is not one, and this is the mistake people actually make. And a
missing file is a warning, not a failure: the session continues, the comment marks the hole,
and the issue is listed in the inspector.

<!-- verify: issues -->
```
import file not found: <workspace>/missing.md
```

### Code fences are not evaluated

<!-- verify: example fences -->
<!-- verify: file style.md -->
````markdown
Two-space indents in YAML.
````

<!-- verify: file AGENTS.md -->
`````markdown
Documenting the syntax, not using it:

```
@style.md
```

Using it:

@style.md
`````

<!-- verify: block -->
`````
<!-- from: <workspace>/AGENTS.md (workspace) -->
Documenting the syntax, not using it:

```
@style.md
```

Using it:

<!-- from: <workspace>/style.md (imported) -->
Two-space indents in YAML.
`````

The fence tracker is a simple toggle on lines starting with three backticks, so an *unclosed*
fence swallows every import after it. If an import stopped resolving for no apparent reason,
count your fences.

### Depth and cycles

Imports nest four deep. A fifth level is refused with the chain named, so you can see which
link to cut:

```
max import depth 4 exceeded: a.md -> b.md -> c.md -> d.md -> e.md
```

A cycle is caught wherever it closes, and reported the same way:

<!-- verify: example cycles -->
<!-- verify: file AGENTS.md -->
````markdown
@ring.md
````

<!-- verify: file ring.md -->
````markdown
Ring one.
@ring2.md
````

<!-- verify: file ring2.md -->
````markdown
Ring two.
@ring.md
````

<!-- verify: issues -->
```
import cycle detected: ring.md -> ring2.md -> ring.md
```

Neither is fatal. The offending import is replaced by a comment and the turn proceeds.

### Imports from outside the workspace need approval

Reading a file outside the workspace is an untrusted-file read, so the first one parks the turn
and asks you:

<!-- verify: example external -->
<!-- verify: file ~/notes/house-style.md -->
````markdown
Two-space indents in YAML.
````

<!-- verify: file AGENTS.md -->
````markdown
@~/notes/house-style.md
````

<!-- verify: pending -->
```
~/notes/house-style.md
```

<!-- verify: issues -->
```
external import awaiting approval: ~/notes/house-style.md
```

<!-- verify: block -->
````
<!-- from: <workspace>/AGENTS.md (workspace) -->
<!-- external import awaiting approval: ~/notes/house-style.md -->
````

Approve it and the path is written to `approved_external_imports` in the workspace's
`.tst/config.yaml`, so you are asked once per workspace rather than once per session — see
[`configuration.md` §4.4](configuration.md). Deny it and it is omitted for the rest of that
session only; the next session asks again. Approving one file can reveal external imports
nested inside it, so a deep chain asks more than once.

Two consequences that surprise people:

- **A missing external file is "not found", not "awaiting approval".** There is nothing to
  read, so there is nothing to approve, and no prompt appears.
- **Your global steering file's own imports are external.** `~/.tstdesk/AGENTS.md` lives
  outside every workspace, so `@shared.md` next to it resolves outside the workspace and is
  gated like any other outside read — once per workspace you open.

That second one is easy to hit and worth seeing. Nothing here is in the workspace at all:

<!-- verify: example global-import -->
<!-- verify: file ~/.tstdesk/AGENTS.md -->
````markdown
@shared.md
````

<!-- verify: file ~/.tstdesk/shared.md -->
````markdown
Prefer small, reviewable commits.
````

<!-- verify: pending -->
```
~/.tstdesk/shared.md
```

<!-- verify: issues -->
```
external import awaiting approval: ~/.tstdesk/shared.md
```

---

## 6. Why 200 lines

Steering rides in the prompt on every single turn, and long instruction files measurably lose
adherence: the middle of a long list is followed less reliably than a short one, and the effect
compounds as the conversation grows and the file sits further from the model's attention.
Target **under 200 lines per file** and push the detail into path-scoped rules, where it is
present when it matters and absent when it does not.

Past 200 lines, here is exactly what happens:

```
file exceeds 200 lines; long files measurably reduce adherence — see the steering authoring guide
```

That is a warning attached to the source in the instruction inspector. **Nothing is truncated,
nothing is dropped, and no turn fails.** The file loads in full and costs you in full. The
limit is advice with a nag attached, not a cap — TST Desk will not decide for you which of
your rules to discard.

Three details about how the count is taken:

- It is **lines, not tokens**. The inspector shows you the token count separately, per source
  and in total.
- **Exactly 200 lines does not warn.** 201 does.
- It is measured **after imports are inlined**. A twelve-line `AGENTS.md` that imports a
  300-line file warns, and it is right to: the prompt carries all 312 lines. This is the same
  reason §5 says imports do not save tokens.

When a file is too long, in order of what usually works:

1. Cut the paragraphs that restate what the model already does. "Write clean code" costs you
   tokens on every turn and changes nothing.
2. Move anything that only applies to part of the tree into `.tst/rules/` with `appliesTo`.
3. Move rarely-needed reference material — a long list of internal service names, a schema —
   out of steering entirely, and let the agent read the file when it needs it.
4. Only then reach for imports, and only for organisation.

---

## 7. Worked example: a small project

One CLI tool, one package, one contributor. One file is the whole steering surface, and there
is no reason for it to be more.

<!-- verify: example small-project -->
<!-- verify: file AGENTS.md -->
````markdown
# hoverfly — working agreement

A single-package Python 3.11 CLI. No framework, no plugins, no service.

## Conventions
- `ruff` for lint and format. `mypy --strict` clean before any commit.
- Errors raise. Nothing returns `None` to signal failure.
- One thing per function. Split a module before it passes 400 lines.

## Tests
- `pytest -q` green is part of "done", not a follow-up.
- A bug fix starts with the test that reproduces it.

## Ask first
- Adding a dependency.
- Changing the CLI's public flags.
- Anything under `scripts/release.sh`.
````

<!-- verify: stack -->
```
AGENTS.md    (workspace)
```

The steering block is that file verbatim behind a single provenance comment — see §2 for what
the concatenation looks like once there is more than one source.

What makes it work: every line is a thing the agent would otherwise get wrong, and the "ask
first" list is specific enough to act on. There is no `.tst/rules/` directory, no imports, and
no nested files. Resist adding them until a single file genuinely stops fitting.

---

## 8. Worked example: a large project

A monorepo — Python API, Svelte frontend, SQL migrations — where a single file would be a
thousand lines and most of it irrelevant to any given turn. The root file stays short and
universal; everything domain-specific becomes a scoped rule.

<!-- verify: example large-project -->
<!-- verify: file ~/.tstdesk/AGENTS.md -->
````markdown
Show me the plan before a multi-file change. I would rather redirect early.
````

<!-- verify: file AGENTS.md -->
````markdown
# monolith — working agreement

Three trees: `src/` (Python API), `ui/` (Svelte), `db/` (migrations).
Detailed rules live in `.tst/rules/` and load when you touch the tree they cover.

## Always
- Conventional commits. One logical change per commit.
- No secret in a file, a log, or a fixture. Ever.
- If the spec and this file disagree, the spec wins — say so, do not guess.
````

<!-- verify: file .tst/rules/00-standards.md -->
````markdown
Type hints on every public function, in every language that has them.
No dead code, no commented-out blocks, no debug prints left behind.
````

<!-- verify: file .tst/rules/10-python-api.md -->
````markdown
---
appliesTo: ["src/**/*.py", "tests/**/*.py"]
---
Every endpoint returns a Pydantic model, never a raw dict.
Every route needs a test in `tests/api/` before merge.
Async throughout the I/O path — no blocking call in the event loop.
````

<!-- verify: file .tst/rules/20-svelte.md -->
````markdown
---
appliesTo: ["ui/**/*.svelte", "ui/**/*.ts"]
---
Runes, not stores, inside components.
Design tokens only. No hardcoded colour or spacing.
````

<!-- verify: file .tst/rules/30-migrations.md -->
````markdown
---
appliesTo: ["db/migrations/**"]
---
Every migration is reversible and has a tested `down`.
Never edit a migration that has shipped. Add another one.
````

<!-- verify: file ui/AGENTS.md -->
````markdown
`npm run check` before you call a UI change done.
````

At the start of a session, nothing has been touched, so only the universal material is in the
prompt:

<!-- verify: touched -->
```
(none)
```

<!-- verify: stack -->
```
~/.tstdesk/AGENTS.md           (user global)
AGENTS.md                      (workspace)
.tst/rules/00-standards.md     (rules)
.tst/rules/10-python-api.md    (rules) [scoped inactive]
.tst/rules/20-svelte.md        (rules) [scoped inactive]
.tst/rules/30-migrations.md    (rules) [scoped inactive]
ui/AGENTS.md                   (nested: ui)
```

The agent then works across the API and the frontend, and two rules switch on. The migration
rules stay out of the prompt, because the session never went near `db/`:

<!-- verify: touched -->
```
src/api/routes.py
ui/src/App.svelte
```

<!-- verify: stack -->
```
~/.tstdesk/AGENTS.md           (user global)
AGENTS.md                      (workspace)
.tst/rules/00-standards.md     (rules)
.tst/rules/10-python-api.md    (rules) [scoped]
.tst/rules/20-svelte.md        (rules) [scoped]
.tst/rules/30-migrations.md    (rules) [scoped inactive]
ui/AGENTS.md                   (nested: ui)
```

Four choices in there are worth copying:

- **The root file is short and universal.** Anything that only applies to one tree was moved
  out. It is the file that can never be scoped, so it is the one to keep lean.
- **Rules are numbered.** `00-`, `10-`, `20-` fixes the order, which byte-order sorting would
  otherwise decide for you.
- **`00-standards.md` is deliberately unscoped**, because it applies everywhere — and its name
  puts it in the validator's subset, so review sees it too.
- **`ui/AGENTS.md` is one line.** A nested file loads on every turn regardless of what the
  session touches, so anything sizeable that belongs to one tree belongs in a scoped rule
  instead. Use nested files for the short, standing fact about a directory.

The scoped rules cost nothing until they are relevant. That is the whole point of the
mechanism, and on a repo this size it is the difference between a steering block you can afford
on every turn and one you cannot.

---

## 9. Coming from another tool

**Nothing needs porting.** Point TST Desk at a repo already set up for Claude Code, Cursor, or
anything else that reads a markdown instruction file, and the parts it understands work on the
first run.

| You have | What happens |
|---|---|
| `CLAUDE.md` at the repo root | Read as the workspace steering file. Nothing to rename |
| `CLAUDE.md` in subdirectories | Read as nested steering for that subtree |
| `~/.claude/CLAUDE.md` | Read as your user-global file |
| `@path` imports in any of them | Resolved the same way, max depth 4 |
| Both `AGENTS.md` and `CLAUDE.md` | `AGENTS.md` wins; the other is shown as shadowed |

The §3 example above is exactly this case, run for real: a workspace with no `AGENTS.md`
anywhere still resolves a full three-level stack.

What does **not** carry over, and what to do about it:

- **`.cursor/rules/`, `.github/copilot-instructions.md`, and similar tool-specific
  directories** are not read. Copy the content into `.tst/rules/` — and if the originals had
  path scoping, this is where you get it back, with `appliesTo`.
- **A `.claude/` directory inside the workspace** is skipped by the nested walk. Only
  `~/.claude/CLAUDE.md` at your home directory is read. Move anything you need out of it.
- **MCP server definitions, hooks, slash commands, and subagent files** are configuration for
  another product, not steering. They are ignored.
- **Frontmatter conventions from other tools** are stripped, not interpreted. If your
  `CLAUDE.md` opens with a YAML block — `description`, `globs`, `alwaysApply`, or anything else
  — it is removed before the file is assembled, so it never reaches the model and you have
  nothing to clean up. What it *meant* does not carry over: only `.tst/rules/` scopes, and only
  through `appliesTo`. A file whose frontmatter scoped it in your old tool arrives here
  unconditional, and the inspector flags it if it carried an `appliesTo` — see §4.

There is no import step and no migration command. When you are ready to commit to the open
name, rename `CLAUDE.md` to `AGENTS.md`; until then, both work, and you can keep one repo
serving both tools indefinitely.

---

## 10. Sharp edges

Writing this guide against the running assembler surfaced three places where the behaviour is
not what an author would predict. One has since been fixed and the example above now
demonstrates the corrected behaviour; the rest are reported as defects, are not worked around
here, and every one of them is demonstrated by a live example above rather than asserted.

- **A bare-name `appliesTo` pattern matches a filename suffix, not a basename.** `config.py`
  activates on `oldconfig.py`, and `**/config.py` compiles to the same thing, so there is no
  anchored spelling. See §4. The direction of the error is safe — a rule loads when it need
  not have — but a scoped rule can be quietly wider than its name suggests.
- **`appliesTo` outside `.tst/rules/` is neither honoured nor stripped.** *Fixed.* Frontmatter
  is stripped at every level now, so nothing reaches the model. It is still not honoured
  outside `.tst/rules/` — that half is deliberate, and flagged rather than silent. See §4.
- **Your user-global steering file's own imports are treated as external.**
  `~/.tstdesk/AGENTS.md` sits outside every workspace, so `@shared.md` beside it triggers the
  untrusted-read approval, once per workspace you ever open. See §5. It is defensible — the
  gate is about where the file lives, not who wrote it — but it makes splitting your personal
  steering across files more friction than it looks.

---

## 11. How this guide is kept honest

`core/tests/test_docs_steering_guide.py` runs on every suite run:

1. Every example is materialised as a real workspace and home directory and assembled by the
   real `ContextAssembler`. The stacks, prompt blocks, import issues and pending approvals
   printed above are compared to its output exactly.
2. The 200-line warning, the dropped-`appliesTo` warning and the import-depth message are
   quoted from strings the test provokes out of the assembler, so rewording any of them in code
   fails here until this page catches up. Every warning the assembler can raise must also have
   a flag named in this guide before a stack line can render it.
3. Every precedence level, every directory skipped by the nested walk, and every pattern in the
   validator's default subset must appear in this text. Adding one fails the suite until it is
   documented.
4. The claim in §6 that nothing is truncated past the limit is checked by assembling an
   over-limit file and looking for its last line in the block.
