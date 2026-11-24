"""What no phone permission opens (owner decision #5, TD-5301).

Checked against a running daemon before the change: a paired phone could
point the mail server elsewhere with no grant and no password, so the
saved password went to the new server on the next send; with "Adding
models and keys" on it could move a saved key's host; and it could shut
the daemon down. Each is now refused from a phone with any grant, and
stays the computer's own.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from tests.test_add_model import _stop
from tests.test_mcp_peer import _drain, _fresh, _pair
from tests.test_models_on_phone import _seeded
from tests.test_peer_policy import (
    LOOPBACK_PLAIN,
    PHONE_PLAIN,
    PHONE_VIEWER,
    PLAIN_REMOTE,
    _code,
    _msg,
)
from tests.test_preset_engine import _config
from tstd.config import CredentialConfig, ModelConfig
from tstd.peer_policy import check
from tstd.phone_never import COMPUTER_ONLY, KEY_ELSEWHERE, MAIL_PASSWORD, never_refusal

EVERY_GRANT = frozenset(
    {"mcp_servers", "approval_switches", "models_and_keys", "computer_use", "unattended_runs"}
)
GRANT_KEYS = {"type": "set_phone_permission", "capability": "models_and_keys", "enabled": True}


def _with_mail(config: ModelConfig, host: str = "smtp.first.example") -> ModelConfig:
    config.notify.email.host = host
    config.notify.email.username = "me"
    return config


class TestTheRules:
    def test_shutdown_is_the_computers(self) -> None:
        msg = _msg("shutdown")
        for phone in (PHONE_VIEWER, PHONE_PLAIN, PLAIN_REMOTE):
            assert _code(check(msg, phone, bridged=False, grants=EVERY_GRANT)) == "loopback_only"
        assert check(msg, LOOPBACK_PLAIN, bridged=False) is None

    def test_releasing_the_stop_stays_refused(self, tmp_path: Path) -> None:
        config = _config(tmp_path)
        assert never_refusal(_msg("set_cu_kill", killed=False), config) == COMPUTER_ONLY
        assert never_refusal(_msg("set_cu_kill", killed=True), config) is None

    @pytest.mark.parametrize(
        ("fields", "refused"),
        [
            ({"host": "smtp.elsewhere.example"}, True),
            ({"host": "SMTP.First.Example "}, False),
            ({"host": "smtp.first.example", "port": 465, "security": "tls"}, False),
            ({"host": "smtp.elsewhere.example", "password": "typed"}, False),
            ({"host": "smtp.elsewhere.example", "method": "microsoft"}, False),
            ({"host": "", "username": "someone-else"}, True),
        ],
    )
    def test_a_new_mail_server_needs_the_password(
        self, tmp_path: Path, fields: dict[str, Any], refused: bool
    ) -> None:
        config = _with_mail(_config(tmp_path))
        msg = _msg("set_email_notify", **{"enabled": True, "username": "me", **fields})
        assert never_refusal(msg, config) == (MAIL_PASSWORD if refused else None)

    @pytest.mark.parametrize(
        ("base_url", "refused"),
        [
            ("https://elsewhere.example/v1", True),
            ("", True),  # cleared: the key would go to each bound model's own URL
            ("https://openrouter.ai/api/v1/", False),
            ("https://OPENROUTER.ai/other/path", False),
            (None, False),  # a rename
        ],
    )
    def test_a_saved_keys_host(self, tmp_path: Path, base_url: str | None, refused: bool) -> None:
        config = _config(tmp_path)
        # A model bound to the key whose own URL is another server.
        config.presets["budget"].brain.base_url = "https://models.example.com/v1"
        fields: dict[str, Any] = {"name": "OpenRouter", "credential": "openrouter"}
        if base_url is not None:
            fields["base_url"] = base_url
        assert never_refusal(_msg("set_credential", **fields), config) == (
            KEY_ELSEWHERE if refused else None
        )

    def test_a_new_key_has_nothing_to_move(self, tmp_path: Path) -> None:
        config = _config(tmp_path)
        msg = _msg("set_credential", name="Fresh", base_url="https://elsewhere.example/v1")
        assert never_refusal(msg, config) is None

    def test_binding_a_key_without_a_host(self, tmp_path: Path) -> None:
        config = _config(tmp_path)
        config.credentials["lab"] = CredentialConfig(name="Lab")
        config.presets["local"].brain.credential = "lab"
        config.presets["budget"].brain.credential = None

        def bind(preset: str) -> str | None:
            msg = _msg("set_tier_credential", preset=preset, tier="brain", credential="lab")
            return never_refusal(msg, config)

        assert bind("local") is None  # where it already goes
        assert bind("vllm") is None  # another server on this computer
        assert bind("budget") == KEY_ELSEWHERE  # a hosted server it never went to
        unbind = _msg("set_tier_credential", preset="budget", tier="brain", credential="")
        assert never_refusal(unbind, config) is None

    def test_a_key_with_a_host_goes_there_whatever_it_is_bound_to(self, tmp_path: Path) -> None:
        config = _config(tmp_path)
        msg = _msg("set_tier_credential", preset="local", tier="brain", credential="openrouter")
        assert never_refusal(msg, config) is None


async def _ask_phone(phone: Any, msg: dict[str, Any]) -> dict[str, Any]:
    await _drain(phone)
    await phone.send(json.dumps(msg))
    return dict(json.loads(await asyncio.wait_for(phone.recv(), 5)))


class TestOverTheWire:
    async def test_the_mail_server_and_a_keys_host_stay_put(self, tmp_path: Path) -> None:
        daemon, task, ws = await _seeded(tmp_path)
        phone, _ = await _pair(daemon)
        first = {"type": "set_email_notify", "enabled": True, "host": "smtp.first.example"}
        moved = {**first, "host": "smtp.elsewhere.example"}
        key = {"type": "set_credential", "name": "OpenRouter", "credential": "openrouter"}
        try:
            await _fresh(ws, {**first, "username": "me"})
            reply = await _ask_phone(phone, {**moved, "username": "me"})
            assert (reply["type"], reply["code"]) == ("error", "loopback_only"), reply
            assert reply["message"] == MAIL_PASSWORD
            assert daemon.config.notify.email.host == "smtp.first.example"

            await _fresh(ws, GRANT_KEYS)
            reply = await _ask_phone(phone, {**key, "base_url": "https://elsewhere.example/v1"})
            assert (reply["type"], reply["message"]) == ("error", KEY_ELSEWHERE), reply
            assert (
                daemon.config.credentials["openrouter"].base_url == "https://openrouter.ai/api/v1"
            )
            # A rename with the grant still works.
            done = await _fresh(phone, {**key, "name": "Router"})
            assert done["type"] == "setup_state", done
            assert daemon.config.credentials["openrouter"].name == "Router"
            # The computer itself does both.
            await _fresh(ws, {**key, "base_url": "https://elsewhere.example/v1"})
            assert (
                daemon.config.credentials["openrouter"].base_url == "https://elsewhere.example/v1"
            )
            await _fresh(ws, {**moved, "username": "me"})
            assert daemon.config.notify.email.host == "smtp.elsewhere.example"
        finally:
            await phone.close()
            await _stop(task, ws)

    async def test_typing_the_password_moves_the_mail_server(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stored: list[str] = []

        async def store(password: str) -> None:
            stored.append(password)

        monkeypatch.setattr("tstd.keychain.store_smtp_password", store)
        daemon, task, ws = await _seeded(tmp_path)
        phone, _ = await _pair(daemon)
        try:
            change = {
                "type": "set_email_notify",
                "enabled": True,
                "host": "smtp.elsewhere.example",
                "username": "me",
                "password": "typed-on-the-phone",
            }
            done = await _fresh(phone, change)
            assert done["type"] == "setup_state", done
            assert daemon.config.notify.email.host == "smtp.elsewhere.example"
            assert stored == ["typed-on-the-phone"]
        finally:
            await phone.close()
            await _stop(task, ws)

    async def test_a_phone_cannot_shut_the_daemon_down(self, tmp_path: Path) -> None:
        daemon, task, ws = await _seeded(tmp_path)
        phone, _ = await _pair(daemon)
        try:
            reply = await _ask_phone(phone, {"type": "shutdown"})
            assert (reply["type"], reply["code"]) == ("error", "loopback_only"), reply
            await asyncio.sleep(0.2)
            assert not task.done()
        finally:
            await phone.close()
            await _stop(task, ws)
