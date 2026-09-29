"""Pin a preset and an engine on a scheduled job (TD-3812).

The job stores a catalog name and an engine kind. A fire uses that pair
and leaves the window's active preset and engine where Settings put them.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import pytest

from tests.test_loop import make_config
from tstd.config import EngineConfig, ModelConfig, Preset, TierConfig
from tstd.daemon import Daemon
from tstd.grok_acp import GrokEngineError
from tstd.mock import MockProvider, Script
from tstd.protocol import JobList, TurnComplete, parse_daemon_event
from tstd.scheduler.history import list_runs
from tstd.scheduler.models import Job
from tstd.scheduler.pin import (
    ScheduledPin,
    bind_scheduled_pin,
    current_scheduled_pin,
    pin_failure_summary,
    reset_scheduled_pin,
)
from tstd.scheduler.runner import run_manual_job
from tstd.scheduler.store import jobs_path, list_jobs, save_job
from tstd.session import Session

_NOW = datetime(2026, 8, 21, 15, 0, tzinfo=UTC)
_DUE = "2026-08-21T12:00:00+00:00"
_SECRET = "sk-abcdefghijklmnopqrst"
_URL = "https://example.com/v1"
_UNKNOWN = "Preset: 'nope' is not in the catalog"
_NOT_A_NAME = "Preset: must be a catalog name"
_SECRET_MSG = "Preset: must not contain secrets"
_BAD_ENGINE = "Engine: must be native or grok"
_GONE = "preset 'retired' no longer exists"
_NO_GROK = "grok engine is unavailable"

# (id, extra save fields, stored fields or the exact error sentence).
_CREATES: list[tuple[str, dict[str, Any], dict[str, Any] | str]] = [
    ("pinned", {"preset": "vllm", "engine": "native"}, {"preset": "vllm", "engine": "native"}),
    ("cased", {"preset": " vllm ", "engine": " Grok "}, {"preset": "vllm", "engine": "grok"}),
    ("omitted", {}, {"preset": None, "engine": None}),
    ("blank", {"preset": "", "engine": ""}, {"preset": None, "engine": None}),
    ("unknown", {"preset": "nope"}, _UNKNOWN),
    ("url", {"preset": _URL}, _NOT_A_NAME),
    ("secret", {"preset": _SECRET}, _SECRET_MSG),
    ("bad-engine", {"engine": "openai"}, _BAD_ENGINE),
]

# The base row is preset vllm, engine grok. Omitted keeps; "" clears.
_EDITS: list[tuple[str, dict[str, Any], dict[str, Any] | str]] = [
    ("keep", {"instruction": "count the mail"}, {"preset": "vllm", "engine": "grok"}),
    ("clear", {"preset": "", "engine": ""}, {"preset": None, "engine": None}),
    (
        "change",
        {"preset": "budget", "engine": "native"},
        {"preset": "budget", "engine": "native"},
    ),
    ("preset-only", {"preset": "budget"}, {"preset": "budget", "engine": "grok"}),
    ("unknown", {"preset": "nope"}, _UNKNOWN),
    ("secret", {"preset": _SECRET}, _SECRET_MSG),
    ("bad-engine", {"engine": "openai"}, _BAD_ENGINE),
]

_FAILURES: list[tuple[str, str | None, str, bool, str]] = [
    ("gone", "retired", "native", True, _GONE),
    ("both", "retired", "grok", False, _GONE),
    ("no-grok", "vllm", "grok", False, _NO_GROK),
]


def _tier(slug: str) -> TierConfig:
    return make_config().presets["test"].brain.model_copy(update={"slug": slug})


def _named(prefix: str) -> Preset:
    return Preset(
        brain=_tier(f"{prefix}-brain"),
        worker=_tier(f"{prefix}-worker"),
        validator=_tier(f"{prefix}-validator"),
    )


def _config(*, engine: Literal["native", "grok"] = "native") -> ModelConfig:
    base = make_config()
    return base.model_copy(
        update={
            "presets": {
                "test": base.presets["test"],
                "vllm": _named("vllm"),
                "budget": _named("budget"),
            },
            "active_preset": "test",
            "engine": EngineConfig(kind=engine),
        }
    )


def _job(workspace: Path, job_id: str = "inbox", **over: Any) -> Job:
    fields: dict[str, Any] = {
        "id": job_id,
        "workspace": str(workspace),
        "instruction": "summarize the inbox",
        "cadence": "every 1 hour",
        "next_run": _DUE,
        "deliver_to": "slack",
    }
    fields.update(over)
    return Job(**fields)


def _workspace(root: Path) -> Path:
    path = root / "ws"
    path.mkdir()
    return path


async def _handle(daemon: Daemon, payload: dict[str, Any]) -> dict[str, Any]:
    raw = await daemon._handle_message(json.dumps(payload), None)
    assert raw is not None
    parsed: dict[str, Any] = json.loads(raw)
    return parsed


def _create_payload(workspace: Path, job_id: str, extra: dict[str, Any]) -> dict[str, Any]:
    body: dict[str, Any] = {
        "type": "save_job",
        "id": job_id,
        "workspace": str(workspace),
        "instruction": "summarize the inbox",
        "cadence": "every 1 hour",
        "deliver_to": "window",
    }
    body.update(extra)
    return body


async def _grok_stub(session: Session, **_kwargs: object) -> None:
    """Finish at once. A missed pin must not wait out the turn timeout."""
    await session.event_log.add(
        TurnComplete(
            session_id=session.id,
            tokens=0,
            cost=0.0,
            tier="brain",
            duration=0.0,
            failed=True,
            error_code="grok_stub",
        )
    )


def _install_guards(monkeypatch: pytest.MonkeyPatch, *, grok_found: bool) -> list[str]:
    saved: list[str] = []
    monkeypatch.setattr("tstd.daemon.grok_loop", _grok_stub)
    monkeypatch.setattr(
        "tstd.daemon.save_active_preset",
        lambda *_a, **_k: saved.append("preset"),
    )
    monkeypatch.setattr(
        "tstd.daemon.save_engine_kind",
        lambda *_a, **_k: saved.append("engine"),
    )

    def _probe(_configured: str = "") -> Path:
        if grok_found:
            return Path("/usr/bin/grok")
        raise GrokEngineError("grok_not_found", "Grok CLI not found at /tmp/secret-grok")

    monkeypatch.setattr("tstd.grok_acp.find_grok_binary", _probe)
    return saved


def _assert_window_untouched(daemon: Daemon, saved: list[str], *, kind: str) -> None:
    assert daemon.config.active_preset == "test"
    assert daemon.config.engine.kind == kind
    assert saved == []


@pytest.mark.parametrize(
    ("preset", "engine", "grok_ok", "summary"),
    [
        (None, None, False, None),
        ("vllm", "native", False, None),
        ("retired", "grok", False, _GONE),
        ("vllm", "grok", False, _NO_GROK),
        ("vllm", "grok", True, None),
    ],
)
def test_pin_failure_summary(
    preset: str | None,
    engine: Literal["native", "grok"] | None,
    grok_ok: bool,
    summary: str | None,
) -> None:
    catalog = {"vllm": object(), "test": object()}
    got = pin_failure_summary(
        ScheduledPin(preset=preset, engine=engine),
        catalog=catalog,
        grok_available=grok_ok,
    )
    assert got == summary


def test_a_bound_pin_does_not_leak_to_the_next_job() -> None:
    job = _job(Path("/tmp/ws-unused"), preset="vllm", engine="native")
    token = bind_scheduled_pin(job)
    assert current_scheduled_pin().preset == "vllm"
    assert current_scheduled_pin().engine == "native"
    reset_scheduled_pin(token)
    assert current_scheduled_pin().preset is None
    assert current_scheduled_pin().engine is None


def test_a_job_saved_before_pins_still_loads(tmp_path: Path) -> None:
    data = tmp_path / "data"
    workspace = _workspace(tmp_path)
    raw = _job(workspace, job_id="legacy").model_dump()
    raw.pop("preset")
    raw.pop("engine")
    path = jobs_path(data)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"version": 1, "jobs": [raw]}), encoding="utf-8")
    loaded = list_jobs(data)
    assert len(loaded) == 1
    assert loaded[0].preset is None
    assert loaded[0].engine is None
    assert loaded[0].instruction == "summarize the inbox"


async def test_save_validates_the_preset_and_stores_only_the_name(tmp_path: Path) -> None:
    data = tmp_path / "data"
    workspace = _workspace(tmp_path)
    daemon = Daemon(data_dir=data)
    daemon.config = _config()
    try:
        for job_id, extra, expect in _CREATES:
            raw = await _handle(daemon, _create_payload(workspace, job_id, extra))
            stored = next((job for job in list_jobs(data) if job.id == job_id), None)
            if isinstance(expect, str):
                assert raw["type"] == "error", job_id
                assert raw["code"] == "job_invalid"
                assert raw["message"] == expect
                assert "pydantic" not in raw["message"]
                assert _URL not in raw["message"]
                assert _SECRET not in raw["message"]
                assert stored is None
                continue
            assert raw["type"] == "job_list", job_id
            assert stored is not None
            for key, value in expect.items():
                assert getattr(stored, key) == value, job_id
        text = jobs_path(data).read_text(encoding="utf-8")
        assert '"preset": "vllm"' in text
        assert "vllm-brain" not in text
        assert "mock.local" not in text
        assert "http" not in text
        assert _SECRET not in text
        assert "example.com" not in text
    finally:
        await daemon._shutdown()


async def test_edit_keeps_or_clears_the_pin(tmp_path: Path) -> None:
    data = tmp_path / "data"
    workspace = _workspace(tmp_path)
    for job_id, _patch, _expect in _EDITS:
        save_job(data, _job(workspace, job_id, preset="vllm", engine="grok", deliver_to="window"))
    daemon = Daemon(data_dir=data)
    daemon.config = _config()
    try:
        for job_id, patch, expect in _EDITS:
            raw = await _handle(daemon, {"type": "save_job", "id": job_id, **patch})
            stored = next(job for job in list_jobs(data) if job.id == job_id)
            if isinstance(expect, str):
                assert raw["type"] == "error", job_id
                assert raw["code"] == "job_invalid"
                assert raw["message"] == expect
                assert _SECRET not in raw["message"]
                assert "pydantic" not in raw["message"]
                assert stored.preset == "vllm"
                assert stored.engine == "grok"
                continue
            listed = parse_daemon_event(json.dumps(raw))
            assert isinstance(listed, JobList)
            row = next(job for job in listed.jobs if job.id == job_id)
            for key, value in expect.items():
                assert getattr(row, key) == value, job_id
                assert getattr(stored, key) == value, job_id
    finally:
        await daemon._shutdown()


async def test_pause_keeps_a_preset_the_catalog_lost(tmp_path: Path) -> None:
    data = tmp_path / "data"
    workspace = _workspace(tmp_path)
    save_job(data, _job(workspace, preset="vllm", engine="grok", deliver_to="window"))
    daemon = Daemon(data_dir=data)
    daemon.config = _config()
    kept = {name: preset for name, preset in daemon.config.presets.items() if name != "vllm"}
    daemon.config = daemon.config.model_copy(update={"presets": kept})
    try:
        resent = await _handle(daemon, {"type": "save_job", "id": "inbox", "preset": "vllm"})
        assert resent["type"] == "error"
        assert resent["code"] == "job_invalid"
        assert resent["message"] == "Preset: 'vllm' is not in the catalog"
        stored = list_jobs(data)[0]
        assert stored.preset == "vllm"
        assert stored.paused is False

        paused = await _handle(daemon, {"type": "save_job", "id": "inbox", "paused": True})
        listed = parse_daemon_event(json.dumps(paused))
        assert isinstance(listed, JobList)
        assert listed.jobs[0].paused is True
        assert listed.jobs[0].preset == "vllm"
        assert listed.jobs[0].engine == "grok"
        assert list_jobs(data)[0].paused is True
    finally:
        await daemon._shutdown()


async def _fire(daemon: Daemon, *, manual: bool) -> None:
    if manual:
        job = list_jobs(daemon.data_dir)[0]
        await run_manual_job(
            daemon.data_dir,
            job,
            _NOW,
            run_turn=daemon._scheduled_run_turn,
            deliver=daemon._scheduler_deliver,
        )
        return
    await daemon.run_due_jobs(_NOW)


@pytest.mark.parametrize("manual", [False, True])
async def test_a_pinned_run_uses_the_job_model_and_leaves_the_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, manual: bool
) -> None:
    data = tmp_path / "data"
    workspace = _workspace(tmp_path)
    save_job(data, _job(workspace, preset="vllm", engine="native"))
    mock = MockProvider(default=Script(kind="stream", content="digest ready"))
    sent: list[tuple[str, str]] = []

    async def notify(channel: str, summary: str) -> None:
        sent.append((channel, summary))

    daemon = Daemon(data_dir=data, provider=mock, notify_send=notify)  # type: ignore[arg-type]
    daemon.config = _config(engine="grok")
    saved = _install_guards(monkeypatch, grok_found=True)
    try:
        await _fire(daemon, manual=manual)
        stored = list_jobs(data)[0]
        assert stored.last_status == "ok"
        assert stored.last_summary == "digest ready"
        assert stored.preset == "vllm"
        assert stored.engine == "native"
        if manual:
            assert stored.next_run == _DUE
        else:
            assert stored.next_run == "2026-08-21T16:00:00+00:00"
        assert stored.last_session_id
        session = daemon.session_registry.get(stored.last_session_id)
        assert session is not None
        assert session.preset == "vllm"
        assert session.engine == "native"
        models = [call.model for call in mock.calls]
        assert "vllm-brain" in models
        assert "test-brain" not in models
        _assert_window_untouched(daemon, saved, kind="grok")
        assert sent == [("slack", "digest ready")]
        text = jobs_path(data).read_text(encoding="utf-8")
        assert "vllm-brain" not in text
        assert "mock.local" not in text
    finally:
        await daemon._shutdown()


async def test_an_unpinned_run_uses_the_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = tmp_path / "data"
    workspace = _workspace(tmp_path)
    save_job(data, _job(workspace, deliver_to="window"))
    mock = MockProvider(default=Script(kind="stream", content="digest ready"))
    daemon = Daemon(data_dir=data, provider=mock)
    daemon.config = _config(engine="native")
    saved = _install_guards(monkeypatch, grok_found=False)
    try:
        await daemon.run_due_jobs(_NOW)
        stored = list_jobs(data)[0]
        assert stored.last_status == "ok"
        assert stored.preset is None
        assert stored.engine is None
        assert stored.last_session_id
        session = daemon.session_registry.get(stored.last_session_id)
        assert session is not None
        assert session.preset == "test"
        assert session.engine == "native"
        models = [call.model for call in mock.calls]
        assert "test-brain" in models
        assert "vllm-brain" not in models
        _assert_window_untouched(daemon, saved, kind="native")
    finally:
        await daemon._shutdown()


@pytest.mark.parametrize(("job_id", "preset", "engine", "grok_found", "summary"), _FAILURES)
async def test_a_pin_that_cannot_start_is_a_failed_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    job_id: str,
    preset: str | None,
    engine: str | None,
    grok_found: bool,
    summary: str,
) -> None:
    data = tmp_path / "data"
    workspace = _workspace(tmp_path)
    save_job(data, _job(workspace, job_id, preset=preset, engine=engine))
    mock = MockProvider(default=Script(kind="stream", content="digest ready"))
    sent: list[tuple[str, str]] = []

    async def notify(channel: str, summary_text: str) -> None:
        sent.append((channel, summary_text))

    daemon = Daemon(data_dir=data, provider=mock, notify_send=notify)  # type: ignore[arg-type]
    daemon.config = _config(engine="grok")
    saved = _install_guards(monkeypatch, grok_found=grok_found)
    try:
        await daemon.run_due_jobs(_NOW)
        stored = list_jobs(data)[0]
        assert stored.last_status == "failed"
        assert stored.last_summary == summary
        assert stored.preset == preset
        assert stored.engine == engine
        assert stored.next_run == "2026-08-21T16:00:00+00:00"
        assert daemon.session_registry.count == 0
        assert mock.calls == []
        runs = list_runs(data, job_id)
        assert len(runs) == 1
        assert runs[0].status == "failed"
        assert runs[0].summary == summary
        assert runs[0].trigger == "schedule"
        assert sent == [("slack", summary)]
        assert "secret-grok" not in summary
        _assert_window_untouched(daemon, saved, kind="grok")
    finally:
        await daemon._shutdown()
