# Configuration reference

Everything TST Desk reads from disk, what each key does, and what happens when you leave it
out. Two YAML files matter: `config.yaml` chooses your models, `.tst/config.yaml` draws the
wall around a workspace. Memory is a third tree of markdown beside the wall, not a key in
either YAML file — see §5.

Every YAML example below is loaded through the real loaders by
`core/tests/test_docs_config_reference.py`. An example that stopped validating would fail the
suite rather than quietly mislead you.

---

## 1. The files, and which one to edit

| File | Who owns it | Edit it? |
|---|---|---|
| `tstd/config.yaml` inside the installed package | The release | **No.** It is packaged in the wheel and replaced wholesale on upgrade. |
| `config.yaml` in your user data directory | You | **Yes.** This is the one the daemon loads. |
| `.tst/config.yaml` in a workspace | You, per project | **Yes.** Git-tracked if you want the team to share it. |
| `.tst/memory/*.md` in a workspace | Distill writes; you correct | **Yes.** Git-tracked on purpose. Standing rules stay in `AGENTS.md`. |
| `.tst/autonomy/CHARTER.md` | You, from the Charter column | **Yes.** Human path only — the agent cannot write it. Spec §12.4. |

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
| `presets` | mapping of name → preset | *required* | The named model stacks you can switch between. Any name is legal; the shipped file declares `tst-default`, `budget`, `local`, and `vllm`. |
| `active_preset` | string | `tst-default` | Which preset is in force. Naming a preset that is not declared is a load error. |
| `search` | mapping | see below | Destination for the `web_search` tool. Omitted in an older user copy is filled from the shipped file at load. |
| `embeddings` | mapping | see below | Local embeddings sidecar for memory ranking. Omitted in an older user copy is filled from the shipped file at load. Empty `base_url` disables the client. Empty `command` is attach-only — the host never spawns on `base_url` alone. |
| `computer_use` | mapping | see below | Desktop computer-use sidecar. Omitted in an older user copy is filled from the shipped file at load. Empty `command` is mock-only — the daemon never spawns `mcp/tst-cu-mcp`. Empty `grounding.base_url` leaves click targeting on the intended (x, y). |
| `session` | mapping | see below | On-disk session event-log window. Omitted in an older user copy is filled from the shipped file at load. Zero is invalid, not unbounded. |
| `remote` | mapping | see below | Opt-in Tailscale bind. Omitted in an older user copy is filled from the shipped file at load. Empty `bind` is loopback only. |
| `notify` | mapping | see below | Outbound notification channels. Omitted in an older user copy is filled from the shipped file at load. Slack and ntfy are off until their `enabled` flag is true and a destination URL is stored in the OS keychain. |
| `autonomy` | mapping | see below | Rootless container used only for autonomous runs (TD-4301). Interactive sessions ignore this block. Omitted in an older user copy is filled from the shipped file at load. A missing or rootful runtime refuses start with install copy. |

### `search`

The agent's `web_search` tool. The query is sent to `base_url` as `?q=...`. The
host is configuration, never a literal in Python (the no-telemetry test
enumerates those). An empty `base_url` disables the tool with copy that names
this key.

| Key | Type | Default | Effect |
|---|---|---|---|
| `base_url` | string | *shipped* | The search endpoint. Empty string disables `web_search`. |
| `timeout_seconds` | float > 0 | `15` | How long a search or fetch request may run. |
| `max_results` | int 1–20 | `8` | Default hit count when the tool call omits `max_results`. |
| `fetch_max_bytes` | int ≥ 1 | `200000` | Cap on a `web_fetch` body. |

### `embeddings`

Memory ranking (TD-2202). The client sends OpenAI `POST /v1/embeddings` to
`base_url`. The host is configuration, never a Python literal. Empty
`base_url` disables the client; a down loopback endpoint falls back to
heading-match (TD-2201) and does not fail the turn. Do not point this at
Ollama `/api/embed`. The Tauri host spawns a sidecar only when `command`
is set (TD-2204). A filled `base_url` with no `command` is attach-only.

| Key | Type | Default | Effect |
|---|---|---|---|
| `base_url` | string | *shipped* | Embeddings endpoint, including the `/v1` suffix. Empty string disables. |
| `model` | string | *shipped* | Embeddings model slug. Empty disables even when `base_url` is set. |
| `timeout_seconds` | float > 0 | `2` | How long a probe may run before heading-match takes over. |
| `top_k` | int ≥ 1 | `4` | Maximum topic files kept after `MEMORY.md` when the sidecar answers. |
| `token_budget` | int ≥ 1 | `2000` | Cap on loaded memory tokens (TD-506 heuristic, file bytes). Lowest-ranked topics drop until under the cap; `MEMORY.md` is the last file dropped. |
| `command` | string or list | *empty* | Host-only argv for the embeddings binary. A string is split on whitespace; a list is used as-is (numbers become strings). Empty or omitted means attach-only — the host never tries to spawn. Sidecar death is logged and does not restart `tstd`. |

### `computer_use`

Desktop screenshot / move / click / type / scroll (TD-3301). Empty
`command` is the in-process mock (CI, no display). A non-empty value is
the argv for `mcp/tst-cu-mcp` over stdio — the daemon owns the child and
does not bind a socket. Linux has no live path (E20).

Browser computer-use (TD-1710): `browser` selects the in-process mock
(CI, never launches Chrome) or Playwright with a persistent profile
under the user data dir. Playwright missing always falls back to mock.

| Key | Type | Default | Effect |
|---|---|---|---|
| `command` | string or list | *empty* | Argv for the computer-use MCP sidecar. A string is split with the shell; a list is used as-is. Empty or omitted is mock-only. |
| `browser` | `mock` or `playwright` | `mock` | Browser driver. `mock` never launches Chrome. `playwright` uses a persistent profile under the user data dir when Playwright is installed; otherwise the mock. |
| `grounding` | mapping | see below | Local vision model for click targeting (TD-3902). |
| `local_worker_preset` | string | `vllm` | After this session has used a `desktop_` or `browser_` tool, the worker *client* uses that named preset's worker tier (loopback URL + optional slug). Brain stays on the active preset. Empty never remaps. The name is a preset key, not a model slug. Lead-turns, `set_tier`, and escalation are unchanged. |

#### `computer_use.grounding`

UI-TARS (or any OpenAI-compatible vision chat) on loopback. Empty
`base_url` is off: `desktop_click` uses the intended (x, y) — the
TD-3304 path. A down endpoint, a missing model, or an unreadable
reply also falls back to that point; the click does not fail because
grounding missed. Off-box URLs are a load error. Cost on loopback is
zero. Latency is recorded on the tool result.

The URL may be the same `vllm` loopback as the tiers (`:8000/v1`) or a
dedicated sidecar. Optional `slug` is discovered from `/v1/models`
when omitted, same as a loopback tier (TD-1805).

| Key | Type | Default | Effect |
|---|---|---|---|
| `base_url` | string | *empty* | OpenAI-compatible endpoint, including the `/v1` suffix. Empty disables. Must be loopback when set. |
| `slug` | string or omitted | *omitted* | Model id. Omitted discovers the single model the endpoint serves. Required only if the server lists several. |
| `timeout_seconds` | float > 0 | `8` | How long a locate request may run before the intended point is used. |

### `notify`

Outbound notification channels (TD-3801, TD-3802). Each channel is a
standalone `send(config, message)` — Slack first, ntfy optional, no
20-platform gateway (spec §8). Discord/Telegram are TD-4707. Off by
default. Destination URLs are keychain secrets (`tst-slack-webhook`,
`tst-ntfy-topic`), never this file, never the audit log. `host` is the
only host a notifier may reach; the keychain URL's host must match it
or the send is dropped.

| Key | Type | Default | Effect |
|---|---|---|---|
| `slack` | mapping | see below | Slack incoming webhook. |
| `ntfy` | mapping | see below | ntfy topic POST. |

#### `notify.slack`

| Key | Type | Default | Effect |
|---|---|---|---|
| `enabled` | bool | `false` | When false, approval-needed and turn-complete never POST. |
| `host` | string | *empty* | Allowed destination hostname. Empty disables even if `enabled` is true. |
| `timeout_seconds` | float > 0 | `5` | How long a webhook POST may run. Failures are logged and never fail the turn. |

#### `notify.ntfy`

| Key | Type | Default | Effect |
|---|---|---|---|
| `enabled` | bool | `false` | When false, approval-needed and turn-complete never POST. |
| `host` | string | *empty* | Allowed destination hostname. Empty disables even if `enabled` is true. Typical public instance is ntfy.sh. |
| `timeout_seconds` | float > 0 | `5` | How long a topic POST may run. Failures are logged and never fail the turn. |

`project_context` is the pinned-file budget on the brain prompt (TD-2805).
Newest pins drop first when over `token_budget`.

### `session`

The durable event log each session writes under the data dir (TD-2901).
Attach replays this file. The cap is an event count — a byte cap would
drop a different prefix than `from_seq`. Zero is a load error, not
"keep forever"; omit the section and the shipped default applies.

| Key | Type | Default | Effect |
|---|---|---|---|
| `log_max_events` | int ≥ 1 | `10000` | Maximum events kept in `events.jsonl`. Oldest drop first. Attach from a rotated seq gets `log_trimmed` and replays from the earliest kept seq. |

### `remote`

Opt-in bind on a Tailscale address (TD-3601). Empty is off: the daemon
listens on `127.0.0.1` only. Set `bind` to a Tailscale IPv4
(`100.64.0.0/10`), Tailscale IPv6 (`fd7a:115c:a1e0::/48`), or an
interface name (`tailscale0`). The server then listens on that address
**and** loopback, never `0.0.0.0` or `::`. A non-Tailscale LAN address
is refused. Live Tailscale is not required to ship or test; the daemon
reads local interface addresses, not `tailscale status`.

| Key | Type | Default | Effect |
|---|---|---|---|
| `bind` | string | *empty* | Interface name or Tailscale IP. Empty / omitted is loopback only. |

**Attach from another device (TD-3701).** Open the same TST Desk UI in a
browser (the desktop window is Tauri; a phone is not). Connect with
`ws://<bind>:<port>` plus the rotating token in `{user_data_dir}/remote-token`
— not the token in `port.json`. Example:
`ws://100.64.1.2:9xxx` and the file
`~/Library/Application Support/com.thatsimpletech.tstdesk/remote-token` on
macOS. A URL of the form `?ws=ws://…&token=…` (or the same pair in the hash)
fills the form. No account, no relay. TD-3603 will copy address + token from
Settings; this branch still types them.

### `autonomy`

Rootless container isolation for autonomous runs (TD-4301, spec §12.5).
Interactive sessions do not require this. The start button (TD-4003)
refuses without a live sandbox. `runtime` and `image` live here, never
in Python — same rule as model slugs. The named runtime is Podman;
another binary is accepted only if it speaks the same `run` argv
(`--network=none`, `--userns=keep-id`, one bind mount of the workspace
at `/workspace`). Firecracker / EZER is a follow-up, not this key.

| Key | Type | Default | Effect |
|---|---|---|---|
| `runtime` | string | `podman` | Argv0 probed on PATH, or an absolute path to an executable. Empty is a load error. |
| `image` | string | `docker.io/library/alpine:3.21` | Image the container execs. Empty is a load error. Pre-pull it; the argv passes `--pull=never` so a start check cannot phone a registry. |

<!-- verify: model -->
```yaml
presets:
  demo:
    brain:
      slug: demo/brain
      base_url: http://127.0.0.1:11434/v1
      input_price: 0
      output_price: 0
      cache_read_price: 0
      context_window: 8192
      max_output_tokens: 256
    worker:
      slug: demo/worker
      base_url: http://127.0.0.1:11434/v1
      input_price: 0
      output_price: 0
      cache_read_price: 0
      context_window: 8192
      max_output_tokens: 256
    validator:
      slug: demo/validator
      base_url: http://127.0.0.1:11434/v1
      input_price: 0
      output_price: 0
      cache_read_price: 0
      context_window: 8192
      max_output_tokens: 256
active_preset: demo
search:
  base_url: http://127.0.0.1:8888/search
  timeout_seconds: 15
  max_results: 8
  fetch_max_bytes: 200000
embeddings:
  base_url: http://127.0.0.1:8080/v1
  model: nomic-embed-text
  timeout_seconds: 2
  top_k: 4
  token_budget: 2000
project_context:
  token_budget: 2000
session:
  log_max_events: 10000
computer_use:
  command: ""
  browser: mock
  grounding:
    base_url: ""
    timeout_seconds: 8
  local_worker_preset: vllm
remote:
  bind: ""
notify:
  slack:
    enabled: false
    host: ""
    timeout_seconds: 5
  ntfy:
    enabled: false
    host: ""
    timeout_seconds: 5
autonomy:
  runtime: podman
  image: docker.io/library/alpine:3.21
```

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
and `vllm` presets) honestly reports a cost of zero.

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

### 3.5 Attaching vLLM or EZER

The shipped `vllm` preset points every tier at `http://127.0.0.1:8000/v1` — vLLM's
OpenAI-compatible server default. EZER serves the same `/v1` shape, so attaching EZER is this
preset, not a different code path. Set it in the user `config.yaml` and restart the daemon
(or pick `vllm` from the first-run wizard / settings):

<!-- verify: model -->
```yaml
presets:
  vllm:
    brain:
      base_url: http://127.0.0.1:8000/v1
      input_price: 0.0
      output_price: 0.0
      cache_read_price: 0.0
      context_window: 32768
      max_output_tokens: 4096
    worker:
      base_url: http://127.0.0.1:8000/v1
      input_price: 0.0
      output_price: 0.0
      cache_read_price: 0.0
      context_window: 32768
      max_output_tokens: 4096
    validator:
      base_url: http://127.0.0.1:8000/v1
      input_price: 0.0
      output_price: 0.0
      cache_read_price: 0.0
      context_window: 32768
      max_output_tokens: 4096
active_preset: vllm
```

In a copy that already has the shipped presets, the only edit is `active_preset: vllm`. Start
the server so `GET http://127.0.0.1:8000/v1/models` answers (vLLM's default `vllm serve …`
listens on 8000; EZER's documented command does the same if it uses that port). If the process
listens elsewhere, change `base_url` in this file — never in Python.

`slug` is omitted on purpose, same as `local` (§3.4). What "unresolved" means on this
endpoint:

- **Down.** Nothing is listening at `127.0.0.1:8000`. The turn fails as `model_unresolved`.
  Diagnostics' `provider` row names `http://127.0.0.1:8000/v1`, not a model tag. Start the
  server and send the next message.
- **Zero models.** The server answered `/v1/models` with an empty list. Load a model, then
  resend.
- **Several models.** Discovery will not guess — `/v1/models` lists embedding models beside
  chat models. Set `slug:` on the tier to the one you want.

The doctor's `provider` row always names the endpoint (`reachable at …` or the discovery
failure's `fix`). It does not print the developer's model list.

To ground computer-use clicks on the same loopback server, set
`computer_use.grounding.base_url` to that `/v1` URL (or a dedicated
sidecar). Empty keeps the intended (x, y). A vision model that is
down or unnamed falls back the same way — it does not fail the click.

---

## 4. `.tst/config.yaml` — the workspace wall

Lives at `<workspace>/.tst/config.yaml`. A freshly opened workspace gets a fully commented
template whose values are the defaults, so a scaffolded file changes nothing until you edit it.

**An absent file means the defaults, not an error.** So does an empty or comment-only one. The
file is read when a workspace opens and again when a paused session resumes, which is what lets
you raise a cap and continue without restarting.

Six top-level sections live here, each read by a different part of the daemon: `boundary`,
`caps`, `attachments`, `policy`, `approved_external_imports`, and `memory`.

### 4.1 `boundary` — where the agent may act

| Key | Type | Default | Effect |
|---|---|---|---|
| `writable_paths` | list of glob strings | `["**"]` | Workspace-relative globs the agent may **write** to. A write outside them is refused as `outside_writable_paths` and classified Class C. |
| `allowed_commands` | list of strings | `[]` | Allowlist for the shell tool, matched on the basename of the resolved binary. **Empty or omitted means any command** — the list narrows a path the classifier and approval gate already guard (TD-606). |
| `network` | `deny` or a list of hosts | `deny` | Hosts the agent may reach, matched as bare lowercase hostnames. Enforced against the web tools: `web_fetch` classifies its URL's host, `web_search` the configured `search.base_url` (TD-4808). Any other string is a load error. Does not gate shell egress — see §4.6. |

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

Steering files (`AGENTS.md`, `CLAUDE.md`, `.tst/rules/**`) and the approval policy itself
(`.tst/config.yaml`) are refused for writing no matter what you put in this list. That is
enforced in the tool, not by configuration.

**`allowed_commands` matching.** The command is split into top-level segments on pipes,
semicolons, and the like; each segment's leading binary — after skipping `VAR=value` prefixes —
is resolved with `which` and matched on the basename, so `git` and `/usr/bin/git` both match
`git`. Only the leading binary of each segment is checked, so `sh`, `sudo`, and `env` match as
themselves: list them deliberately, because listing `sh` allows anything `sh -c` can run.
Listed binaries can also re-exec others — `find -exec`, `xargs`, `make`, and any interpreter
all walk through a basename match. Backticks are refused outright, and a binary that cannot be
resolved is refused fail-closed. This is a policy rail, not a sandbox.

**Shell commands ask unless you skip them.** Every shell call is at least Class B: the
static classifier cannot see inside a command string, so shell never auto-runs on the
classifier's say-so (TD-4805). Settings → Policy **Dangerously skip permissions**
promotes every ask to auto, including shell and Class C (TD-806) — that is skip
everything; turning it on accepts that unparsed write forms and Class C steering
redirects will run. "Always allow this in this workspace" still writes a rule for the
exact command, not a blanket `shell: **`. A `never` rule still refuses.

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

### 4.8 `memory` — how long a memory file may grow

| Key | Type | Default | Effect |
|---|---|---|---|
| `max_lines` | int ≥ 1 | `200` | A `fs_write` / `fs_edit` that would make a `.tst/memory/` file longer than this, or that would replace a file already at this cap, is refused. The copy says to distill, not to append. Distill is the path that may replace a file at cap. |

This is a write refusal, not an autonomy pause. Raising the number does not resume anything; it only lets the next tool write land. The number lives here, never as a literal in the write handler.

<!-- verify: workspace -->
```yaml
memory:
  max_lines: 80
```

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

**Dangerously skip permissions** (skip-all) is not a key in this file. Settings → Policy
has a machine-wide toggle that lives in the user data dir as `approvals.yaml`, so a clone
cannot carry it. While it is on, every `ask` runs as auto — including `shell` and Class C —
and caps do not pause. A `never` rule still refuses. The fs-tool boundary still refuses
steering-file writes. The classifier still runs. `effect: yolo` is not a valid rule.

**Computer-use glow**, **Agent cursor**, and **Show indicators on the real display** are also
not keys in this file. Settings → Appearance persists them as `cu-indicators.yaml` in the
user data dir (glow and cursor default on; the real-display overlay defaults off). Glow and
the agent cursor are drawn on the Screen pane. When the real-display toggle is on, screenshot
tools hide that overlay for the duration of the capture so it cannot appear in the frame.
The host overlay path is a no-op in this release; the hide flag is the contract. This is not
a second hardware pointer.

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
- **`network` gates the web tools, not the shell.** Since TD-4808 the allowlist is enforced
  where a host is visible to the classifier: `web_fetch` reduces its `url` argument to a bare
  host, and `web_search` answers for the configured `search.base_url`. A host outside the list
  is Class C (refused); an allowlisted host still asks (Class B). But `deny` does not stop
  `curl` — the classifier cannot see inside a command string, so shell egress is governed by
  `allowed_commands` and the approval gate alone.

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

## 5. `.tst/memory/` — what the agent remembers

Lives at `<workspace>/.tst/memory/`. Opening a workspace (or starting a session in one that
never had the directory) plants three commented templates. A second open does not overwrite
files you already have.

```
<workspace>/.tst/memory/
  MEMORY.md              ← the index; durable facts about this project
  decisions.md           ← why things are the way they are
  gotchas.md             ← things that bit us
  <topic>.md             ← allowed; distill or you create these, the scaffold does not
```

These files are the product, not runtime state. They are git-tracked on purpose — a bad memory
is one `git revert` away, once distill starts committing them. They are not steering: the
instruction stack never reads this directory. Standing rules stay in `AGENTS.md` and
`.tst/rules/`. The agent may write `.tst/memory/**` (Class A); it may never write
`AGENTS.md`, `CLAUDE.md`, or `.tst/rules/**`. See [`steering.md`](steering.md).

The templates are HTML comments so a freshly opened workspace has no facts a later loader
could treat as memory. Replace the comments with real notes, or leave them for distill.

At the first brain turn the heading-match loader includes `MEMORY.md` (when that file
exists) and any other `.tst/memory/*.md` whose heading tokens overlap the user task.
No embeddings required. When the embeddings sidecar answers, topic files are ranked
by similarity, `MEMORY.md` stays, and heading-match is the tie-break and the
fallback. An empty or missing directory loads nothing and the prompt keeps the
memory placeholder.

A file at `memory.max_lines` (default 200, see §4.8) is distilled, not appended forever. The
agent tools refuse a write that would go over, or that would replace a file already at the cap.

---

## 6. When a config is wrong

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

## 7. How this document is kept honest

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
