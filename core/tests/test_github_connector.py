"""GitHub device flow, classification, and git credential helper (TD-4852).

The fake server is httpx.MockTransport. Sleep and the clock are injected.
Nothing here touches the OS keychain or the network.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import yaml
from pydantic import ValidationError

from tstd.autonomy.classifier import Boundary, DecisionClass, DecisionClassifier
from tstd.config import (
    RESERVED_CREDENTIAL_IDS,
    ConnectorsConfig,
    GithubConnectorConfig,
    ModelConfig,
    default_config_yaml,
)
from tstd.connectors.github_api import github_call
from tstd.connectors.github_classify import GITHUB_ASK_TOOLS, GITHUB_NEVER_TOOLS, GITHUB_READ_TOOLS
from tstd.connectors.github_credential import credential_answer, main
from tstd.connectors.github_flow import poll_for_token, start_device_flow
from tstd.connectors.github_git import contain_dest, git_argv, run_git
from tstd.connectors.github_models import DeviceCode, GithubError, GithubTokens
from tstd.connectors.github_session import GithubCoordinator
from tstd.connectors.github_tokens import MemoryTokenStore, ensure_access
from tstd.connectors.github_tools import _bind
from tstd.daemon import Daemon
from tstd.logging import redact_secrets
from tstd.protocol import GithubConnect, GithubStatus, parse_client_message
from tstd.tools.dispatch import build_decision_request
from tstd.tools.registry import create_registry
from tstd.tools.results import HandlerRefusal

ACCESS = "ghu_" + "a" * 24
REFRESH = "ghr_" + "b" * 24
DEVICE = "device" + "c" * 24
WEB = "gh.example"
API = "api.example"


def _cfg(**over: object) -> GithubConnectorConfig:
    data: dict[str, object] = {
        "client_id": "Iv1.test",
        "api_host": API,
        "web_host": WEB,
        "timeout_seconds": 5,
    }
    data.update(over)
    return GithubConnectorConfig.model_validate(data)


class Clock:
    def __init__(self) -> None:
        self.mono = 0.0
        self.wall = 1_700_000_000.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.mono

    def time(self) -> float:
        return self.wall

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.mono += seconds
        self.wall += seconds


def _transport(
    handler: Callable[[httpx.Request], httpx.Response],
) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


def _device_body() -> dict[str, object]:
    return {
        "device_code": DEVICE,
        "user_code": "ABCD-1234",
        "verification_uri": f"https://{WEB}/login/device",
        "expires_in": 900,
        "interval": 5,
    }


def _token_body(*, refresh: str = REFRESH, expires: int = 28800) -> dict[str, object]:
    return {"access_token": ACCESS, "refresh_token": refresh, "expires_in": expires}


# ── Config ─────────────────────────────────────────────────────────────


def test_shipped_hosts_come_from_yaml_and_the_connector_starts_off() -> None:
    shipped = ModelConfig.model_validate(yaml.safe_load(default_config_yaml()))
    github = shipped.connectors.github
    assert github.client_id == ""
    assert github.api_host == "api.github.com"
    assert github.web_host == "github.com"
    assert github.enabled() is False
    assert "github-oauth" in RESERVED_CREDENTIAL_IDS


def test_empty_or_bad_hosts_keep_the_connector_off() -> None:
    assert _cfg(client_id="").enabled() is False
    assert _cfg(api_host="").enabled() is False
    assert _cfg(web_host="").enabled() is False
    assert _cfg().enabled() is True
    folded = _cfg(api_host="API.Example", web_host="GH.Example")
    assert folded.api_host == "api.example"
    assert folded.web_host == "gh.example"
    with pytest.raises(ValidationError):
        _cfg(api_host="https://api.example")
    with pytest.raises(ValidationError):
        _cfg(web_host="gh.example/login")


# ── Device flow ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_poll_pending_then_slow_down_then_success() -> None:
    clock = Clock()
    steps = [
        (200, {"error": "authorization_pending"}),
        (200, {"error": "slow_down"}),
        (200, _token_body()),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == WEB
        assert request.url.path == "/login/oauth/access_token"
        assert b"device_code" in request.content
        status, body = steps.pop(0)
        return httpx.Response(status, json=body)

    tokens = await poll_for_token(
        client_id="Iv1.test",
        web_host=WEB,
        device_code=DEVICE,
        interval=5,
        expires_in=900,
        timeout_seconds=5,
        transport=_transport(handler),
        sleep=clock.sleep,
        mono=clock.monotonic,
        wall=clock.time,
    )
    assert clock.sleeps == [5, 5, 10]
    assert tokens.access_token == ACCESS
    assert tokens.refresh_token == REFRESH
    assert tokens.expires_at == clock.wall + 28800
    assert DEVICE not in repr(tokens)


@pytest.mark.asyncio
async def test_poll_expires_without_another_request_after_the_deadline() -> None:
    clock = Clock()
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"error": "authorization_pending"})

    with pytest.raises(GithubError, match="expired") as exc:
        await poll_for_token(
            client_id="Iv1.test",
            web_host=WEB,
            device_code=DEVICE,
            interval=5,
            expires_in=9,
            timeout_seconds=5,
            transport=_transport(handler),
            sleep=clock.sleep,
            mono=clock.monotonic,
            wall=clock.time,
        )
    assert exc.value.phase == "expired"
    assert clock.sleeps == [5, 4]
    assert calls == 1


@pytest.mark.asyncio
async def test_terminal_oauth_errors_are_sentences_without_the_description() -> None:
    clock = Clock()

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"error": "access_denied", "error_description": ACCESS},
        )

    with pytest.raises(GithubError, match="denied") as exc:
        await poll_for_token(
            client_id="Iv1.test",
            web_host=WEB,
            device_code=DEVICE,
            interval=1,
            expires_in=30,
            timeout_seconds=5,
            transport=_transport(handler),
            sleep=clock.sleep,
            mono=clock.monotonic,
            wall=clock.time,
        )
    assert exc.value.phase == "denied"
    assert ACCESS not in exc.value.message


@pytest.mark.asyncio
async def test_start_rejects_an_off_host_verification_uri() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        body = _device_body()
        body["verification_uri"] = "https://evil.example/device"
        return httpx.Response(200, json=body)

    with pytest.raises(GithubError, match="failed"):
        await start_device_flow(
            client_id="Iv1.test", web_host=WEB, timeout_seconds=5, transport=_transport(handler)
        )


@pytest.mark.asyncio
async def test_refresh_rotates_and_a_401_retries_once(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    clock = Clock()
    store = MemoryTokenStore()
    await store.save(
        GithubTokens(access_token=ACCESS, refresh_token=REFRESH, expires_at=clock.wall + 10_000)
    )
    calls = {"api": 0}
    leaked = "LEAKED-" + ACCESS

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == WEB:
            return httpx.Response(
                200,
                json={
                    "access_token": "ghu_" + "d" * 24,
                    "refresh_token": "ghr_" + "e" * 24,
                    "expires_in": 1000,
                },
            )
        calls["api"] += 1
        if calls["api"] == 1:
            return httpx.Response(401, json={"message": leaked})
        return httpx.Response(200, json={"ok": True})

    payload = await github_call(
        _cfg(),
        store,
        "GET",
        "/user",
        transport=_transport(handler),
        now=clock.time,
    )
    assert payload == {"ok": True}
    assert calls["api"] == 2
    saved = await store.load()
    assert saved is not None
    assert saved.refresh_token == "ghr_" + "e" * 24
    assert leaked not in caplog.text
    assert ACCESS not in caplog.text
    assert REFRESH not in caplog.text


@pytest.mark.asyncio
async def test_success_and_a_token_in_an_error_body_stay_out_of_the_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    clock = Clock()
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path.endswith("/device/code"):
            return httpx.Response(200, json=_device_body())
        if len(seen) == 2:
            return httpx.Response(500, content=ACCESS.encode())
        return httpx.Response(200, json=_token_body())

    with pytest.raises(GithubError, match="failed"):
        code = await start_device_flow(
            client_id="Iv1.test", web_host=WEB, timeout_seconds=5, transport=_transport(handler)
        )
        await poll_for_token(
            client_id="Iv1.test",
            web_host=WEB,
            device_code=code.device_code,
            interval=code.interval,
            expires_in=code.expires_in,
            timeout_seconds=5,
            transport=_transport(handler),
            sleep=clock.sleep,
            mono=clock.monotonic,
            wall=clock.time,
        )
    # The 500 body consumed the second response. A fresh transport is not
    # required: the assertion is that the secret never reached the log.
    assert ACCESS not in caplog.text
    assert DEVICE not in caplog.text
    assert "github request failed" in caplog.text


@pytest.mark.asyncio
async def test_coordinator_shows_the_user_code_then_stores_the_login() -> None:
    clock = Clock()
    store = MemoryTokenStore()
    events: list[GithubStatus] = []

    async def broadcast(event: GithubStatus) -> None:
        events.append(event)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/device/code"):
            return httpx.Response(200, json=_device_body())
        if request.url.path == "/user":
            assert request.url.host == API
            return httpx.Response(200, json={"login": "octocat"})
        return httpx.Response(200, json=_token_body())

    coord = GithubCoordinator(
        lambda: SimpleNamespace(connectors=SimpleNamespace(github=_cfg())),  # type: ignore[arg-type]
        broadcast,
        store=store,
        transport=_transport(handler),
        sleep=clock.sleep,
        mono=clock.monotonic,
        wall=clock.time,
    )
    try:
        pending = await coord.connect()
        assert pending.phase == "pending"
        assert pending.user_code == "ABCD-1234"
        assert DEVICE not in pending.model_dump_json()
        assert coord._task is not None
        await coord._task
        done = await coord.snapshot()
        assert done.phase == "connected"
        assert done.login == "octocat"
        assert done.user_code == ""
        saved = await store.load()
        assert saved is not None
        assert ACCESS not in repr(saved)
        raw = await coord.disconnect_json()
        body = json.loads(raw)
        assert body["phase"] == "idle"
        assert await store.load() is None
        assert events[-1].phase == "idle"
    finally:
        await coord.aclose()


def test_repr_omits_the_device_code() -> None:
    code = DeviceCode(DEVICE, "ABCD-1234", f"https://{WEB}/login/device", 90, 5)
    assert DEVICE not in repr(code)
    assert "ABCD-1234" in repr(code)


# ── Tools and classification ───────────────────────────────────────────


@pytest.mark.parametrize(
    ("name", "expected", "rule"),
    [
        *[(name, DecisionClass.A, "github-read") for name in sorted(GITHUB_READ_TOOLS)],
        *[(name, DecisionClass.B, "side-effect-ask-floor") for name in sorted(GITHUB_ASK_TOOLS)],
        *[(name, DecisionClass.C, "side-effect-never") for name in sorted(GITHUB_NEVER_TOOLS)],
    ],
)
def test_github_tools_classify_by_name(
    name: str, expected: DecisionClass, rule: str, tmp_path: Path
) -> None:
    tool = create_registry().require(name)
    request = build_decision_request(
        tool,
        {"owner": "acme", "repo": "widgets", "path": "src/a.txt", "dest": "clone-here"},
    )
    decision = DecisionClassifier(Boundary(workspace_root=tmp_path)).classify(request)
    assert request.hosts == frozenset()
    assert request.reads == ()
    assert request.writes == ()
    assert decision.decision_class is expected
    assert decision.rule is not None
    assert decision.rule.id == rule


def test_registered_auto_github_tools_are_the_read_set() -> None:
    registry = create_registry()
    auto = {
        name
        for name in (*GITHUB_READ_TOOLS, *GITHUB_ASK_TOOLS, *GITHUB_NEVER_TOOLS)
        if registry.require(name).side_effect_class == "auto"
    }
    assert auto == set(GITHUB_READ_TOOLS)


@pytest.mark.asyncio
async def test_settings_tool_refuses_even_when_called() -> None:
    session = SimpleNamespace(
        config=SimpleNamespace(connectors=SimpleNamespace(github=_cfg())),
        github_tokens=MemoryTokenStore(),
        github_transport=None,
    )
    with pytest.raises(HandlerRefusal) as exc:
        await _bind("github_update_settings")(session=session, tool_call_id="tc")
    assert exc.value.code == "github_failed"
    assert "not available" in exc.value.message


@pytest.mark.asyncio
async def test_ensure_access_refreshes_inside_the_skew_window() -> None:
    clock = Clock()
    store = MemoryTokenStore()
    await store.save(GithubTokens(ACCESS, REFRESH, expires_at=clock.wall + 30))

    def handler(request: httpx.Request) -> httpx.Response:
        assert REFRESH.encode() in request.content
        return httpx.Response(200, json=_token_body(refresh="ghr_" + "f" * 24, expires=50))

    token = await ensure_access(_cfg(), store, now=clock.time, transport=_transport(handler))
    assert token == ACCESS
    saved = await store.load()
    assert saved is not None
    assert saved.refresh_token == "ghr_" + "f" * 24


# ── Git ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_git_clone_uses_a_helper_and_does_not_write_the_token(tmp_path: Path) -> None:
    recorded: list[tuple[list[str], dict[str, str]]] = []

    async def runner(
        argv: list[str], env: dict[str, str], cwd: Path, timeout_seconds: float
    ) -> tuple[int, str, str]:
        del cwd, timeout_seconds
        recorded.append((argv, env))
        dest = Path(argv[-1])
        git_dir = dest / ".git"
        git_dir.mkdir(parents=True)
        url = argv[-2]
        (git_dir / "config").write_text(f'[remote "origin"]\n\turl = {url}\n', encoding="utf-8")
        return 0, "ok " + ACCESS, ""

    text = await run_git(
        workspace=tmp_path,
        web_host=WEB,
        api_host=API,
        client_id="Iv1.test",
        timeout_seconds=5,
        action="clone",
        dest="checkout",
        owner="acme",
        repo="widgets",
        runner=runner,
        executable="/usr/bin/python3",
    )
    argv, env = recorded[0]
    rendered = (
        " ".join(argv) + json.dumps(env) + (tmp_path / "checkout" / ".git" / "config").read_text()
    )
    assert ACCESS not in rendered
    assert "[REDACTED]" in text
    assert "credential.helper=" in argv
    helper = next(part for part in argv if part.startswith("credential.helper=!"))
    assert "tstd.connectors.github_credential" in helper
    assert "config" not in argv
    assert "set-url" not in argv
    assert "--force" not in argv
    assert env["TST_GITHUB_WEB_HOST"] == WEB
    assert env["TST_GITHUB_CLIENT_ID"] == "Iv1.test"
    assert "GIT_ASKPASS" not in env
    assert all("TOKEN" not in name for name in env)


def test_git_argv_refuses_force_shaped_destinations() -> None:
    with pytest.raises(GithubError):
        git_argv(
            executable="python",
            web_host=WEB,
            action="clone",
            dest="-evil",
            owner="acme",
            repo="widgets",
        )


def test_contain_dest_refuses_a_symlink_outside_the_workspace(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "ws"
    root.mkdir()
    (root / "link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(GithubError, match="outside"):
        contain_dest(root, "link")


def test_credential_answer_fills_only_the_configured_https_host() -> None:
    good = "protocol=https\nhost=GH.Example\n"
    assert "x-access-token" in credential_answer(good, web_host=WEB, token=ACCESS)
    assert (
        credential_answer("protocol=https\nhost=other.example\n", web_host=WEB, token=ACCESS) == ""
    )
    assert credential_answer("host=gh.example\n", web_host=WEB, token=ACCESS) == ""
    assert (
        credential_answer(
            "protocol=https\nhost=gh.example\npassword=already\n", web_host=WEB, token=ACCESS
        )
        == ""
    )
    assert (
        credential_answer("protocol=https\nhost=gh.example:443\n", web_host=WEB, token=ACCESS) != ""
    )


def test_helper_main_writes_nothing_when_the_keychain_is_isolated(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main("protocol=https\nhost=gh.example\n") == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert ACCESS not in captured.err


def test_redact_covers_user_to_server_tokens() -> None:
    for secret in (ACCESS, REFRESH, "gho_" + "a" * 24, "ghs_" + "b" * 24):
        assert secret not in redact_secrets(f"saw {secret}")


# ── Daemon ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_daemon_connect_is_off_without_a_client_id(tmp_path: Path) -> None:
    daemon = Daemon(data_dir=tmp_path / "data")
    raw = await daemon._dispatch_message(GithubConnect().model_dump_json(), None)
    assert raw is not None
    body = json.loads(raw)
    assert body["type"] == "github_status"
    assert body["phase"] == "off"
    assert "client_id" in body["message"]
    assert "device_code" not in raw
    state = await daemon._setup_state_event()
    assert state.github.phase == "off"


@pytest.mark.asyncio
async def test_daemon_pending_snapshot_survives_setup_state(tmp_path: Path) -> None:
    daemon = Daemon(data_dir=tmp_path / "data")
    daemon.config = daemon.config.model_copy(
        update={
            "connectors": ConnectorsConfig(
                github=GithubConnectorConfig(client_id="Iv1.test", api_host=API, web_host=WEB)
            )
        }
    )
    gate = asyncio.Event()

    async def blocked(_seconds: float) -> None:
        await gate.wait()

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_device_body())

    daemon._github = GithubCoordinator(
        lambda: daemon.config,
        daemon._broadcast_github_status,
        transport=_transport(handler),
        sleep=blocked,
    )
    try:
        raw = await daemon._dispatch_message(GithubConnect().model_dump_json(), None)
        assert raw is not None
        body = json.loads(raw)
        assert body["phase"] == "pending"
        assert body["user_code"] == "ABCD-1234"
        assert DEVICE not in raw
        state = await daemon._setup_state_event()
        assert state.github.user_code == "ABCD-1234"
        assert state.github.verification_uri == f"https://{WEB}/login/device"
    finally:
        await daemon._github.aclose()


def test_github_messages_round_trip() -> None:
    parsed = parse_client_message(GithubConnect().model_dump_json())
    assert isinstance(parsed, GithubConnect)
    status = GithubStatus(
        phase="pending", user_code="ABCD-1234", verification_uri=f"https://{WEB}/x"
    )
    again = GithubStatus.model_validate_json(status.model_dump_json())
    assert again.seq == 1
    assert not hasattr(again, "device_code") or "device_code" not in again.model_dump()
