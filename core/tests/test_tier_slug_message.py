"""Naming a tier's model from the settings screen (TD-1703).

Two things are under test here, and neither is the file format — that lives
in ``test_config_slug_write.py``. This is the wire contract: that
``set_tier_slug`` reaches the writer and acks with a refreshed
``setup_state``, and that the slugs the ack reports are the ones the *file*
has rather than the ones discovery filled in.

That second one carries the weight. ``resolve_tier_slugs`` fills unset slugs
in place, so a loopback tier that was deliberately left to the endpoint comes
back from a probe holding a concrete tag. If the settings screen showed that,
the field would look configured and one save would pin a model the user meant
to leave floating (TD-1805).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from tstd.config import ModelConfig, default_config_yaml
from tstd.daemon import Daemon
from tstd.discovery import resolve_tier_slugs

_TAG = "qwen3.8:27b"


def _config_with_preset(name: str) -> ModelConfig:
    """The shipped config forced onto *name*, read through the real loader."""
    config = ModelConfig.model_validate(yaml.safe_load(default_config_yaml()))
    return config.model_copy(update={"active_preset": name})


@pytest.fixture
def writes(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, str]]:
    """Record slug writes instead of touching the developer's real config.

    The handler calls ``save_tier_slug`` with no path, which resolves to
    ``user_data_dir()/config.yaml`` — a real file on the machine running the
    suite. Recording here keeps these runs hermetic; whether the write itself
    is correct is settled next door (TD-1809's lesson).
    """
    recorded: list[tuple[str, str, str]] = []

    def _spy(preset: str, tier: str, slug: str, path: Path | None = None) -> Path:
        recorded.append((preset, tier, slug))
        return Path("/does/not/matter")

    monkeypatch.setattr("tstd.daemon.save_tier_slug", _spy)
    return recorded


def _daemon(monkeypatch: pytest.MonkeyPatch, data_dir: Path, preset: str) -> Daemon:
    monkeypatch.setattr("tstd.daemon.cached_config", lambda: _config_with_preset(preset))
    return Daemon(data_dir=data_dir)


async def _send(daemon: Daemon, payload: dict[str, Any]) -> dict[str, Any]:
    raw = await daemon._handle_message(json.dumps(payload), None)
    assert raw is not None, f"no reply to {payload['type']}"
    parsed: dict[str, Any] = json.loads(raw)
    return parsed


class TestTheAck:
    async def test_a_slug_reaches_the_writer(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, writes: list[tuple[str, str, str]]
    ) -> None:
        daemon = _daemon(monkeypatch, tmp_path, "local")
        monkeypatch.setattr("tstd.daemon.load_config", lambda: _config_with_preset("local"))

        reply = await _send(
            daemon,
            {"type": "set_tier_slug", "preset": "local", "tier": "brain", "slug": _TAG},
        )

        assert writes == [("local", "brain", _TAG)]
        assert reply["type"] == "setup_state"

    async def test_the_ack_carries_the_reloaded_slugs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, writes: list[tuple[str, str, str]]
    ) -> None:
        """The reply reflects the config the daemon adopted after the write."""
        daemon = _daemon(monkeypatch, tmp_path, "local")
        saved = _config_with_preset("local")
        saved.presets["local"].brain.slug = _TAG
        monkeypatch.setattr("tstd.daemon.load_config", lambda: saved)

        reply = await _send(
            daemon,
            {"type": "set_tier_slug", "preset": "local", "tier": "brain", "slug": _TAG},
        )

        assert reply["tier_slugs"]["brain"] == _TAG

    async def test_setup_state_reports_every_tier(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        daemon = _daemon(monkeypatch, tmp_path, "tst-default")
        event = await daemon._setup_state_event()

        assert set(event.tier_slugs) == {"brain", "worker", "validator"}
        # tst-default is off-box, so every tier names its model in the file.
        assert all(slug for slug in event.tier_slugs.values())


class TestConfiguredNotResolved:
    async def test_discovery_does_not_leak_into_the_reported_slugs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The story's point: a discovered tag must not look configured."""
        daemon = _daemon(monkeypatch, tmp_path, "local")
        assert daemon.config.tier("brain").slug is None, "fixture: local leaves slugs unset"

        # What a probe does — fill the unset slugs in place.
        async def _fake_discover(*_a: Any, **_k: Any) -> str:
            return "whatever-the-endpoint-served"

        monkeypatch.setattr("tstd.discovery.discover_model", _fake_discover)
        await resolve_tier_slugs(daemon.config)
        assert daemon.config.tier("brain").slug == "whatever-the-endpoint-served"

        event = await daemon._setup_state_event()

        assert event.tier_slugs["brain"] is None, (
            "a discovered tag was reported as the configured slug; "
            "saving it would pin a model the user left to the endpoint"
        )

    async def test_a_model_copy_does_not_defeat_the_snapshot(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``model_copy`` is shallow, so a copied config shares tier objects.

        Pinning this because the obvious implementation — keep a copy of the
        config and read slugs off it later — silently does not work.
        """
        daemon = _daemon(monkeypatch, tmp_path, "local")
        shallow = daemon.config.model_copy()

        daemon.config.tier("brain").slug = "mutated-after-the-copy"

        assert shallow.tier("brain").slug == "mutated-after-the-copy"
        event = await daemon._setup_state_event()
        assert event.tier_slugs["brain"] is None


class TestRefusals:
    @pytest.mark.parametrize(
        ("field", "value"),
        [("preset", ""), ("tier", ""), ("slug", "")],
    )
    async def test_a_blank_field_is_refused_at_the_wire(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        writes: list[tuple[str, str, str]],
        field: str,
        value: str,
    ) -> None:
        daemon = _daemon(monkeypatch, tmp_path, "local")
        payload = {"type": "set_tier_slug", "preset": "local", "tier": "brain", "slug": _TAG}
        payload[field] = value

        reply = await _send(daemon, payload)

        assert reply["type"] == "error"
        assert writes == [], "a malformed message must not reach the writer"

    async def test_a_writer_refusal_becomes_a_bad_request(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ConfigError is the writer's vocabulary; the wire's is an error event."""
        from tstd.config import ConfigError

        def _refuse(*_a: Any, **_k: Any) -> Path:
            raise ConfigError("Unknown preset 'nope'")

        monkeypatch.setattr("tstd.daemon.save_tier_slug", _refuse)
        daemon = _daemon(monkeypatch, tmp_path, "local")

        reply = await _send(
            daemon,
            {"type": "set_tier_slug", "preset": "nope", "tier": "brain", "slug": _TAG},
        )

        assert reply["type"] == "error"
        assert "Unknown preset" in json.dumps(reply)
