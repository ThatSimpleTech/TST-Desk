# How models work

TST Desk ships with no model. You bring one: a hosted provider that speaks
the OpenAI chat API, or a model server running on your own computer. This
page walks through adding one, what each item in the model picker does, and
how to go further by hand.

## The model picker

The chip under the message box reads `<model> <effort>`, for example
`My model Medium`, followed by `· Fast` and `· Ultracode` when those are on.
Click it to open the picker:

| Item | What it does |
|---|---|
| A model (label, with a line under it) | Switches this chat to that model, and makes it the model new chats use, so the next new chat does not go back to the old one. The check mark is on the current one. The line under the name is the model's description. **Only this chat**, at the end of the row, switches the chat and leaves the model new chats use as it is. While a reply is being written the switch waits for it: the chip shows `→ <model> after this turn`, and the next message runs on the new model. A model marked **Grok**, or any model on the other engine than this chat's, reads **Opens a new chat**: a chat keeps the engine it started on, so the click starts a new chat on that model in the same folder. Other chats that are open are never changed. |
| **Effort** | How hard the model thinks before it answers: Low, Medium (recommended), High, Extra, Max. Higher effort is more thorough and slower. It is only sent to a model whose entry sets `reasoning_effort: true`. |
| **Fast** | On or off. Runs this model faster: a local model may borrow more hardware, an API model may use its provider's priority tier. Shown only for a model whose entry has a `fast` block ([configuration.md](configuration.md), "A preset"). A line under it says what it is doing. A restart turns it off. |
| **Ultracode** | On or off for this chat, with any effort. When it is on, the agent may split a big job into independent pieces, run several workers on them at once, have a separate reviewer check each result, and answer from the checked results. Small questions and one-line fixes are still answered directly. It makes more model calls, so it costs more and asks the worker model's server for several answers at once; the limits are `workflow` in [configuration.md](configuration.md). Workers use TST Desk's own file and shell tools, so the worker model must support tool calling: a hosted API model or a local server with tool calls works; a text-only route, or a gateway that runs its own tools instead of the ones TST Desk sends, does not. The chat keeps it, a restart included. |
| **More models** | Models you keep around but do not want on the main list (`picker: more`). Shown only when there is one. |
| **Teams** | Entries that give each role its own model (see [Teams](#teams)). Shown only when there is one. |
| **Add a model** | Opens the walkthrough below. |
| **How models work** | Opens this page. |

Before any model is set up, the chip itself reads **Add a model**.

With models of more than one kind, the main list is in sections: **Local**
(served on this computer), **API** (a provider's server) and **Grok**.
TST Desk reads the section off each entry; `group:` in the entry sets it by
hand, which is how a server elsewhere on your own network gets under
Local, or how you make a section of your own.

Give an entry `slot: 1` (up to 9) and ⌘⌥1 on macOS, or Ctrl+Alt+1
elsewhere, picks it from anywhere in the window, exactly as a click on its
row would. The digit shows beside the model in the picker.

<!-- verify: model -->
```yaml
presets:
  house:
    label: House server
    description: The GPU box on my network
    group: Local
    slot: 1
    brain: &house
      slug: house/model
      base_url: http://192.168.1.20:8000/v1
      credential: my-provider
      context_window: 65536
      max_output_tokens: 8192
      input_price: 0.0
      output_price: 0.0
      cache_read_price: 0.0
    worker: *house
    validator: *house
credentials:
  my-provider:
    name: My provider
```

Next to the chip, **Plan** turns on plan mode: the agent looks around
without changing anything, then shows you a plan. Nothing runs until you
approve it.

The account row at the bottom of the sidebar shows the active model's label
and whether its key is stored.

## Add a model

The walkthrough opens by itself on the first run. Open it again from the
picker (**Add a model**) or from Settings → Models.

1. **Server URL.** Paste the API URL of the server. It usually ends in
   `/v1`, for example `https://api.example.com/v1`, or
   `http://127.0.0.1:8000/v1` for a server on this computer. If the server
   runs on this computer, **Find models on this computer** looks for it
   instead: it asks Ollama, LM Studio, vLLM and llama.cpp on their usual
   ports and lists the ones that answer. Pick one to go straight to its
   models. The ports it asks are `local_servers` in `config.yaml`.
2. **API key.** Paste the key from your provider's account page. It is
   stored in your operating system's keychain, never in a file, and is only
   sent to the URL from step 1. A server on this computer needs no key, so
   this step is skipped for it.
3. **Pick a model.** TST Desk asks the server for its model list
   (`GET /v1/models`) and shows it. Type to filter a long list. Where the
   server reports them, the list shows each model's context size and price.
4. **Label and description.** The label is the name you will see in the
   picker. The description is one line about what it is for, such as
   "General coding and chat". Both can be changed later in `config.yaml`.
5. **Save.** The model is written to your `config.yaml` as one entry and
   becomes the model for new chats.
6. **Test.** TST Desk sends the model one short message and shows the
   reply. If it fails, the message says why; fix it and press **Try again**.

Then pick a workspace folder and start chatting.

## Where models live

Each model is an entry under `presets:` in your user `config.yaml`
(the [configuration reference](configuration.md) lists where it lives on each platform). An entry names its label,
description, where it shows in the picker, and the model used for each of
three roles. The walkthrough writes an entry like this one; it spells the
model out under each role, where the example below says it once with a YAML
anchor (`&my_model`) and reuses it (`*my_model`):

<!-- verify: model -->
```yaml
presets:
  my-model:
    label: My model
    description: General coding and chat
    brain: &my_model
      slug: provider/model-name
      base_url: https://api.example.com/v1
      credential: my-provider
      context_window: 128000
      max_output_tokens: 8192
      input_price: 0.0
      output_price: 0.0
      cache_read_price: 0.0
    worker: *my_model
    validator: *my_model
active_preset: my-model
credentials:
  my-provider:
    name: My provider
```

- `slug` is the model id exactly as the server lists it.
- Settings → Configuration shows a model written with an anchor like this one
  but leaves its tiers to the file: a change to one would change all three
  ([configuration.md](configuration.md), §2).
- `credential` names the key in the keychain. Settings → API keys lists
  your keys; a server on this computer needs none, so leave it out.
- `context_window` is how much conversation TST Desk keeps before it
  summarizes older turns. Keep it no larger than what the server really
  serves. A local server often serves far less than the model's maximum.
- Prices are US dollars per million tokens and only feed the cost display.
  Use `0.0` for a free or local model.
- `active_preset` is the entry new chats use. If it names an entry that no
  longer exists, the first entry is used instead.

An entry with no `label` is left out of the picker, unless a chat is
already using it. To move an entry under **More models**, add
`picker: more`.

## Keep more than one

Settings → Model lists every model you have added, each with three
buttons:

- **Duplicate** writes a copy with its own entry in `config.yaml`. Change
  the copy (its models, keys or context size under **Build a team**, or by
  hand) and the original stays as it was. A copy spells out all three roles
  in full, so it never shares a YAML anchor with the entry it came from.
- **Rename** changes the label you see in the picker. The entry's key in
  `config.yaml` stays the same, because chats and scheduled jobs remember a
  model by that key.
- **Remove** takes the entry out of `config.yaml`. It deletes no key, and a
  chat that is already using the model keeps it until you switch that chat.
  If it was the model new chats use, the first one left takes over. Remove
  is refused when another entry reuses part of this one (an anchor), or when
  `computer_use.local_worker_preset` names it; the message says which.

## Settings that travel with a model

Some settings suit a model's size and speed rather than your taste: how
compaction writes its summary, when old tool results are cleared, how many
workers a server can serve at once, the effort a chat starts on. Put them
under `overrides` in the model's entry and they apply to every chat on
that model, in place of the global values. Switching a chat to the model
switches them too.

<!-- verify: model -->
```yaml
presets:
  small-local:
    label: Small local
    description: A small model on this computer
    brain: &small
      base_url: http://127.0.0.1:8000/v1
      context_window: 32768
      max_output_tokens: 4096
      input_price: 0.0
      output_price: 0.0
      cache_read_price: 0.0
    worker: *small
    validator: *small
    overrides:
      session:
        default_effort: low
      compaction:
        summary: extractive
      clear_tool_results:
        keep_recent_tool_results: 2
      workflow:
        max_parallel: 1
```

Only the keys you write change; the rest keep their global values. The
sections and keys that may appear are listed under `overrides` in
[configuration.md](configuration.md#a-preset). Approvals, shell rules,
MCP servers, hooks and keys are not among them: choosing a model never
changes what the agent is allowed to do.

## A model on your own computer

Any server that speaks the OpenAI chat API works, such as llama.cpp's
server, vLLM, Ollama or LM Studio. Start it, then add it with the
walkthrough: **Find models on this computer** finds it on its usual port,
or paste its local URL. Addresses on this computer
(`127.0.0.1`, `localhost`) never get a key sent to them.

By hand, a local entry may leave `slug` out: TST Desk then asks the server
which model it is serving each time a chat starts.

<!-- verify: model -->
```yaml
presets:
  on-this-computer:
    label: On this computer
    description: A model served locally, nothing leaves the machine
    brain: &local_model
      base_url: http://127.0.0.1:8000/v1
      context_window: 32768
      max_output_tokens: 4096
      input_price: 0.0
      output_price: 0.0
      cache_read_price: 0.0
    worker: *local_model
    validator: *local_model
```

## Grok as one of your models

If you use the Grok CLI, it can sit on the picker beside your models. Its
entry needs only a label and the engine, because the Grok CLI brings its
own model and its own sign-in:

<!-- verify: model -->
```yaml
presets:
  grok:
    label: Grok
    description: The Grok CLI, with its own model
    engine: grok
```

Settings → Engine → **Add Grok to your models** writes this entry for you
once the Grok CLI is installed; by hand it is the four lines above.

A new chat started while this entry is the active one runs on the Grok
CLI. A chat keeps the engine it started on, so the picker holds this entry
for a chat that is already running on a model of yours, and holds your
models for a chat that is running on Grok: start a new chat to move
between them. Any other entry may name `engine: native` or `engine: grok`
too; one that names neither follows Settings → Engine, as before.

## Teams

Every turn has three roles:

- **Brain** plans the work and talks with you.
- **Worker** does the edits, searches and commands, and writes summaries.
- **Validator** checks the work before a turn ends.

A normal entry uses one model for all three. A **team** gives the roles
different models. It is worth it when a strong, costly model should plan
while a cheaper or local one does the bulk of the work. For most people,
one good model is simpler and works as well.

Mark a team with `picker: team` so it shows under **Teams** in the picker:

<!-- verify: model -->
```yaml
presets:
  planner-and-helper:
    label: Planner and helper
    description: A hosted model plans, a local one does the work
    picker: team
    brain:
      slug: provider/large-model
      base_url: https://api.example.com/v1
      credential: my-provider
      context_window: 128000
      max_output_tokens: 8192
      input_price: 3.0
      output_price: 15.0
      cache_read_price: 0.3
    worker: &helper
      base_url: http://127.0.0.1:8000/v1
      context_window: 32768
      max_output_tokens: 4096
      input_price: 0.0
      output_price: 0.0
      cache_read_price: 0.0
    validator: *helper
credentials:
  my-provider:
    name: My provider
```

Settings → Model → **Build a team** edits a model's roles: the model, key
and context size of each. **Editing**, above it, chooses which of your
models that is. It starts on the one new chats use, and choosing another
there switches no chat and changes no default.

## When something goes wrong

Each time the picker opens, TST Desk checks what it can without sending a
key or contacting a provider, and says so under a model's name: **No key
is stored for …**, **Nothing is answering at 127.0.0.1:…** for a model
served on this computer, or **The Grok CLI was not found**. It is a
warning, not a lock: the model can still be picked.

| You see | What to do |
|---|---|
| "No model is set up yet" | Add one: picker → **Add a model**, or Settings → Models. |
| "The server refused the key" | The key is wrong or expired. Make a new one on the provider's site and add it again. |
| "returned HTTP 404" when listing models | The URL is not the API URL. Most servers want the one that ends in `/v1`. |
| "No server answered" | Check the URL, and for a local server, that it is running. |
| "The server lists no models" | A local server may still be loading its model. Wait and try again. |
| The test message fails | The detail says why. A model that lists fine but fails a chat is often not a chat model; pick another. |

The full list of keys for an entry is in [configuration.md](configuration.md#a-preset).
