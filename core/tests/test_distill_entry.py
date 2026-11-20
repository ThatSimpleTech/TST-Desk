"""Memory distill runs on the chat's own model (TD-5259).

Distill used to take the daemon's active entry whatever the chat was on,
so a chat kept on a local model had its conversation sent to a hosted one
for distilling, or the other way round. These check which entry is asked,
and which model server.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.test_add_model import _ask, _stop
from tests.test_preset_engine import _seeded, _text
from tstd.config import ModelConfig, TierConfig
from tstd.daemon import Daemon
from tstd.session import Session

Asked = list[tuple[str, str, str]]


def _watch(daemon: Daemon, monkeypatch: pytest.MonkeyPatch) -> Asked:
    """Each distill as ``(entry, the base_url its client was made for, worker slug)``."""
    seen: Asked = []
    made_for: list[str] = []
    real = daemon._client_for

    async def client_for(tier: TierConfig) -> Any:
        made_for.append(tier.base_url)
        return await real(tier)

    async def fake_distill(session: Session, provider: Any, config: ModelConfig) -> None:
        seen.append((config.active_preset, made_for[-1], config.tier("worker").slug))
        return None

    monkeypatch.setattr(daemon, "_client_for", client_for)
    monkeypatch.setattr("tstd.daemon.distill_if_due", fake_distill)
    monkeypatch.setattr("tstd.daemon.completed_turn_count", lambda _s: 1)
    return seen


async def _chat(daemon: Daemon, ws: Any, workspace: Path) -> Session:
    opened = await _ask(ws, {"type": "open_workspace", "path": str(workspace)}, "session_state")
    sess = daemon.session_registry.get(opened["session_id"])
    assert sess is not None
    return sess


def _where(daemon: Daemon, entry: str) -> tuple[str, str, str]:
    preset = daemon.config.presets[entry]
    assert preset.brain is not None and preset.worker is not None
    return entry, preset.brain.base_url, preset.worker.slug


async def test_a_chat_on_another_entry_is_distilled_by_that_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    daemon, task, ws, workspace = await _seeded(tmp_path, monkeypatch)
    try:
        seen = _watch(daemon, monkeypatch)
        sess = await _chat(daemon, ws, workspace)
        assert daemon.config.active_preset == "tst-default"
        daemon._bind_session_preset(sess, "local")
        await daemon._run_distill(sess)
        # The local entry's own server, not the active entry's.
        assert seen == [_where(daemon, "local")]
        assert _where(daemon, "local")[1] != _where(daemon, "tst-default")[1]
    finally:
        await _stop(task, ws)


async def test_a_chat_on_the_active_entry_is_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    daemon, task, ws, workspace = await _seeded(tmp_path, monkeypatch)
    try:
        seen = _watch(daemon, monkeypatch)
        sess = await _chat(daemon, ws, workspace)
        await daemon._run_distill(sess)
        assert seen == [_where(daemon, "tst-default")]
    finally:
        await _stop(task, ws)


async def test_it_follows_the_entry_the_chat_is_on_now(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    daemon, task, ws, workspace = await _seeded(tmp_path, monkeypatch)
    try:
        seen = _watch(daemon, monkeypatch)
        sess = await _chat(daemon, ws, workspace)
        daemon._bind_session_preset(sess, "local")
        await daemon._run_distill(sess)
        daemon._bind_session_preset(sess, "budget")
        await daemon._run_distill(sess)
        assert seen == [_where(daemon, "local"), _where(daemon, "budget")]
    finally:
        await _stop(task, ws)


async def test_the_active_entry_changing_does_not_move_another_chats_distill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    daemon, task, ws, workspace = await _seeded(tmp_path, monkeypatch)
    try:
        seen = _watch(daemon, monkeypatch)
        sess = await _chat(daemon, ws, workspace)
        daemon._bind_session_preset(sess, "local")
        await _ask(ws, {"type": "set_preset", "name": "budget"}, "setup_state")
        assert daemon.config.active_preset == "budget"
        await daemon._run_distill(sess)
        assert seen == [_where(daemon, "local")]
    finally:
        await _stop(task, ws)


async def test_a_grok_chat_keeps_the_active_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Grok engine has no model of its own here: the TD-5239 rule stands."""
    daemon, task, ws, workspace = await _seeded(tmp_path, monkeypatch)
    try:
        seen = _watch(daemon, monkeypatch)
        sess = await _chat(daemon, ws, workspace)
        # On the Grok entry, and also a Grok chat that names an entry with a model.
        daemon._bind_session_preset(sess, "grok")
        sess.engine = "grok"
        await daemon._run_distill(sess)
        daemon._bind_session_preset(sess, "local")
        sess.engine = "grok"
        await daemon._run_distill(sess)
        assert seen == [_where(daemon, "tst-default")] * 2
    finally:
        await _stop(task, ws)


async def test_a_chat_whose_entry_was_removed_falls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    daemon, task, ws, workspace = await _seeded(tmp_path, monkeypatch)
    try:
        seen = _watch(daemon, monkeypatch)
        sess = await _chat(daemon, ws, workspace)
        daemon._bind_session_preset(sess, "vllm")
        gone = await _ask(ws, {"type": "delete_preset", "name": "vllm"}, "setup_state")
        assert gone["type"] == "setup_state", gone
        assert "vllm" not in daemon.config.presets
        sess.preset = "vllm"
        await daemon._run_distill(sess)
        assert seen == [_where(daemon, "tst-default")]
    finally:
        await _stop(task, ws)


async def test_no_model_anywhere_is_still_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    only_grok = "presets:\n  grok:\n    engine: grok\nactive_preset: grok\n"
    assert "tst-default" in _text()
    daemon, task, ws, workspace = await _seeded(tmp_path, monkeypatch, only_grok)
    try:
        seen = _watch(daemon, monkeypatch)
        sess = await _chat(daemon, ws, workspace)
        await daemon._run_distill(sess)
        assert seen == []
    finally:
        await _stop(task, ws)
