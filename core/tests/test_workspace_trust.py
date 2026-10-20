"""Trust prompt for repo-supplied workspace config (TD-5113).

A cloned repo's ``.tst/config.yaml`` used to apply on open, so it could
grant itself shell ``auto`` rules, a network allowlist or raised caps.
These tests hold the three acceptance criteria:

1. A config that loosens anything is shown (``boundary_update.config_trust``
   lists every loosened setting) and does not apply until trusted.
2. The decision is stored per workspace, keyed to the file's hash; a
   changed file asks again.
3. Until it is trusted the workspace runs on the packaged defaults.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.machine_helpers import eventually
from tests.test_artifacts import _open, _shutdown
from tests.test_tst_approvals import _SESSION, _TURN_DONE, _Ws
from tests.test_two_daemons import _Steps
from tstd import cli, cli_turn
from tstd.autonomy.classifier import DecisionClass
from tstd.boundary_config import BoundaryConfig
from tstd.config import ConfigError
from tstd.daemon import Daemon
from tstd.loop import _load_import_approvals
from tstd.mock import Script
from tstd.peer_policy import LOOPBACK_PLAIN, PeerView, check
from tstd.policy import PolicyConfig, policy_from_mapping, resolve
from tstd.protocol import (
    ApprovalRequest,
    BoundaryUpdate,
    ToolResult,
    TrustWorkspaceConfig,
)
from tstd.session import Session
from tstd.tools.registry import create_registry
from tstd.workspace_trust import (
    TRUST_FILE,
    TrustStore,
    UntrustedConfigError,
    find_loosened,
    read_view,
)

SHELL_AUTO = """\
policy:
  rules:
    - tool: shell
      args: "touch *"
      effect: auto
"""

LOOSE = """\
policy:
  rules:
    - tool: shell
      args: "touch *"
      effect: auto
boundary:
  network:
    - example.com
caps:
  spend_usd: 500
"""

TIGHT = """\
policy:
  class_c_default: never
  rules:
    - tool: fs_write
      effect: ask
boundary:
  allowed_commands: [echo]
  writable_paths: ["src/**"]
caps:
  spend_usd: 5
"""


def _write(ws: Path, text: str) -> str:
    (ws / ".tst").mkdir(parents=True, exist_ok=True)
    raw = text.encode("utf-8")
    (ws / ".tst" / "config.yaml").write_bytes(raw)
    return hashlib.sha256(raw).hexdigest()


def _keys(data: dict[str, Any]) -> list[str]:
    return [item.key for item in find_loosened(Path("config.yaml"), data)]


# ── what loosens ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        ({"policy": {"rules": [{"tool": "shell", "effect": "auto"}]}}, ["policy.rules"]),
        ({"policy": {"rules": [{"tool": "shell", "effect": "never"}]}}, []),
        ({"policy": {"rules": [{"tool": "fs_*", "effect": "ask"}]}}, []),
        ({"policy": {"class_c_default": "never", "approval_timeout_seconds": 5}}, []),
        ({"boundary": {"network": ["api.example.com"]}}, ["boundary.network"]),
        ({"boundary": {"network": "deny"}}, []),
        ({"boundary": {"network": []}}, []),
        ({"boundary": {"allowed_commands": ["npm"]}}, []),
        ({"boundary": {"writable_paths": ["src/**", "docs/*.md"]}}, []),
        ({"boundary": {"writable_paths": ["/etc/**"]}}, ["boundary.writable_paths"]),
        ({"boundary": {"writable_paths": ["../sibling/**"]}}, ["boundary.writable_paths"]),
        ({"boundary": {"writable_paths": ["~/.ssh/*"]}}, ["boundary.writable_paths"]),
        ({"boundary": {"writable_paths": ["C:/Users/**"]}}, ["boundary.writable_paths"]),
        ({"caps": {"spend_usd": 26}}, ["caps.spend_usd"]),
        ({"caps": {"spend_usd": 5, "wall_clock_hours": 1, "max_iterations": 10}}, []),
        ({"caps": {"wall_clock_hours": 9}}, ["caps.wall_clock_hours"]),
        ({"caps": {"max_iterations": 201}}, ["caps.max_iterations"]),
        ({"attachments": {"max_count": 50}}, ["attachments.max_count"]),
        ({"memory": {"max_lines": 5000}}, ["memory.max_lines"]),
        ({"approved_external_imports": ["/home/x/notes.md"]}, ["approved_external_imports"]),
        ({"mcp_servers": {"evil": {"command": "curl"}}}, ["mcp_servers"]),
        ({"hooks": {"before_tool": "sh x.sh"}}, ["hooks"]),
        # A section the loader refuses applies nothing, so it loosens nothing.
        ({"boundary": {"network": ["x.com"], "writable_path": ["**"]}}, []),
        ({"policy": {"rules": [{"tool": "shell", "effect": "sometimes"}]}}, []),
    ],
)
def test_only_settings_looser_than_the_packaged_defaults_count(
    data: dict[str, Any], expected: list[str]
) -> None:
    assert _keys(data) == expected


def test_each_loosened_setting_names_its_value_and_default() -> None:
    items = find_loosened(Path("c.yaml"), yaml.safe_load(LOOSE))
    shown = {(i.key, i.value, i.default) for i in items}
    assert ("policy.rules", "shell: touch * → auto", "ask (class B and C)") in shown
    assert ("boundary.network", "example.com", "deny") in shown
    assert ("caps.spend_usd", "500", "25") in shown


# ── the stored decision ─────────────────────────────────────────────────


def test_no_file_comment_only_and_tightening_only_apply_without_asking(tmp_path: Path) -> None:
    store = TrustStore(tmp_path / "data")
    ws = tmp_path / "ws"
    ws.mkdir()
    assert read_view(ws, store).state == "none"
    _write(ws, "# boundary:\n#   network: deny\n")
    assert read_view(ws, store).state == "none"
    _write(ws, TIGHT)
    view = read_view(ws, store)
    assert (view.state, view.applies) == ("none", True)
    assert view.effective is not None
    assert view.effective["caps"]["spend_usd"] == 5


def test_a_loosening_config_is_pending_until_decided_and_keyed_to_its_hash(
    tmp_path: Path,
) -> None:
    store = TrustStore(tmp_path / "data")
    ws = tmp_path / "ws"
    sha = _write(ws, LOOSE)

    view = read_view(ws, store)
    assert (view.state, view.sha256, view.applies, view.effective) == ("pending", sha, False, None)

    store.record(ws, sha, "trusted")
    assert read_view(ws, store).state == "trusted"
    # A fresh store over the same data dir: the decision is durable.
    assert read_view(ws, TrustStore(tmp_path / "data")).state == "trusted"
    assert (tmp_path / "data" / TRUST_FILE).is_file()
    assert not (ws / ".tst" / TRUST_FILE).exists()

    # One byte changed: asks again.
    _write(ws, LOOSE + "# edited\n")
    assert read_view(ws, store).state == "pending"

    sha2 = _write(ws, LOOSE.replace("500", "600"))
    store.record(ws, sha2, "declined")
    view = read_view(ws, store)
    assert (view.state, view.applies, view.effective) == ("declined", False, None)


def test_a_garbled_trust_file_fails_closed(tmp_path: Path) -> None:
    data = tmp_path / "data"
    store = TrustStore(data)
    ws = tmp_path / "ws"
    sha = _write(ws, LOOSE)
    store.record(ws, sha, "trusted")
    (data / TRUST_FILE).write_text(": not yaml : [", encoding="utf-8")
    assert read_view(ws, store).state == "pending"


def test_tst_desk_writes_keep_their_trust_and_refuse_an_untrusted_file(
    tmp_path: Path,
) -> None:
    store = TrustStore(tmp_path / "data")
    ws = tmp_path / "ws"
    ws.mkdir()
    config = ws / ".tst" / "config.yaml"

    # No config yet: the user's own loosening write is trusted as written.
    store.write_through(ws, lambda: _write(ws, SHELL_AUTO), owns=("policy.rules",))
    assert read_view(ws, store).state == "trusted"

    # A repo change on top is not.
    _write(ws, LOOSE)
    before = config.read_bytes()
    with pytest.raises(UntrustedConfigError):
        store.write_through(ws, lambda: _write(ws, SHELL_AUTO), owns=("policy.rules",))
    assert config.read_bytes() == before


def test_untrusted_import_approvals_approve_nothing(tmp_path: Path) -> None:
    outside = tmp_path / "outside.md"
    outside.write_text("secret\n", encoding="utf-8")
    ws = tmp_path / "ws"
    sha = _write(ws, f"approved_external_imports:\n  - {outside}\n")
    session = Session(str(ws))
    session.trust_store = TrustStore(tmp_path / "data")
    assert _load_import_approvals(session) == set()
    session.trust_store.record(ws, sha, "trusted")
    assert _load_import_approvals(session) == {outside.resolve()}


def test_only_this_computer_may_decide() -> None:
    msg = TrustWorkspaceConfig(session_id="s1", sha256="0" * 64, trust=True)
    assert check(msg, LOOPBACK_PLAIN, bridged=False) is None
    phone = PeerView(remote=True, machine=False, viewer=True)
    refusal = check(msg, phone, bridged=False)
    assert refusal is not None
    assert json.loads(refusal)["code"] == "loopback_only"


# ── through the daemon ──────────────────────────────────────────────────


def _walls(daemon: Daemon, sid: str) -> list[BoundaryUpdate]:
    session = daemon.session_registry.get(sid)
    assert session is not None
    return [e for e in session.event_log.all_events if isinstance(e, BoundaryUpdate)]


async def _send(daemon: Daemon, msg: dict[str, Any]) -> dict[str, Any] | None:
    reply = await daemon._handle_message(json.dumps(msg), None)
    return None if reply is None else json.loads(reply)


async def test_open_shows_the_loosened_rules_and_runs_on_defaults(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    sha = _write(ws, LOOSE)
    daemon = Daemon(data_dir=tmp_path / "data")
    sid = await _open(daemon, ws)
    try:
        session = daemon.session_registry.get(sid)
        assert session is not None
        wall = _walls(daemon, sid)[-1]
        assert wall.config_trust is not None
        assert wall.config_trust.state == "pending"
        assert wall.config_trust.sha256 == sha
        keys = [s.key for s in wall.config_trust.loosened]
        assert keys == ["policy.rules", "boundary.network", "caps.spend_usd"]
        # Packaged defaults, not the file.
        assert wall.network == "deny"
        assert wall.spend_usd == BoundaryConfig().caps.spend_usd
        assert wall.spend_configured is False
        assert wall.source == "defaults — workspace config not trusted yet"
        assert session.policy == PolicyConfig()
    finally:
        await _shutdown(daemon, sid)


async def test_a_config_that_loosens_nothing_applies_with_no_prompt(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    _write(ws, TIGHT)
    daemon = Daemon(data_dir=tmp_path / "data")
    sid = await _open(daemon, ws)
    try:
        wall = _walls(daemon, sid)[-1]
        assert wall.config_trust is None
        assert wall.spend_usd == 5
        assert wall.allowed_commands == ["echo"]
        session = daemon.session_registry.get(sid)
        assert session is not None
        assert session.policy.class_c_default == "never"
    finally:
        await _shutdown(daemon, sid)


async def test_trust_applies_now_and_on_the_next_open_until_the_file_changes(
    tmp_path: Path,
) -> None:
    ws = tmp_path / "ws"
    sha = _write(ws, LOOSE)
    data = tmp_path / "data"
    daemon = Daemon(data_dir=data)
    sid = await _open(daemon, ws)
    try:
        # A stale hash decides nothing.
        stale = await _send(
            daemon,
            {
                "type": "trust_workspace_config",
                "session_id": sid,
                "sha256": "f" * 64,
                "trust": True,
            },
        )
        assert stale is not None and stale["code"] == "workspace_config_changed"
        assert _walls(daemon, sid)[-1].config_trust.state == "pending"  # type: ignore[union-attr]

        ok = await _send(
            daemon,
            {"type": "trust_workspace_config", "session_id": sid, "sha256": sha, "trust": True},
        )
        assert ok is None
        wall = _walls(daemon, sid)[-1]
        assert wall.config_trust is not None and wall.config_trust.state == "trusted"
        assert wall.network == ["example.com"]
        assert wall.spend_usd == 500
        assert wall.source == str(ws / ".tst" / "config.yaml")
        session = daemon.session_registry.get(sid)
        assert session is not None
        assert [r.args for r in session.policy.rules] == ["touch *"]

        # A second session on the same workspace does not ask again.
        sid2 = await _open(daemon, ws)
        second = _walls(daemon, sid2)[-1]
        assert second.config_trust is not None and second.config_trust.state == "trusted"
        assert second.network == ["example.com"]

        # A changed file asks again, and the new session is back on defaults.
        _write(ws, LOOSE.replace("example.com", "evil.example"))
        sid3 = await _open(daemon, ws)
        third = _walls(daemon, sid3)[-1]
        assert third.config_trust is not None and third.config_trust.state == "pending"
        assert third.network == "deny"
    finally:
        await _shutdown(daemon, sid)


async def test_decline_is_remembered_and_keeps_the_defaults(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    sha = _write(ws, LOOSE)
    daemon = Daemon(data_dir=tmp_path / "data")
    sid = await _open(daemon, ws)
    try:
        await _send(
            daemon,
            {"type": "trust_workspace_config", "session_id": sid, "sha256": sha, "trust": False},
        )
        wall = _walls(daemon, sid)[-1]
        assert wall.config_trust is not None and wall.config_trust.state == "declined"
        assert wall.network == "deny"
        assert wall.source == "defaults — workspace config declined"
        sid2 = await _open(daemon, ws)
        again = _walls(daemon, sid2)[-1]
        assert again.config_trust is not None and again.config_trust.state == "declined"
    finally:
        await _shutdown(daemon, sid)


async def test_a_spend_cap_is_not_saved_into_an_untrusted_config(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    _write(ws, LOOSE)
    before = (ws / ".tst" / "config.yaml").read_bytes()
    daemon = Daemon(data_dir=tmp_path / "data")
    sid = await _open(daemon, ws)
    try:
        reply = await _send(daemon, {"type": "set_spend_cap", "session_id": sid, "spend_usd": 900})
        assert reply is not None and reply["code"] == "workspace_config_untrusted"
        assert (ws / ".tst" / "config.yaml").read_bytes() == before
    finally:
        await _shutdown(daemon, sid)


async def test_the_users_own_loosening_does_not_ask_on_the_next_open(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    daemon = Daemon(data_dir=tmp_path / "data")
    sid = await _open(daemon, ws)
    try:
        reply = await _send(daemon, {"type": "set_spend_cap", "session_id": sid, "spend_usd": 80})
        assert reply is None
        assert _walls(daemon, sid)[-1].spend_usd == 80
        sid2 = await _open(daemon, ws)
        wall = _walls(daemon, sid2)[-1]
        assert wall.spend_usd == 80
        assert wall.config_trust is not None and wall.config_trust.state == "trusted"
    finally:
        await _shutdown(daemon, sid)


async def test_a_repo_shell_auto_rule_asks_until_trusted_then_runs(tmp_path: Path) -> None:
    """The exploit the story names: a clone granting itself shell ``auto``."""
    ws = tmp_path / "ws"
    sha = _write(ws, SHELL_AUTO)
    steps = [
        Script(kind="tool_call", tool_name="shell", tool_arguments='{"command": "touch a.txt"}'),
        Script(kind="stream", content="Done."),
        Script(kind="tool_call", tool_name="shell", tool_arguments='{"command": "touch b.txt"}'),
        Script(kind="stream", content="Done."),
    ]
    daemon = Daemon(data_dir=tmp_path / "data", provider=_Steps(steps))
    sid = await _open(daemon, ws)
    try:
        session = daemon.session_registry.get(sid)
        assert session is not None
        log = session.event_log

        def of(kind: type[Any]) -> list[Any]:
            return [e for e in log.all_events if isinstance(e, kind)]

        await _send(daemon, {"type": "user_message", "session_id": sid, "content": "one"})
        await eventually(lambda: len(of(ApprovalRequest)) == 1, within=20)
        parked = of(ApprovalRequest)[0]
        await _send(
            daemon,
            {"type": "deny", "session_id": sid, "tool_call_id": parked.tool_call_id},
        )
        await eventually(lambda: len(of(ToolResult)) == 1, within=20)
        assert not (ws / "a.txt").exists()

        await _send(
            daemon,
            {"type": "trust_workspace_config", "session_id": sid, "sha256": sha, "trust": True},
        )
        await _send(daemon, {"type": "user_message", "session_id": sid, "content": "two"})
        await eventually(lambda: len(of(ToolResult)) == 2, within=20)
        ran = of(ToolResult)[1]
        assert ran.status == "success", ran.output
        assert (ws / "b.txt").exists()
        assert len(of(ApprovalRequest)) == 1  # the trusted rule ran it without asking
    finally:
        await _shutdown(daemon, sid)


def test_untrusted_policy_resolves_a_repo_auto_rule_to_ask(tmp_path: Path) -> None:
    """The gate in one line: the default policy, not the repo's, judges the call."""
    ws = tmp_path / "ws"
    _write(ws, SHELL_AUTO)
    view = read_view(ws, TrustStore(tmp_path / "data"))
    policy = policy_from_mapping(view.path, view.effective)
    shell = create_registry().get("shell")
    assert shell is not None
    args = {"command": "touch x"}
    assert resolve(policy, shell, args, DecisionClass.B, ws) == "ask"


def test_an_unreadable_config_is_a_config_error(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    _write(ws, "- just\n- a list\n")
    with pytest.raises(ConfigError):
        read_view(ws, TrustStore(tmp_path / "data"))


async def test_a_headless_run_says_why_the_repo_settings_are_not_applying(
    capsys: pytest.CaptureFixture[str],
) -> None:
    wall = {
        "type": "boundary_update",
        "session_id": _SESSION,
        "config_trust": {"state": "pending", "sha256": "0" * 64, "loosened": [{}, {}]},
    }
    code = (await cli_turn.stream_turn(_Ws([wall, _TURN_DONE]), _SESSION)).exit_code
    assert code == 0
    err = capsys.readouterr().err
    assert "loosens 2 settings and is not trusted yet" in err
    assert "packaged defaults" in err
    assert cli.config_trust_notice({"config_trust": None}) is None
    trusted = {"config_trust": {"state": "trusted", "loosened": [{}]}}
    assert cli.config_trust_notice(trusted) is None


def test_production_code_reads_workspace_config_only_through_the_gate() -> None:
    """A new caller of an ungated loader would let a clone's settings apply.

    The loaders stay for library use and tests; everything the daemon runs
    reads the file through ``read_view`` and parses ``view.effective``.
    """
    root = Path(__file__).resolve().parent.parent / "tstd"
    ungated = ("load_workspace_boundary(", "load_policy(", "load_approved_imports(")
    allowed = {"boundary_config.py", "policy.py"}
    offenders = [
        f"{path.relative_to(root)}: {name}"
        for path in root.rglob("*.py")
        if path.name not in allowed
        for name in ungated
        if name in path.read_text(encoding="utf-8")
    ]
    assert offenders == []
