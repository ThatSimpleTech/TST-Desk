"""Learned token ratios survive a restart, and size the workspace map (TD-5013).

TD-5007 kept the ratios in memory only, so the first call after a daemon
restart was estimated at 1.0: about 15% under on EZER, where the real
ratio is ~1.17. The daemon now saves them to ``token-calibration.json``
in its data folder. Persistence is opt-in: a store built with no path,
which is what every other test builds, never writes.

The workspace map's byte budget (TD-5006) converted its token share at a
flat 4 characters per token. It now uses the brain endpoint's learned
figure when the store has one.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from tests.test_context_fit import _forge_config
from tests.test_loop import mock_factory, wait_for_turn
from tstd import loop as loop_module
from tstd.config import ModelConfig, TierConfig
from tstd.context import calibration as calibration_module
from tstd.context.calibration import (
    CALIBRATION_FILE,
    EMA_ALPHA,
    MAX_ENTRY_AGE,
    MAX_RATIO,
    MIN_RATIO,
    CalibrationStore,
    calibration_key,
    calibration_path,
    process_calibration_store,
    tier_chars_per_token,
)
from tstd.context.manifest import ManifestConfig, manifest_config_for_window
from tstd.context.prompt import PromptAssembler
from tstd.context.tokens import CHARS_PER_TOKEN
from tstd.daemon import Daemon
from tstd.loop import brain_manifest_config
from tstd.mock import MockProvider, Script
from tstd.router import TierRouter
from tstd.session import Session, SessionRunner

_KEY = ("http://mock.local/v1", "ezer-forge")
_OTHER = ("http://other.local/v1", "qwen")
_LOGGER = "tstd.context.calibration"


def _records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == _LOGGER]


def _fields(record: logging.LogRecord) -> dict[str, Any]:
    fields: dict[str, Any] = record.__dict__.get("extra_fields", {})
    return fields


def _stamp(age: timedelta = timedelta(0)) -> str:
    return (datetime.now(UTC) - age).isoformat(timespec="seconds")


def _entry(
    key: tuple[str, str] = _KEY, ratio: object = 1.17, updated_at: object = None
) -> dict[str, object]:
    return {
        "base_url": key[0],
        "model": key[1],
        "ratio": ratio,
        "updated_at": _stamp() if updated_at is None else updated_at,
    }


def _write(path: Path, entries: list[object], version: object = 1) -> None:
    path.write_text(json.dumps({"version": version, "ratios": entries}), encoding="utf-8")


def _persistent(path: Path) -> CalibrationStore:
    store = CalibrationStore()
    store.enable_persistence(path)
    return store


# ── Round trip ───────────────────────────────────────────────────────────


def test_ratios_round_trip_through_the_file(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger=_LOGGER)
    path = tmp_path / CALIBRATION_FILE
    first = _persistent(path)
    assert first.path == path
    assert _records(caplog) == [], "a missing file is the normal first run: silent"

    first.observe(_KEY, 1_000, 1_200)
    first.observe(_KEY, 1_000, 1_100)
    first.observe(_OTHER, 2_000, 2_000)

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["version"] == 1
    assert [(e["base_url"], e["model"]) for e in data["ratios"]] == sorted([_KEY, _OTHER])
    for entry in data["ratios"]:
        stamp = datetime.fromisoformat(entry["updated_at"])
        assert stamp.utcoffset() == timedelta(0), "updated_at is UTC"
        assert datetime.now(UTC) - stamp < timedelta(minutes=1)

    second = _persistent(path)
    assert second.ratio(_KEY) == first.ratio(_KEY)
    assert second.ratio(_OTHER) == 1.0


def test_a_loaded_ratio_continues_the_ema(tmp_path: Path) -> None:
    path = tmp_path / CALIBRATION_FILE
    _write(path, [_entry(ratio=1.2)])
    store = _persistent(path)

    updated = store.observe(_KEY, 1_000, 2_000)

    # A seed would be replaced by the first sample (2.0). A loaded ratio
    # is a measurement and moves by EMA_ALPHA.
    assert updated == pytest.approx(1.2 + EMA_ALPHA * (2.0 - 1.2))
    assert json.loads(path.read_text())["ratios"][0]["ratio"] == updated


def test_a_ratio_learned_before_loading_wins(tmp_path: Path) -> None:
    path = tmp_path / CALIBRATION_FILE
    _write(path, [_entry(ratio=1.2), _entry(_OTHER, ratio=1.4)])
    store = CalibrationStore()
    store.observe(_KEY, 1_000, 1_500)

    store.enable_persistence(path)

    assert store.ratio(_KEY) == 1.5
    assert store.ratio(_OTHER) == 1.4


# ── What a load leaves out ───────────────────────────────────────────────


def _corrupt(path: Path) -> None:
    path.write_text('{"version": 1, "ratios": [', encoding="utf-8")


def _not_utf8(path: Path) -> None:
    path.write_bytes(b"\xff\xfe\x00junk")


def _wrong_version(path: Path) -> None:
    _write(path, [_entry()], version=2)


def _version_true(path: Path) -> None:
    _write(path, [_entry()], version=True)


def _not_an_object(path: Path) -> None:
    path.write_text(json.dumps([_entry()]), encoding="utf-8")


def _ratios_not_a_list(path: Path) -> None:
    path.write_text(json.dumps({"version": 1, "ratios": {"x": 1}}), encoding="utf-8")


def _a_directory(path: Path) -> None:
    path.mkdir()


@pytest.mark.parametrize(
    "spoil",
    [
        _corrupt,
        _not_utf8,
        _wrong_version,
        _version_true,
        _not_an_object,
        _ratios_not_a_list,
        _a_directory,
    ],
)
def test_an_unusable_file_loads_nothing_with_one_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, spoil: Any
) -> None:
    caplog.set_level(logging.DEBUG, logger=_LOGGER)
    path = tmp_path / CALIBRATION_FILE
    spoil(path)

    store = _persistent(path)

    assert store.ratio(_KEY) is None
    records = _records(caplog)
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING
    assert _fields(records[0])["path"] == str(path)


def test_the_next_save_replaces_a_corrupt_file(tmp_path: Path) -> None:
    path = tmp_path / CALIBRATION_FILE
    _corrupt(path)
    store = _persistent(path)

    store.observe(_KEY, 1_000, 1_300)

    assert _persistent(path).ratio(_KEY) == 1.3


def test_bad_entries_are_left_out_and_counted_in_one_line(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger=_LOGGER)
    path = tmp_path / CALIBRATION_FILE
    malformed: list[object] = [
        "not an entry",
        {"base_url": _KEY[0], "ratio": 1.2, "updated_at": _stamp()},  # no model
        _entry(("", "m"), ratio=1.2),  # empty base_url
        _entry(("http://a/v1", "a"), ratio="1.2"),
        _entry(("http://b/v1", "b"), ratio=True),
        _entry(("http://c/v1", "c"), updated_at="yesterday"),
        _entry(("http://d/v1", "d"), updated_at="2026-10-09T10:00:00"),  # naive
        _entry(("http://e/v1", "e"), updated_at=1_760_000_000),
    ]
    out_of_band: list[object] = [
        _entry(("http://f/v1", "f"), ratio=MIN_RATIO - 0.01),
        _entry(("http://g/v1", "g"), ratio=MAX_RATIO + 0.01),
        _entry(("http://h/v1", "h"), ratio=float("nan")),
    ]
    stale: list[object] = [
        _entry(("http://i/v1", "i"), updated_at=_stamp(MAX_ENTRY_AGE + timedelta(hours=1))),
    ]
    kept: list[object] = [
        _entry(_KEY, ratio=1.17),
        _entry(("http://lo/v1", "lo"), ratio=MIN_RATIO),
        _entry(("http://hi/v1", "hi"), ratio=MAX_RATIO),
        _entry(_OTHER, ratio=2, updated_at=_stamp(MAX_ENTRY_AGE - timedelta(days=1))),
    ]
    _write(path, [*malformed, *out_of_band, *stale, *kept])

    store = _persistent(path)

    assert store.ratio(_KEY) == 1.17
    assert store.ratio(("http://lo/v1", "lo")) == MIN_RATIO
    assert store.ratio(("http://hi/v1", "hi")) == MAX_RATIO
    assert store.ratio(_OTHER) == 2.0
    for letter in "abcdefghi":
        assert store.ratio((f"http://{letter}/v1", letter)) is None
    records = _records(caplog)
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING
    assert _fields(records[0]) == {
        "path": str(path),
        "loaded": 4,
        "malformed": len(malformed),
        "out_of_band": len(out_of_band),
        "stale": 1,
    }


def test_only_stale_entries_log_at_info(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger=_LOGGER)
    path = tmp_path / CALIBRATION_FILE
    _write(path, [_entry(updated_at=_stamp(timedelta(days=45)))])

    store = _persistent(path)

    assert store.ratio(_KEY) is None
    (record,) = _records(caplog)
    assert record.levelno == logging.INFO
    assert _fields(record)["stale"] == 1


def test_a_key_written_twice_keeps_the_newest(tmp_path: Path) -> None:
    path = tmp_path / CALIBRATION_FILE
    _write(
        path,
        [
            _entry(ratio=1.9, updated_at=_stamp(timedelta(days=2))),
            _entry(ratio=1.1, updated_at=_stamp(timedelta(hours=1))),
            _entry(ratio=1.5, updated_at=_stamp(timedelta(days=5))),
        ],
    )

    assert _persistent(path).ratio(_KEY) == 1.1


# ── Saving ───────────────────────────────────────────────────────────────


def test_saves_leave_no_temp_files(tmp_path: Path) -> None:
    store = _persistent(tmp_path / CALIBRATION_FILE)
    for prompt_tokens in (1_100, 1_200, 1_300, 1_250):
        store.observe(_KEY, 1_000, prompt_tokens)

    assert [p.name for p in tmp_path.iterdir()] == [CALIBRATION_FILE]


def test_a_failed_replace_keeps_the_old_file_and_removes_the_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / CALIBRATION_FILE
    store = _persistent(path)
    store.observe(_KEY, 1_000, 1_200)
    before = path.read_text(encoding="utf-8")

    def _fail(src: str, dst: str) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", _fail)
    store.observe(_KEY, 1_000, 2_000)

    assert path.read_text(encoding="utf-8") == before
    assert [p.name for p in tmp_path.iterdir()] == [CALIBRATION_FILE]


@pytest.mark.skipif(
    sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="POSIX permission bits; root writes through them",
)
def test_a_read_only_folder_logs_once_and_never_raises(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger=_LOGGER)
    folder = tmp_path / "data"
    folder.mkdir()
    store = _persistent(folder / CALIBRATION_FILE)
    folder.chmod(0o500)
    try:
        for prompt_tokens in (1_100, 1_200, 1_300):
            store.observe(_KEY, 1_000, prompt_tokens)
    finally:
        folder.chmod(0o700)

    assert store.ratio(_KEY) is not None, "the ratio still works from memory"
    records = _records(caplog)
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING
    assert "not saved" in records[0].getMessage()
    assert list(folder.iterdir()) == []


def test_an_ignored_observation_does_not_save(tmp_path: Path) -> None:
    path = tmp_path / CALIBRATION_FILE
    store = _persistent(path)

    store.observe(_KEY, 100, 200)  # under MIN_RAW_TOKENS
    store.observe(_KEY, 1_000, None)

    assert not path.exists()


def test_a_store_with_no_path_never_writes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    writes: list[Path] = []
    monkeypatch.setattr(calibration_module, "_atomic_write", lambda path, text: writes.append(path))
    monkeypatch.chdir(tmp_path)
    store = CalibrationStore()

    for prompt_tokens in (1_100, 1_200, 1_300):
        store.observe(_KEY, 1_000, prompt_tokens)

    assert store.path is None
    assert writes == []
    assert list(tmp_path.iterdir()) == []
    assert not list(Path.home().rglob(CALIBRATION_FILE))


# ── The daemon's startup hook ────────────────────────────────────────────


async def test_the_daemon_loads_and_saves_in_its_data_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # conftest gives each test a store that never learns; this one learns.
    monkeypatch.setattr(calibration_module, "_PROCESS_STORE", CalibrationStore())
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    path = calibration_path(data_dir)
    _write(path, [_entry(ratio=1.17)])

    daemon = Daemon(data_dir=data_dir)
    task = asyncio.create_task(daemon.run())
    try:
        for _ in range(100):
            if daemon.ws_server.port:
                break
            await asyncio.sleep(0.05)
        assert daemon.ws_server.port > 0

        store = process_calibration_store()
        assert store.path == data_dir / CALIBRATION_FILE
        assert store.ratio(_KEY) == 1.17

        store.observe(_OTHER, 1_000, 1_400)
        saved = json.loads(path.read_text(encoding="utf-8"))
        assert {(e["base_url"], e["model"]) for e in saved["ratios"]} == {_KEY, _OTHER}
    finally:
        daemon._shutdown_event.set()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=5)
        except (TimeoutError, asyncio.CancelledError):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


# ── The workspace map's budget ───────────────────────────────────────────


def test_the_map_budget_shrinks_by_the_ratio() -> None:
    flat = manifest_config_for_window(32_768)
    scaled = manifest_config_for_window(32_768, chars_per_token=CHARS_PER_TOKEN / 1.6)

    assert flat.max_bytes == 32_768 * CHARS_PER_TOKEN * 15 // 100
    assert scaled.max_bytes == int(32_768 * (CHARS_PER_TOKEN / 1.6) * 15 // 100)
    assert scaled.max_bytes / flat.max_bytes == pytest.approx(1 / 1.6, rel=1e-3)


def _brain(config: ModelConfig) -> TierConfig:
    return config.tier("brain")


def test_the_brain_budget_uses_the_brain_endpoint_ratio() -> None:
    config = _forge_config()
    store = CalibrationStore()
    window = _brain(config).context_window

    assert brain_manifest_config(config, store) == manifest_config_for_window(window)

    store.observe(calibration_key(_brain(config)), 1_000, 1_600)

    assert calibration_key(_brain(config)) == _KEY
    assert tier_chars_per_token(store, _brain(config)) == CHARS_PER_TOKEN / 1.6
    assert brain_manifest_config(config, store) == manifest_config_for_window(
        window, chars_per_token=CHARS_PER_TOKEN / 1.6
    )


def test_an_unresolved_brain_slug_stays_flat() -> None:
    config = _forge_config()
    store = CalibrationStore()
    store.observe(_KEY, 1_000, 1_600)
    _brain(config).slug = None  # loopback discovery has not run yet

    assert tier_chars_per_token(store, _brain(config)) == CHARS_PER_TOKEN
    assert brain_manifest_config(config, store) == manifest_config_for_window(
        _brain(config).context_window
    )


async def test_the_loop_sizes_its_map_from_the_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[ManifestConfig | None] = []

    class _Recording(PromptAssembler):
        def __init__(self, *args: Any, manifest_config: ManifestConfig | None = None, **kw: Any):
            seen.append(manifest_config)
            super().__init__(*args, manifest_config=manifest_config, **kw)

    monkeypatch.setattr(loop_module, "PromptAssembler", _Recording)
    config = _forge_config()
    store = CalibrationStore()
    store.observe(_KEY, 1_000, 1_600)
    session = Session(str(tmp_path))
    mock = MockProvider(sequences={"ezer-forge": [Script(kind="stream", content="hello")]})
    runner = SessionRunner(
        session,
        loop_factory=lambda s: loop_module.agent_loop(
            s, TierRouter(), mock_factory(mock), config, calibration_store=store
        ),
    )
    await runner.start()
    try:
        await session.add_user_message("hi")
        await wait_for_turn(session, 1, 10.0)
    finally:
        await runner.cancel()

    assert seen == [
        manifest_config_for_window(
            _brain(config).context_window, chars_per_token=CHARS_PER_TOKEN / 1.6
        )
    ]
