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

The Linux leaf is `tst-desk`, not the Tauri identifier
`com.thatsimpletech.tstdesk`. The host and `tstd` / `tst run` share that
path. If only the reverse-DNS leftover exists, it is renamed once; if
both exist, `tst-desk` wins and the leftover is left alone.

There is no dedicated environment variable for this path. On Linux and
Windows it moves with the platform's own data-directory variable, as the table shows; on macOS
the path is fixed. `tstd --data-dir <dir>` and `tst --data-dir <dir>` read and write
`<dir>/config.yaml` along with the rest of that daemon's files. With no flag, this file
stays in the platform directory above. The OS keychain does not move.

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
| `credentials` | mapping of id → `{ name, base_url? }` | empty | Named API keys. The `name` is what Settings shows. Optional `base_url` is the host that key talks to (TD-1718). The secret is never here — it lives in the OS keychain as `tst-<id>`. Omitted in an older user copy is filled from the shipped file at load. |
| `search` | mapping | see below | Destination for the `web_search` tool. Omitted in an older user copy is filled from the shipped file at load. |
| `embeddings` | mapping | see below | Local embeddings sidecar for memory ranking. Omitted in an older user copy is filled from the shipped file at load. Empty `base_url` disables the client. Empty `command` is attach-only — the host never spawns on `base_url` alone. |
| `computer_use` | mapping | see below | Desktop computer-use sidecar. Omitted in an older user copy is filled from the shipped file at load. Empty `command` is mock-only — the daemon never spawns `mcp/tst-cu-mcp`. Empty `grounding.base_url` leaves click targeting on the intended (x, y). |
| `session` | mapping | see below | On-disk session event-log window. Omitted in an older user copy is filled from the shipped file at load. Zero is invalid, not unbounded. |
| `remote` | mapping | see below | Opt-in Tailscale bind. Omitted in an older user copy is filled from the shipped file at load. Empty `bind` is loopback only. |
| `notify` | mapping | see below | Outbound notification channels. Omitted in an older user copy is filled from the shipped file at load. Slack and ntfy are off until their `enabled` flag is true and a destination URL is stored in the OS keychain. |
| `autonomy` | mapping | see below | Rootless container for autonomous runs (TD-4301) and interactive verify after writes (TD-4204). Interactive sessions ignore `runtime` and `image`. Omitted in an older user copy is filled from the shipped file at load. A missing or rootful runtime refuses start with install copy. |
| `mcp` | mapping | empty servers | User-listed MCP servers (TD-4401). Omitted in an older user copy defaults to no servers. Empty `servers` adds no doctor rows. HTTP `url` must be loopback; off-box is refused before dial. No `env` map — tokens stay in the keychain. |
| `engine` | mapping | see below | Which agent loop new sessions use. `native` is the TST 3-tier OpenAI-compatible loop. `grok` spawns the installed Grok Build CLI over ACP. Omitted in an older user copy is filled from the shipped file at load. |
| `steering` | mapping | see below | User-global steering (TD-4845). Omitted in an older user copy is filled from the shipped file at load. |

### `credentials`

Named API keys (TD-1717). Each entry is an id (the keychain account suffix) and a
`name` the Settings screen shows. Optional `base_url` is the host that key talks
to (TD-1718): selecting the key uses that host even when the active preset is
local. Omit it for a keyed local server so the tier URL stays in charge.
Settings → API keys has a Host field that writes this key (TD-1722); blank
keeps the preset URL. Add as
many as you need — OpenRouter, a keyed local server, a second remote. A tier's
`credential` field picks which one that model sends. The secret must be
printable ASCII (space through `~`). On macOS, `security -w` prints
anything else as hex, so save refuses it. An empty or whitespace-only
item is treated as missing.

| Key | Type | Default | Effect |
|---|---|---|---|
| `name` | string, 1–40 chars | *required* | The local given name. Shown in Settings → Model. Never a secret. |
| `base_url` | string | none | OpenAI-compatible endpoint this key talks to. When set, a bound tier uses it instead of the preset `base_url`. The shipped `openrouter` entry points at OpenRouter. Settings → API keys Host writes this; empty removes it. |

Ids must be a lowercase slug `[a-z][a-z0-9-]{0,31}`. `slack-webhook`,
`ntfy-topic`, `discord-webhook`, `telegram-bot`, and `smtp-password` are
reserved for other keychain accounts.

<!-- verify: model -->
```yaml
presets:
  demo:
    brain:
      slug: demo/brain
      base_url: https://openrouter.ai/api/v1
      credential: openrouter
      input_price: 1
      output_price: 2
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
credentials:
  openrouter:
    name: OpenRouter
    base_url: https://openrouter.ai/api/v1
```

### `search`

The agent's `web_search` tool. The query is sent to `base_url` as `?q=...`,
then to each `fallback_base_urls` entry in order when the URLs before it fail
or return nothing parseable (a captcha page is a 200 with zero rows). The
first URL with rows wins. Hosts are configuration, never literals in Python
(the no-telemetry test enumerates those). An empty `base_url` disables the
primary only; all empty disables the tool with copy that names this key.

| Key | Type | Default | Effect |
|---|---|---|---|
| `base_url` | string | *shipped* | The first search endpoint. Empty string skips it. |
| `fallback_base_urls` | list of strings | *shipped* | Endpoints tried in order after `base_url` fails. Empty list disables fallback. Every host in the chain must be on the workspace `network` allowlist or the call stays Class C. |
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

Desktop screenshot / move / click / type / scroll (TD-3301), plus
background app-scoped tools (`list_apps`, `ui_snapshot`, `ui_action`,
`launch_app`) that do not take the pointer (TD-4829). Empty `command`
is the in-process mock (CI, no display). A non-empty value is the argv
for `mcp/tst-cu-mcp` over stdio — the daemon owns the child and does
not bind a socket. Linux X11 is a live path (TD-2001): first-run
onboarding is the same kind of honesty as Windows (no grant dialog). A
Wayland session is unsupported (TD-2002); `health` reports
`supported: false` and `session_type: "wayland"`.

Settings → Computer use (TD-4830) writes `~/.tst-cu-mcp/config.yaml`
(or `$TST_CU_MCP_CONFIG`): `actuation.enabled`, `mode` (`background` or
`full_control`), `scoping.denied_apps`, `scoping.unhide_on_finish`. The
MCP server re-reads that file on each background tool call.

Browser computer-use (TD-1710): `browser` selects the in-process mock
(CI, never launches Chrome) or Playwright with a persistent profile
under the user data dir. Playwright missing always falls back to mock.

| Key | Type | Default | Effect |
|---|---|---|---|
| `command` | string or list | *empty* | Argv for the computer-use MCP sidecar. A string is split with the shell; a list is used as-is. Empty or omitted is mock-only. |
| `browser` | `mock` or `playwright` | `mock` | Browser driver. `mock` never launches Chrome. `playwright` uses a persistent profile under the user data dir when Playwright is installed; otherwise the mock. |
| `grounding` | mapping | see below | Local vision model for click targeting (TD-3902). |
| `local_worker_preset` | string | `vllm` | After this session has used a `desktop_` or `browser_` tool, the worker *client* uses that named preset's worker tier (loopback URL + optional slug). Brain stays on the active preset. Empty never remaps. The name is a preset key, not a model slug. Lead-turns, `set_tier`, and escalation are unchanged. |
| `max_prompt_images` | int ≥ 0 | `1` | How many `image_url` parts a completion may keep. Oldest screenshots are dropped first. `1` matches ezer-forge (`--limit-mm-per-prompt image:1`). |

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

### `judgments`

Provider-neutral judgment features (TD-708/709/710/711, dev build). A
*judgment* is a typed answer to a narrow question over bounded text
state, asked on the configured backend — the default connector is the
worker tier you already have, so nothing here requires an external
judgments API. Every flag defaults off, and off means the dispatch and
autonomy paths are byte-identical to the pre-seam product. Every
judgment fails closed: an unavailable, unparseable, or low-confidence
answer falls back to the unjudged behavior, never to a block.

| Key | Type | Default | Effect |
|---|---|---|---|
| `verification` | bool | `false` | After an actuating desktop/browser tool succeeds, judge whether it had its intended effect from a bounded text before/after state (TD-709). A refuted actuation is annotated on the tool result so the loop can re-check or retry. Never a screenshot; never raw page content. |
| `semantic_breaker` | bool | `false` | Unattended runs gain a fifth circuit breaker (TD-710): N consecutive no-progress judgments trip `breaker:no_semantic_progress` as a fault report. Inert on any judgment failure. |
| `candidate_selection` | bool | `false` | Adds the `browser_pick` tool (TD-711): act-by-description — code extracts `{index, tag, role, accessible_name, bounding_box}` per candidate, the configured judgment backend picks one, and the click lands on its box. No page text, HTML, cookies, or URL crosses the boundary; no confident match refuses with fallback guidance. |
| `confidence_threshold` | float 0–1 | `0.6` | Minimum confidence to assert a judgment. Below it the feature treats the answer as unavailable. |
| `max_state_chars` | int | `2000` | Per-field cap on the state text sent to the backend. |
| `backend` | `worker` or `typesafe` | `worker` | The connector. `worker` uses the configured worker tier. `typesafe` uses the hosted typed-judgment API — opt-in, and it falls back to `worker` when its key is not stored. |
| `typesafe_base_url` | string | *empty in code; the shipped config sets `https://api.typesafe.ai`* | The TypeSafe API base URL. Empty means unconfigured — the `typesafe` backend falls back to `worker`. Destinations live here, never in source (§2.3). |
| `typesafe_model` | string | `jev-latest` | The TypeSafe model alias for judgments. |
| `typesafe_credential` | string | `typesafe` | The keychain credential id the TypeSafe connector reads. The key itself lives only in the OS keychain (prime §2.2) — add it under Settings → API keys with that name. |

### `notify`

Outbound notification channels (TD-3801, TD-3802, TD-4707). Each channel is a
standalone `send(config, message)` — Slack is the default, ntfy / `discord`
/ `telegram` are extras, no 20-platform gateway (spec §8). Off by
default. Destination URLs are keychain secrets (`tst-slack-webhook`,
`tst-ntfy-topic`, `tst-discord-webhook`, `tst-telegram-bot`), never this
file, never the audit log. `host` is the only host a notifier may reach;
the keychain URL's host must match it or the send is dropped.

| Key | Type | Default | Effect |
|---|---|---|---|
| `slack` | mapping | see below | Slack incoming webhook. Default channel. |
| `ntfy` | mapping | see below | ntfy topic POST. |
| `discord` | mapping | see below | Discord incoming webhook. |
| `telegram` | mapping | see below | Telegram Bot API `sendMessage`. |
| `email` | mapping | see below | SMTP report for a scheduled job (TD-3820). |

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

#### `notify.discord`

Same keys as Slack. Store the webhook URL in the keychain as
`tst-discord-webhook`. Set `host` to that URL's hostname (commonly
`discord.com`).

#### `notify.telegram`

Same keys as Slack. Store `https://<host>/bot<token>/sendMessage?chat_id=<id>`
in the keychain as `tst-telegram-bot`. `chat_id` is a query parameter on
that secret URL, not a yaml field. Set `host` to that URL's hostname.

#### `notify.email`

SMTP for a job whose `deliver_to` is `email` (TD-3820). STARTTLS (port
587) or implicit TLS (port 465). A server that will not negotiate TLS
is refused before AUTH. The password is the keychain account
`tst-smtp-password`, never a yaml key. A `password` key in this file is
dropped. The log records the recipient domain only, never the password
and never the message body. `host` is the only host this channel may
reach.

The message is multipart (TD-3821). The plain part is the report
Markdown. The HTML part is that same Markdown in one centred column
(max-width 680px, inline styles only): a header with the job title and
the local date, the report, and a footer
`Sent by TST Desk · <title> · ran <local time> on <preset>`. The job has
no display name, so the title is the same 60-character instruction
label as the subject. A job with no preset names the active preset.
HTML in the report is escaped, links are http or https only, and images
are left out. Send test email uses a short sample of that layout.

| Key | Type | Default | Effect |
|---|---|---|---|
| `enabled` | bool | `false` | When false, a job set to email does not send. |
| `host` | string | *empty* | SMTP hostname. Empty refuses the send. |
| `port` | int 1–65535 | `587` | SMTP port. 587 with `starttls`, 465 with `tls`. |
| `security` | `starttls` or `tls` | `starttls` | `starttls` upgrades on port 587. `tls` is implicit TLS. |
| `username` | string | *empty* | SMTP AUTH username. Empty refuses the send. |
| `method` | `smtp`, `microsoft`, or `google` | `smtp` | `smtp` uses the fields below. `microsoft` sends the same report through Graph (TD-4853). `google` sends it through Gmail after Settings → Email signs in (TD-4854). |
| `from_address` | string | *empty* | One mailbox address in the From header. Graph and Gmail use it when it is an address; otherwise the signed-in mailbox. |
| `timeout_seconds` | float > 0 | `30` | How long the SMTP conversation may run. |

### `connectors`

Outbound accounts. Each connector is off until its keys are set. There
is no hosted relay.

#### `connectors.github`

GitHub App device flow (TD-4852). No client secret and no callback URL.
`client_id` is the App's public client id. Empty means the connector is
off and Settings explains how to create the App. The access token and
the refresh token live in the OS keychain as `tst-github-oauth`, never
in this file and never in a log. Pydantic leaves the hosts empty; the
shipped `config.yaml` sets the public hosts `api.github.com` and
`github.com`. A missing host keeps the connector off, the same as a
missing client id.

| Key | Type | Default | Effect |
|---|---|---|---|
| `connectors` | mapping | `{}` | Named account connectors. |
| `github` | mapping | empty | The GitHub App connector. |
| `client_id` | string | *empty* | Public GitHub App client id. Empty turns the connector off. |
| `api_host` | string | *empty* | REST hostname. The shipped file sets `api.github.com`. |
| `web_host` | string | *empty* | Device-flow hostname. The shipped file sets `github.com`. |
| `timeout_seconds` | float > 0 | `20` | How long one GitHub HTTP call may run. |

#### `connectors.microsoft`

Microsoft sign-in for scheduled-job email (TD-4853). A public client:
authorization code with PKCE, no client secret. The daemon listens on
`127.0.0.1` and an ephemeral port, then closes after one redirect or
`sign_in_timeout_seconds`. Register that loopback redirect on the app
(mobile and desktop). Scopes are `offline_access`, `Mail.Send`, and
`User.Read`. Empty `client_id` hides the Microsoft method in Settings
and shows how to set one. Tokens live in the OS keychain as
`tst-microsoft-oauth`, never in this file and never in a log. Pydantic
leaves the hosts empty; the shipped `config.yaml` sets the public
hosts. A missing host keeps the connector off, the same as a missing
client id. `tenant` defaults to `common`.

| Key | Type | Default | Effect |
|---|---|---|---|
| `microsoft` | mapping | empty | The Microsoft mail connector. |
| `client_id` | string | *empty* | Public client id of the app registration. Empty turns the connector off. |
| `tenant` | string | `common` | Tenant path segment: `common`, `organizations`, `consumers`, a GUID, or a domain. |
| `login_host` | string | *empty* | Authorization hostname. The shipped file sets the public login host. |
| `graph_host` | string | *empty* | Graph hostname. The shipped file sets the public Graph host. |
| `timeout_seconds` | float > 0 | `20` | How long one token or Graph call may run. |
| `sign_in_timeout_seconds` | float > 0 | `300` | How long the loopback listener waits for the redirect. |

#### `connectors.google`

Google sign-in for scheduled-job email (TD-4854). An installed-app
(Desktop) OAuth client: authorization code with PKCE. Google does not
treat that client's secret as confidential. It still lives in the OS
keychain as `tst-google-client-secret`, entered in Settings, never in
this file. A `client_secret` key here is dropped. The daemon listens on
`127.0.0.1` and an ephemeral port, then closes after one redirect or
`sign_in_timeout_seconds`. The scope is
`https://www.googleapis.com/auth/gmail.send`, plus OpenID email so
Settings can name the account. Empty `client_id` hides the Google
method and shows how to create the Desktop client. Tokens live in the
keychain as `tst-google-oauth`. Pydantic leaves the hosts empty; the
shipped `config.yaml` sets the public hosts. A missing host keeps the
connector off, the same as a missing client id. A refresh that returns
`invalid_grant` (an unverified app, or a client still in testing) marks
the connection expired and tells the user to reconnect.

| Key | Type | Default | Effect |
|---|---|---|---|
| `google` | mapping | empty | The Google mail connector. |
| `client_id` | string | *empty* | OAuth client id. Empty turns the connector off. |
| `auth_host` | string | *empty* | Authorization hostname. The shipped file sets the public accounts host. |
| `token_host` | string | *empty* | Token hostname. The shipped file sets the public token host. |
| `userinfo_host` | string | *empty* | OpenID userinfo hostname. The shipped file sets the public userinfo host. |
| `gmail_host` | string | *empty* | Gmail API hostname. The shipped file sets the public Gmail host. |
| `timeout_seconds` | float > 0 | `20` | How long one token or Gmail call may run. |
| `sign_in_timeout_seconds` | float > 0 | `300` | How long the loopback listener waits for the redirect. |

### `speech`

Hold-to-talk dictation (TD-4701). Off by default. There is no cloud
speech URL and no Web Speech API path. The window records only while
the control is held, then the daemon POSTs to
`{base_url}/audio/transcriptions`. Empty `base_url` is unconfigured:
the control (when `enabled`) tells the user to set this key. Optional
`credential` is a named keychain id used as a Bearer token; the secret
is never this file. Omit `credential` for a keyless loopback whisper
server.

| Key | Type | Default | Effect |
|---|---|---|---|
| `enabled` | bool | `false` | When false, the hold-to-talk control is hidden. |
| `base_url` | string | *empty* | OpenAI-compatible transcriptions root, including `/v1`. Empty disables even if `enabled` is true. No shipped cloud URL. |
| `model` | string | *empty* | Transcriptions `model` field. Empty omits it (local servers often have a default). |
| `timeout_seconds` | float > 0 | `30` | How long the POST may run. |
| `credential` | string | *empty* | Named keychain id sent as `Authorization: Bearer`. Empty sends no key. |

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
speech:
  enabled: false
  base_url: http://127.0.0.1:8080/v1
  model: ""
  timeout_seconds: 30
  credential: ""
```

`project_context` is the pinned-file budget on the brain prompt (TD-2805).
Newest pins drop first when over `token_budget`.

### `steering`

User-global steering (TD-4845). Discovery always reads `~/.tstdesk/AGENTS.md`
when that file exists. `~/.claude/CLAUDE.md` is Claude Code's global file.
It is not read, and nothing it imports is read, unless
`claude_global_fallback` is true. Workspace-root and nested `CLAUDE.md`
files are project files and are not gated by this key.

The same key gates `~/.claude/skills/` (TD-5011). Claude Code's user-global
skills are a catalog line each in every brain prompt (14 skills on the
owner's box came to about 900 estimated tokens), so they are opt-in. A
workspace's own `.claude/skills/` is not gated, and when the workspace is
the home folder, `~/.claude/skills/` is that workspace's own tree.

When the key is false, `~/.claude/CLAUDE.md` exists, and
`~/.tstdesk/AGENTS.md` does not, Doctor's steering row says the file was
not loaded and names this key. Turning the key on restores the previous
fallback, including the approval prompt for an import that leaves the
workspace.

| Key | Type | Default | Effect |
|---|---|---|---|
| `claude_global_fallback` | bool | `false` | When true, `~/.claude/CLAUDE.md` is the user-global file if `~/.tstdesk/AGENTS.md` is absent, and `~/.claude/skills/` joins the skill fallback. When false, neither is read. |

### `session`

The durable event log each session writes under the data dir (TD-2901).
Attach replays this file. The cap is an event count — a byte cap would
drop a different prefix than `from_seq`. Zero is a load error, not
"keep forever"; omit the section and the shipped default applies.

| Key | Type | Default | Effect |
|---|---|---|---|
| `log_max_events` | int ≥ 1 | `10000` | Maximum events kept in `events.jsonl`. Oldest drop first. Attach from a rotated seq gets `log_trimmed` and replays from the earliest kept seq. |
| `send_file_max_bytes` | int 1..2147483648 | `536870912` | Largest file `send_file` copies into the chat (512 MiB). Bigger is refused with `file_too_large`; nothing is copied. |
| `send_file_session_max_bytes` | int ≥ 1 | `4294967296` | Total of sent files one session may hold (4 GiB). Past it, `session_quota`. |

### `provider_retry`

How stubbornly a retryable provider failure (429, 5xx, transport) is
retried before the turn is failed. Delays back off exponentially from
`initial_delay`, are capped per-wait at `max_delay`, and honour a
`Retry-After` header when the provider sends one.

The defaults spend four attempts in roughly eight seconds. That covers a
blip. It does not cover a gateway serving a shared upstream pool, which
answers `429` with "temporarily rate-limited upstream, please retry
shortly" and no `Retry-After` — free and preview model slugs do this
routinely, and the turn dies eight seconds into a wait that needed a
minute. `max_retries: 8` is roughly three minutes of patience.

Two calls outside the turn never resend a timed-out attempt: the
after-write review (`autonomy.verify`) and memory distillation (TD-5146).
Each attempt has already waited a full read timeout, the session stays
busy while it waits, and a slow local model is not faster the second
time. Other retryable failures (429, 5xx, a refused connection) are
retried as above. Both calls stream, so the read timeout is the longest
gap between chunks rather than a cap on the whole answer.

| Key | Type | Default | Effect |
|---|---|---|---|
| `max_retries` | int 0–20 | `3` | Retries after the initial attempt. `0` disables retrying; the first failure ends the turn. |
| `initial_delay` | float > 0 | `1.0` | Base delay before the first retry, in seconds. Each subsequent wait doubles. |
| `max_delay` | float > 0 | `60.0` | Ceiling on any single wait, in seconds. Bounds the worst case when `max_retries` is high. |

### `scheduler`

How long one scheduled turn may run before the daemon cancels it (TD-3819). The same number is the base wait for `tst run`. A job can set its own limit, from 1 to 60 minutes, on the Scheduled form. When the limit expires the turn is cancelled and the receipt says `stopped after N minutes (max run time)`. That reason is not retried.

`tst run --timeout SECONDS` replaces this base for one command. The provider retry allowance is still added when the config can be read, the same as before.

| Key | Type | Default | Effect |
|---|---|---|---|
| `max_run_seconds` | number > 0, at most 3600 | `900` | Seconds one turn may run when the job does not set its own limit. 900 is 15 minutes. The receipt rounds the budget up to whole minutes. |

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
| `port` | int | `0` | Listener port while remote attach is on (TD-5005). `0` reuses the port the listener last used, else picks a random one. A pinned port that is busy at start falls back to a random one. Must be `0` or 1024–65535. |

**Pair a phone (TD-5014).** This is the usual way. On the computer, choose
pair in Settings or run `tst pair`. It shows the computer's name, its
Tailscale address and port, an 8-character code such as `ABCD-2345`, and a
QR code. On the phone, scan the QR, open the `tstdesk://pair` link, or type
the address and code. The code works once, lasts 10 minutes, and stops
working after 5 wrong tries; it is kept only in the daemon's memory and
never logged. If remote attach is off, pairing turns it on first.

Pairing gives that phone its own key. The phone saves it and connects on its
own from then on. The computer keeps only a sha256 of each key, never the key,
in `{user_data_dir}/remote-devices.yaml` (readable by you only). Up to 32
devices can be paired. `tst devices` lists them and `tst devices remove <id>`
(or Settings) forgets one and closes only its connections. Turning remote
attach off stops the listener but keeps the list, so paired phones connect
again when it is turned back on, on the same port. Only the computer itself
can pair or remove devices, with one exception: a phone's "Forget this
computer" removes that phone's own entry. Pairing the same phone again
replaces its old entry instead of adding a second one. Settings shows each
device's paired date, last seen, and whether it is connected right now. If
the file is damaged it is renamed to `remote-devices.yaml.corrupt-<time>` and
the list starts empty. If it only cannot be read (permissions, a disk error),
it is left alone: phones are told to keep retrying, not that their key is gone,
and pairing waits until it can be read again.

A pairing link opened from another app or a web page never pairs on its own:
the phone shows "Pair with … at …?" and which saved computer it would
replace. Only the in-app scanner and the typed code pair directly.

**Pair another computer (TD-5015).** One computer can show another's
sessions in its own list, beside its own. On the computer whose sessions you
want to see, choose pair in Settings or run `tst pair`, as for a phone. On the
computer you sit at, open Settings → Computers → "Add a computer" and paste the
`tstdesk://pair` link (the Pair dialog has "Copy link", since a desk can't
scan), or type the address and code. In a terminal:

```text
tst machines                                   # list paired computers and their status
tst machines add 'tstdesk://pair?h=…&p=…&c=…'  # pair with a link
tst machines add 100.64.1.5:39079 ABCD-2345    # or host:port and the code it shows
tst machines remove mch_0123456789ab           # forget one
```

Only Tailscale addresses (`100.64.0.0/10`, `fd7a:115c:a1e0::/48`) and
`*.ts.net` names are accepted, checked when you pair and again before every
connection. The other computer must have remote attach on (pairing turns it
on there). This computer connects only while a TST Desk window or a paired
phone is open, and stops 60 seconds after the last one closes. A computer that
can't be reached keeps its last-known sessions in the list, greyed, and is
tried again on its own (or with "Try now"). Its sessions open again when it is
back. Up to 16 computers can be paired. Self-pairing is allowed and shows
every session twice, the copy marked "this computer".

Pairing gives this computer its own key for the other one, exactly like a
phone's, and the other computer lists it under Phones and devices. The key is
kept in the OS keychain (service `com.thatsimpletech.tstdesk`, account
`tst-machine.<mch>`), never in a file. `{user_data_dir}/computers.yaml`
(readable by you only, replaced whole on every write) holds only the index:
for each computer its `mch_` id, its `machine_id`, name, host, port, the
device id it was given there, and the paired and last-seen times. Do not edit
it by hand. Remove asks the other computer to forget this one first, then
deletes the key and the entry; when the other computer is offline, it still
lists this computer until it is removed there. A damaged file is renamed to
`computers.yaml.corrupt-<time>` and the list starts empty; one that cannot be
read is left alone.

What another computer may do here is limited: it can list, open, drive and
answer the approvals of this computer's sessions, but cannot change approval
settings, keys, pairing, jobs, policy rules or anything machine-wide, and it
never sees other computers this one paired (one hop).

**Attach with the token (the older way, TD-3701).** Open the same TST Desk UI
in a browser (the desktop window is Tauri; a phone is not). Connect with
`ws://<bind>:<port>` plus the token in `{user_data_dir}/remote-token`
— not the token in `port.json`. Example:
`ws://100.64.1.2:9xxx` and the file
`~/Library/Application Support/com.thatsimpletech.tstdesk/remote-token` on
macOS. A URL of the form `?ws=ws://…&token=…` (or the same pair in the hash)
fills the form. No account, no relay. Settings shows the address and port and
never the token; copy the token from `remote-token`. The address and token stay
the same across restarts (TD-5005). Settings → **New token** replaces the token
and disconnects every remote client that used it; paired phones keep their own
keys. Turning remote attach off deletes it.

**Android shell (TD-5001).** `android/` builds a sideload APK that loads this
same UI. The phone build accepts only a Tailscale address or a `*.ts.net`
name. Install and the token path are in `android/README.md`. The APK is not
a Play upload.

### `autonomy`

Rootless container isolation for autonomous runs (TD-4301, TD-4302,
spec §12.5). Interactive sessions do not require this. The start
button (TD-4003) refuses without a live sandbox. `runtime` and `image`
live here, never in Python — same rule as model slugs. The named
runtime is Podman; another binary is accepted only if it speaks the
same `run` argv (`--userns=keep-id`, one bind mount of the workspace
at `/workspace`, `--network=none` when the charter wall is deny; a
host allowlist omits that flag so Podman slirp can work, and never
`--network=host`). The argv never bind-mounts `$HOME`, `~/.ssh`, cloud
creds, the OS keychain, or the user-data-dir; there is no extra-mount
API. Firecracker / EZER is a follow-up, not this key.

`verify` is the unused validator call for *interactive* mode (spec §12.6
first sentence, TD-4204). It is not the autonomy supervisor. A write
turn may enqueue one validator review of the diff and tests. Write-less
turns never verify. There is no charter and no auto-revert.

| Key | Type | Default | Effect |
|---|---|---|---|
| `runtime` | string | `podman` | Argv0 probed on PATH, or an absolute path to an executable. Empty is a load error. |
| `image` | string | `docker.io/library/alpine:3.21` | Image the container execs. Empty is a load error. Pre-pull it; the argv passes `--pull=never` so a start check cannot phone a registry. |
| `check_every` | int ≥ 1 | `5` | Autonomy turns between validator drift checks (TD-4201, spec §12.6). A Class B decision this turn also checks, even if N is not reached. Interactive sessions ignore this. |
| `verify` | `off` \| `after_write` \| `ask` | `after_write` | Interactive: after a filesystem write turn, call the validator once (`after_write`), ask first (`ask`), or never (`off`). Autonomy sessions ignore this. The call streams and sends the validator tier's `extra_body`, so ending the session or quitting also ends the server's work; a timed-out attempt is reported as a verify error, not resent (TD-5146). Stop does not reach the review: it starts after the turn has ended. |

### `mcp`

User-listed MCP servers (TD-4401). The user names each server in
user-data-dir `config.yaml` — that listing is the load, not dynamic
discovery (TD-601). Interactive and autonomy sessions both load them.
A dead or refused server is a doctor row (`mcp:<id>` after `steering`),
not a dead daemon. Builtins stay registered. The computer-use sidecar
(`computer_use.command` → `mcp/tst-cu-mcp`) is a different product.
A packaged sidecar with empty `command` serves `tstd --cu-mcp` (TD-1725).

Tool names are prefixed `{server_id}__{remote_name}` so an MCP tool
named `fs_read` cannot replace the builtin. Registry provenance is
`mcp:<server_id>`. The approval floor is `ask` (TD-4402 classifies
path/host fields). No free-form `env` map — a token pasted here would
land on disk; Settings (TD-4403) keeps paste-a-token in the keychain.
Settings persist add / disable / remove through `set_mcp_server` and
`delete_mcp_server`; the write is surgical so teaching comments survive.
There is no `env` field on those messages. Live sessions keep the tool
set they attached with until a new session.

| Key | Type | Default | Effect |
|---|---|---|---|
| `servers` | mapping of id → server | `{}` | Listed servers. Ids are lowercase slugs `[a-z][a-z0-9-]{0,31}`, same as credentials. Empty or omitted means none — no extra doctor rows. |

#### `mcp.servers.<id>`

| Key | Type | Default | Effect |
|---|---|---|---|
| `transport` | `stdio` or `http` | *required* | How the daemon talks to the server. stdio spawns `command`. http POSTs JSON-RPC to `url`. |
| `command` | list of strings | `[]` | Argv for a stdio server. Required when `transport` is `stdio`. A string is a load error (not split). Empty skips the server with a doctor fail. |
| `url` | string | *empty* | Loopback `http(s)` endpoint for an http server. Required when `transport` is `http`. Non-loopback is refused before any request. Empty skips the server with a doctor fail. |
| `enabled` | bool | `true` | When false, the server is skipped (doctor `skip`). |

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
credentials:
  openrouter:
    name: OpenRouter
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
steering:
  claude_global_fallback: false
session:
  log_max_events: 10000
  send_file_max_bytes: 536870912
  send_file_session_max_bytes: 4294967296
provider_retry:
  max_retries: 3
  initial_delay: 1.0
  max_delay: 60.0
scheduler:
  max_run_seconds: 900
computer_use:
  command: ""
  browser: mock
  grounding:
    base_url: ""
    timeout_seconds: 8
  local_worker_preset: vllm
  max_prompt_images: 1
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
  discord:
    enabled: false
    host: ""
    timeout_seconds: 5
  telegram:
    enabled: false
    host: ""
    timeout_seconds: 5
  email:
    enabled: false
    host: ""
    port: 587
    security: starttls
    username: ""
    from_address: ""
    timeout_seconds: 30
autonomy:
  runtime: podman
  image: docker.io/library/alpine:3.21
  check_every: 5
  verify: after_write
mcp:
  servers:
    example:
      transport: stdio
      command: ["python", "-m", "some_mcp"]
      enabled: true
    loop:
      transport: http
      url: http://127.0.0.1:9
      enabled: true
engine:
  kind: native
  binary: ""
voice:
  base_url: ""
  credential: ""
  timeout_seconds: 30
```

### `engine`

Which agent loop new sessions start. The window is always a viewer; this picks whose loop it is viewing.

| Key | Type | Default | Effect |
|---|---|---|---|
| `kind` | `native` or `grok` | `native` | `native` is the TST 3-tier OpenAI-compatible loop. `grok` spawns the installed Grok Build CLI over ACP (`grok agent stdio`). Auth for grok stays in `~/.grok/auth.json` — never this file. New sessions pick up a change; a running session keeps the engine it started with. |
| `binary` | string | empty | Path to the `grok` executable. Empty means PATH, then `~/.grok/bin/grok`. |

The Grok CLI itself is unchanged: `grok`, `grok -p`, `grok agent stdio`, SSH, and tmux keep working with or without TST Desk.

### `voice`

Hold-to-talk dictation (TD-4701). The Settings toggle is off by default and lives in `voice.yaml` in the daemon's data directory (the platform directory above when `--data-dir` is omitted), not this file. This block only names a transcription endpoint. Empty `base_url` means the composer uses OS dictation (no network from `tstd`). Works with whichever engine the session is running — it fills the composer, it does not talk to the agent loop.

| Key | Type | Default | Effect |
|---|---|---|---|
| `base_url` | string | *empty* | OpenAI-compatible `/v1` root. `tstd` POSTs `{base_url}/audio/transcriptions`. Empty disables the endpoint path. The host is configuration, never Python. |
| `credential` | string | *empty* | Named key from the catalog. Required when `base_url` is off-box; omit on loopback. |
| `timeout_seconds` | float > 0 | `30` | How long to wait for the transcription response. |

### A preset

Every preset declares all three tiers. Omitting one is a load error naming it.

| Key | Type | Effect |
|---|---|---|
| `brain` | tier | Planning turns. The router opens every session here for the first two turns, and escalates back to it after three consecutive worker failures. |
| `worker` | tier | Token-heavy execution — edits, and the tool-call classifier. Where turns land once the lead turns are spent. |
| `validator` | tier | Reviewing diffs and tests. Interactive mode may call it once after a write turn (`autonomy.verify`, TD-4204). Autonomy supervision is a later story. |

Routing is turn-count and failure driven; it does not read anything from this file beyond the
three tier definitions. You can pin a tier for a session from the title bar.

### A tier

| Key | Type | Default | Effect |
|---|---|---|---|
| `slug` | string, non-empty | none | The model identifier sent as `model` on every request. Optional **only** when `base_url` is on loopback — see §3.4. `slug:` with no value means the same as leaving it out; `slug: ""` is an error. |
| `base_url` | string | *required* | The OpenAI-compatible endpoint when the bound credential has no host of its own. Requests go to `{base_url}/chat/completions`. A credential `base_url` wins (TD-1718). |
| `credential` | string | none | Named key from `credentials`. A bound id is sent even on loopback, and that key's host is used when set. Unbound loopback sends no key. Unbound remote uses `openrouter`. |
| `input_price` | float ≥ 0 | *required* | Dollars per **million** prompt tokens that were not served from cache. |
| `output_price` | float ≥ 0 | *required* | Dollars per **million** completion tokens. |
| `cache_read_price` | float ≥ 0 | *required* | Dollars per **million** prompt tokens served from cache. |
| `context_window` | int > 0 | *required* | The compaction budget — see below. |
| `max_output_tokens` | int > 0 | *required* | Sent as `max_tokens` on the tier's requests, and the answer reservation subtracted from `context_window` — see below. |
| `vision` | bool | `false` | When `true`, PNG/JPEG/GIF/WebP attachments are accepted on user turns routed to this tier (TD-4705). Capability is config — never inferred from the slug in code. |
| `extra_body` | map | none | Extra JSON fields added to every chat request the tier makes: the turn, the progress check, a delegated worker, the after-write review (validator tier), memory distillation (worker tier), and (on the worker tier) the tool-call classifier, DoD polls and judgment calls that run on the worker. They fill in only fields the request did not set itself. For provider-specific settings such as a llama.cpp server's `chat_template_kwargs` — see §3.6. It cannot set `model`, `messages`, `tools`, `tool_choice`, `stream`, `stream_options`, `max_tokens` or `max_completion_tokens`, and its values must be plain JSON; a load error names the problem. Sent as written: never put a key or token here. |
| `verdict_extra_body` | map | none | Merged over `extra_body` on the worker tier's one-word verdict calls only: the tool-call classifier, DoD polls and judgment calls that run on the worker. Nested maps merge and this map wins a clash. The turn, a delegated worker and memory distillation never send it. Same rules as `extra_body`; it does nothing on the brain and validator tiers. Use it to turn a reasoning model's thinking off for an 8-token answer — see §3.6. |

**Prices are per million tokens, and they are yours to keep accurate.** They drive the live
cost meter, the audit trail, and the spend cap. Nothing verifies them against your provider, so
a wrong number here buys you a wrong cap, not an error. A preset priced at zero (the `local`
and `vllm` presets) honestly reports a cost of zero.

**`context_window` is a compaction budget; `max_output_tokens` is also the output cap.**
`context_window` is never sent. `max_output_tokens` is sent as `max_tokens` on the turn, the
progress check and a delegated worker's requests (TD-5008); the validator review, the supervisor
check and memory distillation already sent it. Together they set the point at which the
conversation is compacted: at 80% of `context_window - max_output_tokens`, the oldest messages
are replaced by an extractive summary, keeping the system message and the two most recent user
turns. No model call is spent doing it. The session stores the summary as a second system
message, but a request carries only one: the summary joins the first system message, and any
later system message goes ahead of the next user message. Strict local chat templates (Qwen
3.5 and later, Mistral) refuse a system message anywhere but first; others, such as Llama 3 and
Gemma 4, render it (TD-5142). If a server reports that its template raised an error on a
conversation (llama.cpp, or a server that names jinja2's exceptions), the turn fails once with
`template_error` and names the template's reason. It is not retried, because the same messages
fail the same way. So:

- `context_window` must be no larger than what the *server* actually serves. A local server
  usually caps context well below the model's architectural ceiling; setting the ceiling here
  means compaction never fires and the server silently truncates instead — and what it drops
  first is your steering block.
- `max_output_tokens` caps generation, and the provider counts a reasoning model's thinking
  inside that cap. Size it for the thinking plus the answer, or turn the thinking down with
  `extra_body` (§3.6). A reply cut off at the cap ends with `finish_reason: length`: cut text
  is kept and the cap is logged, and a cut tool call is not run. The model gets the call back
  with empty arguments and a note to send smaller pieces. Raising the cap also shrinks the
  compaction budget, so on a small window it is a trade. The tool-call classifier, DoD polls
  and judgment calls that run on the worker tier keep their own 8-token cap; on a reasoning
  model, turn their thinking off with `verdict_extra_body` (§3.6).

**How the prompt is measured (TD-5007).** The estimate counts the message text, the tool-call
arguments, every tool definition the request sends (as the request serializes it — a few
thousand tokens on every call), and 4 tokens of chat-template overhead per message. It starts at
4 characters per token. Once an endpoint reports `usage.prompt_tokens` for a main turn call, the
ratio between that figure and the estimate is learned per `base_url` and slug (the first report
sets it, later ones move it by 30%, clamped to 0.5–3.0) and every later estimate for that
endpoint is scaled by it. A request of under 256 estimated tokens, one carrying an image, or a
response with no usage figure teaches nothing. The `turn start` log line carries the ratio in
use as `token_ratio`.

Learned ratios survive a restart (TD-5013). The daemon keeps them in `token-calibration.json`
in its data directory (the platform directory in §1, or `--data-dir`), loads the file at start
and rewrites it whenever a ratio moves, and at least once a day for an endpoint in use, so a
ratio that has settled is not dropped as stale. A loaded ratio keeps moving by 30% per report,
as if the daemon had never stopped. An entry not updated for 30 days, a ratio outside 0.5–3.0,
a malformed entry, or a file that cannot be read is ignored, with one log line, and that
endpoint starts at 4 characters per token again. The file is a cache, not a setting: delete it
while TST Desk is stopped to make every endpoint relearn (a running daemon rewrites it from
memory on the next report), and do not edit it. A save that fails (a read-only folder, a full
disk) is logged once and the ratio stays in memory; the turn is never affected.

The workspace map's share of the brain window (15%, see the steering guide) converts to
characters at the brain endpoint's learned ratio too: at a ratio of 1.17 that is about 3.4
characters per token instead of 4. It is taken once, for the brain of the session's own preset,
when its loop starts, so the map does not resize mid-session. A brain tier whose model is
discovered on the first turn (no `slug` on a local server) sizes that session's map at 4
characters per token.

A single tool result is capped at the smaller of 50,000 characters and a quarter of
(`context_window` − `max_output_tokens` − tokens already in the prompt), converted to characters
at the learned ratio (4 characters per token before the endpoint has reported usage), and never
below 4,000 characters. On a 32,768-token tier one result cannot fill the window by itself. At
128,000 tokens or larger the cap stays 50,000 unless the learned ratio is above about 2. If the
in-flight turn's tool results still exceed the compaction budget, their middles are replaced
with a note to re-read with offset and limit before the next model call, largest first, until
the prompt is down to 70% of the budget (TD-5010). The gap lets the turn's next few results
fit without rewriting the ones already sent, which would throw away a local server's prompt
cache. The turn's results always keep at least 30% of the budget, though. When everything
else in the prompt (the system prompt, the tools, the history compaction keeps, and the turn's
results at 1,000 characters each) already fills more than 40% of it, the target rises, up to
the budget itself, rather than cutting every result down to 1,000 characters. A provider that still reports a context overflow is retried once at half the budget,
elided the same way. If it overflows again, the turn fails with a short message that names the
configured window. The raw provider error stays in the log.

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
- No API key is read or sent for an unbound loopback endpoint. Bind a named key
  (`credential`) when the local server requires one. A preset whose tiers are all
  unbound loopback needs no key at all; one off-box tier and the workspace needs
  one again.

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

### 3.6 Provider-specific request fields (`extra_body`)

Some servers take settings the OpenAI shape has no field for. `extra_body` on a tier is added
to the JSON body of every chat request that tier makes (TD-5008). On EZER, a llama.cpp
reasoning model behind LiteLLM, this keeps the thinking short so the answer fits inside
`max_output_tokens`:

<!-- verify: tier -->
```yaml
base_url: http://127.0.0.1:8000/v1
input_price: 0.0
output_price: 0.0
cache_read_price: 0.0
context_window: 32768
max_output_tokens: 4096
extra_body:
  chat_template_kwargs:
    reasoning_effort: low
```

It reaches the turn, the progress check and a delegated worker. On the worker tier it also
reaches the tool-call classifier, DoD polls and judgment calls that run on the worker, which
keep their own `max_tokens` of 8 and `temperature` of 0. The after-write review (validator
tier) and memory distillation (worker tier) send it too (TD-5146); the supervisor check does
not yet. The values go to the server as
written, so check the server's own documentation for the field names, and never put a key or a
token here. The settings screen keeps the block when it writes a slug, a credential or the
active preset.

`extra_body` only fills in fields a request left unset: where TST Desk sets a field itself, its
value wins. A `temperature` here applies to the turn, which sets none, but not to the
classifier, which asks for 0. The fields TST Desk owns on every request are a load error, so
a setting never applies to some requests and silently not to others:

<!-- verify: tier-invalid -->
```yaml
base_url: http://127.0.0.1:8000/v1
input_price: 0.0
output_price: 0.0
cache_read_price: 0.0
context_window: 32768
max_output_tokens: 4096
extra_body:
  max_tokens: 16384
```

```
Invalid model configuration in .../config.yaml: presets.<preset>.<tier>.extra_body: Value error,
extra_body cannot set max_tokens: TST Desk sends it itself. Use slug for the model and
max_output_tokens for the output cap
```

The reserved fields are `model`, `messages`, `tools`, `tool_choice`, `stream`,
`stream_options`, `max_tokens` and `max_completion_tokens` (the newer OpenAI name for the
output cap). Values must be plain JSON: strings, numbers, booleans, null, lists and maps. YAML
reads an unquoted `2026-10-09` as a date, so quote dates and times, or the config fails to
load with `extra_body must be plain JSON`.

**Verdict calls (`verdict_extra_body`).** The tool-call classifier, DoD polls and judgment calls
that run on the worker ask for a one-word answer in 8 tokens (TD-5012). On a reasoning model
the 8 tokens end inside the thinking, so the reply is empty: the classifier falls back to
Class B and a prose DoD item never goes green. `verdict_extra_body` is merged over `extra_body`
on those calls only, so the thinking can be off there and stay on for the turn and a delegated
worker. On EZER, a llama.cpp Qwen model behind LiteLLM:

<!-- verify: tier -->
```yaml
base_url: http://127.0.0.1:8000/v1
input_price: 0.0
output_price: 0.0
cache_read_price: 0.0
context_window: 32768
max_output_tokens: 4096
extra_body:
  chat_template_kwargs:
    reasoning_effort: low
verdict_extra_body:
  chat_template_kwargs:
    enable_thinking: false
```

A verdict call then sends `chat_template_kwargs: {reasoning_effort: low, enable_thinking:
false}`; everything else sends `{reasoning_effort: low}`. Nested maps merge key by key, and
`verdict_extra_body` wins where both set the same key. With `reasoning_effort: low` alone,
EZER's verdicts came back empty with `finish_reason: length`; with `enable_thinking: false`
the classifier answered in 2 tokens. Check your own server's documentation for its switch.
The same reserved keys and the plain-JSON rule apply. When a verdict comes back empty at its
cap, the daemon logs one warning per endpoint and model that names `verdict_extra_body`. If
`verdict_extra_body` is already set, the warning says to check how the switch is spelled.

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
`git`. On Windows, `cmd.exe` internals (`echo`, `dir`, `type`, …) have no file for `which` to
find; they are matched by name because the shell tool always runs through `cmd /c`. Path-shaped
tokens are never treated as internals. Only the leading binary of each segment is checked, so
`sh`, `sudo`, and `env` match as themselves: list them deliberately, because listing `sh`
allows anything `sh -c` can run. Listed binaries can also re-exec others — `find -exec`,
`xargs`, `make`, and any interpreter all walk through a basename match. Backticks are refused
outright, and a binary that cannot be resolved is refused fail-closed. This is a policy rail,
not a sandbox.

**Shell commands ask unless you skip them.** Every shell call is at least Class B: the
static classifier cannot see inside a command string, so shell never auto-runs on the
classifier's say-so (TD-4805). Settings → Policy **Dangerously skip permissions**
promotes every ask to auto, including shell and Class C (TD-806) — that is skip
everything; turning it on accepts that unparsed write forms and Class C steering
redirects will run. "Always allow this in this workspace" still writes a rule for the
exact command, not a blanket `shell: **`. A `never` rule still refuses.

**File contents change through `fs_edit` and `fs_write`, not the shell.** Only those two
tools name the files they touch, so only their changes get a diff in the Files and Work panes,
a checkpoint commit, and credit with the progress guard (§4.2). A file the shell writes
(`sed -i`, a `>` redirect, a heredoc, `tee`, `dd of=`, or a `python -c` / `node -e` write, also
inside `bash -c`) gets none of the three. The base prompt every tier gets says so, and so do
the three tools' descriptions (TD-5009). Deleting, moving or renaming files, formatters, code
generators and package managers stay in the shell. When a shell command's text looks like it
writes a workspace file, its result carries a one-line note, just under the exit code, that
points the next change to `fs_edit` and `fs_write` and says the write it saw need not be
redone. The note is read from the command text after the command has run, so it can appear on
a command that failed. It never blocks, delays or changes the command, and it is not a rule you
can set. Writes to `/dev`, `nul` or `$null`, `$TMPDIR` (also `${TMPDIR:-/tmp}`), a name set from
`mktemp`, a captured `.log`, git's own `.git/` files, or any path outside the workspace
(`/tmp/...`, `~/...`, `../...`) get no note. Input redirects (`< file`, `<<<`) are reads. In an
autonomous session the container's `/workspace` counts as the workspace.

### 4.2 `caps` — when the agent stops and asks

| Key | Type | Default | Effect |
|---|---|---|---|
| `spend_usd` | float ≥ 0 | `25.0` | Session spend, in dollars, that pauses the run. Settings → Budget writes this for the open workspace. Until it is set, the built-in $25 pause still applies and the title bar does not show a budget. |
| `wall_clock_hours` | float ≥ 0 | `8.0` | Session runtime, in hours, that pauses the run. |
| `max_iterations` | int ≥ 1 | `200` | Loop iterations that pause the run. |

All three are checked **before every model call**, in that order, and all three trigger at or
past the value rather than strictly past it. `spend_usd: 0` is legal and pauses before the first
call.

Hitting a cap **pauses**; it does not kill the session. The session moves to `paused` with the
violation as its reason, the UI raises a card, and the run waits — indefinitely, on an event, not
a poll. Resuming re-reads this file and re-checks the caps, so resuming without raising the cap
pauses again immediately. That is the loop: raise the number here, then resume.

**Interactive turns also pause when they stop making progress.** This is not a cap and it is
not in this file. Skip-all still skips the three caps above. It does not skip the progress
guard. An interactive turn pauses when the same tool call comes back three times, when 52
model calls have passed since the last successful `fs_write` or `fs_edit`, or when 12 tool
calls have passed with no reply and no file write. That twelfth stretch asks the model one
question with the tools removed. `DONE` ends the turn with the answer. `CONTINUE` with a new
next step allows another 12 calls, up to three times. Anything else pauses. Resume, on the
banner, starts the counters over and continues the same turn. A reply resets the 12-call
window only. A file write resets the 52-call budget too. Unattended autonomy runs keep the
charter breakers and do not take this check.

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
user data dir (glow and cursor default on; the real-display ring defaults on). Glow and
the agent cursor are drawn on the Screen pane. The rust ring on the real display
follows the same `cu_session` open/close tags (TD-3407) on macOS, Windows, and
Linux X11 — it stays up until the turn ends, you cancel, or the kill-switch
fires, not for eight seconds after the last click. Wayland has no real-display
ring. Screenshots hide the ring for the grab so it cannot appear in the frame.
This is not a second hardware pointer.

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
  host, and `web_search` answers for every host in its chain (`search.base_url` plus
  each fallback). A host outside the list is Class C (refused); an allowlisted host still
  asks (Class B). But `deny` does not stop
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
