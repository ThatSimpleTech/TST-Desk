"""Tests for the diagnostics wire (TD-1104 doctor).

``run_diagnostics`` answers with one ``diagnostics_report`` listing each
check as ok / fail / skip with actionable detail — the failure rows carry
a concrete ``fix``.  The key-present and provider-valid rows share a
single one-token provider probe: an auth failure still proves
reachability, a transport failure proves nothing about the key.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import tempfile
from pathlib import Path
from typing import Any

import pytest
from websockets.asyncio.client import connect

from tstd.config import cached_config
from tstd.daemon import Daemon
from tstd.policy import save_approved_imports
from tstd.protocol import PROTOCOL_VERSION
from tstd.provider import ProviderError


async def _connect_and_handshake(uri: str, token: str) -> Any:
    """Open a connection and perform the hello handshake."""
    ws = await connect(uri)
    await ws.send(json.dumps({"type": "hello", "token": token, "version": PROTOCOL_VERSION}))
    ack = json.loads(await ws.recv())
    assert ack["type"] == "hello_ack"
    return ws


async def _start_daemon(tmp: Path) -> tuple[Daemon, asyncio.Task[Any]]:
    """Start a daemon on a temp dir and wait for its port."""
    daemon = Daemon(data_dir=tmp)
    task = asyncio.create_task(daemon.run())
    for _ in range(50):
        if daemon.ws_server.port:
            break
        await asyncio.sleep(0.05)
    assert daemon.ws_server.port > 0
    return daemon, task


async def _stop_daemon(task: asyncio.Task[Any]) -> None:
    """Cancel the daemon task and wait for its shutdown to finish.

    The daemon holds audit.db open until _shutdown() closes the audit
    store; on Windows the surrounding TemporaryDirectory cleanup cannot
    unlink an open file, so teardown must complete here, not race it.
    """
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def _report(tmp: Path) -> list[dict[str, Any]]:
    """Handshake, run diagnostics with no workspace, return the check rows."""
    daemon, task = await _start_daemon(tmp)
    try:
        ws = await _connect_and_handshake(
            f"ws://127.0.0.1:{daemon.ws_server.port}", daemon.ws_server.token
        )
        await ws.send(json.dumps({"type": "run_diagnostics"}))
        resp = dict(json.loads(await ws.recv()))
        assert resp["type"] == "diagnostics_report"
        await ws.close()
        return list(resp["checks"])
    finally:
        await _stop_daemon(task)


def _row(checks: list[dict[str, Any]], name: str) -> dict[str, Any]:
    return next(c for c in checks if c["name"] == name)


class FakeKeychain:
    """In-memory stand-in for the store helper. Presence reads ``stored``."""

    def __init__(self) -> None:
        self.stored: dict[str, str] = {}

    async def store(self, api_key: str, provider_name: str = "openrouter") -> None:
        self.stored[provider_name] = api_key


class FakeProviderClient:
    """from_keychain yields instances returning a scripted result."""

    scripted: Any = object()  # non-ProviderError ⇒ success
    raise_on_build: Exception | None = None

    @classmethod
    async def from_keychain(cls, base_url: str, **kwargs: object) -> FakeProviderClient:
        if cls.raise_on_build is not None:
            raise cls.raise_on_build
        return cls()

    async def chat_completion(self, request: Any) -> Any:
        return self.scripted

    @classmethod
    def reset(cls) -> None:
        cls.scripted = object()
        cls.raise_on_build = None


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Never read the developer's real user config (preset etc.)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    cached_config.cache_clear()


@pytest.fixture
def fakes(monkeypatch: pytest.MonkeyPatch) -> FakeKeychain:
    fk = FakeKeychain()

    async def _store(api_key: str, provider_name: str = "openrouter") -> None:
        await fk.store(api_key, provider_name)

    async def _present(provider_name: str = "openrouter") -> bool:
        return provider_name in fk.stored

    monkeypatch.setattr("tstd.daemon.api_key_is_stored", _present)
    monkeypatch.setattr("tstd.daemon.store_api_key", _store)
    monkeypatch.setattr("tstd.daemon.ProviderClient", FakeProviderClient)
    FakeProviderClient.reset()
    return fk


class TestWithoutWorkspace:
    @pytest.mark.asyncio
    async def test_rows_and_row_order(self, fakes: FakeKeychain) -> None:
        fakes.stored["openrouter"] = "sk-ok"
        with tempfile.TemporaryDirectory() as tmp:
            checks = await _report(Path(tmp))
            assert [c["name"] for c in checks] == [
                "daemon",
                "api_key",
                "provider",
                "git",
                "grok_cli",
                "workspace",
                "steering",
                "skills",
            ]
            # Daemon alive by definition; git and probe-driven rows green.
            assert _row(checks, "daemon")["status"] == "ok"
            assert _row(checks, "api_key")["status"] == "ok"
            assert _row(checks, "provider")["status"] == "ok"
            assert _row(checks, "git")["status"] == "ok"
            assert _row(checks, "grok_cli")["status"] in {"ok", "skip"}
            # No workspace opened: every workspace-scoped row skips.
            assert _row(checks, "workspace")["status"] == "skip"
            assert _row(checks, "steering")["status"] == "skip"
            assert _row(checks, "skills")["status"] == "skip"

    @pytest.mark.asyncio
    async def test_no_key_fails_with_fix(self, fakes: FakeKeychain) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            checks = await _report(Path(tmp))
            key_row = _row(checks, "api_key")
            assert key_row["status"] == "fail"
            assert key_row["fix"]
            assert _row(checks, "provider")["status"] == "skip"

    @pytest.mark.asyncio
    async def test_rejected_key_proves_reachability(self, fakes: FakeKeychain) -> None:
        fakes.stored["openrouter"] = "sk-bad"
        FakeProviderClient.scripted = ProviderError(
            code="auth_failed", message="unauthorized", status_code=401
        )
        with tempfile.TemporaryDirectory() as tmp:
            checks = await _report(Path(tmp))
            key_row = _row(checks, "api_key")
            assert key_row["status"] == "fail"
            assert "401" in key_row["detail"]
            assert key_row["fix"]
            assert _row(checks, "provider")["status"] == "ok"

    @pytest.mark.asyncio
    async def test_unreachable_provider_skips_key_verdict(self, fakes: FakeKeychain) -> None:
        fakes.stored["openrouter"] = "sk-ok"
        FakeProviderClient.scripted = ProviderError(
            code="connection_error", message="connect failed", status_code=None
        )
        with tempfile.TemporaryDirectory() as tmp:
            checks = await _report(Path(tmp))
            prov = _row(checks, "provider")
            assert prov["status"] == "fail"
            assert "base_url" in prov["fix"]
            assert _row(checks, "api_key")["status"] == "skip"


class TestWithWorkspace:
    async def _open_workspace(self, ws: Any, path: str) -> None:
        await ws.send(json.dumps({"type": "open_workspace", "path": path}))
        opened = dict(json.loads(await ws.recv()))
        assert opened["type"] == "session_state", opened

    @pytest.mark.asyncio
    async def test_writable_and_clean_steering(self, fakes: FakeKeychain) -> None:
        fakes.stored["openrouter"] = "sk-ok"
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as work:
            daemon, task = await _start_daemon(Path(tmp))
            try:
                ws = await _connect_and_handshake(
                    f"ws://127.0.0.1:{daemon.ws_server.port}", daemon.ws_server.token
                )
                await self._open_workspace(ws, work)
                await ws.send(json.dumps({"type": "run_diagnostics"}))
                resp = dict(json.loads(await ws.recv()))
                checks = list(resp["checks"])
                workspace = _row(checks, "workspace")
                assert workspace["status"] == "ok"
                # Rows name the workspace by basename only — the absolute
                # path never touches the wire (it's clipboard-bound).
                assert Path(work).name in workspace["detail"]
                assert work not in workspace["detail"]
                steering = _row(checks, "steering")
                assert steering["status"] == "ok"
                await ws.close()
            finally:
                await _stop_daemon(task)

    @pytest.mark.asyncio
    async def test_skills_row_lists_ignored_frontmatter(self, fakes: FakeKeychain) -> None:
        # TD-5107: the skill loads; the row names the keys it carried that
        # TST Desk does not act on, by skill name, never by path.
        fakes.stored["openrouter"] = "sk-ok"
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as work:
            skill = Path(work) / ".claude" / "skills" / "notes" / "SKILL.md"
            skill.parent.mkdir(parents=True)
            skill.write_text(
                "---\nname: notes\ndescription: Notes\nmodel: haiku\n"
                "hooks: {PreToolUse: []}\n---\nbody\n"
            )
            plain = Path(work) / ".claude" / "skills" / "plain" / "SKILL.md"
            plain.parent.mkdir(parents=True)
            plain.write_text("---\ndescription: Plain\n---\nbody\n")
            daemon, task = await _start_daemon(Path(tmp))
            try:
                ws = await _connect_and_handshake(
                    f"ws://127.0.0.1:{daemon.ws_server.port}", daemon.ws_server.token
                )
                await self._open_workspace(ws, work)
                await ws.send(json.dumps({"type": "run_diagnostics"}))
                resp = dict(json.loads(await ws.recv()))
                checks = list(resp["checks"])
                names = [c["name"] for c in checks]
                assert names.index("skills") == names.index("steering") + 1
                skills = _row(checks, "skills")
                assert skills["status"] == "ok"
                assert skills["detail"] == (
                    "2 skill(s) found; frontmatter keys ignored: notes (hooks, model)"
                )
                assert skills.get("fix") is None
                assert work not in skills["detail"]
                await ws.close()
            finally:
                await _stop_daemon(task)

    @pytest.mark.asyncio
    async def test_non_utf8_skill_still_gets_a_report(self, fakes: FakeKeychain) -> None:
        # A Windows-1252 SKILL.md used to raise UnicodeDecodeError out of
        # discovery, closing the connection with 1011 and no report. It is
        # now skipped (and logged) and the good skill is still counted.
        fakes.stored["openrouter"] = "sk-ok"
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as work:
            bad = Path(work) / ".tst" / "skills" / "bad" / "SKILL.md"
            bad.parent.mkdir(parents=True)
            bad.write_bytes(b"---\ndescription: caf\xe9\n---\nbody\n")
            plain = Path(work) / ".tst" / "skills" / "plain" / "SKILL.md"
            plain.parent.mkdir(parents=True)
            plain.write_text("---\ndescription: Plain\n---\nbody\n", encoding="utf-8")
            daemon, task = await _start_daemon(Path(tmp))
            try:
                ws = await _connect_and_handshake(
                    f"ws://127.0.0.1:{daemon.ws_server.port}", daemon.ws_server.token
                )
                await self._open_workspace(ws, work)
                await ws.send(json.dumps({"type": "run_diagnostics"}))
                resp = dict(json.loads(await ws.recv()))
                assert resp["type"] == "diagnostics_report"
                checks = list(resp["checks"])
                skills = _row(checks, "skills")
                assert skills["status"] == "ok"
                assert skills["detail"] == "1 skill(s) found"
                assert _row(checks, "steering")["status"] == "ok"
                await ws.close()
            finally:
                await _stop_daemon(task)

    @pytest.mark.asyncio
    async def test_skills_row_fails_alone_when_discovery_raises(
        self, fakes: FakeKeychain, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Discovery is designed not to raise. If it does with anything, not
        # just OSError, only the skills row fails; the report still arrives.
        async def boom(*_args: object, **_kwargs: object) -> list[object]:
            raise ValueError(f"cannot read {work}/.tst/skills/odd/SKILL.md")

        monkeypatch.setattr("tstd.daemon.list_workspace_skills_async", boom)
        fakes.stored["openrouter"] = "sk-ok"
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as work:
            daemon, task = await _start_daemon(Path(tmp))
            try:
                ws = await _connect_and_handshake(
                    f"ws://127.0.0.1:{daemon.ws_server.port}", daemon.ws_server.token
                )
                await self._open_workspace(ws, work)
                await ws.send(json.dumps({"type": "run_diagnostics"}))
                resp = dict(json.loads(await ws.recv()))
                assert resp["type"] == "diagnostics_report"
                checks = list(resp["checks"])
                skills = _row(checks, "skills")
                assert skills["status"] == "fail"
                assert "skill discovery failed" in skills["detail"]
                assert "odd/SKILL.md" in skills["detail"]
                assert work not in skills["detail"]
                assert skills["fix"]
                assert _row(checks, "steering")["status"] == "ok"
                await ws.close()
            finally:
                await _stop_daemon(task)

    @pytest.mark.asyncio
    async def test_broken_import_fails_steering_with_fix(self, fakes: FakeKeychain) -> None:
        fakes.stored["openrouter"] = "sk-ok"
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as work:
            (Path(work) / "CLAUDE.md").write_text("# Rules\n\n@no-such-file.md\n")
            daemon, task = await _start_daemon(Path(tmp))
            try:
                ws = await _connect_and_handshake(
                    f"ws://127.0.0.1:{daemon.ws_server.port}", daemon.ws_server.token
                )
                await self._open_workspace(ws, work)
                await ws.send(json.dumps({"type": "run_diagnostics"}))
                resp = dict(json.loads(await ws.recv()))
                checks = list(resp["checks"])
                steering = _row(checks, "steering")
                assert steering["status"] == "fail"
                assert "@import" in steering["fix"]
                # The named file stays visible, but the absolute workspace
                # path is relativized off the wire.
                assert "no-such-file.md" in steering["detail"]
                assert work not in steering["detail"]
                assert _row(checks, "workspace")["status"] == "ok"
                await ws.close()
            finally:
                await _stop_daemon(task)

    @pytest.mark.asyncio
    async def test_unapproved_external_import_flags_steering(self, fakes: FakeKeychain) -> None:
        fakes.stored["openrouter"] = "sk-ok"
        with (
            tempfile.TemporaryDirectory() as tmp,
            tempfile.TemporaryDirectory() as work,
            tempfile.TemporaryDirectory() as outside,
        ):
            external = (Path(outside) / "shared.md").resolve()
            external.write_text("shared rules\n")
            (Path(work) / "AGENTS.md").write_text(f"# Rules\n\n@{external}\n")
            daemon, task = await _start_daemon(Path(tmp))
            try:
                ws = await _connect_and_handshake(
                    f"ws://127.0.0.1:{daemon.ws_server.port}", daemon.ws_server.token
                )
                await self._open_workspace(ws, work)
                await ws.send(json.dumps({"type": "run_diagnostics"}))
                resp = dict(json.loads(await ws.recv()))
                steering = _row(list(resp["checks"]), "steering")
                # Fail-closed: an unapproved import outside the workspace
                # reads as an issue, not as inlined content (TD-505).
                assert steering["status"] == "fail"
                assert "awaiting approval" in steering["detail"]
                await ws.close()
            finally:
                await _stop_daemon(task)

    @pytest.mark.asyncio
    async def test_approved_external_import_passes_steering(self, fakes: FakeKeychain) -> None:
        fakes.stored["openrouter"] = "sk-ok"
        with (
            tempfile.TemporaryDirectory() as tmp,
            tempfile.TemporaryDirectory() as work,
            tempfile.TemporaryDirectory() as outside,
        ):
            external = (Path(outside) / "shared.md").resolve()
            external.write_text("shared rules\n")
            (Path(work) / "AGENTS.md").write_text(f"# Rules\n\n@{external}\n")
            daemon, task = await _start_daemon(Path(tmp))
            # Approved through TST Desk, so the write keeps its trust (TD-5113);
            # the same list arriving with a clone would wait for the trust card.
            daemon.workspace_trust.write_through(
                work,
                lambda: save_approved_imports(work, [external]),
                owns=("approved_external_imports",),
            )
            try:
                ws = await _connect_and_handshake(
                    f"ws://127.0.0.1:{daemon.ws_server.port}", daemon.ws_server.token
                )
                await self._open_workspace(ws, work)
                await ws.send(json.dumps({"type": "run_diagnostics"}))
                resp = dict(json.loads(await ws.recv()))
                steering = _row(list(resp["checks"]), "steering")
                # The durable allowlist reaches the inspector: an approved
                # import no longer reads as "awaiting approval".
                assert steering["status"] == "ok", steering
                await ws.close()
            finally:
                await _stop_daemon(task)

    @pytest.mark.asyncio
    async def test_malformed_config_still_reports_steering(self, fakes: FakeKeychain) -> None:
        fakes.stored["openrouter"] = "sk-ok"
        with (
            tempfile.TemporaryDirectory() as tmp,
            tempfile.TemporaryDirectory() as work,
            tempfile.TemporaryDirectory() as outside,
        ):
            external = (Path(outside) / "shared.md").resolve()
            external.write_text("shared rules\n")
            (Path(work) / "AGENTS.md").write_text(f"# Rules\n\n@{external}\n")
            tst = Path(work) / ".tst"
            tst.mkdir()
            (tst / "config.yaml").write_text("policy:\n  rules: [\n")
            daemon, task = await _start_daemon(Path(tmp))
            try:
                ws = await _connect_and_handshake(
                    f"ws://127.0.0.1:{daemon.ws_server.port}", daemon.ws_server.token
                )
                await self._open_workspace(ws, work)
                await ws.send(json.dumps({"type": "run_diagnostics"}))
                resp = dict(json.loads(await ws.recv()))
                # A config the allowlist loader cannot parse must not break
                # the doctor report — it falls back to an empty allowlist
                # and the import reads as pending.
                assert resp["type"] == "diagnostics_report"
                steering = _row(list(resp["checks"]), "steering")
                assert steering["status"] == "fail"
                assert "awaiting approval" in steering["detail"]
                await ws.close()
            finally:
                await _stop_daemon(task)
