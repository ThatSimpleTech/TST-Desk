"""The phone-permissions file and the origins sidecar (TD-5279).

Everything fails closed: whatever is not plainly a live grant reads as off.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from tstd import phone_permissions as perms
from tstd.config import load_config
from tstd.phone_store import PhoneStore, server_saved_summary, value_summary

NOW = datetime(2026, 11, 20, 18, 0, 0, tzinfo=UTC)


def _write(tmp_path: Path, body: object) -> None:
    perms.permissions_path(tmp_path).write_text(yaml.safe_dump(body), encoding="utf-8")


def test_absent_file_means_everything_off(tmp_path: Path) -> None:
    assert perms.load_grants(tmp_path) == {}
    store = PhoneStore(tmp_path)
    assert store.live() == frozenset()
    states = store.states()
    assert set(states) == set(perms.CAPABILITIES)
    assert not any(state.enabled for state in states.values())
    assert store.doctor_row() is None


@pytest.mark.parametrize(
    "text",
    ["{{{ not yaml", "- a list\n", "just a string", "", "version: 1\ngrants: nope\n"],
)
def test_corrupt_file_means_everything_off(tmp_path: Path, text: str) -> None:
    perms.permissions_path(tmp_path).write_text(text, encoding="utf-8")
    assert perms.load_grants(tmp_path) == {}


def test_bytes_that_are_not_text_mean_everything_off(tmp_path: Path) -> None:
    perms.permissions_path(tmp_path).write_bytes(b"\xff\xfe\x00grants")
    assert perms.load_grants(tmp_path) == {}


@pytest.mark.parametrize(
    "entry",
    [
        {"enabled": "yes"},  # only a real true
        {"enabled": 1},
        {"enabled": False},
        {"enabled": True, "expires_at": "tomorrow"},  # unreadable expiry is not forever
        {"enabled": True, "expires_at": 12},
        "true",
        None,
    ],
)
def test_an_entry_that_is_not_plainly_a_grant_is_off(tmp_path: Path, entry: object) -> None:
    _write(tmp_path, {"version": 1, "grants": {"mcp_servers": entry}})
    assert perms.load_grants(tmp_path) == {}


def test_another_file_version_is_off(tmp_path: Path) -> None:
    _write(tmp_path, {"version": 2, "grants": {"mcp_servers": {"enabled": True}}})
    assert perms.load_grants(tmp_path) == {}
    _write(tmp_path, {"grants": {"mcp_servers": {"enabled": True}}})
    assert perms.load_grants(tmp_path) == {}


def test_unknown_capability_in_file_is_ignored(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "version": 1,
            "grants": {"everything": {"enabled": True}, "mcp_servers": {"enabled": True}},
        },
    )
    assert set(perms.load_grants(tmp_path)) == {"mcp_servers"}


def test_a_grant_round_trips_and_lists_every_capability(tmp_path: Path) -> None:
    grants = perms.changed({}, "mcp_servers", True, None, NOW)
    perms.save_grants(tmp_path, grants)
    assert perms.load_grants(tmp_path) == grants
    on_disk = yaml.safe_load(perms.permissions_path(tmp_path).read_text(encoding="utf-8"))
    assert on_disk["version"] == 1
    assert set(on_disk["grants"]) == set(perms.CAPABILITIES)
    assert on_disk["grants"]["mcp_servers"] == {
        "enabled": True,
        "granted_at": "2026-11-20T18:00:00Z",
        "expires_at": None,
    }
    others = [g for name, g in on_disk["grants"].items() if name != "mcp_servers"]
    assert all(g == {"enabled": False} for g in others)
    perms.save_grants(tmp_path, perms.changed(grants, "mcp_servers", False, None, NOW))
    off = yaml.safe_load(perms.permissions_path(tmp_path).read_text(encoding="utf-8"))
    assert all(g == {"enabled": False} for g in off["grants"].values())
    assert perms.load_grants(tmp_path) == {}


def test_expired_grant_reads_as_off() -> None:
    grants = perms.changed({}, "mcp_servers", True, 3600, NOW)
    assert grants["mcp_servers"].expires_at == "2026-11-20T19:00:00Z"
    assert perms.live(grants, NOW + timedelta(minutes=59, seconds=59)) == {"mcp_servers"}
    assert perms.live(grants, NOW + timedelta(hours=1)) == frozenset()
    assert perms.next_expiry(grants, NOW) == NOW + timedelta(hours=1)
    assert perms.next_expiry(grants, NOW + timedelta(hours=2)) is None
    assert perms.next_expiry(perms.changed({}, "mcp_servers", True, None, NOW), NOW) is None


def test_a_bad_ask_is_refused() -> None:
    with pytest.raises(ValueError, match="Unknown phone permission"):
        perms.changed({}, "everything", True, None, NOW)
    with pytest.raises(ValueError, match="seven days"):
        perms.changed({}, "mcp_servers", True, perms.MAX_EXPIRY_SECONDS + 1, NOW)
    with pytest.raises(ValueError, match="seven days"):
        perms.changed({}, "mcp_servers", True, 0, NOW)


def test_save_is_atomic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A write that dies part-way leaves the old file, and no stray temp file."""
    perms.save_grants(tmp_path, perms.changed({}, "mcp_servers", True, None, NOW))
    before = perms.permissions_path(tmp_path).read_bytes()

    def boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(perms.yaml, "safe_dump", boom)
    with pytest.raises(OSError, match="disk full"):
        perms.save_grants(tmp_path, {})
    assert perms.permissions_path(tmp_path).read_bytes() == before
    assert [p.name for p in tmp_path.iterdir()] == ["phone-permissions.yaml"]


def test_a_failed_save_leaves_the_store_as_it_was(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = PhoneStore(tmp_path)

    def boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("read-only")

    monkeypatch.setattr(perms, "save_grants", boom)
    with pytest.raises(OSError, match="read-only"):
        store.set("mcp_servers", True, None)
    assert store.live() == frozenset()


def test_the_store_follows_the_clock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [NOW]
    monkeypatch.setattr(perms, "utc_now", lambda: clock[0])
    store = PhoneStore(tmp_path)
    store.set("mcp_servers", True, 3600)
    assert store.live() == {"mcp_servers"}
    state = store.states()["mcp_servers"]
    assert state.enabled and state.expires_at == "2026-11-20T19:00:00Z"
    row = store.doctor_row()
    assert row is not None and row.status == "warn"
    assert row.detail == "paired phones may change: MCP servers ON (until 2026-11-20T19:00:00Z)"
    clock[0] = NOW + timedelta(hours=1, seconds=1)
    assert store.live() == frozenset()
    assert store.states()["mcp_servers"].enabled is False
    assert store.doctor_row() is None
    # A second store reads the same file and reaches the same answer.
    assert PhoneStore(tmp_path).live() == frozenset()


def test_the_doctor_row_says_until_turned_off(tmp_path: Path) -> None:
    store = PhoneStore(tmp_path)
    store.set("mcp_servers", True, None)
    row = store.doctor_row()
    assert row is not None
    assert row.detail == "paired phones may change: MCP servers ON (until turned off)"
    assert row.fix is None


def test_workspace_config_cannot_grant(tmp_path: Path) -> None:
    """The grants are no config key: one written into a config file does nothing."""
    data = tmp_path / "data"
    data.mkdir()
    workspace = tmp_path / "ws" / ".tst"
    workspace.mkdir(parents=True)
    text = "phone_permissions:\n  mcp_servers:\n    enabled: true\n"
    (workspace / "config.yaml").write_text(text, encoding="utf-8")
    (data / "config.yaml").write_text(text, encoding="utf-8")
    for path in (workspace / "config.yaml", data / "config.yaml"):
        assert not hasattr(load_config(path), "phone_permissions")
    # Only phone-permissions.yaml in the data dir is read, and it is absent.
    assert PhoneStore(data).live() == frozenset()
    assert PhoneStore(workspace).live() == frozenset()


def test_origins_round_trip_and_ignore_what_is_malformed(tmp_path: Path) -> None:
    store = PhoneStore(tmp_path)
    store.record_origin("probe", "dev_1", "Pixel 8")
    store.record_origin("other", "dev_2", "Tablet")
    again = PhoneStore(tmp_path)
    origin = again.origin_of("probe")
    assert origin is not None and (origin.device_id, origin.device_name) == ("dev_1", "Pixel 8")
    assert again.servers_from("dev_1") == ["probe"]
    again.forget_origin("probe")
    assert PhoneStore(tmp_path).servers_from("dev_1") == []
    perms.origins_path(tmp_path).write_text(
        "version: 1\nservers:\n  bad: nope\n  worse: {device_id: 3}\n", encoding="utf-8"
    )
    assert perms.load_origins(tmp_path) == {}
    perms.origins_path(tmp_path).write_text("{{{", encoding="utf-8")
    assert perms.load_origins(tmp_path) == {}


def test_a_failed_origin_write_is_not_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = PhoneStore(tmp_path)

    def boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(perms, "save_origins", boom)
    store.record_origin("probe", "dev_1", "Pixel 8")
    assert store.origin_of("probe") is None


def test_the_notice_names_the_command_and_never_a_value() -> None:
    added = server_saved_summary("probe", ["/usr/bin/touch", "/tmp/a b"], "", existed=False)
    assert added == "added MCP server 'probe': /usr/bin/touch '/tmp/a b'"
    changed = server_saved_summary("web", [], "https://mcp.example.test/x", existed=True)
    assert changed == "changed MCP server 'web': https://mcp.example.test/x"
    long = server_saved_summary("big", ["x" * 500], "", existed=False)
    assert len(long) < 240 and long.endswith("…")
    assert value_summary("probe", "env", "TOKEN", stored=True) == (
        "stored a value for variable 'TOKEN' of MCP server 'probe'."
    )
    assert value_summary("web", "header", "Authorization", stored=False) == (
        "removed the value of header 'Authorization' of MCP server 'web'."
    )


def test_one_grant_does_not_switch_on_another(tmp_path: Path) -> None:
    for name in perms.CAPABILITIES:
        store = PhoneStore(tmp_path / name)
        store.set(name, True, None)
        assert store.live() == {name}
        assert [n for n, s in store.states().items() if s.enabled] == [name]
        assert perms.LABELS[name] in (store.doctor_row() or object()).detail  # type: ignore[union-attr]
