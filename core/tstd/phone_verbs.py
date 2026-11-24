"""Every verb, placed for a paired phone (TD-5279, TD-5284).

A phone acts as the person, call by call: it sends turns and answers
approvals. Some verbs reach further than the session it is looking at,
and those are not open to it by default. This module is the table.

Pure data and two small rules read off a message's own fields; the
checking is in ``peer_policy.check``.
"""

from __future__ import annotations

from .phone_permissions import (
    APPROVAL_SWITCHES,
    COMPUTER_USE,
    MCP_SERVERS,
    MODELS_AND_KEYS,
    UNATTENDED_RUNS,
)
from .protocol import ClientMessage

# What a phone may send only after the computer switched the capability on
# (TD-5279), verb to capability. The MCP page: a stdio server is a command
# this computer runs, now and at every session start, and it never meets
# the classifier. A paired phone could add one with no word from the person
# at the computer, and it stayed in config.yaml after the phone was
# removed. A server's values go with it: they are that command's
# environment, or the headers sent to its host. A phone still sees the list
# in ``setup_state`` either way.
PHONE_OPT_IN: dict[str, str] = {
    "set_mcp_server": MCP_SERVERS,
    "delete_mcp_server": MCP_SERVERS,
    "set_mcp_secret": MCP_SERVERS,
    "delete_mcp_secret": MCP_SERVERS,
    # Pasted JSON (TD-5253) is the same act as the Add form, several at once.
    "preview_mcp_import": MCP_SERVERS,
    "import_mcp_servers": MCP_SERVERS,
    # Skip-all (TD-5283) persists machine-wide and releases every parked
    # call, Class C included. A phone can already approve call by call;
    # this is the same power with nobody looking at each call.
    "set_skip_all_approvals": APPROVAL_SWITCHES,
    # TD-5296: a new model entry names a host the conversation goes to, and
    # a key or a key's host decides where that key is sent. Choosing among
    # the models already on the computer (the picker, TD-5242) stays open,
    # and so do a copy of one and a local Grok entry: neither adds a host.
    "add_model": MODELS_AND_KEYS,
    "set_api_key": MODELS_AND_KEYS,
    "set_credential": MODELS_AND_KEYS,
    "set_tier_credential": MODELS_AND_KEYS,
    # TD-5297: computer use moves this computer's pointer and types on its
    # keyboard. Turning it off and turning every indicator on only tighten;
    # engaging the stop stays open, and releasing it is never a phone's.
    "set_cu_policy": COMPUTER_USE,
    "set_cu_indicators": COMPUTER_USE,
    # TD-5298: a run nobody watches. An autonomy run works to its charter
    # with nobody there; Run now starts a job's turn at once; a saved job
    # runs later, in a session nobody opened. Pausing a job only tightens,
    # and deleting one stays open.
    "start_autonomy": UNATTENDED_RUNS,
    "run_job": UNATTENDED_RUNS,
    "save_job": UNATTENDED_RUNS,
}


# Everything a paired phone may send with no setting (TD-5284), written
# out. Until this list existed a phone could send any verb nobody had
# thought to list against it, so a new verb was open to phones by default.
# Now a verb in none of the sets is refused from a phone, exactly as it
# already was from another computer, and a test fails until it is placed:
#
# - ``LOOPBACK_VERBS``: the computer's own, no setting.
# - ``PHONE_OPT_IN``: refused unless the computer switched it on.
# - ``MACHINE_VERBS``: paired computers, with their own rule in ``check``.
# - here: open to a phone.
#
# This list is what a phone could already send the day it was written; it
# changed nothing by itself. Entries move to ``PHONE_OPT_IN`` one capability
# at a time: the model and key verbs (TD-5296), computer-use control
# (TD-5297) and starting an unattended run (TD-5298) have. Some acts are
# refused outright whatever a phone was granted (TD-5301, ``phone_never``):
# releasing the computer-use stop, ``shutdown``, sending a saved key to
# another server and changing the mail server without the password. Still
# here and waiting: the connector sign-ins. Some are refused or narrowed
# for a remote peer by their own handler (``start_pairing``,
# ``remove_remote_device``); being here only means the policy passes them on.
PHONE_OPEN: frozenset[str] = frozenset(
    {
        "add_grok_preset",
        "add_pin",
        "always_allow",
        "answer_question",
        "approve",
        "approve_grok_plan",
        "archive_session",
        "attach",
        "cancel",
        "cancel_pairing",
        "check_cu_permissions",
        "check_presets",
        "close_worktree",
        "compact_context",
        "create_rule",
        "delete_api_key",
        "delete_credential",
        "delete_job",
        "delete_job_template",
        "delete_session",
        "deny",
        "deny_verify",
        "design_hit_test",
        "detach",
        "dismiss_schedule_proposal",
        "duplicate_preset",
        "end_session",
        "export_usage",
        "extract_attachment",
        "fork_from",
        "get_charter",
        "get_hooks",
        "get_instruction_stack",
        "get_preset_roles",
        "get_session_job_source",
        "get_setup_state",
        "get_usage",
        "github_connect",
        "github_disconnect",
        "google_connect",
        "google_disconnect",
        "list_artifacts",
        "list_commands",
        "list_endpoint_models",
        "list_grok_extensions",
        "list_grok_sessions",
        "list_instructions",
        "list_job_runs",
        "list_job_templates",
        "list_jobs",
        "list_memory",
        "list_pins",
        "list_policy_rules",
        "list_sessions",
        "memory_accept",
        "memory_edit",
        "memory_reject",
        "microsoft_connect",
        "microsoft_disconnect",
        "move_session",
        "new_session",
        "open_artifact",
        "open_in_terminal",
        "open_workspace",
        "parse_job",
        "preview_restore_files",
        "read_artifact_chunk",
        "refresh_model_cards",
        "remove_pin",
        "remove_queued",
        "remove_remote_device",
        "rename_session",
        "reset_cu_permissions",
        "restore_files",
        "resume",
        "review_plan",
        "revoke_policy_rule",
        "rotate_remote_token",
        "run_diagnostics",
        "run_grok_command",
        "run_verify",
        "save_charter",
        "save_job_template",
        "save_memory",
        "search_sessions",
        "send_queued",
        "set_branch",
        "set_coworker",
        "set_cu_kill",
        "set_effort",
        "set_email_notify",
        "set_engine",
        "set_fast",
        "set_grok_mode",
        "set_judgments",
        "set_load_global_memory",
        "set_plan",
        "set_preset",
        "set_preset_label",
        "set_remote_attach",
        "set_session_preset",
        "set_session_star",
        "set_spend_cap",
        "set_tier",
        "set_tier_context_window",
        "set_tier_slug",
        "set_ultracode",
        "set_voice",
        "set_workspace_pin",
        "shutdown",
        "start_pairing",
        "steering_reject",
        "stop_turn",
        "submit_line_comments",
        "switch_preset",
        "test_email",
        "test_model",
        "transcribe",
        "user_message",
        "validate_api_key",
        "worker_stop",
    }
)

# The first frames of a connection. They are answered by the handshake and
# never reach ``check``; named so the inventory test can account for them.
HANDSHAKE_FRAMES: frozenset[str] = frozenset({"hello", "pair"})


def _only_tightens(msg: ClientMessage) -> bool:
    """A message that can only narrow what runs needs no permission.

    Read off the message's own fields, so the rule stays in this pure
    table. Turning skip-all *off* brings the approval prompts back;
    turning computer use off stops it; every indicator on shows more;
    pausing a job stops its runs.
    """
    if msg.type in ("set_skip_all_approvals", "set_cu_policy"):
        return getattr(msg, "enabled", True) is False
    if msg.type == "save_job":
        # Pausing an existing job. Other fields sent with the pause take
        # effect only when someone resumes it, which needs the permission.
        return getattr(msg, "id", None) is not None and getattr(msg, "paused", False) is True
    if msg.type == "set_cu_indicators":
        return all(
            getattr(msg, name, False) is True
            for name in ("glow", "agent_cursor", "show_on_real_display")
        )
    return False


def never_from_a_phone(msg: ClientMessage) -> bool:
    """What no grant opens to a phone, read off the message alone.

    Releasing the computer-use stop: whoever engaged it may have had a
    reason, and a phone that released it could set the pointer moving
    again with nobody at the computer. Engaging it stays open. Shutting the
    daemon down (TD-5301): nothing on a phone can start it again, so the
    computer would be out of reach until someone sat down at it. The
    rules that depend on what is saved are in ``phone_never``.
    """
    if msg.type == "shutdown":
        return True
    return msg.type == "set_cu_kill" and getattr(msg, "killed", True) is False


def required_capability(msg: ClientMessage) -> str | None:
    """The phone permission *msg* needs from a remote peer, if any."""
    capability = PHONE_OPT_IN.get(msg.type)
    if capability is None or _only_tightens(msg):
        return None
    return capability
