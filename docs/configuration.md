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
| `shell` | mapping | see below | The read-only shell list (TD-5103): commands that run without asking. Omitted in an older user copy means the whole built-in list, on. Config can shrink it or switch it off, never add to it. |
| `remote` | mapping | see below | Opt-in Tailscale bind. Omitted in an older user copy is filled from the shipped file at load. Empty `bind` is loopback only. |
| `notify` | mapping | see below | Outbound notification channels. Omitted in an older user copy is filled from the shipped file at load. Slack and ntfy are off until their `enabled` flag is true and a destination URL is stored in the OS keychain. |
| `autonomy` | mapping | see below | Rootless container for autonomous runs (TD-4301) and interactive verify after writes (TD-4204). Interactive sessions ignore `runtime` and `image`. Omitted in an older user copy is filled from the shipped file at load. A missing or rootful runtime refuses start with install copy. |
| `mcp` | mapping | empty servers | User-listed MCP servers (TD-4401). Omitted in an older user copy defaults to no servers. Empty `servers` adds no doctor rows. HTTP `url` must be loopback; off-box is refused before dial. A server's token is `keychain:<entry>` in its `env` or `headers`, never the value (TD-5110). |
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

### `shell`

The shell commands that run without asking (TD-5103). Every other shell call
asks, as in §4.1. The list is fixed in the daemon: `shell.read_only` can drop
entries or switch the whole list off, and naming anything that is not built in
is a load error, so this file can never widen what auto-runs. Changes apply to
sessions opened after the daemon reads the file; restart the daemon after
editing it.

| Key | Type | Default | Effect |
|---|---|---|---|
| `read_only` | mapping | see below | The list. |

#### `shell.read_only`

| Key | Type | Default | Effect |
|---|---|---|---|
| `enabled` | bool | `true` | The one key that turns the list off: `false` and every shell command asks again. |
| `commands` | list of entries | all 16 | Which built-in entries are in force: `ls`, `pwd`, `cat`, `head`, `tail`, `wc`, `file`, `stat`, `git status`, `git log`, `git diff`, `git show`, `git branch`, `rg`, `grep`, `find`. Delete one to have it ask. Any other value refuses the load. |

<!-- verify: model -->
```yaml
presets:
  local:
    brain: { base_url: "http://127.0.0.1:11434/v1", input_price: 0, output_price: 0, cache_read_price: 0, context_window: 32768, max_output_tokens: 4096 }
    worker: { base_url: "http://127.0.0.1:11434/v1", input_price: 0, output_price: 0, cache_read_price: 0, context_window: 32768, max_output_tokens: 4096 }
    validator: { base_url: "http://127.0.0.1:11434/v1", input_price: 0, output_price: 0, cache_read_price: 0, context_window: 32768, max_output_tokens: 4096 }
active_preset: local
shell:
  read_only:
    enabled: true
    commands: [ls, cat, head, git status, git diff, rg]
```

<!-- verify: model-invalid -->
```yaml
presets:
  local:
    brain: { base_url: "http://127.0.0.1:11434/v1", input_price: 0, output_price: 0, cache_read_price: 0, context_window: 32768, max_output_tokens: 4096 }
    worker: { base_url: "http://127.0.0.1:11434/v1", input_price: 0, output_price: 0, cache_read_price: 0, context_window: 32768, max_output_tokens: 4096 }
    validator: { base_url: "http://127.0.0.1:11434/v1", input_price: 0, output_price: 0, cache_read_price: 0, context_window: 32768, max_output_tokens: 4096 }
active_preset: local
shell:
  read_only:
    commands: [ls, npm test]
```

**What qualifies.** A command runs without asking only when every command in
the line is one of the entries above, joined by `;`, newlines, `|`, `||` or
`&&`, and all of these hold:

- Words are plain (letters, digits and `_ - . / : = , + @ %`; `~` and `^`
  inside a word, as in `HEAD~1`) or quoted, with no `$`, backtick or `\` inside
  double quotes. A redirect (`>`, `2>/dev/null`, `<`), `$VAR`, `$(…)`, a
  backtick, an unquoted glob (`*`, `?`, `[`), braces, a leading `~`, a
  comment, `&`, parentheses or a wrapper such as `timeout`, `env` or
  `FOO=1` asks. Quote a glob you mean for the program itself:
  `find . -name '*.py'`.
- No option that writes, runs another program, reads a list of file names,
  or follows symlinks during a walk: `find -exec`, `-execdir`, `-ok`,
  `-okdir`, `-delete`, `-fprint`, `-fprint0`, `-fprintf`, `-fls`, `-L`,
  `-follow`, `-files0-from`; `rg --pre`, `--pre-glob`, `-z`, `-L`,
  `--hostname-bin`; `grep -R` (and BSD grep's `-S`, `-O`); `ls -L`; `wc --files0-from`;
  `file -C`, `-f`, `-z`; `git --output`, `--ext-diff`, `--show-signature`.
  A long option abbreviated to a prefix of one of these (`--deref`) asks too.
- `git` takes no option before the subcommand except `--no-pager`, `-P` and
  `--no-optional-locks`, so `git -c …`, `git -C …` and `--git-dir` ask.
  `git branch` only lists: any other option, or a branch name without
  `--list`, `-a`, `-r`, `--contains`, `--merged` or `--points-at`, asks
  (`git branch -v name` would create `name`).
- Every word, and every value an option may carry, resolves inside the
  workspace after symlinks are followed. So `cat /etc/hosts`, `ls ..` and a
  symlink that leads out of the workspace ask. A pattern that starts with
  `/` (`rg /api`) asks for the same reason. A long option's value is the
  text after `=`. A short option's starts after whichever letter of the
  cluster takes one, which the list does not guess: in `file -bm/etc/x`
  both `m/etc/x` and `/etc/x` are checked, so it asks. A cluster longer than
  256 characters asks. A word that looks like an option is checked as a
  file too, because BSD tools (macOS) stop reading options at the first
  operand.
- The program is found on `PATH` outside the workspace, and `PATH` has no
  empty or relative entry.
- For a git entry: the workspace root holds its own `.git` (a workspace
  inside a bigger repository would list files outside the wall), git is
  2.40 or newer, `GIT_DIR` and `GIT_WORK_TREE` are unset, and neither
  `GIT_CONFIG_GLOBAL` nor `GIT_CONFIG_SYSTEM` names a file inside the
  workspace.
- For `rg`: `RIPGREP_CONFIG_PATH` does not name a file inside the
  workspace, since one line of it can add `--pre`.

On Windows the list never matches, because commands run through `cmd /c`.

**What a listed git command runs with.** git reads its own config and
attributes, and both can name a command to run. A git command the list reads
runs with `core.fsmonitor=false`, `core.hooksPath` and `core.attributesFile`
pointed at nothing, and `safe.bareRepository=explicit` (added through
`GIT_CONFIG_COUNT`, after any you set), in-tree `.gitattributes` read from
the empty tree (`GIT_ATTR_SOURCE`), `GIT_OPTIONAL_LOCKS=0` so `git status`
does not rewrite the index, and `GIT_NO_LAZY_FETCH=1`. So no attribute file
the agent can write routes a file through a filter or textconv driver your
own config names, and a hook in the tree does not run. Only
`.git/info/attributes` still applies. This holds whoever approved the command,
so `git diff` from the list shows raw bytes where a textconv driver would
have converted them. In a SHA-256 repository the empty tree has another
name and git refuses with `bad --attr-source`; take the git entries off
the list there.

A user policy rule (§4.4) still decides first: an `ask` or `never` rule that
matches stops a listed command as before. The audit database records the
entries that let each call run in `allowed_calls` (one row per call, keyed by
its `tool_calls` row).

**Rules that guard a file guard it here too.** An `ask` or `never` rule for
`fs_read`, `fs_list` or `fs_grep` (or a glob such as `fs_*` that covers one)
is a guard: the TD-5141 recipe in §4.4 names `.env` that way. While you have
any guard, a listed command asks, as any shell call does, when:

- a word names a file a guard matches, compared as written, normalized, and
  workspace-relative with symlinks followed, ignoring case: `cat .env`,
  `grep KEY ./.env`, `head link-to-env`, `grep -f.env x a.txt`; or
- it can print a file that no word names: `grep -r` (and `--recursive`,
  `-d recurse`, `--directories`), `rg`, `git diff`, `git show`, `git log`,
  and `git status -v`. Whether one of those would reach a guarded file
  can't be known before it runs.

`ls`, `find`, `git status` and `git branch`, which print names rather than
contents, and reads of unguarded files stay on the list as long as no word
names a guarded file. A command that asks this way runs on your yes, and an unattended
run takes it without asking, as it takes every Class B call. To refuse it
outright, add a `shell` rule too, such as
`{ tool: shell, args: "*.env*", effect: never }`, which refuses any command
whose text mentions `.env`.

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
time. A stall (`stream_idle_timeout`, or `read_timeout` mid-stream,
TD-5106) counts as a timeout here too. Other retryable failures (429,
5xx, a refused connection) are retried as above. Both calls stream, so
the read timeout is the longest gap between chunks rather than a cap on
the whole answer. The first gap is the whole prompt being read, so the
after-write review's prompt is capped by `autonomy.verify_max_prompt_tokens`
(TD-5153).

A gateway can also fail inside a `200` stream. LiteLLM reports an
upstream failure partway through a reply as an in-band
`data: {"error": {...}}` event and then ends the body without
`data: [DONE]`; OpenRouter sends the same `error` object beside a choice
whose `finish_reason` is `"error"`. Either shape, and a `finish_reason`
of `"error"` on its own, counts as the failure. Before anything has streamed (an opening chunk
that only names the role, with no text, reasoning or tool call, does not count), that event is
treated as the HTTP error its `code` names: a 429 or 5xx is retried as above, and a
400, 401 or 404 is not. A `code` that is not an HTTP error status (400 to 599), or no `code`,
counts as a 502. The exception is a window refusal named by a string code such as
`context_length_exceeded`, or only in its message: it counts as the 400 it is, so it becomes
`context_overflow` and gets the one retry with a tighter window instead of being resent
(TD-5158). Once part of the reply has streamed, it ends the turn as
`stream_interrupted` and is not retried, because a second attempt would
repeat text the window already shows. A stream that ends with neither a
`finish_reason` nor `[DONE]` is `stream_interrupted` too. The window
shows "Response interrupted", the partial text is not kept as the
model's answer, and Retry on the message resends it (TD-5152). The
after-write review and memory distillation get the same errors.

| Key | Type | Default | Effect |
|---|---|---|---|
| `max_retries` | int 0–20 | `3` | Retries after the initial attempt. `0` disables retrying; the first failure ends the turn. |
| `initial_delay` | float > 0 | `1.0` | Base delay before the first retry, in seconds. Each subsequent wait doubles. |
| `max_delay` | float > 0 | `60.0` | Ceiling on any single wait, in seconds. Bounds the worst case when `max_retries` is high. |

### `scheduler`

How long one scheduled turn may run before the daemon cancels it (TD-3819). The same number is the base wait for `tst run`. A job can set its own limit, from 1 to 60 minutes, on the Scheduled form. When the limit expires the turn is cancelled and the receipt says `stopped after N minutes (max run time)`. That reason is not retried.

`tst run --timeout SECONDS` replaces this base for one command. The provider retry allowance is still added when the config can be read, the same as before. The wait is the same when `tst run --session` or `--continue` sends into an existing session (TD-5111); see architecture.md for its output modes and exit codes.

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
`--network=host`, and `--memory` at the charter's `caps.shell_memory_mb`,
half of RAM when it is unset, TD-5116). The argv never bind-mounts `$HOME`, `~/.ssh`, cloud
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
| `verify` | `off` \| `after_write` \| `ask` | `after_write` | Interactive: after a filesystem write turn, call the validator once (`after_write`), ask first (`ask`), or never (`off`). Autonomy sessions ignore this. The call streams and sends the validator tier's `extra_body`, so ending the session or quitting also ends the server's work; a timed-out attempt is reported as a verify error, not resent (TD-5146). Stop ends a running review although it starts after the turn has ended: the call is cancelled, which hangs up the stream, the timeline shows `Verify stopped.` with no verdict, and the session is free for the next message. A message already queued behind the review still runs. Stop leaves an `ask` review that is waiting for confirmation alone (TD-5151). The review prompt is cut to fit `verify_max_prompt_tokens` first: shell output loses its head (each output keeps its tail), then the oldest diffs go, with `(N earlier lines omitted)` / `(N earlier diffs omitted)` markers where the cuts are, and the verdict's summary ends with what was left out. A prompt that still does not fit, or has no room for any of the diff, is not sent and is reported as a verify error (TD-5153). |
| `verify_max_prompt_tokens` | int ≥ 1024 \| null | `null` | Most estimated tokens the after-write review's prompt may take: the validator steering, the diffs, the shell output and the instructions together. `null` is 60% of the validator tier's `context_window` (about 39k tokens on a 65,536-token slot, which a local server reading ~390 tokens/s finishes inside the default 120 s `read_timeout`). Never more than `context_window` less `max_output_tokens`, whatever is set. Estimated with the token counter the turn uses, scaled by the ratio learned from the validator endpoint's usage (TD-5007); each review that finishes teaches that ratio too. The 120 s figure holds once that ratio is learned. Before then (a validator endpoint, `base_url` plus slug, that differs from the brain's and has no saved ratio) the flat 4 characters per token can undercount: on a model that reports about 1.6 times the estimate, a full 39k estimate is about 61k real tokens, roughly 156 s at 390 tokens/s, so the first big review there can time out, and a review that times out teaches nothing. Lower it when a slow local validator times out; the first chunk arrives only after the whole prompt is read (TD-5153, TD-5158). |

### `mcp`

User-listed MCP servers (TD-4401). The user names each server in
user-data-dir `config.yaml` — that listing is the load, not dynamic
discovery (TD-601). Interactive and autonomy sessions both load them.
A dead or refused server is a doctor row (`mcp:<id>` after `skills`),
not a dead daemon. Builtins stay registered. The computer-use sidecar
(`computer_use.command` → `mcp/tst-cu-mcp`) is a different product.
A packaged sidecar with empty `command` serves `tstd --cu-mcp` (TD-1725).

Tool names are prefixed `{server_id}__{remote_name}` so an MCP tool
named `fs_read` cannot replace the builtin. Registry provenance is
`mcp:<server_id>`. The approval floor is `ask` (TD-4402 classifies
path/host fields). Settings persist add / disable / remove through
`set_mcp_server` and `delete_mcp_server`; the write is surgical so
teaching comments survive. There is no `env` or `headers` field on those
messages: a rewrite keeps the server's existing ones, unless it changes
the transport, which drops them and deletes their keychain items. Live
sessions keep the tool set they attached with until a new session.

**Tokens per server (TD-5110).** A stdio server's `env` and an http
server's `headers` map names to values. A value is either a literal,
which is not a secret because it sits in this file, or
`keychain:<entry>`. A reference names the OS keychain item
`tst-mcp.<server id>.<entry>` (service `com.thatsimpletech.tstdesk`), so
a server can only reach its own items. The daemon reads each item when
it starts that server and gives it to that child's environment, on top
of the daemon's own, or to that server's requests. It never goes into
the daemon's environment, so no other child sees it. A missing or
unreadable item fails that server with a doctor row that names the item;
the server does not start without it. `env` on an http server, or
`headers` on a stdio one, fails the server the same way.
Requests to an http server never use a proxy: `HTTP_PROXY`,
`ALL_PROXY` and the macOS or Windows system proxy are ignored for them,
so a header goes only to the configured loopback URL.

Settings → MCP servers stores a token without ever showing it. Under a
listed server, name it (an env var for stdio, a header such as
`Authorization` for http, whose value is the whole header, for example
`Bearer …`) and save. The value goes to the keychain under the name
lowercased (`GITHUB_TOKEN` → `github_token`) and this file gets only the
reference. Saving a name that already has an entry replaces the value
there. Remove drops the name from this file and deletes its item;
removing the server deletes all of its items. A stored value must be
one line of printable ASCII and at least 8 characters. Every value the
daemon reads or stores is scrubbed by its exact text from the log, the
audit DB and the events from then on, whatever its shape. Settings
lists each name with where it lives (keychain, stored or not, or this
file); it never shows a value, literal or not.

Strict OpenAI-compatible servers (vLLM, llama.cpp, LM Studio) reject the
whole request when one tool is malformed, so each listed tool is checked
at load (TD-5104). A root `anyOf` / `oneOf` / `allOf` is flattened into
one object schema where that is possible. A tool whose prefixed name is
over 64 characters or uses anything but letters, digits, `_` and `-`, or
whose input schema still fails JSON Schema validation, is left out. The
doctor row and the Settings row name each one with its reason; the
server's other tools still load.

Every tool call carries a progress token (TD-5105). Progress the server
reports resets the call's idle timer, so a render or a build can run past
`timeout_seconds` as long as it keeps reporting, up to
`max_call_seconds`. Progress shows on the tool's row in the step
timeline, on the desktop and the phone. A call that times out, or whose
turn the user stops, sends `notifications/cancelled` to the server so it
can stop its work too. Over `http` there is no progress yet (one JSON
reply per call), so `timeout_seconds` bounds the whole call.

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
| `timeout_seconds` | number > 0 | `30` | Longest silence in a tool call: no result and no progress for this long ends the call and sends `notifications/cancelled`. Over http it bounds the whole call. Settings keeps it when it toggles or edits the server. |
| `startup_timeout_seconds` | number > 0 | `8` | Time allowed for each step of the handshake (`initialize`, then `tools/list`), whatever `timeout_seconds` says. Raise it for a server that loads a model before it answers. |
| `max_call_seconds` | number > 0 | `600` | Hard cap on one stdio tool call, progress or not. Never below `timeout_seconds`. |
| `env` | mapping of name → string | `{}` | stdio only. Extra environment for this server's child, on top of the daemon's. Names are `[A-Za-z_][A-Za-z0-9_]*`. A value is a literal or `keychain:<entry>` (entry: lowercase letters, digits, `_` and `-`, at most 64). Numbers become strings; a YAML boolean is refused, so quote it. A value may not hold a line break or NUL. |
| `headers` | mapping of name → string | `{}` | http only. Extra headers on every request to this server, same value rules as `env`. Names are HTTP field names, one spelling each (case is ignored). `Accept`, `Connection`, `Content-Length`, `Content-Type`, `Host` and `Transfer-Encoding` are TST Desk's own and are refused. |

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
  verify_max_prompt_tokens: null
mcp:
  servers:
    example:
      transport: stdio
      command: ["python", "-m", "some_mcp"]
      enabled: true
    render:
      transport: stdio
      command: ["python", "-m", "render_mcp"]
      timeout_seconds: 120
      startup_timeout_seconds: 30
      max_call_seconds: 1800
      env:
        RENDER_TOKEN: "keychain:render_token"
        RENDER_QUALITY: "draft"
    loop:
      transport: http
      url: http://127.0.0.1:9
      enabled: true
      headers:
        Authorization: "keychain:authorization"
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
| `connect_timeout` | float > 0 | `30` | Seconds to open the connection to `base_url`. |
| `read_timeout` | float > 0 | `120` | Longest wait for any bytes from the server, the response headers included. A local model reading a long prompt sends nothing until its first token, so this is the knob for slow prefill — see §3.5. |
| `total_timeout` | float > 0 or `null` | `null` | Wall-clock cap on one attempt, streaming included. `null` means no cap. A stream cut by it after output began is not retried. Before TD-5106 the client had a field named "total" set to 180; it was only the wait for a free pooled connection and never bounded a request. That wait stays 180 s and is not a key. |
| `stream_idle_timeout` | float > 0 or `null` | `300` | Aborts a stream that sends no usable chunk for this long. SSE keep-alive comments (`: ping`) do not count, so a server that only pings is caught; `read_timeout` alone never catches it. A stall before any output is retried under `provider_retry`; a stall partway through a reply ends the turn as `stream_stalled`, because a retry would repeat text the window already shows. Reading stops at `data: [DONE]`, so a server that holds the body open after a finished reply is not a stall (TD-5152). `null` turns the watchdog off. |

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

**Seeing and steering compaction (TD-5109).** The title bar shows how full the window is, such
as `12k / 64k`, from the daemon's own estimate of the next request (measured as described
below). The bar turns amber in the last quarter before compaction and red once the next request
would compact. Hover it for the exact figures, the point where compaction triggers, and a
**Compact now** button. Three ways to compact without waiting for the budget:

- **Compact now**, or `/compact` alone in the composer: everything between the system message
  and the latest user turn becomes the summary. The latest turn stays word for word.
- `/compact keep the parser work` (any words after the command): the same, and the summary
  opens with those words. A message that mentions any of their longer words (four letters or
  more) keeps up to 600 characters instead of 200, and when the summary is over its cap, older
  messages that don't mention them are dropped first. This is plain text matching, not a model
  call, so it costs nothing and gives the same result every time. Guidance past 500 characters
  is cut.
- **Summarize from here**, beside **Edit** on a user message: that message and everything after
  it become the summary; everything before it stays word for word.

None of these runs while a turn is in progress. Stop the turn first, or wait for it. They change
what the model is sent, not the window: every message stays on screen. A message that is already
inside a summary can't be the start of another one; a message before or after that summary still
can, and that holds after the app restarts. Each summary opens with how many of your messages it
stands for, which is how a message is found again. The timeline records each compaction with
who asked for it, the token counts before and after, and the guidance. Grok sessions keep Grok's
own slash commands and don't show the gauge.

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
configured window. The raw provider error stays in the log. Each trim shows on the timeline as a
quiet **Trim** row that leads with its numbers, **N results · Xk tokens freed**, not as a
compaction: no message is dropped, and only the middles of this turn's results are shortened. A
pane 360px or wider adds "Trimmed tool output" and the before and after token counts (TD-5150).

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

**Timeouts for a slow local model (TD-5106).** The tier defaults suit a hosted API. A local
model on EZER can spend minutes reading a long prompt before its first token, and a server
that hangs mid-load may keep the socket open with keep-alive pings. Give each local tier room
to read and a ceiling on the whole reply:

<!-- verify: tier -->
```yaml
# slug omitted: resolved from the endpoint's /v1/models
base_url: http://127.0.0.1:8000/v1
input_price: 0.0
output_price: 0.0
cache_read_price: 0.0
context_window: 65536
max_output_tokens: 4096
connect_timeout: 10        # loopback: a refusal is instant, a hang is not
read_timeout: 600          # first token after a long prefill
stream_idle_timeout: 600   # silence mid-reply that still counts as alive
total_timeout: 3600        # no single reply runs past an hour
```

A stall before any output is retried under `provider_retry`, so with the default three retries
a dead server costs up to four waits of `read_timeout` (or `stream_idle_timeout` when the
server pings). Lower `provider_retry.max_retries` with long timeouts. The after-write review
and memory distillation never resend a stall (TD-5146), so they wait once. A scheduled job
whose turn ends as `stream_stalled` failed for a transient reason, like a timeout, so a job
with `retries` tries again after its `retry_delay` (TD-5154).

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

**A file that loosens anything waits for your trust (TD-5113).** This file usually arrives with
a clone, so one that loosens the packaged defaults (an `auto` rule, a network host, a raised
cap, an external import) is shown to you first and does not apply until you trust it. See §4.8.

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

**Proposed steering edits (TD-5140).** The agent can still propose a change to a steering
file with `propose_steering_edit`: `AGENTS.md`, `CLAUDE.md`, a `.tst/rules/*.md` rule, a
`SKILL.md`, or a slash-command file (`.tst/commands/*.md`, `.claude/commands/*.md`) inside the
workspace. The tool writes nothing and is Class A (static rule `steering-proposal`), so it
never shows an approval card of its own; a `never` policy rule for it still refuses it. The
window shows the diff on a card with Accept, Edit and Reject. Only Accept, or Edit then Save,
writes the file, and the daemon writes it, never the filesystem tool. If the file changed
after the proposal, Accept is refused ("the file changed since this was proposed") and a
fresh diff replaces the card. The signed charter (`.tst/autonomy/CHARTER.md`) and
`.tst/config.yaml` can't be proposed, and neither can a path that runs code later (below).
Accept and Save are only taken from this computer's own window (`loopback_only` from a phone
or another computer); Reject works from a phone. Every accepted, edited or rejected proposal
goes in the session log as `steering_decision` with its diff. The card shows exactly what
Accept writes. The session log blanks secret-shaped strings (a long run of letters, digits,
`-` or `_` after `sk-`, `ghp_` and the like; ordinary words such as `task-tool-availability`
can match too), so a proposal whose file name, current file or new text holds one is refused
(`steering_secret_shaped`), and so is a Save of one; change that file by hand. Open proposals live in the daemon's memory, so a restart closes them: the daemon logs
each one as `steering_decision` `expired` when it restores the session, its card does not
come back, and the agent has to propose it again. A card the daemon no longer holds says
that nothing was written and offers only Dismiss.

**Paths that run code later ask first (TD-5108).** Some files run nothing when written, but
git, a login shell, direnv or an editor runs what they say the next time it starts. A write to
one of them is Class C, so it asks even though it is inside the workspace and inside this
list:

- any path with a `.git`, `.vscode`, `.idea` or `.husky` folder in it, at any depth — a nested
  `sub/.git/` with `HEAD` and `config` makes `sub` a repo, and a `.git` file is a pointer to one;
- `.envrc`, `.gitconfig`, and shell startup files (`.profile`, `.bashrc`, `.bash_profile`,
  `.bash_login`, `.bash_logout`, `.bash_aliases`, `.zshrc`, `.zshenv`, `.zprofile`, `.zlogin`,
  `.zlogout`), by name at any depth;
- git's global folder, `~/.config/git/` or `$XDG_CONFIG_HOME/git/` (TD-5157): its `config` is
  read like `~/.gitconfig` and can include other files, `attributes` can route any file through
  a filter, and `ignore` sits beside them. Any path with `.config/git` in it counts, and so does
  one written as `$XDG_CONFIG_HOME/git/…`, `${XDG_CONFIG_HOME:-…}/git/…` or
  `%XDG_CONFIG_HOME%\git\…`. So does anything that is in `~/.config/git/` or in the folder
  `XDG_CONFIG_HOME` names when it is set, judged by where it is, so a workspace that is
  `~/.config` itself (where the path reads `git/config`) is covered too. `~/.config/git/` counts
  even when `XDG_CONFIG_HOME` points elsewhere;
- git hook-manager configs: `.pre-commit-config.yaml`, and lefthook's `lefthook.yml` and
  `lefthook-local.yml` in any of `.yml`, `.yaml`, `.toml`, `.json`, dotted or not. Your own
  `git commit` runs what they name.

Names match ignoring case. This covers `fs_write` and `fs_edit`, and shell redirects and `tee`
into these paths (`echo … > .git/hooks/pre-commit`, `… >> ~/.bashrc`,
`… >> ~/.config/git/config`); for a redirect, both the path as written and the file it links to
count. On Windows, where a command runs through `cmd /c`, the line is
also read the way cmd reads it (TD-5157): `\` separates folders, `^` escapes the next character,
and `>`, `>>`, `1>` and `>&` redirect with or without a space, so `echo x > .git\hooks\pre-commit`
and `type nul > "%USERPROFILE%\.gitconfig"` ask too. Other shell write forms (`cp`, `mv`,
`sed -i`, `copy`) are not parsed; they ask through the shell floor below. Unlike a steering
file, you can approve the write. An `auto` policy rule cannot pre-approve it (§4.4), and
"Always allow" is not offered. Reads are unaffected. Skip-all runs these writes without asking,
like every other Class C ask (§4.4); an autonomous run stops on them.

Every git command the daemon runs itself (checkpoints, memory commits, the charter sign, the
workspace manifest, `fs_grep`, the autonomy gate and summary, revert, the GitHub connector) is
started with `-c core.fsmonitor=false -c core.hooksPath=/dev/null -c
safe.bareRepository=explicit` (`nul` on Windows). Command-line settings beat every config file,
so with or without skip-all the daemon never runs a repo's fsmonitor command or any hook: not
`.git/hooks`, not a `core.hooksPath` folder, not a workspace script a hook calls. Your own `git
commit` still runs your hooks; the daemon's memory and charter commits and the connector's push
skip them, including a secret scanner. The validator's diff runs no external diff or textconv.

The daemon's git runs no filter driver either (TD-5157). A driver's command lives in git config,
but an in-tree `.gitattributes`, an ordinary write, can point any file at a driver you
configured, such as `nbstripout` (which imports from the repo root) or git-lfs (which reads
`.lfsconfig`). So checkpoints, memory commits and the charter sign stage the bytes on disk
(`git hash-object --no-filters` into an index), never `git add` or `git commit`, and the drift
revert writes blob bytes itself instead of `git checkout-index`. Git's own end-of-line rules are
redone around that, so line endings come out as before. The calls that must read the
working tree or write your index (`git status` for the dirty-tree notice, the validator's
`git diff`, and updating your index after a memory or charter commit) set every configured
driver's `clean`, `smudge` and `process` to nothing for that call. Those switches cover the
drivers the workspace repo's config names, not ones only a submodule's own config names, so
`git status` and the validator's `git diff` do not look inside submodules: a submodule shows
only when its commit moved, as a `Subproject commit` line, never as an inline diff (which
would run the submodule's own external diff). What changes:

- **Line endings: unchanged.** The daemon applies git's own rules (`text`, `eol` and `crlf`
  attributes, `core.autocrlf`, `core.eol`, and git's "safer autocrlf" checks): a CRLF file is
  stored as LF in a checkpoint where `git add` would store it so, and a revert writes CRLF back
  where a checkout would. A test holds both directions to git's own output. The `ident` and
  `working-tree-encoding` attributes are not applied.
- **git-lfs.** A checkpoint stores an LFS file's content, not a pointer, so the branch holds the
  whole file. A revert that takes a pointer from HEAD fills it from the local store
  (`.git/lfs/objects`) when the object is there and its size and SHA-256 match; nothing is
  fetched. Otherwise it writes the pointer text, logs the path (`lfs_pointers`), and
  `git lfs checkout` fills it in.
- **A revert where a file and a folder swapped places** removes what the drifted checkpoints
  added first, then writes the good version, so either direction comes back. Unlike
  `git checkout-index -f`, it never removes content no checkpoint holds: a folder still in the
  way after that (say it holds a file you made) or a file where a folder must go stops the
  revert with an error and leaves both, and the auto-branch is not moved.
- **Memory and charter commits** are built with `commit-tree` and land on HEAD with a
  `commit: …` reflog line, as before. They are signed when `commit.gpgSign` is set. A memory
  commit whose note did not change is skipped, and one during a merge, cherry-pick or revert is
  refused, as `git commit -- <path>` refuses it.

Still not pinned: `gpg.program` when `commit.gpgSign` is set (memory and charter commits), and
`core.askPass` (the connector). They run only when a config file names them, and
`.git/config`, `.gitconfig` and `~/.config/git/` are Class C to write, so planting one asks.
Under skip-all it does not ask, and the daemon would then run the planted command. The
connector's `clone` checks out like your own `git clone`, with your filters. The filter
switch-off lists the drivers git config names just before each call, so a driver added in the
moment between needs a write to one of those Class C files. This is recorded in DECISIONS.md
(TD-5108, TD-5157) as residual risk.

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
classifier's say-so (TD-4805). The one exception is the fixed read-only list in
§2 `shell` (TD-5103): `git status`, `cat a.txt | head` and the like are Class A from the
rule table, never from a model's judgment. Settings → Policy **Dangerously skip permissions**
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
| `shell_memory_mb` | int ≥ 64 \| null | `null` | Memory, in MiB, that one shell command may use. `null` is half of the machine's RAM. A command over it is killed; it does not pause the run. See below. |

The first three are checked **before every model call**, in that order, and all three trigger at
or past the value rather than strictly past it. `spend_usd: 0` is legal and pauses before the first
call.

Hitting one of those three **pauses**; it does not kill the session. The session moves to `paused` with the
violation as its reason, the UI raises a card, and the run waits — indefinitely, on an event, not
a poll. Resuming re-reads this file and re-checks the caps, so resuming without raising the cap
pauses again immediately. That is the loop: raise the number here, then resume.

**`shell_memory_mb` caps each shell command's memory (TD-5116).** A runaway build used to be
able to take the machine's memory, and the kernel's out-of-memory killer could take the daemon
with it. Every command the shell tool runs now gets its own cap, applied the strongest way the
machine allows:

- **Linux with systemd user scopes.** The command runs in its own transient scope
  (`systemd-run --user --scope`, unit `tstd-shell-<id>.scope`) with `MemoryMax` at the cap,
  `MemorySwapMax=0` so it is killed at the cap rather than pushed into swap, and
  `OOMPolicy=stop`. The kernel kills inside that scope only, never the daemon. Afterwards the
  daemon reads the unit's result, or the scope's `memory.events` when systemd has not caught up
  yet, and clears the failed unit.
- **Other Linux.** The shell sets `ulimit -v` (`RLIMIT_AS`) to the cap and then runs the
  command, so the command and everything it starts inherit the limit and cannot raise it. This
  counts address space, not resident memory: an allocation past it fails and the command dies of
  its own out-of-memory error. Runtimes that reserve large address ranges up front (sanitizers,
  some JVM and WebAssembly setups) can fail well below the cap here; raise the cap if they do.
  A lower limit the daemon already runs under is kept.
- **Autonomous sessions.** Podman runs get `--memory=<cap>m`, from the charter's `caps` (a
  charter's caps replace this file's for the run).
- **macOS and Windows hosts.** Not capped: macOS does not enforce `RLIMIT_AS`, and neither has
  systemd scopes. Autonomous runs there are still capped by Podman.

Which Linux method works is probed once, on the first shell command after the daemon starts: a
scope with the same settings must start, receive the command text unchanged (systemd 254+ is
told not to expand `$$` and `${x}` in it), and show the cap in its `memory.max`. The answer holds
until the daemon restarts, so restart it after changing systemd or cgroup setup.

When the cap stops a command, its result says so plainly, on a line under the exit code:
`memory cap: killed — the command went over its 2048 MB memory cap (caps.shell_memory_mb).` With
`ulimit` the line says the command ran out of memory at its cap, read from its error text
(`MemoryError`, `Cannot allocate memory`, `std::bad_alloc`, `out of memory`, …; a JavaScript
heap limit is not counted). In a container an exit of 137 (SIGKILL) is reported as the likely
cause. A command the tool itself timed out, cancelled or stopped gets no such line. Each command
has its own cap; two commands running at once can each use it. A value above half of RAM loosens
the default, so a repo's config that sets one waits for your trust (§4.8). Skip-all does not
lift it: the cap is applied by the shell tool, not by an approval.

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
| `max_file_bytes` | int ≥ 1 | `4000000` | Size of one attached file. Measured on the file's real bytes, after decoding. |
| `max_total_bytes` | int ≥ 1 | `8000000` | Size of all the attachments on one message, added together. |
| `max_count` | int ≥ 1 | `10` | Files on one message. |

**Text files, and images when the active tier has vision.** A file that is not valid UTF-8, or
that contains a NUL byte, is refused as binary. A PDF, a compiled binary, and a UTF-16 text
file land here. PNG, JPEG, GIF, and WebP are accepted when the active tier sets `vision: true`.
The bytes are kept as they are. A file over `max_file_bytes` is refused whole.

**These are enforced by the daemon, not by the composer.** The message-box refuses an oversize
or binary file early, with copy that names the file, but that is a convenience. The same
refusal happens again on arrival, so a client that skips the check — an older build, or
anything that is not the app — gets the same answer. Both sides read the numbers above; the
daemon sends them to the UI on the `boundary_update` event when the workspace opens.

**A refusal drops the whole message**, including the text you typed alongside it. Nothing is
half-sent, and the error says so. Attachments are also all-or-nothing within one message: one
bad file refuses the send rather than quietly delivering the rest.

**There is a ceiling above these numbers.** The daemon's local websocket accepts frames up to
16 MiB. Base64 adds about a third, so a `max_total_bytes` much above `12000000` is a message
the transport drops before the daemon can refuse it with the copy above. The defaults sit
under that ceiling: 8 MB of files is about 10.7 MB on the wire.

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
| `rules` | list of rules | `[]` | Per-tool overrides. `never` beats `ask` beats `auto`, however specific the rules are. A shell command is checked one command at a time (below). |
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
in the approval card writes a rule here for you, and keeps the file trusted (§4.8).

**Always allow saves a rule for that one call (TD-5149).** The rule names the exact path, or
the one command, with any `*`, `?` or `[` in it made literal. A read of `app/[id]/page.tsx`
saves `args: "app/[[]id]/page.tsx"`, which matches that file and no other, and a file literally
named `*.env*` saves `"[*].env[*]"`, not a glob over every env file. Settings → Policy shows such
a rule as `exactly app/[id]/page.tsx`, with the saved pattern on hover; a glob shows as written.
Only `auto` rules are shown as exact. An `ask` or `never` rule always shows as written, because
it also matches the text in another case (`.env` catches `.ENV`), and "exactly" would understate
what it guards (TD-5158).

- Always allow never replaces a rule you wrote. When an `ask` or `never` rule with the same
  `tool` and `args` is saved, the card doesn't offer it. A click sent anyway (a card shown
  before the rule was added) saves nothing: it runs the call once for `ask`, refuses it for
  `never`, and says so. Revoke the rule first if you want the call to run without asking.
- Always allow and Revoke read this file when you click, so a rule you add or remove by hand
  while a session is open is checked and kept, not written over with the rules the session
  opened with. After the save the session runs under the file as saved. A hand edit to a file
  that loosens anything waits on the trust card first (§4.8), and nothing is saved into it
  until then, so your edit is kept that way too. If the `policy:`
  section can't be read (bad YAML or a bad value), nothing is written: Always allow runs the
  call once and Revoke fails, both saying why, until you fix the file.
- A wider `ask` or `never` rule, such as `*.env*`, still applies to the file Always allow saved
  a rule for, because rules are deny first (below).

**Reads inside the workspace never ask (TD-5141).** `fs_read`, `fs_list` and `fs_grep` on a
path inside the workspace are Class A by a fixed rule, so they run without asking and without
a model call to judge them. That includes `.env` and other secret files. A rule still wins, so
to guard a file, name it for both tools that read contents. The same rules keep the
read-only shell list (§2 `shell`, TD-5103) off the file: `cat .env`, `grep -r KEY .` and
`git show HEAD:.env` ask instead of running.

<!-- verify: workspace -->
```yaml
policy:
  rules:
    - tool: fs_read
      args: "*.env*"
      effect: never
    - tool: fs_grep
      args: "*.env*"
      effect: never
```

- `effect: ask` asks instead, but an unattended (autonomy) run treats `ask` as `auto`, so only
  `never` holds there.
- Through shell, these rules make a listed command ask, not refuse. Add a `shell` rule with
  `effect: never` (`args: "*.env*"`) to refuse it there too.
- A rule matches the path the call names. `fs_grep` over a whole folder (`path: .`) isn't caught
  by a `*.env*` rule. In a git repo that search skips `.gitignore`d files, so keep `.env`
  ignored, as most templates already do. `fs_list` shows names only, never contents.
- `ask` and `never` rules ignore case, so `.ENV` is caught on macOS and Windows, where it opens
  `.env`. `auto` rules match case exactly.
- A `never` rule wins over any `auto` rule (TD-5101), so a broad `fs_read: "*"` auto rule no
  longer hides `*.env*`. Specificity only picks which rule a refusal names: fewer wildcards and
  an exact tool name count as more specific.

Outside the workspace nothing changed: reads there are Class C and the path guard refuses them
before any rule is read, symlinks included.

**Rules are deny first (TD-5101).** A call is refused when any `never` rule matches it. If none
does, it asks when any `ask` rule matches. Only then can `auto` apply. A narrow `auto` rule never
beats a broad `never` or `ask` rule. When several rules of the winning effect match, the most
specific one is the one the refusal or approval card names. No matching rule leaves the
decision class to decide, as before.

**Shell rules judge every command in the line (TD-5101).** `args` for `shell` is matched against
each command of a chained line, not the line as a whole. The line is split at `;`, `&&`, `||`,
`|`, `&`, newlines and parentheses, after each `\`-newline is removed as the shell removes it,
and the commands inside `$(…)`, backticks, `<(…)`, the `$(…)` of an unquoted heredoc, the string
given to `eval`, `sh -c` or `cmd /c`, an `alias` value and a `trap` action count as commands
too (TD-5156). Quoted text is not split, so `git commit -m "fix; docs"` is one command.

- An `auto` rule applies only when every command matches an `auto` rule. With `npm test*`
  allowed, `npm test; curl x`, `npm test && rm -rf y`, `npm test | sh` and `npm test $(curl x)`
  all ask, because `curl x`, `rm -rf y`, `sh` and the substitution have no rule.
- A `never` or `ask` rule refuses or asks when it matches any one of the commands, or the whole
  line. The refusal names the command: ``policy rule `shell: curl *` → never (on `curl x`)``.
- `timeout`, `nice`, `nohup`, `time` and `stdbuf` are taken off the front before an `auto` rule
  is matched, so `npm test*` covers `timeout 60 npm test`. An option these wrappers don't
  usually take (`time -o file`) keeps the wrapper on, so the command asks.
- Environment assignments stay on for `auto`: `FOO=1 npm test` and `env FOO=1 npm test` do not
  match `npm test*`, because `LD_PRELOAD`, `NODE_OPTIONS` or `PATH` change what runs. Write
  `FOO=1 npm test*` if you want that form allowed.
- `never` and `ask` rules see through more: wrappers, `env`, assignments, `sudo`, `doas`,
  `xargs`, `exec`, `command`, leading redirects, shell keywords such as `then`, and quoting, so
  `rm *` also refuses `sudo rm -rf x`, `then rm -rf x` and `"rm" -rf x`. They also see the
  program without its folder (TD-5156): `curl *` refuses `/usr/bin/curl x`, and on Windows
  `C:\Tools\curl.exe x` (an `.exe`, `.com`, `.bat` or `.cmd` extension comes off too). `auto`
  rules still match the path as written. They read bash's brace expansion (`{curl,x}` and
  `c{url,} x` are `curl`) and the command of `coproc NAME { …; }`.
- A heredoc's body belongs to the command that opens it, so an `auto` rule must match the body
  too. A trailing `*` matches any body.
- A line that can't be split with confidence never auto-runs, whatever the rules say. That is
  an unclosed quote or `$(`, `$'…'`, `$((…))`, `((…))`, `case` inside `$(…)`, or nesting more
  than 16 levels deep. It is also any line the shells split differently (TD-5156): a heredoc
  whose end bash and dash put in different places (a body line ending in `\` just before the
  delimiter, a heredoc opened inside `$(…)` with its body after the `)`, or `EOF)` inside
  `$(…)`), a `\`-newline that joins two halves of an operator (`&`, newline, `&`) or follows
  `$`, and on Windows `for /f … in ('…')`, which runs the quoted command. bash reads some
  words whole where other shells see structure, so these are incomplete too: `${ cmd; }` and
  `${| cmd; }` (bash 5.3 runs `cmd`), any `[[ … ]]`, a `(` glued to a word (`a=(…)` arrays,
  extglob `@(…)`, `!(…)`; `name()` is fine), a `name[…]` subscript with a space, an operator,
  a quote or `$(` in it (`a[i]` and `a[$i]` are fine), the old arithmetic `$[…]`, and a
  program name bash brace-expands (`{curl,x}`). `never` and `ask` rules still apply to it.
  TST Desk's tests run a corpus of lines under bash, bash `--posix`, dash and busybox ash and
  check the split against what each shell ran.
- "Always allow" is offered only for a single command, and saves exactly that command, with any
  `*`, `?` or `[` in it made literal. It isn't offered for a chained line: one rule can't cover
  it, and a rule per command would let `sh` run on its own after `npm test | sh` was approved.
  Add a rule for each command here instead.
- On Windows the line is read both as `cmd /c` reads it (single quotes don't quote there) and
  as `sh -c` does, for autonomy runs, and a rule must hold under both.

These rules match text, so they are a guard rail, not a sandbox. A command spelled through a
variable (`$C x`), brace expansion, a script file or another program's arguments
(`find -exec`, `ssh host …`) is not seen as the command it runs. An
`auto` rule with a trailing `*` also covers redirects on that command (`git status > out.txt`),
so prefer exact commands for `auto`, and use `never` for what must not run.

**Dangerously skip permissions** (skip-all) is not a key in this file. Settings → Policy
has a machine-wide toggle that lives in the user data dir as `approvals.yaml`, so a clone
cannot carry it. While it is on, every `ask` runs as auto — including `shell` and Class C —
and caps do not pause. That includes writes to paths that run code later (§4.1, TD-5108):
skip-all does not ask for them, as Claude Code's bypass mode does not. The daemon's own git
still never runs a repo's fsmonitor command, a hook or a filter driver, but a `gpg.program`
planted in `.git/config` without asking does run on its next memory or charter commit (§4.1). A
`never` rule still refuses. The fs-tool
boundary still refuses steering-file writes, and skip-all never accepts a proposed steering
edit (TD-5140): only your Accept on the card writes one. An unattended run parks its
proposals for the run summary and never accepts them either. The classifier still runs.
`effect: yolo` is not a valid rule.

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

**Loosening approvals on a workspace you trust.** Reads, listings and content searches inside
the workspace never ask anyway (§4.4). Here `git status` and `git diff` never ask, alone or
chained to each other, and `curl` and `rm` are refused wherever they appear in a line, even
chained after an allowed command. Anything Class C still asks, and an unanswered request
denies after five minutes:

<!-- verify: workspace -->
```yaml
policy:
  class_c_default: ask
  approval_timeout_seconds: 300.0
  rules:
    - tool: shell
      args: "git status"
      effect: auto
    - tool: shell
      args: "git diff"
      effect: auto
    - tool: shell
      args: "curl *"
      effect: never
    - tool: shell
      args: "rm *"
      effect: never
approved_external_imports:
  - ~/notes/house-style.md
```

The rules are exact rather than `git status*`: a trailing `*` would also allow
`git status > src/app.py`. Before TD-5101 the glob covered the whole line, so `git status*`
also auto-ran `git status; curl x | sh`.

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

### 4.8 Trusting a config that came with the workspace (TD-5113)

A cloned repo could put shell `auto` rules, a network allowlist or a higher spend cap in this
file and have them apply the moment you opened it. Now the daemon reads the file once, hashes
those exact bytes, and compares every setting with the packaged defaults. A setting loosens
them when:

| Setting | Loosens when |
|---|---|
| `policy.rules` | A rule has `effect: auto`. |
| `boundary.network` | It names any host. The default is `deny`. |
| `boundary.writable_paths` | An entry is absolute, starts with `~`, or climbs out with `..`. Writes outside the workspace are refused whatever this says; the entry is shown because the file asked for it. |
| `caps.*`, `attachments.*`, `memory.max_lines` | The value is above the default. For `caps.shell_memory_mb` the default is half of this machine's RAM, so the same file can loosen on a small laptop and not on a big box. |
| `approved_external_imports` | It lists any path. |
| `mcp_servers`, `hooks` | The key is set at all. TST Desk does not read either from a workspace yet; they are judged now so a later feature cannot run them untrusted. |

Tightening never asks: `allowed_commands`, `ask` and `never` rules, `class_c_default: never`,
an approval timeout, narrower `writable_paths` and lower caps apply as written. A section the
loader would refuse (§6) loosens nothing, since it does not apply anyway.

When the file loosens something:

- **You see it before it applies.** A card above the composer lists each loosened setting next
  to its default, with **Trust and apply** and **Keep defaults**. `tst run` cannot show the
  card, so a run that opens the session prints one line on stderr saying the run is on the
  defaults.
- **Until you trust it, the workspace runs on the packaged defaults.** The whole file is set
  aside, not only the loosened keys. The title bar's source reads `defaults — workspace config
  not trusted yet`.
- **The decision is kept per workspace and tied to the file's SHA-256.** It lives in
  `workspace_trust.yaml` in the user data dir, never in the workspace, so a repo cannot ship
  its own trust. Any change to the file, your own hand edit included, asks again. Keep defaults
  is remembered too: the card shrinks to one line with **Review**, which opens it again.
- **TST Desk's own writes keep their trust, for what you have seen.** Always allow, Revoke,
  Settings → Budget and approving an external import rewrite this file. When the file was in
  force before the write, the new hash is stored as trusted, so your own rules never ask. That
  covers only what was in force before the write plus the key the write sets. If the write
  makes a refused section valid, so the file now loosens something the card never listed
  (a budget saved over a bad `caps` value exposes the `boundary.network` beside it), the new
  hash is not trusted: the file waits on the card and the workspace drops to the defaults.
- **While the file is not trusted, nothing is saved into it.** Approval cards offer approve and
  deny but not Always allow. An Always allow sent anyway (a card shown before the file changed)
  approves that one call and replies `workspace_config_untrusted`: the rule is not saved.
  Revoke and Settings → Budget are refused with `workspace_config_untrusted`, and an approved
  import lasts for the session only.
- **Only this computer decides.** A phone or another computer that sends the decision gets
  `loopback_only`. A session on another computer shows the card without its buttons.

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
