# Architecture guide

How TST Desk is put together, why the pieces sit where they do, and where a contributor plugs
in. Written for someone who has cloned the repo and wants to change something without breaking
the invariants.

The spec (`docs/tst-desk-spec.md`) says what we are building. This says how it is built. Where
the two disagree, the spec wins and this file is the bug.

Every table and every code block below is checked against the code by
`core/tests/test_docs_architecture_guide.py`. The protocol tables are compared against the
discriminated unions in `core/tstd/protocol.py`, so adding a message without documenting it
fails the suite. The "how to add a tool" walkthrough is registered and dispatched by that test,
so a walkthrough that stopped working fails too.

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
audit database. It binds a WebSocket server to loopback and nothing else — a non-loopback bind is
refused outright by `validate_interface` in `core/tstd/ws.py`, which is prime directive §2.1
enforced in code rather than by convention.

**The host (`shell/`)** manages the window and the daemon process lifecycle: it resolves the
`tstd` binary, spawns it with an explicit `--data-dir` and `--parent-pid`, waits for the port
file, probes the handshake, supervises with a bounded restart budget, and on window close asks
the daemon to shut down before killing it. It also exposes the daemon's port and token to the
webview through a Tauri command, so the window never reads the port file itself. AGENTS.md §6
states the rule plainly: *"Keep it thin. … Business logic belongs in Python."* The host never
parses a protocol message beyond checking for `hello_ack`. When you find yourself wanting to add
a decision to the Rust side, that is the signal that it belongs in `tstd` instead.

One correction to the layout you may have read elsewhere: **the keychain is not in the host.**
AGENTS.md §6 lists it among the host's jobs, but there is no keychain code in `shell/` at all.
Secrets live in `core/tstd/keychain.py`, which shells out to `security` on macOS, `secret-tool`
on Linux, and the Windows credential store, all under the service name
`com.thatsimpletech.tstdesk`. Native folder picking is likewise driven from the UI through the
Tauri dialog plugin rather than from Rust. The host's native surface is narrow by design: window
state, dialog, and opener.

**The window (`ui/`)** is a renderer. AGENTS.md §6 again: *"The UI never derives truth it wasn't
given — if the daemon didn't send it, don't infer it."* Every number in the title bar, every
approval card, every timeline entry is a daemon event the UI was handed.

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
anywhere.

**Disconnect cleanup is scoped to the stream.** When a connection drops,
`Daemon._on_connection_closed` calls `_cleanup_attach`, which removes the connection from
`_attached_clients` and cancels *that connection's streaming task*. It does not reach the runner.
The only things that stop a loop are `Session.cancel` (user asked) and daemon shutdown.

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

`interrupted` is the one that needs explaining. It is a tombstone: the daemon died while the
session was alive, so the persisted registry knows the session existed and which workspace it
belonged to, but the in-memory event log did not survive. There is nothing to resume, so the
state has no exits at all.

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

---

## 3. The wire protocol

One WebSocket, JSON frames, snake_case field names in both Python and TypeScript. Client→daemon
frames are `ClientMessage` subclasses; daemon→client frames are `DaemonEvent` subclasses. Both
sides discriminate on a `type` field, and `core/tstd/protocol.py` declares them as pydantic
discriminated unions (`ClientMessageT`, `DaemonEventT`) so parsing is total and typed.

### The handshake

1. Client connects to `ws://127.0.0.1:<port>` from the port file.
2. Client sends `hello` with the token and its `PROTOCOL_VERSION`, within the handshake timeout.
   Missing the window is `handshake_timeout`.
3. Daemon validates the version first, then the token. An out-of-range version fails with
   `version_unsupported` and a message naming the versions on both sides; a bad token fails with
   `auth_failed`. Either way the daemon sends a typed `error` frame and closes with 1008.
4. Daemon replies `hello_ack` and the connection is live. From here the daemon also sends `ping`
   on a timer, to handshaken clients only.

`hello_ack` is a bare JSON object built by `build_hello_ack`, not a `DaemonEvent` — it precedes
the event stream rather than joining it.

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
lossless. `ping` is the odd one out: it is a daemon→client frame with no `seq` at all,
deliberately not a `DaemonEvent`, because a liveness frame that advanced the sequence would
corrupt replay.

### Unknown types

`parse_client_message` and `parse_daemon_event` check the incoming `type` against
`_KNOWN_CLIENT_TYPES` / `_KNOWN_EVENT_TYPES` before validating, so an unrecognised name gets
`unknown_message` with an actionable string rather than a pydantic dump. These frozensets are
hand-maintained alongside the unions, so a test derives both sets from the unions themselves and
fails on drift in either direction — a union member with no frozenset entry is rejected by the
parser meant to accept it, and a frozenset entry with no union member admits a type nothing can
validate.

### Client → daemon

Every message in `ClientMessageT`. "Session" says whether the message carries a `session_id`.

<!-- verify: client-messages -->

| Message | Session | Purpose |
|---|---|---|
| `hello` | — | Opening handshake: auth token plus protocol version. |
| `open_workspace` | — | Open a workspace directory as a new session. |
| `user_message` | yes | Queue a user turn for the session's loop. |
| `fork_from` | yes | Replace a past user turn and fork a sibling from there. Refused mid-turn. |
| `set_branch` | yes | Switch to another sibling at a forked user turn. |
| `approve` | yes | Approve a parked tool call. |
| `deny` | yes | Deny a parked tool call, with an optional reason. |
| `always_allow` | yes | Approve and save the narrowest policy rule that would have allowed it. Refused for class-C calls. |
| `list_policy_rules` | yes | List the workspace's saved policy rules. |
| `revoke_policy_rule` | yes | Remove one saved rule, identified by `(tool, args)`. |
| `set_skip_all_approvals` | — | Turn skip-all approvals on or off. Machine-wide; Class C and `never` are unaffected. Acked with `setup_state`. |
| `resume` | yes | Resume a session paused at a declared cap, after the cap was raised. |
| `cancel` | yes | Cancel a running session. |
| `attach` | yes | Subscribe to a session, replaying from `from_seq`. |
| `detach` | yes | Unsubscribe from a session; the session is unaffected. |
| `set_tier` | yes | Pin the active model tier for the session. |
| `get_instruction_stack` | yes | Ask for the resolved steering stack and its token counts. |
| `shutdown` | — | Ask the daemon to shut down cleanly. Sent by the supervising host. |
| `list_sessions` | — | Ask for the current session list. |
| `new_session` | yes | Create a fresh session in an existing session's workspace. |
| `archive_session` | yes | File a session away, or restore it. Never a kill. |
| `delete_session` | yes | Destroy a session and its event log. Refused mid-turn. |
| `move_session` | yes | Reassign a session to another workspace, keeping its id and log. |
| `get_setup_state` | — | Ask for the onboarding state: key presence, presets, active preset. |
| `set_api_key` | — | Store an API key in the OS keychain. |
| `validate_api_key` | — | Probe a key with one cheap live call. |
| `delete_api_key` | — | Remove an API key from the OS keychain. |
| `set_preset` | — | Choose the active model preset. |
| `set_tier_slug` | — | Set the model slug for one tier of one preset. |
| `run_diagnostics` | — | Run the doctor checks. |
| `get_usage` | — | Ask for token and cost rollups by session, day and week (TD-1706). |
| `export_usage` | — | Write a usage export; the daemon chooses the path and reports it back, so the verb cannot write anywhere the client names (TD-1706). |

### Daemon → client

Every member of `DaemonEventT`. "Seq" says where the sequence number comes from: `session` events
are stamped by a session's event log, `connection` events fix it at 1, and `ping` carries none.

<!-- verify: daemon-events -->

| Event | Seq | Purpose |
|---|---|---|
| `ready` | connection | Daemon and protocol versions. Declared and parseable, but not emitted in v0.1. |
| `session_state` | session | A session state transition, with an optional reason. |
| `conversation_reset` | session | The conversation forked or a sibling was selected. The viewer drops rows after that user turn and replaces it. |
| `assistant_delta` | session | A streamed chunk of assistant output. |
| `assistant_reasoning` | session | A streamed chunk of a reasoning model's thinking. Separate from `assistant_delta` because it is not part of the answer: the window folds it behind a disclosure, and it is never replayed to the provider as assistant speech. |
| `tool_call` | session | A tool call about to execute, with its decision class. |
| `tool_result` | session | The outcome of a tool call, with an error code and diff when applicable. |
| `shell_output` | session | A streamed chunk of a shell command's stdout or stderr. |
| `approval_request` | session | A tool call parked for approval, with the summary, the reason, and the rule "always allow" would write. |
| `decision_logged` | session | A decision appended to the autonomy ledger. |
| `checkpoint_notice` | session | A one-time notice that checkpointing is degraded. |
| `cost_update` | session | Accrued spend: this turn, this session, all time, by tier, and the classifier separately. |
| `boundary_update` | session | The resolved workspace boundary and caps, and where they came from. |
| `turn_complete` | session | A finished turn: tokens, cost, tier, duration, and any failure code. |
| `tier_state` | session | The active tier, any pinned override, and the configured slugs. |
| `context_compacted` | session | Older turns were compacted to fit the context window. Never silent. |
| `steering_reloaded` | session | Steering files were re-resolved after a detected change. |
| `rule_activated` | session | A path-scoped rule entered the prompt because a matching file was touched. |
| `tier_switched` | session | The active tier was overridden, naming the previous tier. |
| `instruction_stack` | session | The resolved steering stack: sources, tokens, imports, cache state. |
| `session_list` | connection | The current session list. |
| `policy_rules` | connection | The workspace's saved policy rules. |
| `setup_state` | connection | Onboarding state, and the ack for `set_api_key` / `set_preset` / `set_tier_slug` / `set_skip_all_approvals`. |
| `api_key_validated` | connection | The result of a key probe. Never carries the key. |
| `diagnostics_report` | connection | Doctor results: one row per check, with a fix when it failed. |
| `usage_report` | connection | The rollups `get_usage` asked for, bucketed and broken out by tier (TD-1706). |
| `usage_exported` | connection | Where `export_usage` wrote, and how many rows (TD-1706). |
| `ping` | — | Application-level liveness. Belongs to no session; advances nothing. |
| `error` | session | A typed error, usually in response to a bad message. |

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
| `create_registry` | `core/tstd/tools/registry.py` | Declares which tools exist and their schemas, approval class, parallel safety, and classifier metadata. Registration is explicit — there is no dynamic discovery in v0.1. |
| `register_builtin_handlers` | `core/tstd/tools/handlers.py` | Binds each registered tool name to the async callable that executes it. Registry and handler are separate on purpose: a tool with no handler is a configuration error the model is told about, not a crash. |
| `RULE_TABLE` | `core/tstd/autonomy/classifier.py` | The static decision rules, in priority order, first match wins. Anything the table cannot decide falls to the worker-tier classifier and defaults to class B — fail toward asking, never toward acting. |
| `Precedence` | `core/tstd/context/discover.py` | The steering hierarchy: `user global` < `workspace` < `rules` < `nested`. Adding a scope means adding a level here, and `docs/steering.md` documents each one. |
| `ToolDispatcher` | `core/tstd/tools/dispatch.py` | The single chokepoint every tool call passes through: schema validation, classifier, path guard, policy gate, handler, checkpoint, ledger. Prime directive §2.6 lives here, and reaching a handler unclassified raises rather than executing. |
| `Daemon` | `core/tstd/daemon.py` | `_start_session` is where a session is wired: router, boundary, policy, tool stack, and the provider factory. The provider is a closure, not an object, so a session can be opened and attached before any API key exists — the key is only needed when the loop makes its first model call. |

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
plus the validated arguments, all as keyword arguments, and return a string:

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
3. **Classify.** `RULE_TABLE` first. A read inside the workspace matches no static rule, so it
   falls through to the worker-tier classifier. A call reaching a handler without a classifier
   attached raises `UnclassifiedToolCall` — the chokepoint refuses to execute unclassified
   actions rather than defaulting to permissive.
4. **Guard.** Every `path_fields` argument is canonicalised and checked. Reads are checked
   against the workspace wall; writes additionally against `writable_paths`, the steering-file
   ban, and hardlinks.
5. **Gate.** Policy resolves the class to `auto`, `ask`, or `never`. Class A is auto, class B
   asks, class C takes the workspace's `class_c_default`. `ask` with no approval handler
   attached raises — the approval gate is part of the chokepoint, not an optional add-on.
6. **Execute.** Your handler runs. An exception becomes a `handler_error` result, not a dead turn.
7. **Record.** Touched paths go on the session, so path-scoped steering rules can activate.
   Mutations get a diff and a checkpoint commit. Class A and B decisions append to the ledger.

Steps 3 through 5 are why a new tool inherits the entire trust surface for free, and why
bypassing the dispatcher is never the right shortcut.

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
