"""Editor help for the feature sections of config.yaml (TD-5203).

The model plane (presets, tiers, keys) is in :mod:`tstd.config_help`, which
merges this table into its own. Values are a description, or a
(description, unit) pair when the docs give a unit.
"""

from __future__ import annotations

S = "seconds"
TOKENS = "tokens"
CHARS = "characters"
BYTES = "bytes"
PERCENT = "percent"

_TIMEOUT = ("How long one request may run.", S)
_ON_OFF_CHANNEL = "Send on this channel. Its destination URL is stored in the keychain."
_HOST_CHANNEL = "The only hostname a send may reach. Empty turns the channel off."

FEATURE_HELP: dict[str, str | tuple[str, str]] = {
    "SearchConfig.base_url": "The first search endpoint, sent the query as ?q=. Empty skips it.",
    "SearchConfig.fallback_base_urls": "Endpoints tried in order after base_url fails. Empty "
    "turns fallback off.",
    "SearchConfig.timeout_seconds": ("How long a search or fetch request may run.", S),
    "SearchConfig.max_results": "Hits returned when a search does not say how many (1 to 20).",
    "SearchConfig.fetch_max_bytes": ("Largest page body web_fetch reads.", BYTES),
    "SearchConfig.fetch_page_chars": ("Page text in one web_fetch window.", CHARS),
    "SearchConfig.fetch_cache_seconds": (
        "How long a fetched page stays in memory, never on disk. 0 turns the cache off.",
        S,
    ),
    "EmbeddingsConfig.base_url": "Embeddings endpoint, including /v1. Empty turns embeddings off.",
    "EmbeddingsConfig.model": "Embeddings model name. Empty turns embeddings off.",
    "EmbeddingsConfig.timeout_seconds": (
        "How long a request may run before heading-match ranking takes over.",
        S,
    ),
    "EmbeddingsConfig.top_k": "Most topic files kept after MEMORY.md.",
    "EmbeddingsConfig.token_budget": ("Cap on loaded memory.", TOKENS),
    "EmbeddingsConfig.command": "Command that starts the embeddings server. Empty only "
    "attaches to one already running.",
    "ProjectContextConfig.token_budget": (
        "Cap on pinned project files on the brain prompt; the last pin in goes first.",
        TOKENS,
    ),
    "SteeringConfig.claude_global_fallback": "Also read ~/.claude/CLAUDE.md and "
    "~/.claude/skills/ when ~/.tstdesk/AGENTS.md is absent.",
    "SessionConfig.log_max_events": (
        "Most events kept in a session's log; past it the oldest tenth goes.",
        "events",
    ),
    "SessionConfig.send_file_max_bytes": (
        "Largest file send_file may copy into the chat (at most 2 GiB).",
        BYTES,
    ),
    "SessionConfig.send_file_session_max_bytes": (
        "Total of sent files one session may hold.",
        BYTES,
    ),
    "SessionConfig.default_effort": "Reasoning effort a new session starts with until the "
    "picker sets one.",
    "SessionConfig.default_ultracode": "Whether a new session starts with Ultracode on.",
    "ClearToolResultsConfig.enabled": "Clear old tool results before compacting whole turns. "
    "Off goes back to compaction alone.",
    "ClearToolResultsConfig.keep_recent_tool_results": "Newest tool results that are never "
    "cleared.",
    "ClearToolResultsConfig.clear_at_least_tokens": (
        "Skip a clear that would free less than this.",
        TOKENS,
    ),
    "ClearToolResultsConfig.exclude_tools": "Tools whose results are never cleared.",
    "CompactionConfig.summary": "Who writes the summary: the worker model, or a short "
    "extractive summary with no model call.",
    "CompactionConfig.summary_prompt": "Replaces the built-in summary prompt. Empty uses the "
    "built-in one.",
    "CompactionConfig.summary_max_tokens": ("The worker's output cap for one summary.", TOKENS),
    "CompactionConfig.summary_timeout_seconds": (
        "How long compaction waits for the worker before it falls back.",
        S,
    ),
    "CompactionConfig.background_percent": (
        "From this share of the compaction budget the summary is written ahead. 0 is off.",
        PERCENT,
    ),
    "CompactionConfig.reinject_files": "Recent files read again after a compaction. 0 brings "
    "none back.",
    "CompactionConfig.reinject_tokens": ("Most those files may take.", TOKENS),
    "ConsultConfig.tiers": "Tiers offered the consult tool. None turns it off.",
    "ConsultConfig.max_per_turn": "Consults one turn may make, delegate children included.",
    "WorkflowConfig.max_parallel": "Workers running at once. Size it to what the worker's "
    "server can serve together.",
    "WorkflowConfig.max_tasks": "Most tasks one workflow call may hold.",
    "ModelRefreshConfig.auto": "Refresh in the background. Off leaves it to Settings → Model.",
    "ModelRefreshConfig.every_hours": ("Time between background passes after the first.", "hours"),
    "ShellConfig.read_only": "The read-only list.",
    "ShellReadOnlyConfig.enabled": "Off: every shell command asks again.",
    "ShellReadOnlyConfig.commands": "Built-in read-only entries in force. Config can only "
    "shrink the list.",
    "ProviderRetryConfig.max_retries": "Retries after the first attempt. 0 turns retrying off.",
    "ProviderRetryConfig.initial_delay": (
        "Wait before the first retry; each later wait doubles.",
        S,
    ),
    "ProviderRetryConfig.max_delay": ("Ceiling on any single wait.", S),
    "SchedulerConfig.max_run_seconds": (
        "How long one turn may run when its job sets no limit (at most an hour).",
        S,
    ),
    "WorktreeConfig.enabled": "Give each session in a git repository its own worktree and branch.",
    "WorktreeConfig.folder": "Where worktrees go: an absolute path, or one starting with ~. "
    "Empty is worktrees/ in the data folder.",
    "WorktreeConfig.setup_timeout_s": ("How long a workspace's worktree.setup may run.", S),
    "ComputerUseConfig.command": "Command for the computer-use sidecar. Empty is mock-only: "
    "no real pointer.",
    "ComputerUseConfig.browser": "Browser driver: mock never launches a browser.",
    "ComputerUseConfig.grounding": "Local vision model for click targeting.",
    "ComputerUseConfig.local_worker_preset": "Model whose worker tier serves a session after "
    "it uses a desktop or browser tool. Empty never switches.",
    "ComputerUseConfig.max_prompt_images": "How many images a request may keep; the oldest "
    "go first. 0 strips them all.",
    "GroundingConfig.base_url": "Loopback OpenAI-compatible endpoint, including /v1. Empty "
    "turns grounding off.",
    "GroundingConfig.slug": "Model id. Not set: read from /v1/models.",
    "GroundingConfig.timeout_seconds": (
        "How long a locate request may run before the intended point is used.",
        S,
    ),
    "JudgmentsConfig.verification": "Check that a desktop or browser action had its intended "
    "effect.",
    "JudgmentsConfig.semantic_breaker": "Stop an unattended run that keeps acting without "
    "progress.",
    "JudgmentsConfig.candidate_selection": "Add browser_pick: click a page element picked by "
    "description.",
    "JudgmentsConfig.confidence_threshold": "Below this confidence a judgment counts as "
    "unavailable (0 to 1).",
    "JudgmentsConfig.max_state_chars": ("Per-field cap on the state text a judgment sends.", CHARS),
    "JudgmentsConfig.backend": "Where judgments run: the worker tier, or the hosted TypeSafe API.",
    "JudgmentsConfig.typesafe_base_url": "The TypeSafe API URL. Empty falls back to the worker.",
    "JudgmentsConfig.typesafe_model": "The TypeSafe model alias.",
    "JudgmentsConfig.typesafe_credential": "The named key the TypeSafe backend sends.",
    "RemoteConfig.bind": "Tailscale interface or address for the phone listener, used when "
    "Allow remote attach turns on. Never 0.0.0.0.",
    "RemoteConfig.port": "Listener port while remote attach is on: 0, or 1024 to 65535. 0 "
    "reuses the last port. Applies at the next start.",
    "NotifyConfig.slack": "Slack incoming webhook.",
    "NotifyConfig.ntfy": "ntfy topic.",
    "NotifyConfig.discord": "Discord incoming webhook.",
    "NotifyConfig.telegram": "Telegram bot message.",
    "NotifyConfig.email": "Email for scheduled job reports.",
    "SlackNotifyConfig.enabled": _ON_OFF_CHANNEL,
    "SlackNotifyConfig.host": _HOST_CHANNEL,
    "SlackNotifyConfig.timeout_seconds": _TIMEOUT,
    "NtfyNotifyConfig.enabled": _ON_OFF_CHANNEL,
    "NtfyNotifyConfig.host": _HOST_CHANNEL,
    "NtfyNotifyConfig.timeout_seconds": _TIMEOUT,
    "DiscordNotifyConfig.enabled": _ON_OFF_CHANNEL,
    "DiscordNotifyConfig.host": _HOST_CHANNEL,
    "DiscordNotifyConfig.timeout_seconds": _TIMEOUT,
    "TelegramNotifyConfig.enabled": _ON_OFF_CHANNEL,
    "TelegramNotifyConfig.host": _HOST_CHANNEL,
    "TelegramNotifyConfig.timeout_seconds": _TIMEOUT,
    "EmailNotifyConfig.enabled": "Send a job's report by email.",
    "EmailNotifyConfig.method": "smtp, or send through Microsoft Graph or Gmail after signing "
    "in on the Email page.",
    "EmailNotifyConfig.host": "SMTP hostname, the only host a send may reach.",
    "EmailNotifyConfig.port": "SMTP port: 587 with starttls, 465 with tls.",
    "EmailNotifyConfig.security": "starttls upgrades the connection; tls is implicit TLS.",
    "EmailNotifyConfig.username": "SMTP sign-in name. The password is in the keychain.",
    "EmailNotifyConfig.from_address": "The From address.",
    "EmailNotifyConfig.timeout_seconds": ("How long one send may run.", S),
    "ConnectorsConfig.github": "GitHub App sign-in on this device.",
    "ConnectorsConfig.microsoft": "Microsoft sign-in for job email.",
    "ConnectorsConfig.google": "Google sign-in for job email.",
    "GithubConnectorConfig.client_id": "Public GitHub App client id. Empty turns the connector "
    "off.",
    "GithubConnectorConfig.api_host": "REST API hostname.",
    "GithubConnectorConfig.web_host": "Device sign-in hostname.",
    "GithubConnectorConfig.timeout_seconds": ("How long one GitHub call may run.", S),
    "MicrosoftConnectorConfig.client_id": "Public client id of the app registration. Empty "
    "turns it off.",
    "MicrosoftConnectorConfig.tenant": "common, organizations, consumers, a tenant id or a domain.",
    "MicrosoftConnectorConfig.login_host": "Sign-in hostname.",
    "MicrosoftConnectorConfig.graph_host": "Graph API hostname.",
    "MicrosoftConnectorConfig.timeout_seconds": ("How long one call may run.", S),
    "MicrosoftConnectorConfig.sign_in_timeout_seconds": (
        "How long sign-in waits for the browser to come back.",
        S,
    ),
    "GoogleConnectorConfig.client_id": "OAuth client id. Empty turns it off. The client "
    "secret is in the keychain.",
    "GoogleConnectorConfig.auth_host": "Sign-in hostname.",
    "GoogleConnectorConfig.token_host": "Token hostname.",
    "GoogleConnectorConfig.userinfo_host": "Account info hostname.",
    "GoogleConnectorConfig.gmail_host": "Gmail API hostname.",
    "GoogleConnectorConfig.timeout_seconds": ("How long one call may run.", S),
    "GoogleConnectorConfig.sign_in_timeout_seconds": (
        "How long sign-in waits for the browser to come back.",
        S,
    ),
    "SpeechConfig.enabled": "Show the hold-to-talk control.",
    "SpeechConfig.base_url": "OpenAI-compatible transcriptions root, including /v1. Empty "
    "turns it off.",
    "SpeechConfig.model": "The transcriptions model. Empty lets the server choose.",
    "SpeechConfig.timeout_seconds": ("How long a transcription may run.", S),
    "SpeechConfig.credential": "Named key sent as a bearer token. Empty sends none.",
    "AutonomyConfig.runtime": "Rootless container runtime for autonomous runs: a command on "
    "PATH or an absolute path.",
    "AutonomyConfig.image": "Container image those runs use. Pull it first: runs never pull.",
    "AutonomyConfig.check_every": "Autonomous turns between validator drift checks.",
    "AutonomyConfig.verify": "After a write turn: review it, ask first, or never.",
    "AutonomyConfig.verify_max_prompt_tokens": (
        "Cap on that review's prompt. Not set: 60% of the validator's window.",
        TOKENS,
    ),
    "EngineConfig.kind": "native is the built-in three-tier loop; grok runs the installed "
    "Grok Build CLI.",
    "EngineConfig.binary": "Path to the grok program. Empty: PATH, then ~/.grok/bin/grok.",
    "VoiceConfig.base_url": "OpenAI-compatible /v1 root for dictation. Empty uses the "
    "system's dictation.",
    "VoiceConfig.credential": "Named key to send; needed when base_url is off this computer.",
    "VoiceConfig.timeout_seconds": ("How long a transcription may run.", S),
    "McpConfig.servers": "Your MCP servers, by id.",
    "McpConfig.tool_search": "When MCP tool schemas stay out of requests until tool_search "
    "loads them.",
    "McpServerConfig.transport": "stdio runs command; http talks to url.",
    "McpServerConfig.command": "Program and arguments of a stdio server.",
    "McpServerConfig.url": "An http server's endpoint. Off this computer it needs "
    "allow_remote and https.",
    "McpServerConfig.enabled": "Off skips the server.",
    "McpServerConfig.timeout_seconds": ("Longest silence in a tool call. Not set: 30.", S),
    "McpServerConfig.startup_timeout_seconds": ("Time for each handshake step. Not set: 8.", S),
    "McpServerConfig.max_call_seconds": ("Hard cap on one tool call. Not set: 600.", S),
    "McpServerConfig.env": "Extra environment for a stdio server.",
    "McpServerConfig.headers": "Extra headers on every request to an http server.",
    "McpServerConfig.read_only_tools": "This server's tools that only read; plan mode lets "
    "only these run.",
    "McpServerConfig.allow_remote": "Let an http url be off this computer, over https only.",
    "McpToolSearchConfig.mode": "auto holds schemas back past threshold_percent of a tier's "
    "window; always holds them back; off sends them all.",
    "McpToolSearchConfig.threshold_percent": (
        "Share of a tier's context window MCP tool definitions may take under auto.",
        PERCENT,
    ),
    "HooksConfig.session_start": "Commands run when a session's loop starts.",
    "HooksConfig.user_prompt": "Commands run on each message you send, before the model sees "
    "it. One can block it.",
    "HooksConfig.before_tool": "Commands run before a tool call that would run or ask. One "
    "can block it.",
    "HooksConfig.after_tool": "Commands run after a tool call succeeds; what they print goes "
    "back with the result.",
    "HooksConfig.stop": "Commands run when the model ends a turn. One can send it back to work.",
    "HooksConfig.after_write": "Formatters or linters run on the files each write changed.",
    "HookEntry.command": "A shell command, run in the session's folder with the event as "
    "JSON on stdin.",
    "HookEntry.timeout_seconds": ("How long it may run.", S),
    "HookEntry.tools": "before_tool and after_tool only: tool-name globs it runs for. Empty "
    "is every tool.",
    "HookEntry.name": "A label for the inspector.",
    "AfterWriteHook.name": "Label in the timeline. Empty is the program's name.",
    "AfterWriteHook.command": "Program and arguments, run without a shell. An item {files} "
    "becomes the changed files.",
    "AfterWriteHook.match": "Which changed files it runs on: globs on the file name, or on "
    "the path when they contain /.",
    "AfterWriteHook.timeout_seconds": ("How long it may run.", S),
    "LspConfig.servers": "Language servers, first match wins. Empty starts none.",
    "LspConfig.idle_seconds": ("Shut down a server unused this long.", S),
    "LspConfig.max_diagnostics": "Most problems listed per file in one write's result.",
    "LspServerConfig.name": "Label in the note to the model. Empty is the program's name.",
    "LspServerConfig.command": "Program and arguments, speaking LSP over stdio, never a socket.",
    "LspServerConfig.match": "Which files it handles: globs on the file name, or on the path "
    "when they contain /.",
    "LspServerConfig.language_id": "LSP language id sent when a file opens, such as python.",
    "LspServerConfig.init_options": "Sent as the server's initializationOptions.",
    "LspServerConfig.start_timeout_seconds": ("How long the initialize handshake may take.", S),
    "LspServerConfig.request_timeout_seconds": ("How long one lookup may take.", S),
    "LspServerConfig.diagnostics_wait_seconds": (
        "After a write, how long to wait for diagnostics. 0 turns that off.",
        S,
    ),
}
