# How models work

TST Desk ships with no model. You bring one: a hosted provider that speaks
the OpenAI chat API, or a model server running on your own computer. This
page walks through adding one, what each item in the model picker does, and
how to go further by hand.

## The model picker

The chip under the message box reads `<model> <effort>`, for example
`My model Medium`. Click it to open the picker:

| Item | What it does |
|---|---|
| A model (label, with a line under it) | Switches this chat to that model. The check mark is on the current one. The line under the name is the model's description. |
| **Effort** | How hard the model thinks before it answers: Low, Medium (recommended), High, Extra, Max, Ultracode. Higher effort is more thorough and slower. It is only sent to a model whose entry sets `reasoning_effort: true`. |
| **More models** | Models you keep around but do not want on the main list (`picker: more`). Shown only when there is one. |
| **Teams** | Entries that give each role its own model (see [Teams](#teams)). Shown only when there is one. |
| **Add a model** | Opens the walkthrough below. |
| **How models work** | Opens this page. |

Before any model is set up, the chip itself reads **Add a model**.

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
   `http://127.0.0.1:8000/v1` for a server on this computer.
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

## A model on your own computer

Any server that speaks the OpenAI chat API works, such as llama.cpp's
server, vLLM, Ollama or LM Studio. Start it, then add it with the
walkthrough using its local URL. Addresses on this computer
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

Settings → Models → **Build a team** edits the active entry's model and
key per role.

## When something goes wrong

| You see | What to do |
|---|---|
| "No model is set up yet" | Add one: picker → **Add a model**, or Settings → Models. |
| "The server refused the key" | The key is wrong or expired. Make a new one on the provider's site and add it again. |
| "returned HTTP 404" when listing models | The URL is not the API URL. Most servers want the one that ends in `/v1`. |
| "No server answered" | Check the URL, and for a local server, that it is running. |
| "The server lists no models" | A local server may still be loading its model. Wait and try again. |
| The test message fails | The detail says why. A model that lists fine but fails a chat is often not a chat model; pick another. |

The full list of keys for an entry is in [configuration.md](configuration.md#a-preset).
