"""TD-5295: the user-level sandbox default, and what a repo may change.

A workspace's ``sandbox:`` section sits over the user's ``sandbox:`` in the
daemon's config. A repo may tighten freely; under ``default: on`` or
``require``, turning the sandbox off, excluding a command or turning on
auto-run waits for the trust card. Where the sandbox only partly holds, a
command asks (``on_partial: ask``) or is refused (``refuse``, ``require``).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.test_shell_sandbox import _dispatcher, _session
from tstd.config import ModelConfig, SandboxDefaults
from tstd.policy import PolicyDecision
from tstd.sandbox import BackendStatus, Enforcement, set_backend
from tstd.session import Session
from tstd.session_lifecycle import reload_session_boundary
from tstd.shell_sandbox import SandboxSection, effective, partial_decision
from tstd.workspace_trust import TrustStore, find_loosened, read_view

ON = SandboxDefaults(default="on")
FULL = BackendStatus("bwrap", Enforcement.FULL)
PARTIAL = BackendStatus("bwrap", Enforcement.PARTIAL, gaps=(("pins", "nested namespaces"),))
AUTO = PolicyDecision("auto", "class A", None)
ASK = PolicyDecision("ask", "class B", None)


class _Fixed:
    name = "fixed"

    def __init__(self, status: BackendStatus) -> None:
        self.status = status

    def probe(self) -> BackendStatus:
        return self.status


@pytest.fixture(autouse=True)
def _reset() -> Iterator[None]:
    yield
    set_backend(None)


class TestMerge:
    def test_a_silent_workspace_follows_the_default(self) -> None:
        assert not effective(SandboxSection(), None).enabled
        assert effective(SandboxSection(), SandboxDefaults()).enabled is False
        assert effective(SandboxSection(), ON).enabled is True
        assert effective(SandboxSection(), SandboxDefaults(default="require")).enabled is True

    def test_what_the_workspace_says_wins(self) -> None:
        assert effective(SandboxSection(enabled=False), ON).enabled is False
        assert effective(SandboxSection(enabled=True), SandboxDefaults()).enabled is True

    def test_auto_run_either_way_and_hidden_paths_add_up_once(self) -> None:
        mine = SandboxDefaults(auto_allow=True, hidden_paths=["~/private"])
        merged = effective(SandboxSection(hidden_paths=["~/.secrets", "~/private"]), mine)
        assert merged.auto_allow is True
        assert merged.hidden_paths == ["~/private", "~/.secrets"]
        assert effective(merged, mine) == merged

    def test_yaml_off_and_on_read_as_words(self) -> None:
        for text, want in (("off", "off"), ("on", "on"), ("require", "require")):
            data = yaml.safe_load(f"sandbox:\n  default: {text}\n")
            assert ModelConfig.model_validate(data).sandbox.default == want

    def test_a_workspace_cannot_set_the_user_keys(self) -> None:
        with pytest.raises(ValueError):
            SandboxSection.model_validate({"on_partial": "ask"})
        with pytest.raises(ValueError):
            SandboxSection.model_validate({"default": "off"})


def _loosened(sandbox: dict[str, Any], default: str) -> list[str]:
    path = Path("/w/.tst/config.yaml")
    return [x.key for x in find_loosened(path, {"sandbox": sandbox}, sandbox_default=default)]


class TestTrust:
    @pytest.mark.parametrize(
        ("sandbox", "off", "on"),
        [
            ({"enabled": False}, [], ["sandbox.enabled"]),
            ({"exclude": ["docker *"]}, [], ["sandbox.exclude"]),
            ({"enabled": True, "exclude": ["docker *"]}, [], ["sandbox.exclude"]),
            ({"auto_allow": True}, [], ["sandbox.auto_allow"]),
            ({"enabled": True, "auto_allow": True}, ["sandbox.auto_allow"], ["sandbox.auto_allow"]),
            ({"enabled": True, "hidden_paths": ["x"]}, [], []),
        ],
    )
    def test_what_loosens_depends_on_the_default(
        self, sandbox: dict[str, Any], off: list[str], on: list[str]
    ) -> None:
        assert _loosened(sandbox, "off") == off
        assert _loosened(sandbox, "on") == on
        assert _loosened(sandbox, "require") == on

    def test_untrusted_off_switch_is_not_applied(self, tmp_path: Path) -> None:
        (tmp_path / ".tst").mkdir()
        (tmp_path / ".tst" / "config.yaml").write_text("sandbox:\n  enabled: false\n")
        store = TrustStore(tmp_path / "data", "on")
        assert read_view(tmp_path, store).state == "pending"
        session = Session(str(tmp_path))
        session.trust_store = store
        session.sandbox_defaults = ON
        assert reload_session_boundary(session, str(tmp_path)) is None
        assert session.boundary_config.sandbox.enabled is True
        # The same file is no loosening when the default is off.
        assert read_view(tmp_path, TrustStore(tmp_path / "data", "off")).loosened == ()


class TestPartial:
    @pytest.mark.parametrize(
        ("defaults", "decision", "effect"),
        [
            (SandboxDefaults(), AUTO, "ask"),
            (SandboxDefaults(), ASK, None),
            (SandboxDefaults(on_partial="refuse"), AUTO, "never"),
            (SandboxDefaults(on_partial="refuse"), ASK, "never"),
            (SandboxDefaults(default="require"), AUTO, "never"),
            (None, AUTO, "ask"),
            (SandboxDefaults(), PolicyDecision("never", "no", None), None),
        ],
    )
    def test_ask_or_refuse(
        self, defaults: SandboxDefaults | None, decision: PolicyDecision, effect: str | None
    ) -> None:
        got = partial_decision(defaults, decision, PARTIAL)
        assert (got.effect if got else None) == effect
        if got is not None:
            assert "nested namespaces" in got.reason
        assert partial_decision(defaults, decision, FULL) is None

    async def test_a_class_a_command_asks_in_a_partial_sandbox(self, tmp_path: Path) -> None:
        set_backend(_Fixed(PARTIAL))
        asked: list[str] = []
        session = _session(tmp_path, effective(SandboxSection(), ON))
        session.sandbox_defaults = ON
        await _dispatcher(tmp_path, asked).dispatch("c1", "shell", {"command": "ls"}, session)
        assert asked == ["ls"]

    async def test_require_refuses_it(self, tmp_path: Path) -> None:
        set_backend(_Fixed(PARTIAL))
        asked: list[str] = []
        must = SandboxDefaults(default="require")
        session = _session(tmp_path, effective(SandboxSection(), must))
        session.sandbox_defaults = must
        result = await _dispatcher(tmp_path, asked).dispatch(
            "c2", "shell", {"command": "ls"}, session
        )
        assert asked == [] and result.status != "success"
        assert "partly holds" in result.output
