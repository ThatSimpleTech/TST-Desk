"""The Settings key test names the key it answers (TD-5200).

Settings → API keys shows a Test verdict on the row of the key that was
tested. Every reply to ``validate_api_key`` carries the request's
``credential``, so the window never has to guess which row asked. The
first-run check of a typed key names no key and gets None back.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests.test_add_model import _ask, _stop
from tests.test_saved_key_host import _session
from tstd.config import ModelDiscoveryError
from tstd.daemon import Daemon
from tstd.keychain import KeychainError
from tstd.protocol import ApiKeyValidated, parse_daemon_event
from tstd.provider import ProviderClient, ProviderError

SPARE = "https://spare.example/v1"
NO_SUCH_MODEL = ProviderError(
    code="http_404", message="The model `m` does not exist", status_code=404
)
REFUSED = ProviderError(code="auth_failed", message="Unauthorized", status_code=401)


class TestWire:
    def test_a_verdict_names_no_key_unless_told(self) -> None:
        bare = ApiKeyValidated(ok=True, detail="Key accepted.")
        assert bare.credential is None
        assert json.loads(bare.model_dump_json())["credential"] is None

    def test_the_named_key_round_trips(self) -> None:
        frame = ApiKeyValidated(ok=False, detail="nope", credential="tower").model_dump_json()
        parsed = parse_daemon_event(frame)
        assert isinstance(parsed, ApiKeyValidated)
        assert parsed.credential == "tower"

    def test_an_older_frame_without_the_field_still_parses(self) -> None:
        frame = '{"type": "api_key_validated", "seq": 1, "ok": true, "detail": "Key accepted."}'
        parsed = parse_daemon_event(frame)
        assert isinstance(parsed, ApiKeyValidated)
        assert parsed.credential is None


def _probe_gives(monkeypatch: pytest.MonkeyPatch, outcome: object) -> None:
    """The live probe answers *outcome* (or raises it); nothing is sent."""

    async def probe(
        self: Daemon,
        api_key: str | None = None,
        credential: str | None = None,
        *,
        probe: Any = None,
    ) -> ProviderError | None:
        if isinstance(outcome, Exception):
            raise outcome
        assert outcome is None or isinstance(outcome, ProviderError)
        return outcome

    monkeypatch.setattr(Daemon, "_provider_probe", probe)


async def _validate(tmp_path: Path, msg: dict[str, Any]) -> dict[str, Any]:
    task, ws = await _session(tmp_path)
    try:
        return await _ask(ws, msg, "api_key_validated")
    finally:
        await _stop(task, ws)


class TestDaemon:
    @pytest.mark.parametrize(
        ("outcome", "ok", "detail"),
        [
            (None, True, f"Key accepted by {SPARE}."),
            (NO_SUCH_MODEL, True, f"Key accepted by {SPARE}. No model uses it yet."),
            (REFUSED, False, "re-enter a valid API key"),
            (KeychainError("API key not found in keychain."), False, "API key not found"),
            (
                ModelDiscoveryError(
                    "No model is set for this server", endpoint=SPARE, fix="Start the server."
                ),
                False,
                "No model is set",
            ),
        ],
        ids=["accepted", "accepted-no-model", "refused", "keychain", "no-model"],
    )
    async def test_every_verdict_on_a_saved_key_names_it(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        outcome: object,
        ok: bool,
        detail: str,
    ) -> None:
        _probe_gives(monkeypatch, outcome)
        reply = await _validate(tmp_path, {"type": "validate_api_key", "credential": "spare"})
        assert reply["type"] == "api_key_validated", reply
        assert reply["credential"] == "spare"
        assert reply["ok"] is ok
        assert detail in reply["detail"]

    async def test_a_key_with_nowhere_to_go_is_named_too(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The refusal path, through the real probe: nothing is read or sent."""
        sent: list[str] = []

        async def from_keychain(base_url: str, **_: Any) -> Any:
            sent.append(base_url)
            raise KeychainError("stopped before sending")

        monkeypatch.setattr(ProviderClient, "from_keychain", from_keychain)
        reply = await _validate(tmp_path, {"type": "validate_api_key", "credential": "local"})
        assert reply["credential"] == "local"
        assert reply["ok"] is False
        assert "No model uses" in reply["detail"]
        assert sent == []

    async def test_the_first_run_check_names_no_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _probe_gives(monkeypatch, None)
        reply = await _validate(tmp_path, {"type": "validate_api_key", "api_key": "sk-typed"})
        assert reply["type"] == "api_key_validated", reply
        assert reply["ok"] is True
        assert reply["credential"] is None
