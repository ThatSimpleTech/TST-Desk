"""What Settings → Configuration says about each config.yaml key (TD-5203).

Keyed ``Model.field`` after the classes in :mod:`tstd.config` and the two
modules it imports, worded from ``docs/configuration.md`` §2. Kept here
rather than as ``Field(description=...)`` on every field: the config models
are edited by most stories, and a table keeps this text out of their way.
A test walks every field of every model and fails on one with no entry,
so a new key cannot ship without words for the editor.
"""

from __future__ import annotations

from pydantic import BaseModel

from .config_help_features import FEATURE_HELP
from .preset_overrides import (
    ClearToolResultsOverride,
    CompactionOverride,
    ComputerUseOverride,
    ConsultOverride,
    SessionOverride,
    WorkflowOverride,
)

S = "seconds"
TOKENS = "tokens"
USD = "USD per million tokens"

#: The editor's name for each top-level section; the key is shown beside it.
SECTION_TITLES: dict[str, str] = {
    "presets": "Models",
    "active_preset": "Active model",
    "credentials": "API key names",
    "search": "Web search",
    "local_servers": "Local model servers",
    "embeddings": "Embeddings",
    "project_context": "Project context",
    "steering": "Steering",
    "session": "Sessions",
    "clear_tool_results": "Clear stale tool results",
    "compaction": "Compaction",
    "consult": "Consult",
    "delegate": "Delegate",
    "workflow": "Workflow",
    "model_refresh": "Model refresh",
    "shell": "Shell",
    "provider_retry": "Provider retries",
    "scheduler": "Scheduler",
    "worktree": "Worktrees",
    "computer_use": "Computer use",
    "judgments": "Judgments",
    "remote": "Remote attach",
    "notify": "Notifications",
    "connectors": "Connectors",
    "speech": "Speech",
    "autonomy": "Autonomy",
    "engine": "Engine",
    "voice": "Voice",
    "mcp": "MCP",
    "hooks": "Hooks",
    "lsp": "Language servers",
}

_MODEL_HELP: dict[str, str | tuple[str, str]] = {
    "ModelConfig.presets": "The models you can switch between, by name. Each routes three "
    "tiers: brain, worker and validator.",
    "ModelConfig.active_preset": "Which model new sessions use. Empty or unknown falls back to "
    "the first one.",
    "ModelConfig.credentials": "Named API keys: the name Settings shows and the host each key "
    "talks to. The keys themselves live in the OS keychain.",
    "ModelConfig.delegate": "Limits for a child the agent delegates part of a task to.",
    "ModelConfig.search": "Where web_search sends queries, and how web_fetch reads pages.",
    "ModelConfig.local_servers": "Model servers on this computer that Add a model's search "
    "asks for their models. Loopback only; no key is sent.",
    "ModelConfig.embeddings": "Local embeddings server that ranks memory files. Empty base_url "
    "turns it off.",
    "ModelConfig.project_context": "Budget for pinned project files on the brain prompt.",
    "ModelConfig.steering": "Which steering files are read besides ~/.tstdesk/AGENTS.md.",
    "ModelConfig.session": "The session event log, sent-file caps, and what a new session "
    "starts with.",
    "ModelConfig.clear_tool_results": "Clear stale tool results before whole turns are compacted.",
    "ModelConfig.compaction": "How compaction writes its summary and which recent files come "
    "back after it.",
    "ModelConfig.consult": "Which tiers get the consult tool, which asks a stronger tier one "
    "question mid-task, and how often.",
    "ModelConfig.workflow": "Limits for the workflow tool, which runs workers side by side "
    "while Ultracode is on.",
    "ModelConfig.model_refresh": "Refresh every model's prices, windows and slug spelling in "
    "the background.",
    "ModelConfig.shell": "The read-only shell list: commands that run without asking.",
    "ModelConfig.provider_retry": "How long to keep retrying a provider failure that can be "
    "retried (429, 5xx, a dropped connection).",
    "ModelConfig.scheduler": "How long one scheduled turn may run, and the base wait for tst run.",
    "ModelConfig.worktree": "A git worktree per session, so the agent's changes stay off your "
    "checkout until you merge them.",
    "ModelConfig.computer_use": "The desktop computer-use sidecar, the browser driver and "
    "click grounding.",
    "ModelConfig.judgments": "Small typed judgments on the worker model (dev build). All off "
    "by default.",
    "ModelConfig.remote": "The opt-in Tailscale listener for phones. The switch itself is "
    "Allow remote attach, in Phones and devices.",
    "ModelConfig.notify": "Outbound notification channels. Webhook and topic URLs are "
    "keychain secrets, never this file.",
    "ModelConfig.connectors": "Account connectors. Each is off until its client id is set; "
    "tokens live in the keychain.",
    "ModelConfig.speech": "Hold-to-talk transcription through an OpenAI-compatible server.",
    "ModelConfig.autonomy": "The container for autonomous runs, and the review after writes "
    "in interactive sessions.",
    "ModelConfig.engine": "Which agent loop new sessions use.",
    "ModelConfig.voice": "An optional speech-to-text endpoint for hold-to-talk dictation.",
    "ModelConfig.mcp": "MCP servers you list, and when their tool schemas wait behind tool_search.",
    "ModelConfig.hooks": "Commands that run at points in a session, and after the agent "
    "writes a file.",
    "ModelConfig.lsp": "Language servers: errors after a write, and definition, reference "
    "and symbol lookups.",
    "Preset.brain": "Plans and talks to you. A session opens on it for its first turns.",
    "Preset.worker": "Runs the steps: edits, the tool-call classifier, compaction and summaries.",
    "Preset.validator": "Reviews diffs and tests, including the review after writes.",
    "Preset.engine": "Which agent loop a chat on this one uses: TST Desk's own, or the Grok "
    "CLI. Empty uses the Engine setting. With grok, the three roles may be left out.",
    "Preset.label": "The name on the model picker. A model with no label is left off the "
    "picker unless it is in use.",
    "Preset.description": "One line under the label on the picker: what this model is for.",
    "Preset.picker": "Where the picker lists it: the main list, More models, or Teams.",
    "Preset.group": "The picker's section for this one. Empty lets TST Desk choose: Grok, "
    "Local when every role is served on this computer, else API.",
    "Preset.slot": "A quick-switch slot, 1 to 9: ⌘⌥ and the digit (Ctrl+Alt elsewhere) picks "
    "this one. Empty is no shortcut. No two share a slot.",
    "Preset.fast": "The model's Fast mode. With this set, the picker shows a Fast switch.",
    "Preset.overrides": "Context settings for chats on this model, in place of the global "
    "ones of the same names. Nothing here changes approvals, shell rules or keys.",
    "PresetOverrides.session": "What a chat on this model starts with.",
    "PresetOverrides.clear_tool_results": "Clear stale tool results, for this model.",
    "PresetOverrides.compaction": "Compaction, for this model.",
    "PresetOverrides.consult": "The consult tool, for this model.",
    "PresetOverrides.workflow": "Workflow limits, for this model's server.",
    "PresetOverrides.computer_use": "The image cap, for this model.",
    "TierConfig.slug": "The model id sent as model. Optional on a loopback endpoint, where "
    "it is read from /v1/models.",
    "TierConfig.base_url": "The OpenAI-compatible endpoint. A bound key's own host wins over this.",
    "TierConfig.input_price": ("Dollars per million prompt tokens not served from cache.", USD),
    "TierConfig.output_price": ("Dollars per million completion tokens.", USD),
    "TierConfig.cache_read_price": ("Dollars per million prompt tokens served from cache.", USD),
    "TierConfig.context_window": (
        "The compaction budget. Keep it no larger than the server actually serves.",
        TOKENS,
    ),
    "TierConfig.max_output_tokens": (
        "Sent as max_tokens, and kept free for the answer out of context_window.",
        TOKENS,
    ),
    "TierConfig.vision": "Accept image attachments on turns routed here, and let fs_read "
    "return images.",
    "TierConfig.reasoning_effort": "Send the picker's effort level as reasoning_effort. Only "
    "for a server that supports the field.",
    "TierConfig.credential": "Named key to send. Not set: a loopback endpoint sends none, an "
    "off-box one sends openrouter.",
    "TierConfig.extra_body": "Extra JSON fields added to every request this tier makes. "
    "Never put a secret here.",
    "TierConfig.verdict_extra_body": "Merged over extra_body on the worker's one-word verdict "
    "calls only.",
    "TierConfig.deferred_tools": "Builtin tools this tier sees by name only, behind "
    "tool_search (fnmatch patterns such as github_*).",
    "TierConfig.echo_reasoning": "Send the model's plaintext thinking back as "
    "reasoning_content on the tool-call steps it led to.",
    "TierConfig.reply_guard": "Cut a streamed reply that turned to junk, looped or leaked "
    "markup, and ask again. Off when not set.",
    "TierConfig.connect_timeout": ("Time to open the connection.", S),
    "TierConfig.read_timeout": (
        "Longest wait for any bytes from the server, including a slow prompt read.",
        S,
    ),
    "TierConfig.total_timeout": ("Cap on one attempt, streaming included. Not set: no cap.", S),
    "TierConfig.stream_idle_timeout": (
        "Stop a stream that sends no usable chunk for this long. Not set: off.",
        S,
    ),
    "TierConfig.fallbacks": "Backup endpoints tried in order when this one is down. Each is "
    "a whole tier with its own slug, key and prices.",
    "ReplyGuardConfig.junk": "Cut a reply whose recent output is mostly a script nobody in "
    "the conversation writes.",
    "ReplyGuardConfig.junk_ratio": "Share of recent output in such a script that counts as "
    "junk (0 to 1).",
    "ReplyGuardConfig.repetition": "Cut one pattern repeated back to back far past any real text.",
    "ReplyGuardConfig.leaked_markup": "Cut tool-call markup that shows up in the visible "
    "text of a call that offered tools.",
    "ReplyGuardConfig.max_reasoning_chars": (
        "End the call after this much thinking. 0 is no cap.",
        "characters",
    ),
    "ReplyGuardConfig.retries": "Withdraw-and-ask-again rounds per model call.",
    "ReplyGuardConfig.retry_extra_body": "Merged over extra_body on a retry after a thinking "
    "cap, for example to turn thinking off.",
    "FastConfig.hook_url": "A loopback switch the daemon calls to turn Fast on or off for a "
    "local model.",
    "FastConfig.hook_model": "The model name sent to hook_url.",
    "FastConfig.extra_body": "Merged into every request while Fast is on, for example a "
    "provider's priority tier.",
    "FastConfig.note": "One line the Fast switch shows, such as how long switching takes.",
    "CredentialConfig.name": "The name Settings shows for this key.",
    "CredentialConfig.base_url": "The host this key talks to. Empty keeps the tier's URL.",
    "LocalServerConfig.name": "The server's name in the list.",
    "LocalServerConfig.base_url": "Its OpenAI-compatible root on this computer (loopback only).",
}

#: Every field's (description, unit). Unit is "" when the docs give none.
FIELD_HELP: dict[str, tuple[str, str]] = {
    key: value if isinstance(value, tuple) else (value, "")
    for key, value in (_MODEL_HELP | FEATURE_HELP).items()
}

#: A model's override of a global key (TD-5234) says what that key says.
OVERRIDE_OF: dict[str, tuple[type[BaseModel], str]] = {
    "SessionOverride": (SessionOverride, "SessionConfig"),
    "ClearToolResultsOverride": (ClearToolResultsOverride, "ClearToolResultsConfig"),
    "CompactionOverride": (CompactionOverride, "CompactionConfig"),
    "ConsultOverride": (ConsultOverride, "ConsultConfig"),
    "WorkflowOverride": (WorkflowOverride, "WorkflowConfig"),
    "ComputerUseOverride": (ComputerUseOverride, "ComputerUseConfig"),
}
for _name, (_model, _base) in OVERRIDE_OF.items():
    for _field in _model.model_fields:
        _text, _unit = FIELD_HELP[f"{_base}.{_field}"]
        FIELD_HELP[f"{_name}.{_field}"] = (f"{_text} Empty keeps the global value.", _unit)

#: Values the generic editor never shows or writes: names only, with where
#: each value lives. Their own page stores a value in the keychain (TD-5110).
SECRET_FIELDS: dict[str, str] = {
    "McpServerConfig.env": "Values are set in Settings → MCP servers, which keeps tokens in "
    "the keychain.",
    "McpServerConfig.headers": "Values are set in Settings → MCP servers, which keeps tokens "
    "in the keychain.",
}

#: Shown here, edited only on their own page.
READ_ONLY_FIELDS: dict[str, str] = {
    "ModelConfig.credentials": "Names and hosts are edited in Settings → API keys, which "
    "keeps each key in the OS keychain.",
}

#: A string that names an entry of another map: the editor offers those keys.
CHOICES_FROM: dict[str, tuple[str, ...]] = {
    "ModelConfig.active_preset": ("presets",),
    "ComputerUseConfig.local_worker_preset": ("presets",),
    "TierConfig.credential": ("credentials",),
    "SpeechConfig.credential": ("credentials",),
    "VoiceConfig.credential": ("credentials",),
    "JudgmentsConfig.typesafe_credential": ("credentials",),
}

#: Maps and lists whose entries are added on their own page, with the reason.
NOT_ADDABLE: dict[str, str] = {
    "ModelConfig.presets": "Add a model in Settings → Model.",
    "McpConfig.servers": "Add servers in Settings → MCP servers.",
}

#: Entries removed only on their own page, with the reason.
NOT_REMOVABLE: dict[str, str] = {
    "McpConfig.servers": "Remove servers in Settings → MCP servers, which also deletes "
    "their keychain values.",
}

#: Keys with no Reset. The picker sets the active model. A model's three
#: roles are all there or all absent (TD-5239), so one is never reset alone.
NOT_RESETTABLE: frozenset[str] = frozenset(
    {"ModelConfig.active_preset", "Preset.brain", "Preset.worker", "Preset.validator"}
)

#: Long text gets a multi-line box.
MULTILINE_FIELDS: frozenset[str] = frozenset(
    {"CompactionConfig.summary_prompt", "CompactionOverride.summary_prompt", "HookEntry.command"}
)

#: The fields a new entry's form asks for, when not just its required ones.
ADD_FIELDS: dict[str, tuple[str, ...]] = {
    "TierConfig": (
        "slug",
        "base_url",
        "credential",
        "input_price",
        "output_price",
        "cache_read_price",
        "context_window",
        "max_output_tokens",
    ),
    # Fast needs hook_url or extra_body; a provider's priority tier is extra_body alone.
    "FastConfig": ("hook_url", "hook_model", "extra_body", "note"),
    "AfterWriteHook": ("name", "command", "match"),
    "HookEntry": ("command", "name"),
    "LspServerConfig": ("name", "command", "match", "language_id"),
}
