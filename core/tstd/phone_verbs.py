"""Every verb, placed for a paired phone (TD-5279, TD-5284).

A phone acts as the person, call by call: it sends turns and answers
approvals. Some verbs reach further than the session it is looking at,
and those are not open to it by default. This module is the table.

Pure data and two small rules read off a message's own fields; the
checking is in ``peer_policy.check``.
"""

from __future__ import annotations

from .phone_permissions import APPROVAL_SWITCHES, MCP_SERVERS
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
    # Skip-all (TD-5283) persists machine-wide and releases every parked
    # call, Class C included. A phone can already approve call by call;
    # this is the same power with nobody looking at each call.
    "set_skip_all_approvals": APPROVAL_SWITCHES,
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
# changes nothing by itself. Several entries are waiting to move to
# ``PHONE_OPT_IN``, one capability at a time (the model and key verbs,
# computer-use control, starting an unattended run, the connector
# sign-ins, ``shutdown``, releasing the computer-use stop, changing a saved
# key's host and changing the mail server). Some are refused or narrowed
# for a remote peer by their own handler (``start_pairing``,
# ``remove_remote_device``); being here only means the policy passes them on.
PHONE_OPEN: frozenset[str] = frozenset(
    {
        "add_grok_preset",
        "add_model",
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
        "run_job",
        "run_verify",
        "save_charter",
        "save_job",
        "save_job_template",
        "save_memory",
        "search_sessions",
        "send_queued",
        "set_api_key",
        "set_branch",
        "set_coworker",
        "set_credential",
        "set_cu_indicators",
        "set_cu_kill",
        "set_cu_policy",
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
        "set_tier_credential",
        "set_tier_slug",
        "set_ultracode",
        "set_voice",
        "set_workspace_pin",
        "shutdown",
        "start_autonomy",
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
    }
)

# The first frames of a connection. They are answered by the handshake and
# never reach ``check``; named so the inventory test can account for them.
HANDSHAKE_FRAMES: frozenset[str] = frozenset({"hello", "pair"})


def _only_tightens(msg: ClientMessage) -> bool:
    """A message that can only narrow what runs needs no permission.

    Read off the message's own fields, so the rule stays in this pure
    table. Turning skip-all *off* brings the approval prompts back.
    """
    return msg.type == "set_skip_all_approvals" and getattr(msg, "enabled", True) is False


def required_capability(msg: ClientMessage) -> str | None:
    """The phone permission *msg* needs from a remote peer, if any."""
    capability = PHONE_OPT_IN.get(msg.type)
    if capability is None or _only_tightens(msg):
        return None
    return capability
