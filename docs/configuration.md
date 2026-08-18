# Configuration reference

Everything TST Desk reads from disk, what each key does, and what happens when you leave it
out. Two files matter: `config.yaml` chooses your models, `.tst/config.yaml` draws the wall
around a workspace.

Every YAML example below is loaded through the real loaders by
`core/tests/test_docs_config_reference.py`. An example that stopped validating would fail the
suite rather than quietly mislead you.

---

## 1. The three files, and which one to edit

| File | Who owns it | Edit it? |
|---|---|---|
| `tstd/config.yaml` inside the installed package | The release | **No.** It is packaged in the wheel and replaced wholesale on upgrade. |
| `config.yaml` in your user data directory | You | **Yes.** This is the one the daemon loads. |
| `.tst/config.yaml` in a workspace | You, per project | **Yes.** Git-tracked if you want the team to share it. |

The packaged file is a seed, not a setting. On first load, if your user copy is missing, it is
copied there verbatim — comments and all — and from then on nothing the daemon reads comes from
the packaged file. Two consequences worth knowing:

- Editing the packaged copy changes nothing, and your edit is lost at the next upgrade.
- Your copy is never overwritten, so presets or keys added in a later release do **not** appear
  in it. To see what shipped most recently, compare against the packaged file.

**Where your copy lives:**

| Platform | Path |
|---|---|
| macOS | `~/Library/Application Support/com.thatsimpletech.tstdesk/config.yaml` |
| Windows | `%APPDATA%\com.thatsimpletech.tstdesk\config.yaml` |
| Linux / BSD | `$XDG_DATA_HOME/tst-desk/config.yaml`, or `~/.local/share/tst-desk/config.yaml` |

There is no dedicated environment variable for this path and no CLI flag for it. On Linux and
Windows it moves with the platform's own data-directory variable, as the table shows; on macOS
the path is fixed. Note that the daemon's `--data-dir` moves the session store and the audit
database but does **not** move this file.

**Unknown keys are ignored, in both files.** A misspelled key is not an error — it is dropped,
and the default applies. `writeable_paths` (with the extra `e`) leaves you with the default
`["**"]` and no warning. Check spelling against the tables below when a setting seems to have
no effect.

---

## 2. `config.yaml` — the model plane

### Top-level keys

| Key | Type | Default | Effect |
|---|---|---|---|
| `presets` | mapping of name → preset | *required* | The named model stacks you can switch between. Any name is legal; the shipped file declares `tst-default`, `budget`, and `local`. |
| `active_preset` | string | `tst-default` | Which preset is in force. Naming a preset that is not declared is a load error. |

### A preset

Every preset declares all three tiers. Omitting one is a load error naming it.

| Key | Type | Effect |
|---|---|---|
| `brain` | tier | Planning turns. The router opens every session here for the first two turns, and escalates back to it after three consecutive worker failures. |
| `worker` | tier | Token-heavy execution — edits, and the tool-call classifier. Where turns land once the lead turns are spent. |
| `validator` | tier | Reviewing diffs and tests. **Not scheduled automatically in v0.1** — it is on-demand only, so its prices rarely move your bill today. |

Routing is turn-count and failure driven; it does not read anything from this file beyond the
three tier definitions. You can pin a tier for a session from the title bar.

### A tier

| Key | Type | Default | Effect |
|---|---|---|---|
| `slug` | string, non-empty | none | The model identifier sent as `model` on every request. Optional **only** when `base_url` is on loopback — see §3.4. `slug:` with no value means the same as leaving it out; `slug: ""` is an error. |
| `base_url` | string | *required* | The OpenAI-compatible endpoint. Requests go to `{base_url}/chat/completions`. Also decides credentials: a loopback URL is called with no `Authorization` header at all, anything else is called with your stored key. |
| `input_price` | float ≥ 0 | *required* | Dollars per **million** prompt tokens that were not served from cache. |
| `output_price` | float ≥ 0 | *required* | Dollars per **million** completion tokens. |
| `cache_read_price` | float ≥ 0 | *required* | Dollars per **million** prompt tokens served from cache. |
| `context_window` | int > 0 | *required* | The compaction budget — see below. |
| `max_output_tokens` | int > 0 | *required* | The answer reservation subtracted from `context_window` — see below. |

**Prices are per million tokens, and they are yours to keep accurate.** They drive the live
cost meter, the audit trail, and the spend cap. Nothing verifies them against your provider, so
a wrong number here buys you a wrong cap, not an error. A preset priced at zero (the `local`
preset) honestly reports a cost of zero.

**`context_window` and `max_output_tokens` are a compaction budget, not request parameters.**
Neither is sent to the model. Together they set the point at which the conversation is
compacted: at 80% of `context_window - max_output_tokens`, the oldest messages are replaced by
an extractive summary, keeping the system message and the two most recent user turns. No model
call is spent doing it. So:

- `context_window` must be no larger than what the *server* actually serves. A local server
  usually caps context well below the model's architectural ceiling; setting the ceiling here
  means compaction never fires and the server silently truncates instead — and what it drops
  first is your steering block.
- `max_output_tokens` does **not** cap generation. It only reserves room for the answer inside
  the budget. Your provider's own default output limit is what actually applies.

---

## 3. Swapping models

### 3.1 Verify the slug before you trust it

**The model landscape moves weekly.** Slugs are renamed, retired, and re-pointed; prices change
without notice. This document deliberately does not restate any of them, because a slug written
into prose is stale by the next release and there would then be two places to fix.

The shipped `tstd/config.yaml` is the current source for what we ship and what it costs. Before
putting a slug in your config, check it against the provider's own model list — OpenRouter's
`/api/v1/models`, or your endpoint's `/v1/models` — and copy the price from the same page. A
slug that does not exist fails at the first turn that needs it, not at load.

### 3.2 Swap one tier

Replace the tier's block — these keys sit under `brain:`, `worker:`, or `validator:` inside a
preset. Everything in the block moves together: a new model almost always means new prices and
a new context window, and leaving the old prices behind is how a cost meter starts lying.

<!-- verify: tier -->
```yaml
slug: your-provider/your-model     # verified at the provider, not copied from here
base_url: https://openrouter.ai/api/v1
input_price: 1.00                  # $ per million — your provider's real number
output_price: 3.00
cache_read_price: 0.10
context_window: 128000
max_output_tokens: 8192
```

The settings screen edits tier slugs for you and writes them back to this file, preserving its
comments. It does not edit prices; those are a hand edit.

### 3.3 Add your own preset

A preset is just a name you pick. This is a complete, valid `config.yaml` that declares one
preset of your own and nothing else:

<!-- verify: model -->
```yaml
presets:
  my-mix:
    brain:
      slug: your-provider/planning-model
      base_url: https://openrouter.ai/api/v1
      input_price: 2.00
      output_price: 8.00
      cache_read_price: 0.20
      context_window: 400000
      max_output_tokens: 8192
    worker:
      slug: your-provider/fast-model
      base_url: https://openrouter.ai/api/v1
      input_price: 0.10
      output_price: 0.20
      cache_read_price: 0.10
      context_window: 128000
      max_output_tokens: 16384
    validator:
      slug: your-provider/review-model
      base_url: https://openrouter.ai/api/v1
      input_price: 0.50
      output_price: 1.00
      cache_read_price: 0.50
      context_window: 128000
      max_output_tokens: 8192
active_preset: my-mix
```

In practice you add the `my-mix:` block alongside the shipped presets and change
`active_preset`. The first-run wizard is the only part of the UI that writes `active_preset`;
after onboarding, change it by hand here.

**When an edit takes effect.** The model config is read once at daemon start. Changing a preset
or a tier slug from the UI reloads the file immediately; a hand edit does not, so quit and
reopen the app to pick it up. Either way, sessions already open keep the tiers they started
with — only new sessions route on the change.

### 3.4 Local models, and when `slug` may be omitted

`slug` is optional on a loopback `base_url` (`127.0.0.0/8`, `::1`, or `localhost`) and only
there. The endpoint is a safe convention — Ollama, vLLM, LM Studio and llama.cpp all serve an
OpenAI-compatible API — but the model tag belongs to your machine, so no default would be right
for everyone.

<!-- verify: tier -->
```yaml
# slug omitted: resolved from the endpoint's /v1/models on the first turn that needs it
base_url: http://127.0.0.1:11434/v1
input_price: 0.0
output_price: 0.0
cache_read_price: 0.0
context_window: 32768
max_output_tokens: 4096
```

What that buys you, and what it costs you:

- Resolution happens on the **first turn that needs the tier** — not at startup. A model server
  that is not running yet fails that turn, the same way a missing key does. Start it and send
  the next message.
- **Exactly one served model** resolves. Zero or several is refused by name, listing every id it
  saw, because `/v1/models` lists embedding models beside chat models and guessing would bind
  the agent to one that cannot answer.
- Nothing is written back. Swap the model on the server and the next run picks it up.
- A `slug` you write always wins and suppresses the request entirely. That is the fix for a
  server hosting more than one model.
- No API key is read or sent for a loopback endpoint. A preset whose tiers are all loopback
  needs no key at all; one off-box tier and the workspace needs one again.

An **off-box** endpoint with no slug is a load error, because discovery would never run to fill
it in:

<!-- verify: tier-invalid -->
```yaml
base_url: https://openrouter.ai/api/v1
input_price: 1.00
output_price: 3.00
cache_read_price: 0.10
context_window: 128000
max_output_tokens: 8192
```

```
Invalid model configuration in .../config.yaml: presets.<preset>.<tier>: Value error,
slug is required for the off-box endpoint https://openrouter.ai/api/v1;
only a loopback endpoint discovers its model from /v1/models
```

---

## 4. `.tst/config.yaml` — the workspace wall

Lives at `<workspace>/.tst/config.yaml`. A freshly opened workspace gets a fully commented
template whose values are the defaults, so a scaffolded file changes nothing until you edit it.

**An absent file means the defaults, not an error.** So does an empty or comment-only one. The
file is read when a workspace opens and again when a paused session resumes, which is what lets
you raise a cap and continue without restarting.

Five top-level sections live here, each read by a different part of the daemon: `boundary`,
`caps`, `attachments`, `policy`, and `approved_external_imports`.

### 4.1 `boundary` — where the agent may act

| Key | Type | Default | Effect |
|---|---|---|---|
| `writable_paths` | list of glob strings | `["**"]` | Workspace-relative globs the agent may **write** to. A write outside them is refused as `outside_writable_paths` and classified Class C. |
| `allowed_commands` | list of strings | `[]` | Allowlist for the shell tool, matched on the basename of the resolved binary. **Empty or omitted means any command** — the list narrows a path the classifier and approval gate already guard (TD-606). |
| `network` | `deny` or a list of hosts | `deny` | Hosts the agent may reach. Any other string is a load error. Declarative in v0.1 — see §4.6. |

**Glob semantics for `writable_paths`.** Patterns are relative to the workspace root, and the
target is resolved (symlinks followed) before matching:

- A pattern with **no** `/` matches a **basename at any depth**: `*.py` matches
  `src/deep/thing.py`.
- A pattern with a `/` is matched segment by segment, and `**` spans **zero or more** segments.
  So `src/**` matches `src/main.rs`, `src/a/b/c.rs`, and `src` itself.
- `**` alone matches everything, which is the default.

**What `writable_paths` does not cover.** It governs writes by the filesystem tools only.
Reads are walled at the workspace edge instead — anything inside the workspace is readable
regardless of this list. And the shell tool declares no path arguments, so it is confined by its
working directory and `allowed_commands`, not by these globs. A command that writes outside
them is not stopped here.

Steering files (`AGENTS.md`, `CLAUDE.md`, `.tst/rules/**`) are refused for writing no matter
what you put in this list. That is enforced in the tool, not by configuration.

**`allowed_commands` matching.** The command is split into top-level segments on pipes,
semicolons, and the like; each segment's leading binary — after skipping `VAR=value` prefixes —
is resolved with `which` and matched on the basename, so `git` and `/usr/bin/git` both match
`git`. Only the leading binary of each segment is checked, so `sh`, `sudo`, and `env` match as
themselves: list them deliberately, because listing `sh` allows anything `sh -c` can run.
Backticks are refused outright, and a binary that cannot be resolved is refused fail-closed.
This is a policy rail, not a sandbox.

### 4.2 `caps` — when the agent stops and asks

| Key | Type | Default | Effect |
|---|---|---|---|
| `spend_usd` | float ≥ 0 | `25.0` | Session spend, in dollars, that pauses the run. |
| `wall_clock_hours` | float ≥ 0 | `8.0` | Session runtime, in hours, that pauses the run. |
| `max_iterations` | int ≥ 1 | `200` | Loop iterations that pause the run. |

All three are checked **before every model call**, in that order, and all three trigger at or
past the value rather than strictly past it. `spend_usd: 0` is legal and pauses before the first
call.

Hitting a cap **pauses**; it does not kill the session. The session moves to `paused` with the
violation as its reason, the UI raises a card, and the run waits — indefinitely, on an event, not
a poll. Resuming re-reads this file and re-checks the caps, so resuming without raising the cap
pauses again immediately. That is the loop: raise the number here, then resume.

### 4.3 `attachments` — what a message may carry

| Key | Type | Default | Effect |
|---|---|---|---|
| `max_file_bytes` | int ≥ 1 | `256000` | Size of one attached file. Measured on the file's real bytes, after decoding. |
| `max_total_bytes` | int ≥ 1 | `512000` | Size of all the attachments on one message, added together. |
| `max_count` | int ≥ 1 | `10` | Files on one message. |

**Text files only.** Anything that is not valid UTF-8, or that contains a NUL byte, is refused
as binary — a PNG, a PDF, a compiled binary, and a UTF-16 text file all land here. Images are
deliberately out: vision support depends on which models you have chosen and needs capability
detection first.

**These are enforced by the daemon, not by the composer.** The message-box refuses an oversize
or binary file early, with copy that names the file, but that is a convenience. The same
refusal happens again on arrival, so a client that skips the check — an older build, or
anything that is not the app — gets the same answer. Both sides read the numbers above; the
daemon sends them to the UI on the `boundary_update` event when the workspace opens.

**A refusal drops the whole message**, including the text you typed alongside it. Nothing is
half-sent, and the error says so. Attachments are also all-or-nothing within one message: one
bad file refuses the send rather than quietly delivering the rest.

**There is a ceiling above these numbers.** The daemon's local websocket accepts frames up to
1 MiB, and base64 adds a third to whatever you attach — so a `max_total_bytes` much above
`700000` gives you a message the transport drops before the daemon can refuse it politely.
Keep the total under that and the failure modes stay legible.

### 4.4 `policy` — what needs your approval

| Key | Type | Default | Effect |
|---|---|---|---|
| `rules` | list of rules | `[]` | Per-tool overrides. The most specific match wins; an exact tie breaks toward the most restrictive effect. |
| `class_c_default` | `ask` or `never` | `ask` | What happens to a Class C call that no rule matches. `never` refuses it outright instead of asking. |
| `approval_timeout_seconds` | float ≥ 0, or null | `null` | How long an approval request waits before being treated as a denial. Null waits indefinitely. |

A rule has three keys:

| Key | Type | Default | Effect |
|---|---|---|---|
| `tool` | string or glob | *required* | Tool name, or a glob such as `fs_*`. |
| `args` | glob | `"**"` | Matched against a summary of the call's arguments. The default matches every call of the tool. |
| `effect` | `auto`, `ask`, or `never` | *required* | Run without asking, ask first, or refuse. |

**No rule can make a Class C call run silently.** An `effect: auto` rule that matches a Class C
call is downgraded to `class_c_default` — you can loosen approvals for ordinary work, but the
riskiest calls always come back to you (or are refused). "Always allow this in this workspace"
in the approval card writes a rule here for you.

**Skip all approvals** is not a key in this file. Settings → Policy has a machine-wide toggle
that lives in the user data dir as `approvals.yaml`, so a clone cannot carry it. While it is
on, a Class B call that would have asked runs as auto. Class C still parks or refuses. A
`never` rule still refuses. The classifier still runs. `effect: yolo` is not a valid rule.

### 4.5 `approved_external_imports`

| Key | Type | Default | Effect |
|---|---|---|---|
| `approved_external_imports` | list of path strings | `[]` | Steering imports from outside the workspace that you have already approved. `~` is expanded and each path resolved. |

Top-level, not nested under `boundary`. You do not normally hand-write it — approving an
external import in the UI appends to it.

### 4.6 Two gaps to know about

Documenting this file surfaced two places where the shipped comments promise more than the code
delivers. Both are reported as defects; neither is fixed here.

- ~~**`allowed_commands: []` refuses every command.**~~ **Fixed in TD-606.** The daemon passed
  the empty list to the shell tool as a configured-but-empty allowlist, so `echo` was rejected
  with `'echo' (resolved to 'echo') is not in allowed_commands []` — in any workspace with no
  `.tst/config.yaml`, which is the default state. Empty now means what the template always said
  it meant: any command. The allowlist narrows a path that is already guarded, since no rule in
  the classifier's table matches on tool name, so a shell call defaults to class B and the user
  answers for it.
- **`network` is declared but not enforced.** No tool that ships in v0.1 reaches the network, so
  the host allowlist has nothing to gate; it is carried into the boundary display and the
  classifier, and nothing rejects a host today. `deny` does not stop `curl` — shell egress is
  governed by `allowed_commands` alone.

One more sharp edge that is behaviour rather than a defect: saving a policy rule from the UI
rewrites this file through a YAML dump, which **discards your comments**. Your `boundary:` and
`caps:` values survive; the prose around them does not. The model `config.yaml` does not have
this problem — it is edited surgically, line by line.

### 4.7 Worked examples

**A tight wall on a Rust project.** Writes confined to three trees, three binaries allowed,
default caps left alone:

<!-- verify: workspace -->
```yaml
boundary:
  writable_paths:
    - "src/**"
    - "tests/**"
    - "examples/**"
  allowed_commands: ["cargo", "git", "nix"]
  network: deny
```

**A short leash for an unfamiliar task.** Two dollars, half an hour, forty iterations — the run
pauses at whichever comes first, and you decide then whether to extend it:

<!-- verify: workspace -->
```yaml
caps:
  spend_usd: 2.0
  wall_clock_hours: 0.5
  max_iterations: 40
```

**Naming hosts instead of denying all.** Note §4.6 — this is a declaration today, not a control:

<!-- verify: workspace -->
```yaml
boundary:
  network:
    - api.github.com
    - registry.npmjs.org
```

**Tighter attachments on a repo full of large generated files.** One small text file per
message, so a stray drag-and-drop of a build artifact is refused at the composer instead of
being paid for as prompt tokens:

<!-- verify: workspace -->
```yaml
attachments:
  max_file_bytes: 40000
  max_total_bytes: 40000
  max_count: 1
```

**Loosening approvals on a workspace you trust.** Reads never ask, `git status` never asks,
anything else Class C still does, and an unanswered request denies after five minutes:

<!-- verify: workspace -->
```yaml
policy:
  class_c_default: ask
  approval_timeout_seconds: 300.0
  rules:
    - tool: fs_read
      args: "**"
      effect: auto
    - tool: shell
      args: "git status*"
      effect: auto
approved_external_imports:
  - ~/notes/house-style.md
```

**A whole file, sections combined.** Nothing stops you from setting all four at once:

<!-- verify: workspace -->
```yaml
boundary:
  writable_paths: ["src/**", "docs/**"]
  allowed_commands: ["git", "uv", "python3"]
  network: deny
caps:
  spend_usd: 10.0
  wall_clock_hours: 4.0
  max_iterations: 120
policy:
  class_c_default: never
approved_external_imports: []
```

`network` accepts `deny` or a list, and nothing else. This is a load error:

<!-- verify: workspace-invalid -->
```yaml
boundary:
  network: allow
```

```
Invalid boundary config in .../.tst/config.yaml: boundary.network: Value error,
network must be 'deny' or a list of allowed hosts
```

An empty list, `network: []`, is legal and means the same thing as `deny`.

---

## 5. When a config is wrong

Every loader fails loudly and names the offending key. Nothing falls back to a "safe" default on
a malformed file — a config error stops the load rather than running you on settings you did not
write.

| Mistake | What you get |
|---|---|
| Invalid YAML | `Invalid YAML in <path>: <parser message with line and column>` |
| Top level is not a mapping | `<path> must contain a mapping at the top level` |
| `active_preset` names a preset that is not declared | `active_preset 'x' not found in presets [...]` |
| A tier is missing from a preset | `presets.<name>.<tier>: Field required` |
| A price is negative, or a window is zero | The key, and the constraint it broke |
| Off-box tier with no `slug` | The message in §3.4, naming the endpoint |
| `network` is any string but `deny` | The message in §4.7 |
| A misspelled key | **Nothing.** It is ignored and the default applies. |

Model configuration errors surface at load, which means at daemon start. Workspace boundary
errors surface when the workspace opens; on *resume* specifically, an invalid file is logged and
the previous caps are kept rather than dropping the wall.

---

## 6. How this document is kept honest

`core/tests/test_docs_config_reference.py` runs on every suite run and enforces four things:

1. Every YAML example here is loaded through the real loader — `load_config` for model examples,
   the workspace loaders for `.tst/config.yaml` examples — and must validate. Examples shown as
   mistakes must actually be rejected.
2. Every key used in an example must exist in the schema. Unknown keys are ignored at load, so
   without this check a stale example would keep passing while documenting nothing.
3. Every field of every configuration model must appear in this document. Adding a key to the
   schema fails the suite until it is documented here.
4. No slug from the shipped `config.yaml` may appear in this text, so §3.1's advice cannot decay
   into a stale list.
