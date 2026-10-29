# Architecture guide

How TST Desk is put together, why the pieces sit where they do, and where a contributor plugs
in. Written for someone who has cloned the repo and wants to change something without breaking
the invariants.

The spec (`docs/tst-desk-spec.md`) says what we are building. This says how it is built. Where
the two disagree, the spec wins and this file is the bug.

Every table and every code block below is checked against the code by
`core/tests/test_docs_architecture_guide.py`. The protocol tables are compared against the
discriminated unions in `core/tstd/protocol.py`, so adding a message without documenting it
fails the suite. The "how to add a tool" walkthrough (and the plugin `register()` walkthrough)
are registered and dispatched by that test, so a walkthrough that stopped working fails too.

---

## 1. Three processes, one product

TST Desk is not one program. It is a daemon with two clients that happen to ship in the same
installer.

```
┌───────────────────────────────────────────────────────────┐
│  Window  —  SvelteKit + Svelte 5  (ui/)                   │
│  Renders events. Sends messages. Owns no truth.           │
└─────────────────────────┬─────────────────────────────────┘
                          │  WebSocket, JSON frames, 127.0.0.1
┌─────────────────────────▼─────────────────────────────────┐
│  tstd  —  Python daemon  (core/tstd/)                     │
│  Sessions, agent loop, router, context, tools, audit.     │
│  Outlives the window. Outlives the host.                  │
└───────────────────────────────────────────────────────────┘
             ▲
             │  spawn / supervise / shut down  (no keychain — see below)
┌────────────┴──────────────────────────────────────────────┐
│  Host  —  Rust + Tauri 2  (shell/)                        │
│  Deliberately thin. No business logic.                    │
└───────────────────────────────────────────────────────────┘
```

**The daemon (`core/tstd/`)** holds every piece of state that matters: the session registry, each
session's append-only event log, the conversation, the boundary, the policy, the cost ledger, the
audit database. It binds a WebSocket server to loopback by default. A non-loopback bind is
refused by `validate_interface` in `core/tstd/ws.py` unless `remote.bind` names a Tailscale
address (never `0.0.0.0` / `::`) — prime directive §2.1, with the spec §8 opt-in.

**The host (`shell/`)** manages the window and the daemon process lifecycle: it resolves the
`tstd` binary, attaches when `port.json` names a live listener, and otherwise spawns with
`--data-dir` and `--parent-pid`. It probes the handshake and
supervises with a bounded restart budget. Closing the window hides it and leaves `tstd`
running; Quit still shuts the daemon down and reaps the process group. It also exposes the
daemon's port and token to the webview through a Tauri command, so the window never reads the
port file itself. AGENTS.md §6
states the rule plainly: *"Keep it thin. … Business logic belongs in Python."* The host never
parses a protocol message beyond checking for `hello_ack`. When you find yourself wanting to add
a decision to the Rust side, that is the signal that it belongs in `tstd` instead.

One correction to the layout you may have read elsewhere: **the keychain is not in the host.**
AGENTS.md §6 lists it among the host's jobs, but there is no keychain code in `shell/` at all.
Secrets live in `core/tstd/keychain.py`, which shells out to `security` on macOS, `secret-tool`
on Linux, and the Windows credential store, all under the service name
`com.thatsimpletech.tstdesk`. Native folder picking is likewise driven from the UI through the
Tauri dialog plugin rather than from Rust. The host's native surface is narrow by design: window
state, dialog, opener, tray (TD-4703), and the release updater (TD-4837).
The updater asks GitHub for `tstdesk-v*` releases and, on a packaged app,
replaces that app only after `SHA256SUMS.txt` matches. It sends no
identifier. On macOS, a global shortcut (⌘⇧.) toggles
the quick-entry overlay (TD-4702); registration failure surfaces
Accessibility copy in the main window. The opener's `opener:allow-open-path` grant is intentionally left
without a path scope (`shell/capabilities/default.json`): the paths the UI opens are
daemon-assembled — stack files, diff headers, usage exports — and land under whichever workspace
the user picked this session or in the app data dir, while Tauri opener scopes are static
capability globs with no supported way to extend them at runtime, so a scope tight enough to
mean anything would break all three surfaces. Scoping that grant alone also reduces nothing:
the host's own `open_path` command (`shell/src/lib.rs`) performs the identical OS open outside
the capability system, so the residual risk — a compromised frontend asking the OS to open an
arbitrary path — rides that command regardless, and is bounded by provenance (daemon-derived
paths, never free text) rather than by a glob.

**The window (`ui/`)** is a renderer. AGENTS.md §6 again: *"The UI never derives truth it wasn't
given — if the daemon didn't send it, don't infer it."* Every number in the title bar, every
approval card, every timeline entry is a daemon event the UI was handed. The live-model
pill is `{slug} · {host}` from `tier_state` (TD-1720); the UI does not guess a host
from the slug.

### Why a separate process at all

Spec §2 calls this the single most important architectural decision, and it buys three features
with one mechanism:

1. **Coworker mode.** Close the window; the session keeps running.
2. **Remote attach.** Bind the same socket to a Tailscale interface later and a phone can watch
   a running session. No new subsystem — the same protocol through a different door.
3. **The CLI comes free.** Same daemon, different client.

All three are the same property stated three ways: *the session is not the connection*.

### How a client finds the daemon

The daemon writes `port.json` into its data directory (`write_port_file`, `core/tstd/ws.py`)
carrying exactly three fields: the port, an auth token, and the daemon's pid. It is written to a
temporary name, chmod'd `0600`, and moved into place with `os.replace`, so a host polling for it
never sees a half-written file. The token is 64 hex characters and is regenerated on every
`start()`, so it rotates with each daemon restart.

The host reads it and checks the `pid` matches the child it just spawned — a daemon killed with
`SIGKILL` leaves the file behind, and connecting to a stale port is worse than waiting. There is
no discovery protocol and no broadcast; the file on disk is the rendezvous, and the filesystem's
own permissions are what keep the token private. (`core/tstd/discovery.py`, despite the name, is
not this — it discovers model slugs from a local OpenAI-compatible endpoint.)

**`tst run` (TD-3101)** is a second client of that same file. It checks the pid is live,
presents the token in `hello` at `PROTOCOL_VERSION`, and never binds a socket. If the file is
missing or the pid is dead it spawns `python -m tstd.daemon --data-dir <dir>` — not
`--parent-pid` — and leaves the daemon up. A turn is `open_workspace` → `user_message` → print
`assistant_delta` text → exit 1 if `turn_complete.failed`. An
`approval_request` on a TTY is the same card as attach (TD-3103); without a
TTY, `tst run` prints copy to use the window and exits rather than hanging
on stdin. The headless harness (TD-1401) stays the mock path; `tst run` is
not a second harness.

**`tst run` for scripts (TD-5111)** adds no protocol message. `--session <id>`
and `--continue` send `list_sessions`, pick the row (the named id, or the
local, unarchived session of `--workspace` that has not ended, newest by
`updated_at`, which the daemon moves when a turn starts), refuse it while
`busy`, then `attach` from `event_count + 1` so no earlier turn replays,
and send `user_message` with `only_if_idle: true`, so the daemon refuses it
with `session_busy` if another run got a turn in after that list (TD-5160).
The turn is the events from the `user_turn`
carrying that message to its `turn_complete`; a `[REDACTED]` in that
`user_turn` stands for the secret-shaped text the daemon scrubbed. The message comes
from `--message`, or from stdin for `--message -` or no `--message` (a
terminal is read only for `-`). `--output json` prints one object,
`{session_id, status, reply, cost, error}`, where `cost` is
`turn_complete.cost` and `error` is `null` or `{code, message}`;
`--output ndjson` prints each of the session's events as received, one per
line. Errors and refusals go to stderr in every mode, and so does a note when
the session works in a git worktree (TD-5126): its branch and path, from the
session's `worktree_state` event, or for `--session` and `--continue` from the
row's `worktree`. Neither machine mode
prompts: an `approval_request` ends the run and the turn waits for the
window. Exit codes:

| Code | `status` | Meaning |
|---|---|---|
| 0 | `success` | The turn completed and no tool call was refused |
| 1 | `error` | The turn failed, the daemon refused the message, or the run timed out or lost the daemon |
| 2 | — | Usage error (argparse, or no message) |
| 3 | `refused` | The turn completed, but a gate refused a tool call: `boundary_refusal`, `policy_denied`, `approval_denied`, `plan_mode` or `hook_blocked` (a `--deny` rule, a `--on-ask deny-and-continue` refusal, and a `before_tool` hook's block count here), or a `user_prompt` hook blocked the message (`hook_blocked` on `turn_complete`) |
| 4 | `approval_needed` | A call is waiting for approval that this run could not give, or the turn paused for a person (the progress guard or a spend cap, TD-5161); the turn waits for the window |

**Per-run permissions (TD-5118).** `--allow RULE` and `--deny RULE` (each
repeatable; a rule is `TOOL` or `TOOL:ARGS`, the globs of a `policy.rules`
entry, split at the first colon) and `--on-ask prompt|deny-and-continue`
ride on the `user_message` as `run_permissions: {allow, deny, on_ask}`,
sent only when one is set. The daemon keeps them on the session for the
one turn that message starts: the loop sets them as it takes the message
and `turn_complete` clears them, and the queue file never keeps them, so a
held message a restart brings back runs under the workspace's rules. At
the dispatch gate allow rules are `auto` and deny rules `never`, beside
the workspace's (`with_run_rules`): deny first, so a workspace `ask` or
`never` still wins over an allow, and a Class C call is demoted to
`class_c_default` whatever the rule. The classifier has already run by
then, and the read guards of the read-only shell list read the same
merge. A delegate or workflow child's dispatcher reads its parent turn's permissions.
With `deny_and_continue` the session answers an approval (a tool call, an
external import, Grok's ACP request) before any `approval_request` is
logged: an `approval_denied` tool result saying the run answers every
approval with no, the turn goes on, and the run exits 3. A refused
external import (its result names the file) stays out of that turn only,
so the next turn asks about it again; an import a person denies stays
out, unasked, for the rest of the session (TD-505). The `user_turn`
echoes `run_permissions`; when it does not (a daemon older than the CLI),
the run sends `stop_turn` and exits 1 with `run_permissions_unsupported`.

**`tst attach` (TD-3102)** is the same door in follow mode. Same port file and
`hello`. It sends `attach{session_id, from_seq}` (default 1), prints assistant
text the way `tst run` does, and prints one-line text for `turn_complete` and
errors — not JSON dumps. On `approval_request` (TD-3103) a TTY prints a card
(tool, summary, class) and reads `y` / `n` / `always`; a non-TTY prints copy
to use the window and does not send `approve`. Class C never accepts `always`.
Ctrl+C sends `detach`, not `cancel`. An unknown id is the daemon's existing
`session_not_found` error.

**Browser attach (TD-3701).** The same Svelte `ProtocolClient` — not a second
app — works outside Tauri. A phone or laptop browser does not read `port.json`.
It connects to `ws://<tailscale-ip>:<port>` (the extra listener from TD-3601)
and presents `{user_data_dir}/remote-token` (TD-3602) in
`hello.token`. The UI takes that pair from a connect form, or from
`?ws=&token=` / `#ws=&token=` (hash preferred; the token is stripped from the
address bar after read). Loopback in a browser still uses the port-file token.
There is no account and no hosted relay. The daemon still does not bind
`0.0.0.0` / `::`; the form refuses those as connect targets too.

The window is still only a viewer. Read transcript, send, and approve are the
same client messages they are on the desktop. Settings shows the address and
port (TD-5005); the token is the contents of `remote-token`. Both stay the same
across restarts: the port is remembered beside the remote-attach switch and the
token file is kept at shutdown. Settings "New token" replaces it and drops every
remote client that used it; turning remote attach off deletes it.

**Pairing (TD-5014).** The token file is now the older way. The usual way is to pair: the
computer shows a code and a QR (Settings, or `tst pair`), and the phone scans the QR, opens
the link, or types the address and code. The code is 8 characters, lasts 10 minutes, works
once, and stops working after 5 wrong tries; it lives only in the daemon's memory. Redeeming
it gives that phone its own key, on the same port as everything else. The daemon keeps only a
sha256 of each key, in `{user_data_dir}/remote-devices.yaml`, so each device can be listed and
removed on its own (`tst devices`). Removing one closes only its connections. Turning remote
attach off stops the listener but keeps the paired devices, so they connect again when it is
turned back on. Pairing and removing are refused from a remote connection: only the computer
itself can add or remove a device, except that a phone may remove its own entry ("Forget this
computer"). A paired device connecting or dropping makes the daemon push a fresh `setup_state`
to the computer's own windows, so the device list is live.

**Android shell (TD-5001).** A sideload APK is that same UI in a WebView, not a second protocol and not a Play Store service. The page is served to the WebView from `127.0.0.1` inside the phone — an https page would refuse the daemon's cleartext `ws://` as mixed content — and the WebView refuses every other origin. The android UI build's attach parser accepts only a Tailscale address (`100.64.0.0/10`, `fd7a:115c:a1e0::/48`, or `*.ts.net`). Desktop CSP stays `ws://127.0.0.1`. The APK is debug-signed for sideload. There is still no account and no relay. The shell keeps the attach target (address + token) sealed with an Android Keystore AES-GCM key and hands it to the page through a `TstDeskNative` bridge, so the phone reconnects on launch (TD-5005). A refused saved token is cleared and the form comes back with the address filled in. Settings on the phone has "Forget this computer". A plain browser tab still keeps the token in `sessionStorage` only.

On a viewport narrower than 640px the same `AppShell` hides the session rail
and the inspector so the phone is chat + approval. Either pane can be shown
again; they are not a separate product. On a wide viewport both sidebars are
pixel-width and user-draggable: the session rail from its right edge, the
inspector from the chat|activity divider. Each persists in `localStorage` and
is clamped only so the other columns keep a usable minimum.

---

## 2. Why the session owns the loop

This is prime directive §2.5: **the daemon owns sessions; the window is only a viewer. No code
path may make the WebSocket connection the owner of a running loop.**

It is easy to state and easy to violate by accident, because the obvious implementation of
"stream events to a client" is to run the loop inside the connection handler. So it is worth
being precise about where the ownership actually lives.

### The ownership chain

`SessionRunner` (`core/tstd/session.py`) holds the loop as a bare `asyncio.Task`, created by the
daemon at session start and parented by nothing else:

<!-- verify: docstring tstd.session:SessionRunner -->
```
The runner is created by the daemon and runs independently of any
client connection. The connection is only a viewer — closing it
does not affect the session (prime directive §2.5).
```

Three things follow from that, and each is load-bearing:

**The event log is the seam, not the socket.** A session's `SessionEventLog` is append-only and
stamps every event with a monotonic per-session `seq`. Clients subscribe to it; they do not feed
it. An attached client is a subscriber that replays from a `seq` and then follows. Detaching
unsubscribes. Nothing about that touches the loop.

**Approvals are parked on the session, not the connection.** `Session._pending_approvals` holds
an `asyncio.Future` per parked tool call. The comment in the code says why: *"owned by the
session — NOT by any websocket — so a client disconnect leaves them parked and resumable."* Close
the window mid-approval, reopen it, and the card is still there because the future never went
anywhere. An `ask_user` question (TD-5121) parks the same way, in `Session.pending_questions`:
the session waits in `awaiting_approval` until any client answers it, and Stop or cancel ends
the wait. Skip-all releases approvals but never answers a question.

**Disconnect cleanup is scoped to the stream.** When a connection drops,
`Daemon._on_connection_closed` calls `_cleanup_attach`, which removes the connection from
`_attached_clients` and cancels *that connection's streaming task*. It does not reach the runner.
The only things that stop a loop are `Session.cancel` (user asked) and daemon shutdown.
`stop_turn` ends the current turn, or a running after-write review (TD-5151), and leaves the loop
waiting for the next message.

**A message sent mid-turn steers that turn, on the daemon (TD-5112).** A `user_message` with
`steer: true` (the window's Send now while a turn runs) that arrives while a native-engine turn
runs is marked with that turn's id when it is queued. After the turn's next tool batch, just
before the next model call (after any cap or progress-guard pause), the loop takes every message
marked for that turn off the queue and appends each as a user message, so the model reads it
before its next step and the turn carries on, and fits the prompt again with it in. Its
`user_turn` carries a `steer` point, and the audit database keeps one `steers` row per message.
A turn that ends without another batch leaves the message to start the next turn, as before, and
so does Stop: a stopped turn makes no further model call. Any other message waiting then keeps a
turn of its own, as does one sent without `steer`. A steered message starts no turn for context
fitting either: compaction cuts only where a turn started, and in-turn elision reaches the
turn's results from before the steer. The Grok engine runs its own tools, so there a message
waits for the turn to end. Which connection sent it does not matter, so a phone steers the same
way.

**Compaction summaries come from the worker, written ahead (TD-5128).** The agent loop gives its
session a `CompactionSummarizer` (`core/tstd/compaction_model.py`) bound to the worker client and
the cost tracker. A compaction, by budget or on request, asks it for a structured summary of the
turns it folds (goal, decisions, files touched, open items) and falls back to the extractive
summary on any failure; the pure folding stays in `compaction.py`, the summary text in
`compaction_text.py`. Once a request passes `compaction.background_percent` of the budget, the
summarizer starts the next fold's summary as a background task while the turn runs, keyed by the
turns it covers (tool result text aside, since clearing stale results rewrites it). The fold
that matches takes it without a new call; the task is cancelled when the loop ends. The files
the folded turns read or changed are then read again within a budget
(`compaction_files.py`), through the session's path guard and read rules, and go under the
summary. `/compact` folds from a copy and drops its result if a turn started meanwhile.

### The session state machine

Derived from `Session.VALID_TRANSITIONS`. States with no outgoing transitions are terminal —
`TERMINAL_STATES` is computed from this table rather than written out twice, so the two can never
drift.

<!-- verify: states -->

| State | Can move to |
|---|---|
| `idle` | `running` |
| `running` | `awaiting_approval`, `cancelled`, `complete`, `failed`, `paused` |
| `awaiting_approval` | `cancelled`, `running` |
| `paused` | `cancelled`, `running` |
| `complete` | *(terminal)* |
| `failed` | *(terminal)* |
| `cancelled` | *(terminal)* |
| `interrupted` | *(terminal)* |

`interrupted` is a tombstone for a session whose **model conversation was not
saved**. The registry still knows the id and workspace. Events that reached
disk can be replayed so the window shows what we actually have. The loop does
not start, and a new message is refused — continuing without the conversation
would be inventing context. A session that has both `events.jsonl` and
`conversation.json` is revived as `running` instead. When its last
`user_turn` has no `turn_complete` (a restart cut the turn), revive seals it
once: each unanswered tool call gets a "Not finished" tool reply, and the log
gets a failed `turn_complete` with `error_code: interrupted_by_restart`. That
seal is the only thing revive adds; nothing is sent to the model (TD-5018).

Note that `running` spans the session's whole life, not one turn. "Is a turn in flight" is a
separate question answered by `Session.turn_in_flight`, counted off `turn_complete` events —
which is why `delete_session` and `move_session` can refuse mid-turn while `session_state` still
reads `running`.

### What this rules out

If you are adding a feature and find yourself writing any of these, stop:

- Holding conversation state on the connection object.
- Cancelling a session because its last viewer went away.
- Deriving session state in the UI from what it happened to observe while attached.
- Making a reply depend on which client sent the request. Any attached client can approve,
  deny, cancel, or resume; the session does not care which one.

### A worktree per session (TD-5126)

A session or scheduled job opened on a folder in a git repository works in its own git
worktree, so two sessions in one repository never edit the same files. `_start_session`
asks `worktree_session.open_worktree` for one before the loop starts: a new branch
`tst/wt/<id>` from the main checkout's `HEAD`, in `worktree.folder` (the data dir's
`worktrees/` by default). `Session.workspace_path` is then the worktree's copy of the folder,
so every tool, the wall, the checkpointer and the prompt assembler work there unchanged.
The session answers before the worktree's files are there (TD-5187): `git worktree add
--no-checkout` makes the folder and branch, the project's `.tst` folder is checked out at
once so the wall is built from the right config, and `start_setup` checks out the rest in
the background without overwriting a file already there. `session_start` hooks wait for the
files (`Session.worktree_files`), the first turn waits for them and any setup
(`Session.worktree_ready`), and close and delete wait rather than refuse. A failed checkout
moves the session back to its folder; restore finishes one a restart cut off.
`Session.project_path` and the store row's `workspace_path` stay the folder the user opened:
the rail groups by it, `new_session` anchors on it, and Schedule this chat names it.

A folder `HEAD` does not have (new, untracked or ignored) gets no worktree: its copy would be
empty. The workspace config comes along when git does not track it, with the trust decided on
those bytes; `.tst/worktree-include` copies ignored files; a trusted `worktree.setup` runs once
in the background and the first turn waits for it. `worktree_config` keeps an untracked
config's copy following the project's file: TST Desk's own saves (Always allow, Revoke, a
spend cap, import approvals) go to the project's file and refresh the copy, and a trust
decision covers the same bytes in the project folder and every stored worktree. `close_worktree` keeps, merges back or
removes the worktree, then moves the session back to its folder the way `move_session` does.
An unchanged worktree is removed on close, on delete, and when a scheduled run ends. A
delegate child asked for `worktree: true` gets its own from the parent's last commit; its
changes come back committed on its branch for the parent to merge.

`delegate` with `agent: <name>` runs a named worker (TD-5133) from `.tst/agents/*.md` or
`~/.tstdesk/agents/*.md` (`context/workers.py`; user-global wins on a name). It is the same
one-level child with the definition's tool allow list (a subset of one built-in kind's tools),
its tier (from its `preset` when it names one; only user-global files may change tier or preset), its prompt after the worker tier's stack, and its `ask`/`never` rules on the
child dispatcher's `worker_rules`, merged after the workspace and run rules
(`with_worker_rules`), so it can only tighten. The loop reads the trees at each turn start:
`delegate`'s definition lists the workers and their descriptions with an `agent` enum, and
`@name` in the user's message appends a note naming the worker. No wire message changes.

`workflow` (TD-5195) runs several of these children at once, while the session's Ultracode
switch is on (`set_ultracode`, `TierRouter.ultracode`). It is registered wherever `delegate`
is, at depth 0 only, so flipping the switch never changes the tool list or the system
message; its handler refuses while the switch is off. Each task is one delegate child
(`kind` worker or research, optional `agent`), at most `workflow.max_parallel` at a time and
`workflow.max_tasks` a call. With two or more tasks every writer runs with `worktree: true`,
opened and settled under one lock per repository. With `check` on (the default) each task
that finishes is handed to a research child on the validator tier, which checks the claims
against the files and the writer's branch and answers `confirmed`, `doubtful` or `wrong`.
Results come back in task order. Children share the parent's iteration count, Stop reaches
each one, their tool-call ids carry the parent call's id and task index so their approvals
never collide, and every child (a plain `delegate`'s too) sends the session's effort when the
worker tier takes `reasoning_effort`. While the switch is on, the loop adds a short note to
each depth-0 turn's user message, never to the system message.

---

## 3. The wire protocol

One WebSocket, JSON frames, snake_case field names in both Python and TypeScript. Client→daemon
frames are `ClientMessage` subclasses; daemon→client frames are `DaemonEvent` subclasses. Both
sides discriminate on a `type` field, and `core/tstd/protocol.py` declares them as pydantic
discriminated unions (`ClientMessageT`, `DaemonEventT`) so parsing is total and typed.

### The handshake

1. Client connects to `ws://127.0.0.1:<port>` from the port file, or to the extra Tailscale
   listener when `remote.bind` is on.
2. Client sends `hello` with the token and its `PROTOCOL_VERSION`, within the handshake timeout.
   Missing the window is `handshake_timeout`.
3. Daemon validates the version first, then the token. Loopback hellos present the port-file
   token. A non-loopback hello (the extra listener, or a non-loopback peer) must present the
   token in `{user_data_dir}/remote-token` — the port-file token is not enough. The
   protocol field is still `hello.token`. An out-of-range version fails with
   `version_unsupported` and a message naming the versions on both sides; a bad token fails with
   `auth_failed` and does not open a session. Either way the daemon sends a typed `error` frame
   and closes with 1008.
4. Daemon replies `hello_ack` and the connection is live. From here the daemon also sends `ping`
   on a timer, to handshaken clients only.

`hello_ack` is a bare JSON object built by `build_hello_ack`, not a `DaemonEvent` — it precedes
the event stream rather than joining it. After the token checks out it also carries
`machine_id` (`mid_` + 16 hex, minted once into `{user_data_dir}/machine-id`; an identity, not
a secret) and `desktop_name`, so a hub can tell "the same computer at a new address" and "this
is me" (TD-5015). `hello.features` is what the client opts into: `"machines"` asks to be a
viewer, `"bridge"` says this is another computer's hub. Unknown names are ignored.

A phone that is not paired yet sends `pair` instead of `hello` as its first frame (TD-5014).
The daemon checks the one-time code, mints that device a key, answers with a bare `pair_ok`
(built by `build_pair_ok`, out of band like `hello_ack`) and closes with 1000. The phone then
reconnects and sends `hello` with that key in `hello.token`. On a remote connection the daemon
accepts the key of any paired device or the older `remote-token`. A phone pairing again sends
its current key as `replaces`, and the entry it verifies is removed once the new key is minted.
When `remote-devices.yaml` cannot be read, a device-key hello gets `devices_unavailable`, which
the phone treats as "retry", not as a refusal; only `auth_failed` means the key is gone.

`ready` is declared in `DaemonEventT` and carries the daemon and protocol versions, but **no code
in `core/tstd` constructs it**, so it is not on the wire in v0.1. It is documented below because
it is part of the parseable surface every client must handle; do not write a client that waits
for it. The version information a client actually needs came back in `hello_ack`.

### Sequence numbers and replay

Every `DaemonEvent` carries a `seq`. Session-scoped events take theirs from the session's event
log, which stamps them on insertion and is the only writer. Connection-scoped events — replies
about the daemon or the machine rather than about one session — fix `seq` at 1, because they
belong to no session's log and must never advance a client's bookkeeping.

`attach` replays from a given `seq` and then follows live, which is what makes reopening a window
lossless. If that `seq` is below the on-disk window (`session.log_max_events`), attach sends
`log_trimmed` and replays from the earliest kept seq. `ping` is the odd one out: it is a
daemon→client frame with no `seq` at all, deliberately not a `DaemonEvent`, because a liveness
frame that advanced the sequence would corrupt replay.

### Unknown types

`parse_client_message` and `parse_daemon_event` check the incoming `type` against
`_KNOWN_CLIENT_TYPES` / `_KNOWN_EVENT_TYPES` before validating, so an unrecognised name gets
`unknown_message` with an actionable string rather than a pydantic dump. These frozensets are
hand-maintained alongside the unions, so a test derives both sets from the unions themselves and
fails on drift in either direction — a union member with no frozenset entry is rejected by the
parser meant to accept it, and a frozenset entry with no union member admits a type nothing can
validate.

### Other computers: the hub (TD-5015)

The local daemon is a one-hop **hub**. The window keeps its one loopback socket; the daemon
dials the computers it paired with and folds their sessions into the same list. Nothing else
changes shape: the CSP, the Rust shell and the Kotlin code are the same, and an old client sees
exactly what it saw before.

**Peer kinds** (`core/tstd/peer_policy.py`, pure):

| Kind | Who | What it gets |
|---|---|---|
| Viewer | `hello.features` has `"machines"`, and the connection is loopback or a phone-kind device key | Own rows plus every paired computer's rows; `machine_list`; may act on bridged ids (the hub forwards). |
| Machine peer | A device whose record says `kind: machine`, or any hello with `"bridge"` (the hub always sends it) | Deny by default: only `MACHINE_PEER_VERBS`, own sessions only, no broadcasts. A bridged id is `bridged_session`; any other verb is `not_allowed`. Never a viewer, so it never forwards. |
| Everyone else | The CLI, the legacy `remote-token`, an old client | Own rows only, no `machine_list`: unchanged. |

`MACHINE_PEER_VERBS` is the session verbs a hub needs (list, attach, detach, send, stop, the
approval answers, verify, new and fork, branch, tier, plan, effort, Fast and Ultracode
(`set_effort`, `set_fast`, `set_ultracode`), preset, rename, star, archive,
delete, end, the instruction stack, the memory answers, `read_artifact_chunk` for a sent
file's bytes, `compact_context`, `preview_restore_files`, `restore_files` and
`extract_attachment`) plus the self
`remove_remote_device`.
Each was checked for a way to turn approvals off; the approval switches themselves
(`set_skip_all_approvals`, `set_grok_mode`, `approve_grok_plan`, `start_autonomy`) and every
key, credential, pairing, policy-rule, job, computer-use and machine-wide verb are not in it.
`FORWARDED_VERBS`, what a hub forwards for a bridged id, is the same list minus the self
removal; the protocol fixtures carry it as `forwarded_verbs`. A test walks
`_KNOWN_CLIENT_TYPES`, so a new verb starts refused for machine peers until it is classified.

**Ids.** A bridged session id is `<raw>:<mch>`, grammar
`^(?P<raw>[^:]+):(?P<mch>mch_[0-9a-f]{12})$` (`core/tstd/bridge_wire.py`). `mch` is this hub's
own handle for the computer (the `computers.yaml` id and the keychain account suffix), so
re-pairing the same `machine_id` at a new address keeps every bridged id. A uuid4 raw id never
matches. `seq` passes through untouched.

**Links and lanes** (`core/tstd/bridge.py`, `bridge_machine.py`, `bridge_link.py`,
`bridge_lane.py`):

- While at least one viewer is handshaken, each paired computer has a **control link**: one
  socket for presence, last seen and a cached `session_list`, refreshed on connect, on a viewer
  connecting, every 30 s, and 1 s after a row event on a lane. Links stop 60 s after the last
  viewer leaves. Backoff is 1, 2, 4 … capped at 30 s with jitter; 45 s of silence forces a
  reconnect; 60 s without a handshake is `unreachable`. `auth_failed` or a "device removed"
  close is `refused` and stops; `version_unsupported` is `outdated`; a locked keychain is
  `locked` and retries every 5 minutes or on `reconnect_machine`.
- A viewer's first verb for a bridged id opens a **lane**: that viewer's own 1:1 socket to that
  computer, so attach, replay and seq mean what the other computer says for one connection. It
  closes with its viewer, or after 5 idle minutes with nothing attached. After an upstream drop
  it re-attaches from the last forwarded seq + 1: no gap, no duplicate.
- Downstream frames are parsed with `parse_daemon_event`, every session-id field is rewritten
  (a schema-walk test covers every event model), and the frame goes through `scrub_wire_json`
  again.

**Invariants:**

- Nothing in the socket loop awaits the network. `list_sessions` answers from the cache,
  `add_machine` runs as a task, and `Bridge.forward` only enqueues: a verb waits up to 10 s
  (at most 64 frames) for its lane.
- Approvals are never queued: `approve`, `deny` and `always_allow` go on the open lane, else
  on the open control link, else get `machine_unreachable` at once.
- A lane never drops a frame that has a session id. Frames without one are dropped, except
  `session_list` (merged) and `error` (which gains `machine` and an "On <name>: " prefix).
- One hop. A machine peer is never a viewer, upstream rows that already use the bridged grammar
  are dropped, and any upstream `machine` field is replaced. A↔B pairs and self-pairing (every
  row shows twice, `same_computer: true`) cannot loop.
- Every `session_list` a viewer receives — the `list_sessions` reply and the five lifecycle
  replies — is merged at the chokepoint and includes unreachable computers' last-known rows, so
  a bound pane never loses its row. A merged list is pushed only when remote rows change.
- The daemon never dials. Only `bridge_link.py` and `machine_pairing.py` do, and only to
  hosts that pass the Tailscale rule (`100.64.0.0/10`, `fd7a:115c:a1e0::/48`, `*.ts.net`),
  checked at pair time and before every dial.
- A remote peer's `setup_state` drops `remote_devices`, `remote_bind` and `remote_port`;
  `focus_window` is loopback only; the `job_list` push is viewers only.

### Client → daemon

Every message in `ClientMessageT`. "Session" says whether the message carries a `session_id`.

<!-- verify: client-messages -->

| Message | Session | Purpose |
|---|---|---|
| `hello` | — | Opening handshake: auth token plus protocol version. |
| `pair` | — | First frame from a phone redeeming a pairing code, instead of `hello`: code, device name, protocol version. Remote peers only (loopback gets `bad_request`). Optional `replaces`: the phone's current key, whose entry is retired. Success is a bare `pair_ok` with the device's own key, then close 1000; failure is `pair_failed` or `pair_locked`, then close 1008 (TD-5014). |
| `open_workspace` | — | Open a workspace directory as a new session. |
| `user_message` | yes | Queue a user turn for the session's loop. With `steer: true` (additive; the window's Send now mid-turn), sent while a native-engine turn runs, it joins that turn at its next tool-batch boundary instead (TD-5112). The message is written to the session's `queued.json` before the handler returns and leaves it once its `user_turn` is saved, so a restart before the turn starts brings it back held (TD-5155). A message that cannot be written is refused with `message_not_saved` and not queued; a window from TD-5158 on puts its text back in the composer, and the daemon's text makes no such claim, since older windows and APKs do not (TD-5160). With `only_if_idle: true` (additive; `tst run` into an existing session sets it) a session that already owes a turn refuses it with `session_busy`, checked in the same step that queues it (TD-5160). Every refusal names `in_reply_to: user_message`, so the window can give back a Send now row still waiting for its turn (TD-5112). `run_permissions` (additive, `tst run`, TD-5118) are `{allow, deny, on_ask}` for the turn this message starts only; with `steer` the message is refused (`bad_request`), allow or deny rules for a Grok session are refused with `run_rules_unsupported`, and the field from a phone, another computer or for a bridged session is refused `loopback_only`. |
| `send_queued` | yes | Send a message a restart left held, named by its `queued_id` from `queued_messages` (TD-5155). It joins the loop's queue as if just sent, so a drain hold refuses it with `daemon_draining`; a session that can no longer run turns refuses it with `session_not_running`; an id that is no longer held is `queued_not_found`; while the session restores files or closes its worktree it is `session_busy` (TD-5161). A row that kept `tst run` deny rules never steers into a running turn: it waits for a turn of its own, so its denies hold (TD-5161). Each refusal names `in_reply_to`. |
| `remove_queued` | yes | Drop a message a restart left held, unsent (TD-5155). An id that is no longer held is `queued_not_found`. |
| `submit_line_comments` | yes | Send the Files pane's diff comments to the agent as one message (TD-5132). Each of `comments` (1 to 50) names a file by its absolute path, a range of lines in the file after its latest write (`start_line`..`end_line`, from 1, at most 200 lines), the person's `body`, its window-made `id`, the commented `lines` as the diff showed them, and optionally `base_seq`, the seq of the `tool_result` whose diff the comment was made on (echoed as sent). Every path must be inside the session's workspace, or the whole submission is refused with `line_comment_outside_workspace`. The daemon reads each file: where the commented lines still match, the model gets them with three lines of code either side from disk; where the file has changed or cannot be read, or a policy rule makes reading it ask, it gets only the reviewed lines, marked as such. Text over 200,000 characters is refused with `line_comments_too_large`. The message is queued like `user_message` (`session_not_running`, `message_not_saved`, `daemon_draining`, and `session_busy` while the session restores files or closes its worktree, TD-5161), every refusal names `in_reply_to`, and its `user_turn` carries the comments in `line_comments`. Not forwarded for another computer's session. |
| `fork_from` | yes | Replace a past user turn and fork a sibling from there. `user_index` counts user turns from the start of the session and is mapped through compaction summaries; a turn inside one is refused with `user_turn_compacted` (TD-5147). Refused mid-turn, and with `session_busy` while the session restores files or closes its worktree (TD-5161). Every refusal names `in_reply_to`, so the window can give the edited text back to the composer (TD-5158). |
| `set_branch` | yes | Switch to another sibling at a forked user turn, numbered and refused like `fork_from` (`user_turn_compacted` once that turn is inside a summary, TD-5147). Refused mid-turn. Refusals name `in_reply_to` (TD-5158). |
| `compact_context` | yes | Compact now: everything before the latest user turn, or from a given user turn on, with optional guidance (TD-5109). Refused mid-turn. The compacted conversation is saved before `context_compacted` goes out, so it survives a restart. Forwarded for bridged ids (TD-5154). |
| `preview_restore_files` | yes | What "Restore files to here" would undo for one user turn, named by its `user_turn` id (TD-5125). Answered on this connection with `files_restore_preview`; writes nothing. Refused mid-turn (`turn_in_progress`), for an unknown turn (`turn_not_found`), in an autonomous run (`rewind_autonomy`), on the Grok engine, which writes files with its own tools (`rewind_grok`), outside a git repository (`rewind_unavailable`) and for a session whose checkpoints predate `Log-Seq` (`rewind_predates`). Every refusal names `in_reply_to`. Forwarded for bridged ids. |
| `extract_attachment` | yes | Measure a staged PDF, DOCX or XLSX for the composer's chip (TD-5131): `request_id` (the composer's own, echoed) and one `attachment` (`name`, `content_b64`). The text is extracted on the daemon that owns the session, against that session's `attachments` caps, and answered on this connection with `attachment_extracted` once read, from a background task, so the connection's later frames are not held up behind it. Stores nothing and starts no turn; the same file on `user_message` is extracted again. Forwarded for a bridged id. |
| `restore_files` | yes | Restore what a preview showed (TD-5125). `plan_id` is the preview's: a changed file or branch since is `restore_stale` and nothing is written. Hand-edited files are left alone unless named in `include_hand_edited`. Writes the files from the session branch without filter drivers, commits the restore on `tst/session/<id>`, and logs `files_restored`. Never moves HEAD, the index or another ref. Refused like the preview. Forwarded for bridged ids. |
| `approve` | yes | Approve a parked tool call. |
| `deny` | yes | Deny a parked tool call, with an optional reason. |
| `always_allow` | yes | Approve and save the narrowest policy rule that would have allowed it, with glob characters made literal. Refused for class-C calls (`class_c_not_always_allowable`), and with `not_always_allowable` for a chained shell command or one policy cannot split with confidence (TD-5101). While the workspace config is not trusted (TD-5113) it approves the call once, saves nothing and replies `workspace_config_untrusted`. When an `ask` or `never` rule with the same tool and args is saved, it saves nothing, approves the call once (`ask`) or denies it (`never`), and replies `policy_rule_stricter` (TD-5149). |
| `answer_question` | yes | Answer the questions an `ask_user` call parked (TD-5121): one answer per question, in order, each the option labels picked (one unless the question is `multi_select`) and/or the person's own `text`; or `declined: true` to skip them all. A bad answer is refused with `bad_answer` and the questions stay parked; none waiting is `no_pending_question`. Like `approve`, any attached client may answer, a phone included; it is forwarded for bridged ids and, like an approval, never queued while a lane connects. Skip-all never answers one. |
| `review_plan` | yes | Review the plan an `exit_plan_mode` call parked (TD-5123): `decision` `approve` ends plan mode and pins the plan, with an optional `plan` (the person's edit, up to 12,000 characters) that replaces the model's; `reject` keeps plan mode on. Optional `feedback` goes to the model either way. An approval with an empty plan is `bad_plan`; none waiting is `no_pending_plan`. Like `approve`, any attached client may review, a phone included; it is forwarded for bridged ids and never queued while a lane connects. Skip-all never reviews one. |
| `list_policy_rules` | yes | List the workspace's saved policy rules. |
| `revoke_policy_rule` | yes | Remove one saved rule, identified by `(tool, args)`. |
| `set_spend_cap` | yes | Set or clear the workspace's session spend budget (`caps.spend_usd` in `.tst/config.yaml`). Null removes it and the built-in $25 pause applies; a number replaces that pause. Acked with `boundary_update`. |
| `trust_workspace_config` | yes | Trust or decline the workspace's `.tst/config.yaml` when it loosens anything, naming the `sha256` the card showed (TD-5113). A file that changed since is refused with `workspace_config_changed` and re-announced. The decision is stored per workspace in the data dir and applies to every live session there; acked with their `boundary_update`. It also covers the same bytes in the session's project folder and its worktrees, and a trust runs a `worktree.setup` it was holding in that session (TD-5126). Only this computer's own window may send it (`loopback_only` otherwise). |
| `set_skip_all_approvals` | — | Turn skip-all (dangerously skip permissions) on or off. Machine-wide; every ask including shell and Class C runs as auto; caps do not pause; `never` still refuses. Acked with `setup_state`. |
| `set_load_global_memory` | — | Turn global memory on or off. Machine-wide; off never reads `~/.tstdesk/memory/`. Acked with `setup_state` (TD-2603). |
| `set_coworker` | — | Turn coworker mode on or off. Machine-wide; persists `{user_data_dir}/coworker.yaml`. Acked with `setup_state` (TD-2905). |
| `set_voice` | — | Turn hold-to-talk dictation on or off. Machine-wide; persists `{user_data_dir}/voice.yaml`. Default off. Acked with `setup_state` (TD-4701). |
| `transcribe` | — | Hold-to-talk audio. POSTs a clip to `voice.base_url` or `speech.base_url`. Refused when dictation is off or no endpoint is configured. Acked with `transcript`. Not a tool (TD-4701). |
| `set_email_notify` | — | Save email settings for scheduled-job email (TD-3820, TD-4853, TD-4854). `method` is `smtp`, `microsoft`, or `google`. `password` is written to the keychain account `tst-smtp-password` and is not stored in config. `google_client_secret` is written to `tst-google-client-secret`. Empty or omitted leaves the stored secret alone. Acked with `setup_state`. |
| `test_email` | — | Send one short sample report to `to` using the saved method, SMTP, Microsoft Graph, or Gmail (TD-3820, TD-3821, TD-4853, TD-4854). The sample uses the same newsletter renderer as a scheduled report. Acked with `email_test_result`. |
| `github_connect` | — | Start GitHub device-flow sign-in (TD-4852). Acked with `github_status`. The device code stays in the daemon; the reply carries the user code and verification URL only. |
| `github_disconnect` | — | Delete the GitHub keychain item (TD-4852). Acked with `github_status`. |
| `microsoft_connect` | — | Start Microsoft authorization-code sign-in for email (TD-4853). Acked with `microsoft_status`. The daemon opens the browser and listens on 127.0.0.1. The code and the tokens stay in the daemon. |
| `microsoft_disconnect` | — | Delete the Microsoft keychain item (TD-4853). Acked with `microsoft_status`. |
| `google_connect` | — | Start Google authorization-code sign-in for email (TD-4854). Acked with `google_status`. The daemon opens the browser and listens on 127.0.0.1. The code, the client secret, and the tokens stay in the daemon. |
| `google_disconnect` | — | Delete the Google token keychain item (TD-4854). Acked with `google_status`. The desktop client secret is left in place. |
| `set_cu_indicators` | — | Computer-use glow, agent cursor, and real-display overlay. Machine-wide; persists `{user_data_dir}/cu-indicators.yaml`. Acked with `setup_state` (TD-3402). |
| `set_workspace_pin` | — | Pin or unpin a workspace on the Projects list. Machine-wide. Acked with `setup_state` (TD-2806). |
| `resume` | yes | Resume a session paused at a declared cap, after the cap was raised. |
| `cancel` | yes | Cancel a running session. |
| `stop_turn` | yes | Stop the in-flight turn and leave the session open. Idle sessions ignore it. A running after-write review is not idle: Stop ends it and it reports `Verify stopped.` (TD-5151). The composer stop control sends this; `cancel` still ends the session (TD-5003). |
| `run_verify` | yes | Confirm a pending interactive verify after a write (TD-4204 ask mode). |
| `deny_verify` | yes | Skip a pending interactive verify (TD-4204 ask mode). |
| `attach` | yes | Subscribe to a session, replaying from `from_seq`. |
| `detach` | yes | Unsubscribe from a session; the session is unaffected. |
| `set_tier` | yes | Pin the active model tier for the session. |
| `set_effort` | yes | Pick the session's reasoning effort (model picker): `low`, `medium`, `high`, `xhigh` (shown as Extra) or `max`. Sent as `reasoning_effort` on the turn's requests, and on its delegate and workflow children's, to any tier with `reasoning_effort: true`. Default `session.default_effort` (`medium`). Optional `ultracode` also sets the Ultracode switch; null leaves it alone. An older client's `ultracode` (it was a sixth level before TD-5195) is read as `max` with the switch on; its other levels leave the switch alone. Acked with `tier_state`; the session row's `effort` (`high`, or `high+ultracode` with the switch on) is read back on revive. |
| `set_fast` | yes | Turn the model's Fast mode on or off (TD-5176). Only a preset with a `fast` block offers it. A local model's `fast.hook_url` (loopback) is called with `{model, on, by}` and may refuse (409 with `error`); an API model's `fast.extra_body` rides on requests while it is on. Acked with `tier_state` (`fast`, `fast_available`, `fast_note`); not kept across a restart. |
| `set_ultracode` | yes | Turn the session's Ultracode switch on or off (TD-5195). Works with any effort. While it is on, the model may call the `workflow` tool and each turn's user message carries a note saying so. Allowed mid-turn: the note follows from the next turn, while the `workflow` tool follows the switch at once (turned off, the next call is refused). Acked with `tier_state` (`ultracode`). Kept across a restart in the session row's `effort` string (`max+ultracode`), not a key of its own. Default `session.default_ultracode` (`false`). |
| `set_plan` | yes | Turn plan mode on or off. Refused `set_tier` to worker/validator while on (TD-4603). Plan mode is read-only (TD-5123): the classifier refuses, with `error_code` `plan_mode`, every call that could change something (`fs_write`, `fs_edit`, `shell` outside the read-only list, MCP tools not in their server's `read_only_tools`, plugin tools, actuation, GitHub writes, `propose_steering_edit`, `delegate` with `kind: worker`, and any other builtin not on `PLAN_READ_ONLY_BUILTINS`, except a `shell` command on the read-only list, the screenshot tools and `delegate` without `kind: worker`, which `plan_mode_allows` lets through first; TD-5161), ahead of policy rules and skip-all, and again after an approval a call waited on; `delegate` with no `kind` runs the research worker (TD-5117). A `workflow` call with any `kind: worker` task is refused whole the same way; a call of only research tasks runs (TD-5195). The model ends it with `exit_plan_mode`. A restart keeps it: the session row's `plan_mode` is read back on revive. |
| `set_session_preset` | yes | Retarget this session at a catalog preset. Refused mid-turn. Does not write Settings. Acked with `session_list` (TD-1721). |
| `get_instruction_stack` | yes | Ask for the resolved steering stack, token counts, and skills (TD-4502). |
| `get_hooks` | yes | Ask for the session's active hooks (user config, and the workspace's once trusted) with each one's last run (TD-5134). Answered with `hooks_state`. |
| `list_instructions` | — | List a workspace's Instructions files (`AGENTS.md` / `CLAUDE.md` fallback, then `.tst/rules/*`). Human path (TD-2802). |
| `list_commands` | — | List a workspace's slash commands (`.tst/commands/*` and `~/.tstdesk/commands/*`, with `.claude/commands/` fallback). Human path (TD-4501). |
| `list_memory` | — | List a workspace's Memory files (`.tst/memory/*.md`). Human path (TD-2601). |
| `save_memory` | — | Save an edit from the Memory pane through the memory commit path. Never a tool (TD-2602). |
| `create_rule` | — | Create a `.tst/rules/` file on the human path. Never a tool call (TD-2802). |
| `get_charter` | — | Load `.tst/autonomy/CHARTER.md` for the project home Charter column. Human path (TD-4002). |
| `save_charter` | — | Write the charter as structured fields, as the human. Never a tool. Does not commit (TD-4002). |
| `start_autonomy` | — | Sign the charter (commit), refuse unless the sandbox is live, then launch the unattended run. Human path. Does not open an interactive chat session (TD-4003, TD-4101). |
| `list_pins` | — | List `.tst/context/pins.yaml` for the project home Context column (TD-2804). |
| `add_pin` | — | Pin a workspace file or folder. Outside the wall is refused. |
| `remove_pin` | — | Unpin a path. The file stays. |
| `memory_accept` | yes | Accept a distill proposal as proposed (TD-2401). |
| `memory_edit` | yes | Accept a distill proposal with edited file bytes (TD-2401). |
| `memory_reject` | yes | Reject a distill proposal. Writes nothing. |
| `steering_accept` | yes | Write an agent-proposed steering edit as proposed (TD-5140). The daemon writes it, never a tool, after checking the path is still a proposable steering file in the workspace and the file still holds what the diff was built against; a changed file is refused with `steering_proposal_stale` and a fresh `steering_proposal` replaces the card. A closed or unknown id is `no_steering_proposal`. Loopback only. |
| `steering_edit` | yes | Write an agent-proposed steering edit with the user's own `content` (Edit, then Save). Same checks as `steering_accept`; empty content is refused (`steering_edit_empty`), and so is content the session log's secret scrub would change (`steering_secret_shaped`), since the logged diff must show what was written. Loopback only (TD-5140). |
| `steering_reject` | yes | Reject an agent-proposed steering edit. Writes nothing; logged as a `steering_decision` (TD-5140). Not a machine-peer verb. |
| `end_session` | yes | Run distill for this session (TD-2302). Not a kill. |
| `shutdown` | — | Ask the daemon to shut down cleanly. Sent by the supervising host. |
| `drain` | — | Before a restart: wait up to `timeout_s` for open turns, then refuse every verb that starts a turn (`user_message`, `send_queued`, `submit_line_comments`, `fork_from`, `run_grok_command`, `approve_grok_plan`, `start_autonomy`, `run_job`) with `daemon_draining` (and `in_reply_to`, the refused verb) for `hold_s` more. Due jobs wait too; approve, deny, stop and resume still pass. `0`/`0` releases, and a release, a later drain or shutdown ends a drain's wait early. Loopback only (`loopback_only` for a phone or a paired computer). Answered with `drain_result`. Sent by `tst drain` (TD-5023). |
| `list_sessions` | — | Ask for the current session list. A viewer's list also carries every paired computer's cached rows, answered from the cache without dialing (TD-5015). |
| `search_sessions` | — | Search what this computer's sessions said: `query` (up to 500 characters) and `limit` (1–200, default 50). Answered with `session_search_results` from the local full-text index, `session_index.db` in the data dir (TD-5129). Nothing leaves the computer. |
| `new_session` | yes | Create a fresh session in an existing session's workspace: the folder it was opened on, never its worktree, so a git folder gets another worktree (TD-5126). |
| `archive_session` | yes | File a session away, or restore it. Never a kill. |
| `set_session_star` | yes | Star or unstar a session. Machine-wide. Acked with `session_list` (TD-3003). |
| `delete_session` | yes | Destroy a session and its event log. Refused mid-turn. Its worktree goes too when unchanged; one with changes stays on disk on its branch (TD-5126). |
| `move_session` | yes | Reassign a session to another workspace, keeping its id and log. |
| `rename_session` | yes | Set a session's display title, or restore the auto-title when empty. Metadata only. |
| `close_worktree` | yes | Close a session's git worktree (TD-5126). `action` is `keep` (folder and branch stay on disk), `merge` (commit its changes on the branch, merge the branch into the main checkout; refused with nothing changed on a conflict or a mid-merge checkout) or `remove`. An unchanged worktree is removed whatever `action` says; with no `action`, one with changes is refused with `worktree_has_changes` so the window can ask. The session then works in the folder it was opened on. Refused mid-turn (`session_busy`) and while setup runs (`worktree_busy`). Acked with `session_list`. Not a machine-peer verb. |
| `get_setup_state` | — | Ask for the onboarding state: key presence, presets, active preset. |
| `set_api_key` | — | Store an API key in the OS keychain. Optional `base_url` is the host that key talks to (TD-1722). |
| `validate_api_key` | — | Probe a key with one cheap live call. A saved key is probed only at a model that uses it or at its own host (TD-5182). |
| `list_endpoint_models` | — | "Add a model": list a server's `/models`, with the typed key sent only to that URL (TD-5172). A saved `credential` must be a saved key, and is sent only to its own server (TD-5182). |
| `scan_local_servers` | — | "Find models on this computer": ask each loopback `local_servers` entry for `/models`, no key, on the user's click only (TD-5180). |
| `add_model` | — | "Add a model": store the key, write a single-model preset, make it active. Acked with `setup_state`. |
| `test_model` | — | Send one short test message to a preset's model (default: the active one). |
| `delete_api_key` | — | Remove an API key from the OS keychain. Only an API key's id, never another keychain item (TD-5182). |
| `set_preset` | — | Choose the active model preset. |
| `set_engine` | — | Choose the agent engine for new sessions (`native` or `grok`). Acked with `setup_state`. |
| `set_grok_mode` | yes | Switch the live Grok ACP session mode. |
| `run_grok_command` | yes | Enqueue `/{name} {argument}` as the next Grok prompt. Refused with `session_busy` while the session restores files or closes its worktree (TD-5161). |
| `list_grok_sessions` | — | List TUI sessions on disk under `~/.grok/sessions`. |
| `open_in_terminal` | yes | Open `grok --resume` in the user's terminal for this session's Grok id. |
| `approve_grok_plan` | yes | Tell Grok to approve the current plan and implement. Refused with `session_busy` while the session restores files or closes its worktree (TD-5161). |
| `list_grok_extensions` | — | List MCP / skills / plugins discovered in `~/.grok` (no secrets). |
| `set_tier_slug` | — | Set the model slug for one tier of one preset, then refresh that tier's prices and limits from its model card (TD-5185). |
| `refresh_model_cards` | — | Settings' Refresh from servers: re-read each tier's (and backup's) model card from the server it calls, with the key it sends, and write the matched prices and limits to config.yaml (TD-5185). A served window (vLLM `max_model_len`, llama.cpp `meta.n_ctx`, LiteLLM `/model/info` `max_input_tokens`) is taken on loopback too (TD-5189). Acked with `setup_state`. |
| `set_tier_context_window` | — | Settings' Context field: write one tier's `context_window` (more than twice its `max_output_tokens`, else `bad_request`) to config.yaml (TD-5189). Acked with `setup_state`. |
| `set_credential` | — | Create or rename a named API key without touching the secret (TD-1717). Optional `base_url` sets or clears the host that key talks to (TD-1722). |
| `delete_credential` | — | Remove a named key, its secret, and tier bindings (TD-1717). |
| `set_tier_credential` | — | Bind a named API key to one tier of one preset (TD-1717). That key's `base_url` is the host the tier calls when set (TD-1718). |
| `set_mcp_server` | — | Create or replace one listed MCP server (id, transport, command argv and/or url, enabled). No `env` or `headers` field; the server's existing ones are kept, unless the transport changes, which drops them and deletes their keychain items (TD-5110). A hand-set `allow_remote` is kept the same way, and dropped with a transport change (TD-5136). A url on another host or port drops the headers (deleting their items) and `allow_remote` too (TD-5161). Acked with `setup_state` (TD-4403). |
| `delete_mcp_server` | — | Remove one listed MCP server by id, and delete the keychain items its `env` / `headers` named (TD-5110). Unknown id is a typed error. Acked with `setup_state` (TD-4403). |
| `set_mcp_secret` | — | Store one env var (stdio) or header (http) of a listed MCP server (`server_id`, `target`, `name`, `value`) in the keychain item `tst-mcp.<server_id>.<entry>`; `config.yaml` gets only `keychain:<entry>`. The value never appears in any event, log or audit record. Acked with `setup_state` (TD-5110). |
| `delete_mcp_secret` | — | Remove one env var or header from a listed MCP server, and the keychain item it named. Acked with `setup_state` (TD-5110). |
| `run_diagnostics` | — | Run the doctor checks. |
| `get_usage` | — | Ask for token and cost rollups by session, day and week (TD-1706). |
| `export_usage` | — | Write a usage export; the daemon chooses the path and reports it back, so the verb cannot write anywhere the client names (TD-1706). |
| `list_artifacts` | yes | List artifacts persisted with the session (TD-3201). Acked with `artifact_list`. |
| `open_artifact` | yes | Open one artifact by id. Acked with `artifact` (metadata and path, not bytes). Unknown id is a typed error. |
| `read_artifact_chunk` | yes | Read up to 512 KiB of a file the agent sent (TD-5143), by artifact id and byte offset. Answered with `artifact_chunk`. Only `send_file` artifacts can be read. Forwarded for bridged ids. |
| `design_hit_test` | yes | Ask the last computer-use surface (browser DOM or desktop AX) what is at a CSS-pixel point (TD-3403 / TD-3406). Observe only. Acked with `design_hit`. |
| `check_cu_permissions` | — | Re-probe computer-use OS permissions / integrity without raising a TCC prompt (TD-3302, TD-3303, TD-2001). Acked with `cu_permissions`. |
| `reset_cu_permissions` | — | Reset this app's macOS TCC grants (Screen Recording, Accessibility) with `tccutil reset`, forget the prompt stamps, and raise both prompts again (TD-4823). A window action only, never a tool; off macOS it is just a re-probe. Acked with `cu_permissions`. |
| `set_cu_policy` | — | Set machine-wide computer-use enable, background/full-control mode, unhide-on-finish and denied apps. Persists `~/.tst-cu-mcp/config.yaml`; the MCP server re-reads it on the next tool call. Acked with `setup_state` (TD-4830). |
| `set_judgments` | — | Judgment-seam toggles: actuation verification, semantic no-progress breaker, candidate selection, confidence threshold, state cap, and the connector (`worker` or `typesafe`). Machine-wide; persists the `judgments:` block of the user config. All default off; every judgment fails closed. Takes effect on the next session. Acked with `setup_state` (TD-708 dev). |
| `set_cu_kill` | — | Engage or clear the process-wide computer-use kill-switch. Capture still runs. Acked with `cu_kill_state` (TD-3404). |
| `set_remote_attach` | — | Turn Tailscale remote attach on or off. Machine-wide; persists `{user_data_dir}/remote-attach.yaml`. On binds last-known / `tailscale0`; off drops the extra listener and deletes `remote-token`. Acked with `setup_state` (TD-3603). |
| `rotate_remote_token` | — | Replace `remote-token` and drop the remote clients that used it. Paired devices keep their own keys. Machine-wide; a no-op while remote attach is off. The token never crosses the wire. Acked with `setup_state` (TD-5005). |
| `start_pairing` | — | Show a one-time pairing code for a new phone. Turns remote attach on first when it is off. Loopback only. Acked with `pairing_code`, or the error `pairing_unavailable` when no Tailscale address can be bound (TD-5014). |
| `cancel_pairing` | — | Drop the active pairing code. Loopback only. Acked with `setup_state` (TD-5014). |
| `remove_remote_device` | — | Forget one paired device and close only its connections. Loopback, or a phone removing its own entry; any other id from a remote peer gets `loopback_only`. An unknown id changes nothing; a file that cannot be written gets `remove_failed`. Acked with `setup_state` (TD-5014). A hub removing itself from another computer uses the same self-removal (TD-5015). |
| `add_machine` | — | Pair another computer so its sessions show here: a `tstdesk://pair` `link`, or `host`, `port` and `code`. The host must be a Tailscale address or a `*.ts.net` name. Loopback only. Runs as a task; the answer is a pushed `machine_list`, or `pair_failed`, `pair_locked`, `machine_unreachable`, `not_tailscale`, `machine_limit`, `machine_busy` or `keychain_locked`. The code is never logged or echoed (TD-5015). |
| `remove_machine` | — | Forget a paired computer: first, best effort, remove this hub from that computer's paired devices, then drop its key and its `computers.yaml` entry. Loopback only. Acked with `machine_list`; an unknown id is `machine_not_found` (TD-5015). |
| `list_machines` | — | The paired computers. Loopback or a viewer. Acked with `machine_list` (TD-5015). |
| `reconnect_machine` | — | Try a paired computer again now, resetting its backoff (and a `refused` or `locked` stop). Loopback only. Acked with `machine_list` (TD-5015). |
| `list_jobs` | — | List persisted scheduled jobs. Acked with `job_list`. Does not run them (TD-3805). |
| `save_job` | — | Create or replace a scheduled job from draft fields. Optional `preset` (a catalog name) and `engine` (`native` or `grok`) pin that job's model; omitted uses the window's current choice. On an edit, omitted keeps the stored pin and `""` clears it (TD-3812). Optional `grace` is a duration phrase or a number of seconds (how late a slot may be and still run); the job stores seconds. On an edit, omitted keeps the stored grace and `""` clears it; omitted on create means a late slot still runs once (TD-3813). Optional `retries` (0–3) is extra tries after a transient scheduled failure, and `retry_delay` is a duration phrase or seconds (default 10 minutes when retries is at least 1). On an edit, omitted retries keeps the stored count, 0 clears it, omitted delay keeps the stored seconds, and `""` means the default; both are ignored by Run now (TD-3814). An existing `id` edits that job and keeps its run receipt; on that edit an omitted cadence or next run keeps the stored value and an empty string clears it (TD-3810). Pause is this verb with `paused` set. Optional `then` is another job's id, started once after this job ends ok; on an edit, omitted keeps it and `""` clears it, and a cycle is refused. The child's own schedule is not moved (TD-3817). Optional `max_run` is a duration phrase or seconds (1–60 minutes) for this job's turn; the job stores seconds. On an edit, omitted keeps it and `""` clears it so the job uses `scheduler.max_run_seconds`. Omitted on create means that too. Templates may carry it (TD-3819). Does not run the job. Acked with `job_list` (TD-3805). |
| `delete_job` | — | Remove a scheduled job by id and its run history, and clear `then` on any job that pointed at this id. Acked with `job_list`. Unknown id is a typed error (TD-3805, TD-3811, TD-3817). |
| `parse_job` | — | Turn natural language into a job draft. Does not persist. Acked with `job_draft`. Save is a second call (TD-3803). |
| `run_job` | — | Fire one job now. Acked at once with `job_list` (`running` set); a second `job_list` is pushed when the turn finishes. Does not move `next_run` or `paused`, does not consult `grace`, and does not retry a transient failure (TD-3809, TD-3813, TD-3814). A turn that stops on an approval card records `waiting` and does not move the schedule; the same receipt is updated when that session finishes (TD-3815). Unknown id is `job_not_found`; a job already in flight is `job_running`. |
| `list_job_runs` | — | List one job's run history, newest first. Acked with `job_runs`. A slot skipped for lateness is `status` `missed` with `trigger` `schedule`. A run parked on an approval card is `status` `waiting`; that same line becomes `ok` or `failed` when the session finishes. A slot skipped because the previous run is still parked is `missed`. A retried slot sets `attempt` and `attempts` on each try; both are null for Run now and for a job that does not retry. A fire started because another job ended ok is `trigger` `chained`, with `scheduled_for` null and `note` naming the parent. Unknown id is `job_not_found`. Does not run the job (TD-3811, TD-3813, TD-3814, TD-3815, TD-3817). |
| `list_job_templates` | — | List built-in and user job templates. Acked with `job_templates`. Does not create a job or the template file (TD-3816). |
| `save_job_template` | — | Store the current job form as a new template. Create only; the daemon assigns the id. An empty instruction is allowed. A secret-shaped field or an unknown preset is `template_invalid`. Acked with `job_templates`. Does not create a job (TD-3816). |
| `delete_job_template` | — | Remove a user template by id. A built-in is `template_builtin` and is not removed. Unknown id is `template_not_found`. Acked with `job_templates` (TD-3816). |
| `get_session_job_source` | yes | Read one chat into a job draft: workspace, preset, engine, and the earliest remaining user message. Acked with `session_job_source`. Unknown id is `session_not_found`. Does not create a job (TD-3816). |

### Daemon → client

Every member of `DaemonEventT`. "Seq" says where the sequence number comes from: `session` events
are stamped by a session's event log, `connection` events fix it at 1, and `ping` carries none.

<!-- verify: daemon-events -->

| Event | Seq | Purpose |
|---|---|---|
| `ready` | connection | Daemon and protocol versions. Declared and parseable, but not emitted in v0.1. |
| `session_state` | session | A session state transition, with an optional reason. Additive `engine` (`native` \| `grok`) names the loop this session started with. |
| `user_turn` | session | A user message the loop accepted, so replay can show the user's side without inventing it. `turn_id` is the queued message's own id (TD-5155). `steer` is set when a message sent mid-turn joined the running turn at a tool-batch boundary instead of starting one (TD-5112): `turn_id` (the running turn's), `step` (its model calls so far) and `after_tool_call_ids` (the batch the model read just before it); the audit database records the same point in `steers`. Logging it moves the session's `updated_at`, so `session_list` orders by use (TD-5111). `run_permissions` echoes the `tst run` rules the turn runs under (TD-5118), `null` for a turn without them. `line_comments` (TD-5132) are the diff comments a `submit_line_comments` message carried (`id`, `path`, `start_line`, `end_line`, `body`, `base_seq`), so the window can mark each resolved once a later write touches its lines (writes between `base_seq` and the turn carry the range; one that touches it leaves it outdated); `null` for any other turn. |
| `queued_messages` | session | The messages a restart left held, in order, whole each time (TD-5155): `queued_id`, `content` (what the `user_turn` will show) and `images_dropped` (attached images the queue file did not keep). Logged after a restart that left any and on every `send_queued` or `remove_queued`, and logged again when the log window trimmed the last one away, so the latest in a replay is the list. Nothing in it is sent until the person picks Send. |
| `conversation_reset` | session | The conversation forked or a sibling was selected. The viewer drops rows after that user turn and replaces it. |
| `files_restore_preview` | connection | Answer to `preview_restore_files` (TD-5125): `plan_id`, `files` (`path` relative to the repository, `change` `modify` \| `delete` \| `recreate`, `hand_edited`) and `diff`, each file from how it was before that turn to how it is now, cut at 200,000 characters when `diff_truncated`. Scrubbed for secrets like a logged event. No files means nothing to restore. |
| `attachment_extracted` | connection | Answer to `extract_attachment` (TD-5131): `request_id`, `name`, `kind` (`pdf` \| `docx` \| `xlsx`), `units` (pages or sheets; 0 for DOCX), `text_bytes` (what the caps measure) and `tokens` (an estimate at 4 characters a token). A refusal sets `error_code` (`attachment_too_large`, `attachment_unreadable`, `attachment_no_text`, `attachment_not_document`, ...) and `message`, with the counts 0. |
| `files_restored` | session | Files were put back as they were before a user turn (TD-5125): `restored`, `removed`, `left_alone` (hand-edited files nobody ticked) and `lfs_pointers` (written as a pointer because the object is not stored locally). |
| `assistant_delta` | session | A streamed chunk of assistant output. Additive `response_id` names the model response it belongs to (TD-5159); text the daemon writes itself (a failure, a progress note) has none. |
| `assistant_reasoning` | session | A streamed chunk of a reasoning model's thinking. Separate from `assistant_delta` because it is not part of the answer: the window folds it behind a disclosure, and it is never replayed to the provider as assistant speech. OpenRouter `reasoning_details` flatten into this event for the UI; the raw array is echoed on the next assistant message after tools, not as speech. Carries the same `response_id` as the deltas (TD-5159). |
| `assistant_retract` | session | Withdraw every `assistant_delta` and `assistant_reasoning` with this `response_id`: the provider went quiet partway through that response (`reason`: `stream_stalled`) and the loop asks again under a new id (TD-5159). Tool calls already run and earlier responses in the turn stay. An older client drops the event and keeps the withdrawn text above the retry. |
| `tool_call` | session | A tool call about to execute, with its decision class. The class is judged on the same workspace-joined paths dispatch uses, so a relative path shows the class the call runs with; `arguments` stay as the model sent them (TD-5148). |
| `tool_result` | session | The outcome of a tool call, with an error code and diff when applicable. `allowed_by` names the read-only shell list entries that let the call run without asking, and is empty when a policy rule, an approval or any other class default decided (TD-5103). `transcript` holds a `delegate` child's steps (task, replies, tool calls and results, notes), for display only: the model gets the capped summary in `output`. A `workflow` call's holds each worker's and checker's steps under a header (TD-5195). It is empty for every other tool (TD-5117). `hooks` lists the after-write hooks (a formatter or linter from `hooks.after_write`) that ran on an `fs_write` or `fs_edit`'s files: each `name`, `status` (`passed`, `failed`, `timed_out`, `error`), `exit_code`, `duration_ms` and whether it `changed` a file. The step timeline shows their time; a failure's output is already in `output`, for the model (TD-5135). |
| `shell_output` | session | A streamed chunk of a shell command's stdout or stderr. |
| `tool_progress` | session | Progress an MCP server reported for a running tool call: count, optional total and message (TD-5105). Thinned to two a second. |
| `approval_request` | session | A tool call parked for approval, with the summary, the reason, and the rule "always allow" would write (none for class C or a chained shell command, while the workspace config is not trusted, or when it would replace a saved `ask` or `never` rule for the same call, TD-5149). |
| `question_request` | session | An `ask_user` call parked its turn on one to four questions, each with 2-4 options (`label`, optional `description`), an optional `header` and `multi_select` (TD-5121). The session goes to `awaiting_approval` with reason "waiting for your answer" until `answer_question`, Stop, cancel, or `policy.approval_timeout_seconds`. The window and the phone show the options as buttons; the call's `tool_result` clears the card. Not emitted in an unattended run or a `tst run --on-ask deny-and-continue` turn (TD-5118): the tool refuses there with `ask_user_unattended`. Nor for a question whose text (question, header, label or description) the event log's secret scrub would change: that is refused with `ask_user_secret_shaped`, so every client shows the very labels the daemon checks answers against. |
| `question_answered` | session | How a parked `ask_user` call ended (TD-5121): `outcome` is `answered` (with `answers`), `declined`, `stopped`, `timed_out` or `cancelled`. Logged before the call's `tool_result`; the audit database records the questions and the answer in `questions`. |
| `plan_review` | session | An `exit_plan_mode` call parked its turn on the model's plan, in Markdown (TD-5123). The session goes to `awaiting_approval` with reason "waiting for your plan review" until `review_plan`, Stop, cancel, or `policy.approval_timeout_seconds`. The window and the phone show the plan with Approve, Edit and Reject; the call's `tool_result` clears the card. Only in plan mode, never in an unattended run or a `tst run --on-ask deny-and-continue` turn (`exit_plan_unattended`), and not for a plan the secret scrub would change (`plan_secret_shaped`). `tst run` exits `approval_needed` on it in every mode. |
| `plan_reviewed` | session | How a parked plan review ended (TD-5123): `outcome` is `approved` (with the approved `plan`, `edited` when it is the person's edit), `rejected`, `stopped`, `timed_out` or `cancelled`, plus the person's `feedback`. On approval the `tier_state` with `plan: false` and the `plan_pinned` come first. Logged before the call's `tool_result`. |
| `plan_pinned` | session | The plan the person approved, pinned to the session (TD-5123). The newest one is the pinned plan: the Plan pane shows it, and every compaction carries it into its summary ahead of the task list. Logged again as soon as the log window rotates the last one out, and on attach if it is missing; a restart reads it back. |
| `decision_logged` | session | A decision appended to the autonomy ledger. |
| `checkpoint_notice` | session | A one-time notice that checkpointing is degraded. |
| `verify_started` | session | An after-write review started running (TD-5158). It runs after `turn_complete` and holds the loop until its `verify_result`, so the window shows Stop in that span (Stop ends it, TD-5151). Not in the timeline. A review a restart cut off gets a `verify_result` (`error`, "Not reviewed: TST Desk restarted during the review.") on restore. |
| `verify_result` | session | Interactive validator review of a write turn. Timeline only; not a chat bubble (TD-4204). |
| `cost_update` | session | Accrued spend: this turn, this session, all time, by tier, and the classifier separately. A `consult` call (TD-5119) counts on the tier it asked, in the turn that made it; a delegate child's lands on its parent's session, and so does each `workflow` worker's and checker's (TD-5195). |
| `boundary_update` | session | The resolved workspace boundary and caps, and where they came from. Additive `config_trust` (TD-5113) is set when the workspace config loosens anything: `state` (`pending`, `trusted` or `declined`), its `sha256`, and each loosened setting with its default. Pending and declined run on packaged defaults. |
| `turn_complete` | session | A finished turn: tokens, cost, tier, duration, and any failure code. A turn a restart cut is sealed once at revive with `failed`, zeros and `error_code: interrupted_by_restart` (TD-5018). |
| `tier_state` | session | The active tier, any pinned override, slugs, preset, hosts (TD-1720), plan-mode lock (TD-4603; read-only since TD-5123), the picked `effort`, Fast (`fast`, `fast_available`, `fast_note`; TD-5176) and Ultracode (`ultracode`, `ultracode_available`; TD-5195). An older daemon omits `ultracode_available`, so a window hides the switch; a logged or bridged `tier_state` with effort `ultracode` reads as `max` with `ultracode` on. Native-engine sessions only. Additive `fallbacks` maps a tier that moved to a backup endpoint this turn to the backup's 1-based position in its list; `model_slugs` and `hosts` then name the backup (TD-5120), also in one the daemon sends mid-turn for `set_plan`, `set_tier` or `set_preset` (TD-5161). |
| `context_compacted` | session | Older turns were compacted to fit the context window. Never silent. Additive `trigger` (`auto` \| `manual` \| `from_here`) and `guidance` say who asked and what to keep (TD-5109). The trigger `inflight_trim` is not compaction: the loop trimmed the middles of this turn's tool results and no message left, so `dropped_messages` counts the results trimmed. The window shows it as a quieter "Trim" row that leads with the results trimmed and the tokens freed (TD-5150). The trigger `clear_stale` is not compaction either: before any turn is compacted, tool results from earlier turns were replaced with a one-line placeholder naming the tool, its target and the size that went (`clear_tool_results` in config.yaml), and no message left, so `dropped_messages` counts the results cleared. The window shows it as the same quiet "Trim" row, previewed "Cleared earlier tool results" (TD-5115). Whether the worker or the extractive fallback wrote the summary, and whether recent files came back under it, does not change the event (TD-5128). |
| `context_usage` | session | The next request's estimated tokens against the tier's window and its compaction point, for the title-bar gauge. Sent before each model call and after a compaction (TD-5109). Additive `model` names the model the figures are for, and `fallback` its 1-based position in the tier's `fallbacks` (0 for the tier's own model): on a backup the window and the calibrated estimate are the backup's (TD-5120). |
| `worktree_state` | session | The session's git worktree changed: made, set up, or closed (TD-5126). `worktree` is the state now (`branch`, `path`, `main_dirty`, `setup`, `setup_note`), null once the session works in the folder it was opened on; `note` says what happened in a sentence (a setup failure, a merge, or why no worktree could be made). The window refreshes the session list on it. |
| `task_list` | session | The session's whole task list after the native loop's `task_list` tool changed it: items, each with `content` and a `status` of `pending`, `in_progress` or `done` (TD-5122). The newest one is the list; an empty list clears it. The Plan pane and the task strip above the composer (what the phone shows) read it. Logged again as soon as the log window rotates the last one out, and on attach if it is missing, so a restart and a late window always find it. |
| `shell_jobs` | session | The session's background shell jobs after any of them changed (TD-5137): each with `job_id`, `command`, a `status` of `running`, `exited` (with `exit_code`), `stopped` (by `shell_stop`, or killed because the session ended) or `lost` (the daemon restarted while it ran), and `started_at` / `ended_at` in Unix seconds. The newest one is the list. The daemon owns the jobs; the Plan pane lists them. Logged when a job starts (`shell` with `background: true`, or `on_timeout: "background"` at a timeout), exits, or is stopped, and on revive when the old daemon's jobs are marked `lost`. |
| `stop_check` | session | The done-check ran because the model ended an interactive turn (TD-5124): the workspace's `stop_check.checks` and the session's `/goal`, each item with its `source` (`workspace` or `goal`), `via` (`shell` or `worker`), `passed`, `decided` and a capped `detail`. `outcome` is `passed` (the turn ends), `retry` (the failure went back to the model as a user message and the turn goes on; `attempt` counts from 1), `failed` (still failing after `max_retries` tries: the turn ends with `turn_complete.error_code` `stop_check_failed`) or `unchecked` (no item failed but one could not be judged, such as a declined command, so the turn ends and nothing went back; when one item failed and another could not be judged, the failure decides and only it goes back). A `$` check's command also logs its own `tool_call` and `tool_result`. The window shows the check on the assistant's reply. Not emitted for unattended runs, which poll their charter's definition of done, delegate children, or a turn that ends while read-only plan mode (TD-5123) is on. |
| `hook_run` | session | One hook ran (TD-5134): `hook_id` (`<source>:<event>:<n>`), `source` (`user` or `workspace`), `event` (`session_start`, `user_prompt`, `before_tool`, `after_tool`, `stop`), `command`, `result` (`ok`, `context`, `block`, `ask`, `error`, `timeout`), `exit_code`, `reason`, `context`, the start of its `output`, `duration_ms`, and the `tool_name` and `tool_call_id` for a tool hook. The audit writer stores every one in `hook_runs`. |
| `hooks_state` | session | Response to `get_hooks`: each active hook with its command, `timeout_seconds`, `tools` globs and `last` run, plus the workspace config's trust state and why a workspace `hooks` block was set aside. |
| `session_goal` | session | The session's goal after `/goal <statement>` set it (`change: set`), `/goal clear` cleared it (`cleared`) or a passing done-check met it (`met`); `goal` is `null` once cleared (TD-5124). The newest one is the goal: a restart restores it, and the daemon logs it again when the log window rotates it out or an attach finds it missing. |
| `steering_reloaded` | session | Steering files were re-resolved after a detected change. |
| `rule_activated` | session | A path-scoped rule entered the prompt because a matching file was touched. |
| `tier_switched` | session | The active tier was overridden, naming the previous tier. |
| `model_fallback` | session | A tier's endpoint failed after its retry budget (`reason`: a refused or dropped connection, 5xx, rate limit, timeout or stalled stream; `stream_interrupted` for a reply the endpoint cut off partway by dropping or with an in-band 429 or 5xx) and the same request went to the next backup in the tier's `fallbacks`: `from_model`/`from_host` to `to_model`/`to_host`, `fallback` its 1-based position (TD-5120). Text the failed call showed was withdrawn first (`assistant_retract`). Later calls in the turn stay on the backup; the next turn starts on the tier's own model. |
| `instruction_stack` | session | The resolved steering stack: sources, tokens, imports, cache state, and skills (TD-4502), each with the frontmatter keys it ignored and whether only `/name` loads it (TD-5107). |
| `instruction_files` | connection | The workspace's Instructions column: root steering plus `.tst/rules/*` (TD-2802). |
| `command_list` | connection | The workspace's slash commands: name, description, source, body (TD-4501). |
| `context_pins` | connection | The workspace's Context column: pinned files and folders (TD-2804). |
| `memory_files` | connection | The workspace's Memory column: `.tst/memory/*.md` with contents (TD-2601). |
| `charter` | connection | The workspace's Charter column: parsed §12.4 fields, or absent (TD-4002). |
| `autonomy_start` | connection | Reply to `start_autonomy`: signed, ready, optional `session_id` of the daemon-owned run, or the refusal (TD-4003, TD-4101). |
| `autonomy_summary` | session | Unattended-run wrap-up: stop reason, turns, spend, refusals (TD-4303), and `steering_proposals`, the steering edits the run proposed that wait for the user (TD-5140). |
| `memory_proposal` | session | Distill produced file diffs the user must accept, edit, or reject (TD-2401). |
| `steering_proposal` | session | The agent proposed a steering edit with `propose_steering_edit`; nothing is written until the user decides (TD-5140). Shaped like `memory_proposal`: a proposal id, then action (`create` or `replace`), path, diff, before and after per file. `replaces` names a stale proposal this fresh diff supersedes; `autonomy` marks one an unattended run parked for its summary. Only emitted when the event's secret scrub leaves it unchanged, so the card shows exactly what Accept writes. |
| `steering_decision` | session | The decision on a steering proposal, with its diff (TD-5140): the user's `accepted`, `edited` (the diff is what was written) or `rejected`, or the daemon's `expired`, logged when it restores a session whose proposal a restart dropped (nothing written). |
| `session_list` | connection | The current session list. Each row carries the catalog `preset` (TD-1721). `busy` is a turn in flight; `state: running` is loop liveness (TD-1714, TD-1720). Each row carries `machine`: null for this computer, else the `mch_` id of the paired computer it lives on, whose rows use bridged ids. Only viewers get other computers' rows (TD-5015). `worktree` is the git worktree the session works in (`branch`, `path`, `main_dirty`, `setup`, `setup_note`), null in the folder itself; `workspace_path` stays the folder it was opened on, so the rail groups the row with its project (TD-5126). |
| `session_search_results` | connection | Reply to `search_sessions` (TD-5129): at most one hit per session, best first, each with `session_id`, `turn` (the 1-based user turn), `role` (`user` or `assistant`) and a `snippet` around the match. `query` echoes the request so a window that typed on drops a stale reply. `available` is false when the index could not be opened; the rail then filters on titles alone. |
| `machine_list` | connection | The paired computers: id, name, host, port, `status` (`connected`, `connecting`, `unreachable`, `refused`, `locked`, `outdated`), last seen, paired at, `same_computer` for a self-pair, and a `detail` line. Never a key. The reply to the four machine verbs, and pushed to viewers when one changes; never sent to a machine peer (TD-5015). |
| `drain_result` | connection | Reply to `drain`, to that connection only: `drained` (no turn open at all), the session ids still `running`, `paused` for the person (the progress guard or a cap, TD-5155) or in `awaiting_approval` (ids only), and `hold_until` (null once released) (TD-5023). Only `running` turns can finish by themselves; `tst drain` exits 0 when none is left unless `--strict`. |
| `policy_rules` | connection | The workspace's saved policy rules. An `auto` rule with no wildcard carries `exact`, the one text its `args` matches (`app/[[]id]/page.tsx` → `app/[id]/page.tsx`; for `shell`, the command segment once wrappers such as `timeout 60` are stripped). `exact` is null for a glob, and for every `ask` and `never` rule, which also match in another case (TD-5149, TD-5158). |
| `setup_state` | connection | Onboarding state, and the ack for `set_api_key` / `set_preset` / `set_engine` / `set_tier_slug` / `refresh_model_cards` / `set_skip_all_approvals` / `set_load_global_memory` / `set_coworker` / `set_voice` / `set_cu_indicators` / `set_workspace_pin` / `set_remote_attach` / `rotate_remote_token` / `cancel_pairing` / `remove_remote_device` / `set_mcp_server` / `delete_mcp_server` / `set_mcp_secret` / `delete_mcp_secret` / `set_judgments` / `set_email_notify`. `remote_bind` is the bound Tailscale address, never a token. `remote_devices` lists paired devices (id, name, paired_at, last_seen, connected), never a key or its hash (TD-5014). `tier_cards` is the active preset's stored prices and limits per tier with the last card refresh's outcome (TD-5185). Carries `engine`, `grok_available`, `grok_binary`, `voice_enabled`, and `voice_has_endpoint`. `mcp_servers` is the listed MCP servers (id, transport, command, url, enabled, and `env` / `headers` as name, source, entry and stored presence, TD-5110) — never a secret or a literal value. `speech_enabled` / `speech_ready` are hold-to-talk flags (TD-4701); the speech URL never appears. `judgments_*` echoes the judgment-seam toggles (TD-708 dev). A remote peer (a phone or another computer) gets it without `remote_devices`, `remote_bind` and `remote_port` (TD-5015). `email` is the mail form (enabled, method, host, port, security, username, from_address, password_stored) for TD-3820, TD-4853, and TD-4854; the password never appears. `github` is the connector snapshot (enabled, connected, phase, user code, verification URL, login, message) for TD-4852; tokens and the device code never appear. `microsoft` is the sign-in snapshot (enabled, connected, phase, account, message) for TD-4853; tokens and the authorization code never appear. `google` is the sign-in snapshot (enabled, connected, phase, account, message, client_secret_stored) for TD-4854; the secret, the tokens, and the authorization code never appear. |
| `pairing_code` | connection | Reply to `start_pairing`: the code (and `ABCD-2345` display form), host, port, desktop name, the `tstdesk://pair` link, a QR of it as standalone SVG, and when it expires (TD-5014). |
| `pairing_completed` | connection | A phone redeemed the code. Carries that device's summary (never its key). Sent to the computer's own loopback windows only (TD-5014). |
| `api_key_validated` | connection | The result of a key probe. Never carries the key. |
| `endpoint_models` | connection | A server's model list for "Add a model", or why it could not be read. Never carries the key. |
| `local_servers` | connection | The servers on this computer that answered `scan_local_servers` with models, in config order, and every URL asked. |
| `model_tested` | connection | The result of `test_model`: the reply, or what went wrong. |
| `diagnostics_report` | connection | Doctor results: one row per check, with a fix when it failed. The `skills` row names each skill's ignored frontmatter keys (TD-5107). |
| `usage_report` | connection | The rollups `get_usage` asked for, bucketed and broken out by tier (TD-1706). |
| `usage_exported` | connection | Where `export_usage` wrote, and how many rows (TD-1706). |
| `log_trimmed` | connection | Attach asked for a seq the on-disk window dropped. Replay continues from `earliest_seq` (TD-2901). |
| `artifact_ready` | session | An artifact was recorded for this session (TD-3201; `kind: "sent_file"` with name, size, sha256, caption and tool call id for a sent file, TD-5143). |
| `artifact_list` | connection | The artifacts `list_artifacts` asked for. |
| `artifact` | connection | Metadata and path for `open_artifact`. Bytes stay on disk. |
| `artifact_chunk` | connection | Bytes for `read_artifact_chunk` as base64, with the file's size and sha256, or a per-request `error_code`. Never logged; exempt from the wire scrub. |
| `ping` | — | Application-level liveness. Belongs to no session; advances nothing. |
| `error` | session | A typed error, usually in response to a bad message. One from another computer without a session id carries `machine` and a message starting "On <name>: " (TD-5015). A hub refusing a forwarded message sets `in_reply_to` to that message's type, so a download claims only its own refusals (TD-5144). A refused `steering_accept`, `steering_edit` or `steering_reject` also carries `proposal_id`, so the refusal lands on the card it is about (TD-5158). |
| `screen_frame` | session | A computer-use screenshot (browser or desktop) was written to the session dir (TD-1710, TD-3401). Path, not bytes. |
| `cu_kill_state` | connection | Process-wide computer-use kill-switch. Seq is fixed at 1 and it is not written to a session log (TD-3404). `killed=true` clears Screen-pane glow and cursor (TD-3402). |
| `cu_session` | session | Computer-use episode open/close (TD-3407). `active=true` on the first `desktop_*` / `browser_*` tool of a turn; `false` on turn end, cancel, or kill-switch. The real-display ring and Screen-pane glow follow this tag. |
| `focus_window` | connection | A `cu_session` closed and no other session still actuates — bring the local window forward (TD-4832). Live-only: never written to a session log, so attach replay cannot re-trigger it. The host shows, unminimizes, and focuses the window; a browser viewer no-ops; activation failures never fail the turn. Sent to loopback connections only (TD-5015). |
| `design_hit` | connection | Reply to `design_hit_test`: xpath, role, attributes, box, styles (TD-3403 / TD-3406). Not in the session log. |
| `cu_permissions` | connection | macOS Screen Recording / Accessibility plus System Settings deep links (TD-3302), Windows UIPI / secure-desktop integrity (TD-3303), or Linux X11 no-gate / Wayland session limits (TD-2001). |
| `job_list` | connection | The jobs `list_jobs` / `save_job` / `delete_job` / `run_job` asked for, and a push when a run finishes. Each job may name a pinned `preset` and `engine`. `grace` is seconds or null. `retries` is extra tries, `retry_delay` is seconds or null, and `attempt` is tries already used this slot (above 0, a window notice waits). `last_status` may be `missed` or `waiting` (parked on an approval card). `running` is true while a turn is in flight. `then` is the follow-on job's id, or null. `max_run` is seconds or null (null uses `scheduler.max_run_seconds`) (TD-3805, TD-3809, TD-3812, TD-3813, TD-3814, TD-3815, TD-3817, TD-3819). The push when a run finishes goes to viewers only, never to a machine peer (TD-5015). |
| `job_draft` | connection | Reply to `parse_job`: draft fields or a typed refusal. Not saved (TD-3803). |
| `job_runs` | connection | Reply to `list_job_runs`: that job's runs, newest first. `scheduled_for` is null for Run now. `status` may be `missed` for a slot skipped for lateness, or `waiting` for a run parked on an approval card (that line is updated in place when the turn finishes). `attempt` and `attempts` are set when the job retries. `trigger` may be `chained`, and `note` names the parent job (TD-3811, TD-3813, TD-3814, TD-3815, TD-3817). |
| `job_templates` | connection | Reply to `list_job_templates`, `save_job_template`, and `delete_job_template`. Built-ins first. `cadence` is the phrase, not cron. `next_run` may be an ISO instant or `tomorrow at H:MM`. `max_run` is seconds or null. Seq is fixed at 1 (TD-3816, TD-3819). |
| `session_job_source` | connection | Reply to `get_session_job_source`: workspace, preset, engine, and instruction for one chat. Seq is fixed at 1. Does not save a job (TD-3816). |
| `grok_commands` | session | Slash commands the Grok ACP agent advertised. |
| `grok_plan` | session | Plan-mode markdown and entries from ACP `plan` updates. |
| `grok_mode` | session | Current Grok ACP mode, the advertised set, and optional `model` the CLI is using. |
| `grok_preview` | session | A workspace media file or loopback URL to show in Preview. |
| `grok_session_list` | connection | TUI sessions under `~/.grok/sessions`. |
| `grok_extensions` | connection | Read-only MCP / skills / plugins from `~/.grok`. Never secrets. |
| `transcript` | connection | Reply to `transcribe`: `ok`, `text` on success, `error` copy or a short `detail` code on failure. Never a URL (TD-4701). |
| `email_test_result` | connection | Reply to `test_email`: `ok`, and on failure `error_class` plus `message`. The password and the message body are not included (TD-3820). |
| `github_status` | connection | GitHub connector snapshot (TD-4852). `phase` is `off`, `idle`, `pending`, `connected`, `expired`, `denied`, or `error`. `user_code` and `verification_uri` are shown in Settings while pending. Tokens and the device code are not included. Seq is fixed at 1. The same public fields ride on `setup_state.github`. |
| `microsoft_status` | connection | Microsoft connector snapshot (TD-4853). `phase` is `off`, `idle`, `pending`, `connected`, `denied`, `error`, or `expired`. `account` is the mailbox once signed in. Tokens and the authorization code are not included. Seq is fixed at 1. The same public fields ride on `setup_state.microsoft`. |
| `google_status` | connection | Google connector snapshot (TD-4854). `phase` is `off`, `idle`, `pending`, `connected`, `denied`, `error`, or `expired`. `account` is the mailbox once signed in. `client_secret_stored` is a presence probe. The secret, the tokens, and the authorization code are not included. Seq is fixed at 1. The same public fields ride on `setup_state.google`. |

### Adding a message

Six places, in this order:

1. The pydantic model in `core/tstd/protocol.py`.
2. Its union — `ClientMessageT` or `DaemonEventT`.
3. Its frozenset — `_KNOWN_CLIENT_TYPES` or `_KNOWN_EVENT_TYPES`.
4. The matching table above.
5. The TypeScript interface in `ui/src/lib/protocol.ts`, and `KNOWN_EVENT_TYPES` in
   `ui/src/lib/client.ts` for an event.
6. A fixture in `core/scripts/generate_protocol_fixtures.py`, which regenerates
   `ui/src/lib/protocol-fixtures.json` for the UI's round-trip test.

A session verb a paired computer may send (such as `set_ultracode`, TD-5195) also goes in
`MACHINE_PEER_VERBS` in `core/tstd/peer_policy.py` and `BRIDGED_VERBS` in
`ui/src/lib/bridged-id.ts`. A UI test holds `BRIDGED_VERBS` equal to the fixtures'
`forwarded_verbs`.

The doc test fails until step 4 is done, and steps 2 and 3 are checked against each other. The
TypeScript types are a deliberate hand-mirror rather than generated code (`DECISIONS.md`, TD-204
§4) — the fixture round-trip is what keeps the mirror honest.

Prefer additive fields with defaults over a version bump: a client that ignores a new optional
field should keep behaving exactly as it did. That is why `SetupState.key_required`,
`SetupState.skip_all_approvals`, and `SessionSummary.archived` shipped without touching
`PROTOCOL_VERSION`.

---

## 4. Extension points

These are the seams that exist on purpose. Changing anything through one of them is a local
change; changing the same behaviour anywhere else usually is not.

<!-- verify: seams -->

| Seam | Where | What it is for |
|---|---|---|
| `create_registry` | `core/tstd/tools/registry.py` | Declares which tools exist and their schemas, approval class, parallel safety, and classifier metadata. Builtins are registered explicitly; `load_plugins` then loads the `tstd.tools` entry-point group (TD-4601). |
| `load_plugins` | `core/tstd/tools/plugins.py` | Discovers in-process plugin packages, refuses non-permissive licenses, and calls each `register(registry, dispatcher)`. A broken plugin is logged and skipped. Name collisions with builtins are skipped, not replaced. |
| `register_builtin_handlers` | `core/tstd/tools/handlers.py` | Binds each registered tool name to the async callable that executes it. Registry and handler are separate on purpose: a tool with no handler is a configuration error the model is told about, not a crash. |
| `RULE_TABLE` | `core/tstd/autonomy/classifier.py` | The static decision rules, in priority order, first match wins. Anything the table cannot decide falls to the worker-tier classifier and defaults to class B — fail toward asking, never toward acting. |
| `git_command` | `core/tstd/git_safe.py` | The argv every git call the daemon makes starts with: `core.fsmonitor` off, `core.hooksPath` at nothing so no hook runs, and bare repos only when named, set with `-c` so no repo config can override them (TD-5108). A new git call in the daemon goes through here; a test fails on a raw `"git"` argv. |
| `stage_paths` | `core/tstd/git_stage.py` | How the daemon puts files into an index: `hash-object --no-filters` and `update-index --index-info`, never `git add`, so no filter driver an in-tree `.gitattributes` names runs (TD-5157). Line endings follow git's own rules, redone in `core/tstd/git_eol.py`. The checkpointer stages with it, and the memory commit and charter sign commit with `commit_on_head` beside it. Revert writes blobs back with `restore_blobs` (`core/tstd/git_restore.py`), and so does "Restore files to here" (`core/tstd/file_rewind.py`, TD-5125; it reads the session branch with `core/tstd/file_rewind_log.py`, TD-5161), which also stages a file just before an agent write when the person changed it, so the branch keeps their copy. A git call that must read the working tree or write the user's index (`git status`, the validator's `git diff`) adds `filter_pins` from `core/tstd/git_raw.py`. |
| `sandbox_start_error` | `core/tstd/autonomy/sandbox.py` | Why an autonomous run may not start: missing / down / rootful runtime. Interactive sessions do not import this module (TD-4301). |
| `build_plan` | `core/tstd/shell_sandbox.py` | The bubblewrap argv an interactive shell command runs under when the workspace turns `sandbox.enabled` on: read-only root, writable workspace, private `/tmp`, no network, a seccomp filter refusing `socket(AF_UNIX)` and io_uring (no host sockets), `.git` and steering files read-only, keys and keyrings masked; `guard_snapshot`/`guard_restore` move aside anything a command put at a guarded name a bind could not pin. `promote` is the one place a sandboxed command's ask may become `auto`; the dispatcher records the promise and the shell tool refuses a promised call it cannot sandbox (TD-5138). |
| `Precedence` | `core/tstd/context/discover.py` | The steering hierarchy: `user global` < `workspace` < `rules` < `nested`. Adding a scope means adding a level here, and `docs/steering.md` documents each one. |
| `ToolDispatcher` | `core/tstd/tools/dispatch.py` | The single chokepoint every tool call passes through: schema validation, classifier, path guard, policy gate, handler, after-write hooks (TD-5135), language-server diagnostics on the written files (TD-5139), checkpoint, ledger. Prime directive §2.6 lives here, and reaching a handler unclassified raises rather than executing. |
| `LspPool` | `core/tstd/lsp/pool.py` | The daemon's running language servers (TD-5139), one per workspace and `lsp.servers` entry, each an `LspClient` (`core/tstd/lsp/client.py`) speaking LSP over the child's stdin and stdout, never a socket. The dispatcher's after-write diagnostics (`core/tstd/lsp/diagnostics.py`) and the `lsp_definition`, `lsp_references` and `lsp_symbols` tools (`core/tstd/tools/lsp.py`) ask it for a server; the daemon closes it on shutdown. |
| `Daemon` | `core/tstd/daemon.py` | `_attach_session_runtime` is where a session is wired: router, boundary, policy, tool stack, and the provider factory. Open and revive both call it. The provider is a closure, not an object, so a session can be opened and attached before any API key exists — the key is only needed when the loop makes its first model call. When `engine.kind` is `grok`, the loop factory is `grok_loop` instead of `agent_loop`. |
| `grok_loop` | `core/tstd/grok_loop.py` | ACP-backed loop. Spawns `grok agent stdio` and maps session updates onto the same event log native turns use. The CLI remains the engine; this is a viewer. |

Two seams that look like extension points and are not:

- **The event log.** Append-only, single writer, stamps its own `seq`. Do not add a second
  writer or an out-of-band path to clients; redaction (`_redact_event`) happens at insertion
  precisely because one pipeline feeds replay, broadcast, and the audit writer at once.
- **The boundary guard.** `PathGuard` is security code (AGENTS.md §7). A gap there is a defect,
  not a missing nice-to-have.

---

## 5. How to add a tool

A worked example: a `word_count` tool that counts words in a text file. Four edits, in the order
you would actually make them. Every block below is executed by the doc test — it registers this
tool on a real registry, dispatches it through a real `ToolDispatcher`, and compares the output
to the result shown at the end.

### Step 1 — declare the tool

In `_register_builtins` in `core/tstd/tools/registry.py`:

<!-- verify: tool-registration -->
```python
registry.register(
    Tool(
        name="word_count",
        description="Count the words in a UTF-8 text file.",
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute path to the file to count",
                },
            },
            "required": ["path"],
        },
        side_effect_class="auto",
        parallel_safe=True,
        path_fields=("path",),
    )
)
```

The fields that are easy to get wrong:

- `parameters` is JSON Schema and is enforced. Bad arguments come back to the model as an
  `invalid_arguments` result it can correct, rather than failing the turn.
- `path_fields` names the arguments holding file paths. This is what makes the path guard check
  them and what feeds the decision classifier. Omitting it does not make your tool unguarded —
  it makes it *invisible* to the guard, which is worse. Name every path argument.
- `mutates` is False here because counting words changes nothing. Set it True and the same
  `path_fields` become write targets: checked against `writable_paths`, snapshotted for a diff,
  and checkpointed to a revertable commit.
- `parallel_safe` is True because two counts cannot conflict. Anything that writes, or that
  shells out, is not parallel-safe.

### Step 2 — write the handler

In `core/tstd/tools/handlers.py`. Handlers are async and receive `session` and `tool_call_id`
plus the validated arguments, all as keyword arguments, and return a string. (A handler may
instead return a `HandlerOutput` from `core/tstd/tools/results.py`: its `text` is the result,
and its `transcript` goes on the `tool_result` event for the window and never to the model.
`delegate` uses it for its child's steps, TD-5117, and `workflow` for its workers' and
checkers', TD-5195. Its `images` go to the model as vision parts
after the text and never onto an event or into a log; `fs_read` uses them for an image file or
PDF page images when the session's `tool_vision` says the reading tier takes images, TD-5130.)

<!-- verify: tool-handler -->
```python
async def word_count(session: object, path: str, tool_call_id: str = "") -> str:
    """Count the words in a UTF-8 text file."""
    text = await asyncio.to_thread(Path(path).read_text, encoding="utf-8")
    return str(len(text.split()))
```

The blocking read goes through `asyncio.to_thread` because AGENTS.md §6 forbids blocking calls in
the event loop. This is not a style preference: a synchronous read of a large file stalls every
other session in the daemon.

Your handler does not validate the path. By the time it runs, the dispatcher has already
canonicalised it, resolved symlinks, and refused anything outside the boundary. Re-checking is
harmless; *relying on your own check instead* is how a bypass gets written.

### Step 3 — bind the handler

In `register_builtin_handlers` in `core/tstd/tools/handlers.py`:

<!-- verify: tool-wiring -->
```python
dispatcher.register_handler("word_count", word_count)
```

Registration fails loudly with a `KeyError` if the name is not in the registry, so a typo here
surfaces at startup rather than at the first call.

### Step 4 — call it

Given this file in the workspace:

<!-- verify: tool-fixture notes.md -->
```
the daemon owns sessions
the window is only a viewer
```

and this call from the model:

<!-- verify: tool-arguments -->
```json
{"path": "<workspace>/notes.md"}
```

the dispatcher returns:

<!-- verify: tool-result -->
```
10
```

### What happened between the call and the result

Worth reading once, because it is the same path for every tool:

1. **Resolve.** Unknown name → a structured error the model can correct from.
2. **Validate.** Arguments against the tool's JSON Schema.
3. **Classify.** `RULE_TABLE` first. A built-in read inside the workspace is Class A by
   `builtin-read` (TD-5141). A shell command is Class B by `shell-floor`, unless every command
   in it is on the fixed read-only list (`shell-read-only`, `core/tstd/shell_read_only.py`,
   TD-5103), which is Class A. The user's `ask` and `never` rules for the built-in reads
   guard that list too (`Boundary.read_guards`): a listed command that names a guarded file,
   or can print an unnamed one, stays Class B. A call no rule decides falls through to the worker-tier
   classifier. A call reaching a handler without a classifier attached raises
   `UnclassifiedToolCall` — the chokepoint refuses to execute unclassified actions rather
   than defaulting to permissive.
4. **Guard.** Every `path_fields` argument is canonicalised and checked. Reads are checked
   against the workspace wall; writes additionally against `writable_paths`, the steering-file
   ban, and hardlinks.
5. **Gate.** Policy resolves the class to `auto`, `ask`, or `never`. Rules come first, deny
   first: any matching `never` refuses, then any `ask` asks, and `auto` needs a rule for every
   path or shell command in the call (TD-5101, `core/tstd/policy_shell.py`). A line the shells
   split differently never auto-runs; a differential test holds the splitter to bash, dash and
   busybox ash (TD-5156). With no rule,
   class A is auto, class B asks, class C takes the workspace's `class_c_default`. `ask` with
   no approval handler attached raises — the approval gate is part of the chokepoint, not an
   optional add-on.
6. **Execute.** Your handler runs. An exception becomes a `handler_error` result, not a dead turn.
7. **Record.** Touched paths go on the session, so path-scoped steering rules can activate.
   Mutations get a diff and a checkpoint commit. Class A and B decisions append to the ledger.
   A call the read-only shell list let run carries the entries on its result, and the audit
   database keeps them in `allowed_calls`.

Steps 3 through 5 are why a new tool inherits the entire trust surface for free, and why
bypassing the dispatcher is never the right shortcut.

### How to add a plugin

A third-party package can register tools the same way builtins do, without editing
`registry.py`. It exposes a `register(registry, dispatcher)` callable on the `tstd.tools`
entry-point group. TST Desk loads those entry points from `create_registry`, checks the
distribution's license, and skips the whole plugin if the license is not permissive. That skip
is a Class C product decision (logged, not silent) — not a tool-call classification, and not
an absent plugin with no log line.

Plugins do not skip the classifier, PathGuard, or the policy gate. Every call still goes
through `ToolDispatcher.dispatch`. Declare `path_fields` (and `host_fields`) for every
argument that is a path or a host — omitting them does not make the tool unguarded; it makes
those arguments invisible to the guard, which is worse. Missing path/host metadata on a
non-MCP plugin is existing classifier behavior: `side_effect_class` is the floor (`ask` stays
at least B).

The license allowlist is MIT, Apache-2.0, BSD-2-Clause, BSD-3-Clause, ISC, Unlicense, 0BSD,
and CC0-1.0. SPDX expressions that are only those identifiers, combined with AND or OR, pass.
`GPL`, `AGPL`, `LGPL`, `Proprietary`, empty, and unknown do not load. Provenance is
`plugin:<distribution>`. A plugin that reuses a builtin name such as `fs_read` is skipped, not
renamed — same spirit as MCP `{id}__{name}`.

#### Step 1 — write `register()`

<!-- verify: plugin-register -->
```python
def register(registry: ToolRegistry, dispatcher: ToolDispatcher) -> None:
    async def shout(session: object, text: str, tool_call_id: str = "") -> str:
        del session, tool_call_id
        return text.upper()

    registry.register(
        Tool(
            name="shout",
            description="Uppercase a short string.",
            parameters={
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "Text to shout",
                    },
                },
                "required": ["text"],
            },
            side_effect_class="auto",
            parallel_safe=True,
            provenance="plugin:example",
        )
    )
    dispatcher.register_handler("shout", shout)
```

#### Step 2 — declare the entry point

In the plugin package's `pyproject.toml`:

<!-- verify: plugin-entry-points -->
```toml
[project.entry-points."tstd.tools"]
shout = "example_plugin:register"
```

#### Step 3 — a call still hits the classifier

Given this call from the model:

<!-- verify: plugin-arguments -->
```json
{"text": "hello"}
```

the dispatcher returns:

<!-- verify: plugin-result -->
```
HELLO
```

and `decision_class` is set. An unclassified result would mean the plugin bypassed the
chokepoint; that is a defect, not a feature of plugins.

---

## 6. Where to look next

| You want to change | Read |
|---|---|
| What the product is and why | `docs/tst-desk-spec.md` |
| What is being built now | `docs/tst-desk-backlog.md` |
| How to work in this repo | `AGENTS.md` |
| Config keys and their defaults | `docs/configuration.md` |
| Steering files, imports, path-scoped rules | `docs/steering.md` |
| Why a past choice was made | `DECISIONS.md` |
