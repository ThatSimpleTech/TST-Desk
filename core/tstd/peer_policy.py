"""Who a connection is, and which verbs it may send (TD-5015).

Three kinds of peer matter once computers pair with each other:

- A **viewer** opted in with ``hello.features: ["machines"]`` and is
  either loopback (this computer's own window) or a phone-kind device.
  Its session lists carry every paired computer's rows, and it may act
  on them by their bridged ids (the hub forwards).
- A **machine peer** is another computer's hub: a device whose record
  says ``kind: machine``, or any connection whose hello carries
  ``"bridge"``. It is deny-by-default: only ``MACHINE_PEER_VERBS``, own
  sessions only, never a bridged id, never a broadcast. It is never a
  viewer, so bridging stays one hop (A<->B and self-pairing cannot loop).
- Everything else (the CLI, the legacy remote token, an old client) sees
  exactly what it saw before TD-5015: own rows, no ``machine_list``.

Pure: no I/O, no daemon imports. A test walks ``_KNOWN_CLIENT_TYPES`` so
a new verb starts refused for machine peers until it is classified here.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from .protocol import ClientMessage, build_error

DeviceKind = Literal["phone", "machine"]

VIEWER_FEATURE = "machines"
BRIDGE_FEATURE = "bridge"

# What another computer's hub may ask of this one. Each was checked for a
# way to turn approvals off (DECISIONS TD-5015): presets carry only tier
# configs, set_plan only locks the brain tier, always_allow needs a pending
# approval and writes the narrowest rule (never class C). The approval
# switches themselves (set_skip_all_approvals, set_grok_mode,
# approve_grok_plan, start_autonomy) are not here.
MACHINE_PEER_VERBS: frozenset[str] = frozenset(
    {
        "list_sessions",
        "attach",
        "detach",
        "user_message",
        # TD-5155: a held message is a user_message the person already
        # wrote; sending or dropping it is the same act.
        "send_queued",
        "remove_queued",
        "stop_turn",
        "cancel",
        "resume",
        "approve",
        "deny",
        "always_allow",
        "run_verify",
        "deny_verify",
        "new_session",
        "fork_from",
        "set_branch",
        # TD-5154: /compact, "Summarize from here" and the gauge's button.
        # Like fork_from it rewrites only that session's model context.
        "compact_context",
        "set_tier",
        "set_plan",
        "set_session_preset",
        "rename_session",
        "set_session_star",
        "archive_session",
        "delete_session",
        "end_session",
        "get_instruction_stack",
        "memory_accept",
        "memory_edit",
        "memory_reject",
        # TD-5143: bytes of a file the agent sent, by id only. It reads
        # nothing else and changes nothing, so a viewer of a bridged
        # session can download what that session's agent sent.
        "read_artifact_chunk",
        # Only for its own device id; the existing pairing gate checks that.
        "remove_remote_device",
    }
)

# What a hub forwards for a bridged id: the same list less the self-removal.
# ``list_sessions`` is answered from the hub's cache, never forwarded per
# call; it is listed so the UI's copy of this set matches the spec.
FORWARDED_VERBS: frozenset[str] = MACHINE_PEER_VERBS - {"remove_remote_device"}

# Approvals are never queued while a lane connects (A.8): a late approve
# could land after the person stopped expecting it.
APPROVAL_VERBS: frozenset[str] = frozenset({"approve", "deny", "always_allow"})

# The four machine verbs. ``list_machines`` is also open to viewers.
MACHINE_VERBS: frozenset[str] = frozenset(
    {"add_machine", "remove_machine", "list_machines", "reconnect_machine"}
)
VIEWER_MACHINE_VERBS: frozenset[str] = frozenset({"list_machines"})

# Verbs only this computer may send, from any peer kind. ``drain`` holds
# every local turn for a restart; a phone or another computer asking for
# that is not the person at the keyboard deploying (TD-5023).
# ``trust_workspace_config`` lets a repo's own settings loosen the wall
# (TD-5113); it is decided where the files can be read, at the computer.
# Accepting an agent-proposed steering edit writes the instructions every
# later turn follows (TD-5140), so it is decided there too; rejecting is
# open to a phone, and neither is a machine-peer verb.
LOOPBACK_VERBS: frozenset[str] = frozenset(
    {"drain", "trust_workspace_config", "steering_accept", "steering_edit"}
)


@dataclass(frozen=True)
class PeerView:
    """One connection as the policy sees it."""

    remote: bool
    machine: bool
    viewer: bool

    @property
    def loopback(self) -> bool:
        return not self.remote


LOOPBACK_PLAIN = PeerView(remote=False, machine=False, viewer=False)


def classify(remote: bool, device_kind: DeviceKind | None, features: Iterable[str]) -> PeerView:
    """The peer kind for one handshaken connection.

    *device_kind* is None for the loopback token and the legacy remote
    token: neither is a device. ``"bridge"`` only ever narrows.
    """
    wanted = set(features)
    machine = device_kind == "machine" or BRIDGE_FEATURE in wanted
    viewer = not machine and VIEWER_FEATURE in wanted and (not remote or device_kind == "phone")
    return PeerView(remote=remote, machine=machine, viewer=viewer)


def message_session_id(msg: ClientMessage) -> str | None:
    """The message's ``session_id`` when it has one."""
    value = getattr(msg, "session_id", None)
    return value if isinstance(value, str) else None


def check(msg: ClientMessage, peer: PeerView, *, bridged: bool) -> str | None:
    """A ready error frame when *peer* may not send *msg*, else None.

    *bridged* says the message's ``session_id`` is in the bridged grammar.
    Forwarding itself is the daemon's call; this only refuses.
    """
    sid = message_session_id(msg)
    if msg.type in LOOPBACK_VERBS and (peer.remote or peer.machine):
        return build_error(
            "loopback_only", "Only the computer itself can do that.", in_reply_to=msg.type
        )
    # TD-5118: a turn's own allow rules switch approvals off for it, so they
    # come only from ``tst run`` on this computer, for its own sessions; a
    # phone or another computer still approves call by call.
    if getattr(msg, "run_permissions", None) is not None and (
        peer.remote or peer.machine or bridged
    ):
        return build_error(
            "loopback_only",
            "Only tst run on this computer can set a turn's permissions, and only for "
            "this computer's own sessions.",
            session_id=sid,
            in_reply_to=msg.type,
        )
    if peer.machine:
        if msg.type not in MACHINE_PEER_VERBS:
            return build_error(
                "not_allowed",
                "Another computer can't do that here. Use this computer itself.",
                session_id=sid,
                in_reply_to=msg.type,
            )
        if bridged:
            return build_error(
                "bridged_session",
                "That session is on another computer. Open it from the computer it runs on.",
                session_id=sid,
                in_reply_to=msg.type,
            )
        return None
    if msg.type in MACHINE_VERBS:
        if peer.loopback or (peer.viewer and msg.type in VIEWER_MACHINE_VERBS):
            return None
        return build_error(
            "loopback_only",
            "Only the computer itself can add or remove computers.",
            in_reply_to=msg.type,
        )
    if bridged and peer.viewer and msg.type not in FORWARDED_VERBS:
        return build_error(
            "not_on_remote",
            "That doesn't work for a session on another computer yet.",
            session_id=sid,
            in_reply_to=msg.type,
        )
    return None
