"""Daemon and ``tst`` state follows ``--data-dir`` (TD-4843).

A daemon with an explicit data directory must not read or write the
library default. ``cached_config()`` with no path stays that default
for callers that have no daemon.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Callable
from pathlib import Path

import pytest

from tstd.audit import AuditStore, audit_db_path
from tstd.audit_queries import export_jsonl
from tstd.autonomy.charter import charter_path
from tstd.browser.factory import browser_profile_dir
from tstd.cli import _turn_timeout_secs
from tstd.config import cached_config, config_yaml_path, default_config_yaml
from tstd.config_bind import call_config_loader
from tstd.config_write import save_active_preset
from tstd.coworker import save_coworker
from tstd.cu_host import sock_path
from tstd.cu_indicators import CuIndicatorPrefs, save_cu_indicators
from tstd.cu_policy import cu_policy_path
from tstd.daemon import Daemon
from tstd.desktop.permissions import flag_name, mark_shown
from tstd.grok_home import grok_home
from tstd.logging import LOG_FILE_NAME, log_directory, setup_logging
from tstd.memory_pref import save_global_memory
from tstd.policy import save_skip_all
from tstd.remote_attach import save_remote_attach
from tstd.remote_auth import write_remote_token_file
from tstd.scheduler.history import append_run
from tstd.scheduler.models import JobDraft
from tstd.scheduler.store import save_job
from tstd.session_persist import SessionPersist
from tstd.session_stars import save_session_stars
from tstd.session_store import SessionStore
from tstd.tools.registry import create_registry
from tstd.tools.web_search import search_hosts
from tstd.voice import save_voice
from tstd.workspace_pins import save_workspace_pins
from tstd.ws import write_port_file

_BINDINGS = (
    "tstd.logging.user_data_dir",
    "tstd.config.user_data_dir",
    "tstd.daemon.user_data_dir",
    "tstd.cli.user_data_dir",
    "tstd.audit.user_data_dir",
)

# Code calls only. Docstrings mention the name in backticks; a new call
# outside this set fails the allowlist test.
_USER_DATA_DIR_CALLS = frozenset(
    {
        ("logging.py", "def user_data_dir() -> Path:"),
        ("logging.py", "log_dir = log_dir or log_directory(user_data_dir())"),
        ("config.py", "root = data_dir if data_dir is not None else user_data_dir()"),
        ("audit.py", "root = data_dir if data_dir is not None else user_data_dir()"),
        ("daemon.py", "self.data_dir = data_dir or user_data_dir()"),
        ("daemon.py", "data_dir = Path(args.data_dir) if args.data_dir else user_data_dir()"),
        ("cli.py", "return user_data_dir()"),
    }
)

_CALL_RE = re.compile(r"(?<![\w.])user_data_dir\(")


def _patch_default_dir(monkeypatch: pytest.MonkeyPatch, default: Path) -> None:
    """Point every imported ``user_data_dir`` binding at *default*.

    The modules import the function by value. Patching only
    ``tstd.logging.user_data_dir`` would leave ``ensure_user_config`` on
    the real default, and a bug write would miss this directory.
    """

    def _dir() -> Path:
        return default

    for name in _BINDINGS:
        monkeypatch.setattr(name, _dir)
    cached_config.cache_clear()


def _config_text(*, preset: str, name: str, retries: int | None = None) -> str:
    text = default_config_yaml().replace("name: OpenRouter", f"name: {name}", 1)
    if retries is not None:
        text = text.replace("\n  max_retries: 3\n", f"\n  max_retries: {retries}\n", 1)
    return text.rstrip() + f"\n\nactive_preset: {preset}\n"


def _write_sessions(data_dir: Path) -> None:
    store = SessionStore(data_dir)
    asyncio.run(store.upsert("sess", "/ws", "idle"))


def _write_log(data_dir: Path) -> None:
    root = logging.getLogger()
    setup_logging(level="INFO", log_dir=log_directory(data_dir), log_to_stdout=False)
    try:
        logging.getLogger("tstd.data_dir").info("bound")
    finally:
        for handler in list(root.handlers):
            handler.flush()
            handler.close()
            root.removeHandler(handler)


def _write_export(data_dir: Path) -> None:
    store = AuditStore(audit_db_path(data_dir))
    try:
        export_jsonl(store, data_dir / "exports" / "usage.jsonl")
    finally:
        store.close()


def _write_audit(data_dir: Path) -> None:
    store = AuditStore(audit_db_path(data_dir))
    store.close()


def _write_job(data_dir: Path) -> None:
    workspace = data_dir.parent / "job-ws"
    workspace.mkdir(exist_ok=True)
    save_job(
        data_dir,
        JobDraft(
            workspace=str(workspace),
            instruction="ping",
            cadence="every 1 hour",
            deliver_to="window",
        ),
    )


def _cases() -> list[tuple[str, Callable[[Path], None]]]:
    return [
        ("config.yaml", lambda d: save_active_preset("budget", config_yaml_path(d))),
        ("voice.yaml", lambda d: save_voice(d, True)),
        ("approvals.yaml", lambda d: save_skip_all(d, True)),
        ("session_stars.yaml", lambda d: save_session_stars(d, ["s"])),
        ("workspace_pins.yaml", lambda d: save_workspace_pins(d, ["/ws"])),
        ("coworker.yaml", lambda d: save_coworker(d, False)),
        ("memory.yaml", lambda d: save_global_memory(d, True)),
        ("remote-attach.yaml", lambda d: save_remote_attach(d, True)),
        ("remote-token", lambda d: write_remote_token_file(d, "token")),
        ("cu-indicators.yaml", lambda d: save_cu_indicators(d, CuIndicatorPrefs())),
        ("cu-macos-permissions.yaml", lambda d: mark_shown(d, "macos")),
        ("cu-windows-permissions.yaml", lambda d: mark_shown(d, "windows")),
        ("cu-linux-permissions.yaml", lambda d: mark_shown(d, "linux")),
        ("scheduler/jobs.json", _write_job),
        (
            "scheduler/history/job1.jsonl",
            lambda d: append_run(
                d,
                "job1",
                started_at="2026-09-29T00:00:00+00:00",
                scheduled_for=None,
                trigger="manual",
                status="ok",
                summary="done",
                session_id=None,
            ),
        ),
        ("port.json", lambda d: write_port_file(d, 9, "token")),
        ("sessions.json", _write_sessions),
        ("sessions/sess/conversation.json", lambda d: SessionPersist(d).prepare("sess")),
        ("audit.db", _write_audit),
        (f"logs/{LOG_FILE_NAME}", _write_log),
        ("exports/usage.jsonl", _write_export),
        ("browser-profile", lambda d: browser_profile_dir(d).mkdir()),
        ("cu-agent.sock", lambda d: sock_path(d).touch()),
    ]


@pytest.mark.parametrize(
    ("relative", "write"),
    _cases(),
    ids=[row[0] for row in _cases()],
)
def test_audited_file_lands_in_the_data_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    relative: str,
    write: Callable[[Path], None],
) -> None:
    default = tmp_path / "default"
    scratch = tmp_path / "scratch"
    default.mkdir()
    scratch.mkdir()
    _patch_default_dir(monkeypatch, default)

    write(scratch)

    assert (scratch / relative).exists()
    assert not (default / relative).exists()
    assert list(default.rglob("*")) == []


def test_permission_flag_names_match_the_table() -> None:
    assert flag_name("macos") == "cu-macos-permissions.yaml"
    assert flag_name("windows") == "cu-windows-permissions.yaml"
    assert flag_name("linux") == "cu-linux-permissions.yaml"


def test_user_data_dir_calls_are_allowlisted() -> None:
    root = Path(__file__).resolve().parents[1] / "tstd"
    found: set[tuple[str, str]] = set()
    for path in root.rglob("*.py"):
        rel = path.relative_to(root).as_posix()
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("#") or "``" in stripped:
                continue
            if _CALL_RE.search(stripped):
                found.add((rel, stripped))
    missing = _USER_DATA_DIR_CALLS - found
    extra = found - _USER_DATA_DIR_CALLS
    assert not missing and not extra, f"missing={sorted(missing)} extra={sorted(extra)}"


def test_open_default_stays_on_the_library_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    default = tmp_path / "default"
    scratch = tmp_path / "scratch"
    default.mkdir()
    scratch.mkdir()
    _patch_default_dir(monkeypatch, default)
    store = AuditStore.open_default()
    try:
        assert store.db_path == default / "audit.db"
        assert not (scratch / "audit.db").exists()
    finally:
        store.close()


def test_charter_and_shared_homes_are_not_the_data_dir(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    scratch = tmp_path / "scratch"
    assert charter_path(workspace) == workspace / ".tst" / "autonomy" / "CHARTER.md"
    assert scratch not in cu_policy_path().parents
    assert scratch not in grok_home().parents


def test_zero_arg_loader_is_not_given_a_path() -> None:
    def _double() -> str:
        return "cfg"

    assert call_config_loader(_double, Path("ignored")) == "cfg"


def test_loader_typeerror_is_not_swallowed() -> None:
    def _boom(_path: Path) -> str:
        raise TypeError("real loader bug")

    with pytest.raises(TypeError, match="real loader bug"):
        call_config_loader(_boom, Path("config.yaml"))


async def _send(daemon: Daemon, payload: dict[str, object]) -> dict[str, object]:
    raw = await daemon._handle_message(json.dumps(payload), None)
    assert raw is not None
    parsed: dict[str, object] = json.loads(raw)
    return parsed


async def test_daemon_reads_and_writes_its_data_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    default = tmp_path / "default"
    scratch = tmp_path / "scratch"
    default.mkdir()
    scratch.mkdir()
    (default / "config.yaml").write_text(
        _config_text(preset="local", name="Real Catalog"), encoding="utf-8"
    )
    (scratch / "config.yaml").write_text(
        _config_text(preset="budget", name="Scratch Catalog"), encoding="utf-8"
    )
    default_bytes = (default / "config.yaml").read_bytes()
    _patch_default_dir(monkeypatch, default)

    daemon = Daemon(data_dir=scratch)
    assert daemon.config_path == scratch / "config.yaml"
    state = await daemon._setup_state_event()
    assert state.active_preset == "budget"
    row = next(item for item in state.credentials if item.id == "openrouter")
    assert row.name == "Scratch Catalog"

    slug = await _send(
        daemon,
        {"type": "set_tier_slug", "preset": "budget", "tier": "brain", "slug": "scratch/model"},
    )
    assert slug["type"] == "setup_state"
    assert slug["tier_slugs"]["brain"] == "scratch/model"  # type: ignore[index]

    preset = await _send(daemon, {"type": "set_preset", "name": "vllm"})
    assert preset["active_preset"] == "vllm"
    written = (scratch / "config.yaml").read_text(encoding="utf-8")
    assert "active_preset: vllm" in written
    assert "scratch/model" in written
    assert (default / "config.yaml").read_bytes() == default_bytes

    cached_config.cache_clear()
    library = cached_config()
    assert library.active_preset == "local"
    assert library.credentials["openrouter"].name == "Real Catalog"


def test_omitted_data_dir_is_the_library_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    default = tmp_path / "default"
    default.mkdir()
    (default / "config.yaml").write_text(
        _config_text(preset="vllm", name="Default Catalog"), encoding="utf-8"
    )
    _patch_default_dir(monkeypatch, default)
    daemon = Daemon()
    assert daemon.data_dir == default
    assert daemon.config_path == default / "config.yaml"
    assert daemon.config.active_preset == "vllm"
    assert daemon.config.credentials["openrouter"].name == "Default Catalog"


def test_turn_timeout_reads_the_data_dir_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    default = tmp_path / "default"
    scratch = tmp_path / "scratch"
    default.mkdir()
    scratch.mkdir()
    (default / "config.yaml").write_text(
        _config_text(preset="tst-default", name="OpenRouter", retries=3),
        encoding="utf-8",
    )
    (scratch / "config.yaml").write_text(
        _config_text(preset="tst-default", name="OpenRouter", retries=8),
        encoding="utf-8",
    )
    default_bytes = (default / "config.yaml").read_bytes()
    _patch_default_dir(monkeypatch, default)

    assert _turn_timeout_secs(scratch) > _turn_timeout_secs(default)
    assert _turn_timeout_secs() == _turn_timeout_secs(default)
    assert (default / "config.yaml").read_bytes() == default_bytes


def test_search_hosts_use_the_daemon_block(monkeypatch: pytest.MonkeyPatch) -> None:
    from tstd.config import SearchConfig

    monkeypatch.setattr(
        "tstd.tools.web_search.cached_config",
        lambda: (_ for _ in ()).throw(AssertionError("library cache")),
    )
    search = SearchConfig(base_url="https://scratch.example/s", fallback_base_urls=[])
    assert search_hosts(search) == ("scratch.example",)
    registry = create_registry(search=search)
    resolver = registry.require("web_search").host_resolver
    assert resolver is not None
    assert resolver() == ("scratch.example",)


async def test_usage_export_is_under_the_data_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    default = tmp_path / "default"
    scratch = tmp_path / "scratch"
    default.mkdir()
    scratch.mkdir()
    _patch_default_dir(monkeypatch, default)
    daemon = Daemon(data_dir=scratch)
    await daemon._usage_export("jsonl")
    assert list((scratch / "exports").glob("usage-*.jsonl"))
    assert list(default.rglob("*")) == []
