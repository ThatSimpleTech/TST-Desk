"""Element-level desktop tools: driver, guards, dispatch, config (TD-5220).

The guards are security tests (AGENTS.md §7): a password field is never
typed into, the kill-switch and the apps policy stop an action before
any command is sent, and a ref from no known snapshot is refused.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from tests.ax_fakes import (
    FORM_WINDOW,
    PASSWORD,
    REMEMBER,
    USERNAME,
    FakeCli,
    fixture,
    make_driver,
)
from tests.test_cu_verify import _session
from tests.test_dispatch import attach_auto_approver
from tstd.autonomy import (
    AmbiguousClassifier,
    Boundary,
    DecisionClassifier,
    ScriptedJudgmentBackend,
    ideal_judgment,
)
from tstd.config import AccessibilityConfig, ModelConfig
from tstd.cu_policy import CuPolicy
from tstd.cu_verify import ActuationVerifier, DriverStateProbe
from tstd.desktop import DesktopError, MockDesktopDriver, accessibility_driver_from_config
from tstd.desktop.ax_driver import AccessibilityDriver
from tstd.policy import PolicyConfig
from tstd.tools import ToolDispatcher, create_registry
from tstd.tools.boundary import PathGuard
from tstd.tools.desktop_ax import AX_TOOL_NAMES
from tstd.tools.handlers import register_builtin_handlers

FORM_ROUTES = {
    "snapshot": "snapshot_form.json",
    "get:states": "get_states_plain.json",
    "get:role": "get_role_textfield.json",
    "type": "type_ok.json",
    "set-value": "type_ok.json",
    "toggle": "toggle_ok.json",
}


async def _form_driver(tmp_path: Path, **routes: str) -> tuple[AccessibilityDriver, FakeCli]:
    fake = FakeCli({**FORM_ROUTES, **routes})
    driver = make_driver(tmp_path, fake)
    await driver.snapshot(window_id=FORM_WINDOW)
    return driver, fake


class TestSnapshot:
    async def test_by_window_id(self, tmp_path: Path) -> None:
        fake = FakeCli({"snapshot": "snapshot_form.json"})
        text = await make_driver(tmp_path, fake).snapshot(window_id=FORM_WINDOW)
        assert text.splitlines()[0].startswith('app "python3.14" window "Test Form"')
        assert f'checkbox "Remember me" {REMEMBER}' in text
        assert fake.calls[0] == ["snapshot", "-i", "--compact", "--window-id", FORM_WINDOW]

    async def test_too_large_falls_back_to_skeleton(self, tmp_path: Path) -> None:
        fake = FakeCli(
            {
                "snapshot": "snapshot_editor.json",
                "snapshot:skeleton": "snapshot_editor_skeleton.json",
            }
        )
        driver = make_driver(tmp_path, fake, max_snapshot_chars=1000)
        text = await driver.snapshot(app="mousepad")
        assert "--skeleton" in fake.calls[1]
        assert "skeleton: the full tree was too large" in text
        assert len(text) <= 1000

    async def test_root_needs_a_known_snapshot(self, tmp_path: Path) -> None:
        fake = FakeCli({"snapshot": "snapshot_form.json"})
        with pytest.raises(DesktopError) as err:
            await make_driver(tmp_path, fake).snapshot(root="@sunknown:e3")
        assert err.value.code == "ax_stale_ref"
        assert fake.calls == []

    async def test_needs_a_target(self, tmp_path: Path) -> None:
        with pytest.raises(DesktopError) as err:
            await make_driver(tmp_path, FakeCli({})).snapshot()
        assert err.value.code == "ax_invalid_args"

    async def test_reads_while_killed(self, tmp_path: Path) -> None:
        fake = FakeCli({"snapshot": "snapshot_form.json"})
        driver = make_driver(tmp_path, fake)
        driver.set_killed(True)
        assert "Remember me" in await driver.snapshot(window_id=FORM_WINDOW)


class TestPasswordGuard:
    @pytest.mark.parametrize("action", ["type", "set_value"])
    async def test_flagged_field_refused_before_anything_is_sent(
        self, tmp_path: Path, action: str
    ) -> None:
        driver, fake = await _form_driver(tmp_path)
        with pytest.raises(DesktopError) as err:
            await driver.act(PASSWORD, action, "hunter2")
        assert err.value.code == "ax_secure_field"
        assert "user" in err.value.message and "themselves" in err.value.message
        assert fake.commands() == ["snapshot"]

    async def test_live_state_catches_what_the_snapshot_missed(self, tmp_path: Path) -> None:
        driver, fake = await _form_driver(tmp_path, **{"get:states": "get_states_secure.json"})
        with pytest.raises(DesktopError) as err:
            await driver.act(USERNAME, "type", "hunter2")
        assert err.value.code == "ax_secure_field"
        assert "type" not in fake.commands()

    async def test_click_on_a_password_field_is_allowed(self, tmp_path: Path) -> None:
        driver, fake = await _form_driver(tmp_path, click="toggle_ok.json")
        await driver.act(PASSWORD, "click")
        assert fake.commands()[-1] == "click"

    async def test_plain_field_types_and_redacts(self, tmp_path: Path) -> None:
        driver, fake = await _form_driver(tmp_path)
        body = json.loads(await driver.act(USERNAME, "type", "abc"))
        assert fake.calls[-1] == ["type", "--", USERNAME, "abc"]
        assert body["delivery"] == "delivered_verified"
        assert body["window"] == "Test Form"
        assert body["post_state"]["value"] == "[3 chars]"


class TestActGuards:
    async def test_kill_switch_refuses_first(self, tmp_path: Path) -> None:
        driver, fake = await _form_driver(tmp_path)
        driver.set_killed(True)
        with pytest.raises(DesktopError) as err:
            await driver.act(REMEMBER, "toggle")
        assert err.value.code == "cu_killed"
        assert fake.commands() == ["snapshot"]

    async def test_actuation_off_in_settings(self, tmp_path: Path) -> None:
        fake = FakeCli(FORM_ROUTES)
        driver = make_driver(tmp_path, fake, policy=CuPolicy(enabled=False))
        await driver.snapshot(window_id=FORM_WINDOW)
        with pytest.raises(DesktopError) as err:
            await driver.act(REMEMBER, "toggle")
        assert err.value.code == "cu_disabled"
        assert fake.commands() == ["snapshot"]

    async def test_denied_app_is_never_read(self, tmp_path: Path) -> None:
        fake = FakeCli(FORM_ROUTES)
        driver = make_driver(tmp_path, fake, policy=CuPolicy(denied_apps=("python",)))
        with pytest.raises(DesktopError) as err:
            await driver.snapshot(window_id=FORM_WINDOW)
        assert err.value.code == "ax_app_denied"

    async def test_unknown_ref_refused_without_a_command(self, tmp_path: Path) -> None:
        driver, fake = await _form_driver(tmp_path)
        with pytest.raises(DesktopError) as err:
            await driver.act("@sneverseen:e1", "click")
        assert err.value.code == "ax_stale_ref"
        with pytest.raises(DesktopError) as bare:
            await driver.act("@e4", "click")
        assert bare.value.code == "ax_invalid_args"
        assert fake.commands() == ["snapshot"]

    async def test_value_required_and_action_known(self, tmp_path: Path) -> None:
        driver, _fake = await _form_driver(tmp_path)
        with pytest.raises(DesktopError):
            await driver.act(USERNAME, "type")
        with pytest.raises(DesktopError):
            await driver.act(USERNAME, "drag")

    async def test_toggle(self, tmp_path: Path) -> None:
        driver, fake = await _form_driver(tmp_path)
        body = json.loads(await driver.act(REMEMBER, "toggle"))
        assert fake.calls[-1] == ["toggle", REMEMBER]
        assert body["post_state"]["states"] == ["checked"]


class TestWindowsAndLaunch:
    async def test_windows_hide_denied_and_degenerate(self, tmp_path: Path) -> None:
        fake = FakeCli({"list-windows": "list_windows.json"})
        driver = make_driver(tmp_path, fake, policy=CuPolicy(denied_apps=("keepass",)))
        rows = json.loads(await driver.windows())["windows"]
        assert [r["app"] for r in rows] == ["python3.14", "mousepad"]
        assert rows[0] == {
            "window_id": FORM_WINDOW,
            "app": "python3.14",
            "title": "Test Form",
            "pid": 4074446,
            "focused": True,
        }

    async def test_launch_switches_accessibility_on(self, tmp_path: Path) -> None:
        fake = FakeCli({"launch": "launch_ok.json"})
        body = json.loads(await make_driver(tmp_path, fake).launch("mousepad"))
        argv = fake.calls[0]
        assert argv[:4] == ["launch", "mousepad", "--no-attach", "--activate"]
        assert "ACCESSIBILITY_ENABLED=1" in argv and "QT_ACCESSIBILITY=1" in argv
        assert "--arg=--force-renderer-accessibility" not in argv
        assert "NO_AT_BRIDGE" not in fake.envs[0]
        assert body["window_id"] == "w-1.355-org-a11y-atspi-accessible-11"

    async def test_launch_chromium_adds_renderer_flag(self, tmp_path: Path) -> None:
        fake = FakeCli({"launch": "launch_ok.json"})
        await make_driver(tmp_path, fake).launch("/usr/bin/google-chrome")
        assert "--arg=--force-renderer-accessibility" in fake.calls[0]

    async def test_launch_guards(self, tmp_path: Path) -> None:
        fake = FakeCli({"launch": "launch_ok.json"})
        driver = make_driver(tmp_path, fake, policy=CuPolicy(denied_apps=("mousepad",)))
        with pytest.raises(DesktopError) as denied:
            await driver.launch("mousepad")
        assert denied.value.code == "ax_app_denied"
        with pytest.raises(DesktopError) as flag:
            await driver.launch("--version")
        assert flag.value.code == "ax_invalid_args"
        driver.set_killed(True)
        with pytest.raises(DesktopError) as killed:
            await driver.launch("gedit")
        assert killed.value.code == "cu_killed"
        assert fake.calls == []


class TestVerificationState:
    async def test_state_for_ref_is_the_window_without_refs(self, tmp_path: Path) -> None:
        driver, fake = await _form_driver(tmp_path)
        state = await driver.state_for_ref(REMEMBER)
        assert state is not None and state.startswith('window "Test Form"')
        assert "@s3" not in state
        assert fake.calls[-1][-2:] == ["--window-id", FORM_WINDOW]

    async def test_unknown_ref_has_no_state(self, tmp_path: Path) -> None:
        driver, fake = await _form_driver(tmp_path)
        assert await driver.state_for_ref("@sother:e1") is None
        assert await driver.state_for_ref("nonsense") is None
        assert fake.commands() == ["snapshot"]

    async def test_probe_routes_desktop_act_to_the_ref(self, tmp_path: Path) -> None:
        driver, _fake = await _form_driver(tmp_path)
        probe = DriverStateProbe(MockDesktopDriver(), object(), ax=driver)
        state = await probe.snapshot("desktop_act", {"ref": REMEMBER, "action": "toggle"})
        assert state is not None and "Remember me" in state
        assert await DriverStateProbe(object(), object()).snapshot("desktop_act", {}) is None


def _dispatcher(tmp_path: Path, ax: AccessibilityDriver | None, backend: object) -> ToolDispatcher:
    async def _worker(prompt: str) -> str:
        return "B"

    dispatcher = ToolDispatcher(
        create_registry(accessibility=ax is not None),
        classifier=AmbiguousClassifier(
            static=DecisionClassifier(Boundary(workspace_root=tmp_path)),
            call_worker=_worker,
        ),
        path_guard=PathGuard(Boundary(workspace_root=tmp_path)),
        policy=PolicyConfig(),
        workspace=tmp_path,
    )
    register_builtin_handlers(dispatcher, desktop_driver=MockDesktopDriver(), ax_driver=ax)
    dispatcher.verifier = ActuationVerifier(backend)  # type: ignore[arg-type]
    attach_auto_approver(dispatcher)
    return dispatcher


class TestDispatch:
    def test_tools_listed_only_when_on(self) -> None:
        off = {t.name for t in create_registry().list_tools()}
        assert not off & set(AX_TOOL_NAMES)
        on = {t.name: t for t in create_registry(accessibility=True).list_tools()}
        assert set(AX_TOOL_NAMES) <= set(on)
        assert on["desktop_act"].actuates and on["desktop_act"].side_effect_class == "ask"
        assert on["desktop_launch"].actuates
        assert not on["desktop_snapshot"].actuates and not on["desktop_windows"].actuates

    async def test_act_is_verified_on_window_state(self, tmp_path: Path) -> None:
        driver, _fake = await _form_driver(tmp_path)
        backend = ScriptedJudgmentBackend([ideal_judgment("yes")])
        dispatcher = _dispatcher(tmp_path, driver, backend)
        result = await dispatcher.dispatch(
            "c1",
            "desktop_act",
            {"ref": USERNAME, "action": "type", "value": "abc"},
            session=_session(tmp_path),
        )
        assert result.status == "success", result.output
        assert result.verification == "verified"
        state = dict(backend.questions[0].state)
        assert state["State before"].startswith('window "Test Form"')
        assert '"abc"' not in state["Arguments"]

    async def test_secure_refusal_reaches_the_model(self, tmp_path: Path) -> None:
        driver, _fake = await _form_driver(tmp_path)
        dispatcher = _dispatcher(tmp_path, driver, ScriptedJudgmentBackend([]))
        result = await dispatcher.dispatch(
            "c1",
            "desktop_act",
            {"ref": PASSWORD, "action": "type", "value": "hunter2"},
            session=_session(tmp_path),
        )
        assert result.status != "success"
        assert "password field" in result.output


class TestConfig:
    def test_off_by_default(self, tmp_path: Path) -> None:
        assert AccessibilityConfig().enabled is False
        assert accessibility_driver_from_config(ModelConfig(), tmp_path) is None

    def test_on_builds_a_driver(self, tmp_path: Path) -> None:
        cfg = ModelConfig.model_validate(
            {"computer_use": {"accessibility": {"enabled": True, "binary": "/opt/ad"}}}
        )
        assert isinstance(accessibility_driver_from_config(cfg, tmp_path), AccessibilityDriver)

    def test_only_the_bundled_copy_skips_the_release_digest(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """TD-5222: the packaged copy is trusted; a configured one is checked."""
        app = tmp_path / "app"
        app.mkdir()
        (app / "tstd").write_bytes(b"")
        name = "agent-desktop.exe" if sys.platform == "win32" else "agent-desktop"
        (app / name).write_bytes(b"packed")
        monkeypatch.setattr(sys, "executable", str(app / "tstd"))
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        on = {"computer_use": {"accessibility": {"enabled": True}}}
        bundled = accessibility_driver_from_config(ModelConfig.model_validate(on), tmp_path)
        assert bundled is not None and bundled._cli._packaged is True
        on["computer_use"]["accessibility"]["binary"] = "/opt/ad"
        configured = accessibility_driver_from_config(ModelConfig.model_validate(on), tmp_path)
        assert configured is not None and configured._cli._packaged is False

    def test_digest_must_be_hex(self) -> None:
        with pytest.raises(ValueError):
            AccessibilityConfig(sha256="not-a-digest")
        assert AccessibilityConfig(sha256="AB" * 32).sha256 == "ab" * 32

    def test_shipped_config_keeps_it_off(self) -> None:
        import yaml

        shipped = Path(__file__).parents[1] / "tstd" / "config.yaml"
        data = yaml.safe_load(shipped.read_text(encoding="utf-8"))
        assert data["computer_use"]["accessibility"]["enabled"] is False
        assert fixture("snapshot_form.json")["ok"] is True
