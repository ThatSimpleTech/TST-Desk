"""TD-5015: computers.yaml, the keychain account, and the add/remove order.

The file is the index (no key ever), the keychain holds each key under
``tst-machine.<mch>``, and a pairing that cannot be stored here undoes
itself on the other computer with the key it just got.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.machine_helpers import LOCAL, FakeKeychain
from tests.test_remote_devices import FakeClock
from tstd import bridge_pair
from tstd.bridge_pair import MachineKeys, forget_machine, pair_machine
from tstd.config import CREDENTIAL_ID_RE
from tstd.identity import MACHINE_ID_FILE, MACHINE_ID_RE, load_or_mint_machine_id
from tstd.keychain import KeychainError, machine_key_account
from tstd.machine_pairing import PairError, PairOutcome, PairTarget
from tstd.machines import (
    COMPUTERS_FILE,
    MAX_MACHINES,
    MachineLimitError,
    MachineRecord,
    MachineRegistry,
)

_KEY = "k" * 64
_MID = "mid_0123456789abcdef"


def _record(mch: str = "mch_1a2b3c4d5e6f", **over: Any) -> MachineRecord:
    fields: dict[str, Any] = {
        "id": mch,
        "name": "tower",
        "host": "100.64.7.7",
        "port": 39079,
        "device_id": "dev_abcdef012345",
        "paired_at": "2026-10-14T12:00:00Z",
        "machine_id": _MID,
    }
    fields.update(over)
    return MachineRecord(**fields)


def _registry(tmp: Path, clock: FakeClock | None = None) -> MachineRegistry:
    reg = MachineRegistry(tmp, clock=clock or FakeClock())
    reg.load()
    return reg


# ── the file ─────────────────────────────────────────────────────────


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes")
def test_the_file_is_0600_written_whole_and_never_holds_a_key(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    reg.put(_record())
    path = tmp_path / COMPUTERS_FILE
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    text = path.read_text(encoding="utf-8")
    assert _KEY not in text and "token" not in text and "key" not in text
    data = yaml.safe_load(text)
    assert data["version"] == 1
    assert set(data["machines"][0]) == {
        "id",
        "machine_id",
        "name",
        "host",
        "port",
        "device_id",
        "paired_at",
        "last_seen",
    }
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".computers.")]


def test_records_survive_a_restart_in_pairing_order(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    reg.put(_record("mch_000000000001", name="one"))
    reg.put(_record("mch_000000000002", name="two", machine_id=None))
    again = _registry(tmp_path)
    assert [r.name for r in again.records()] == ["one", "two"]
    assert again.get("mch_000000000002") == _record("mch_000000000002", name="two", machine_id=None)
    assert again.by_machine_id(_MID) is not None
    assert again.by_address("100.64.7.7", 39079) is not None


def test_a_damaged_file_is_set_aside_and_the_list_starts_empty(tmp_path: Path) -> None:
    (tmp_path / COMPUTERS_FILE).write_text("version: [1\n", encoding="utf-8")
    reg = _registry(tmp_path, FakeClock(1234.0))
    assert len(reg) == 0 and reg.available
    assert (tmp_path / f"{COMPUTERS_FILE}.corrupt-1234").exists()
    reg.put(_record())
    assert len(_registry(tmp_path)) == 1


def test_malformed_entries_are_skipped_not_fatal(tmp_path: Path) -> None:
    good = _record()
    entries = [
        {**good.__dict__},
        {**good.__dict__, "id": "mch_bad"},
        {**good.__dict__, "id": "mch_000000000009", "port": 0},
        {**good.__dict__, "id": "mch_00000000000a", "machine_id": "mid_short"},
    ]
    (tmp_path / COMPUTERS_FILE).write_text(
        yaml.safe_dump({"version": 1, "machines": entries}), encoding="utf-8"
    )
    assert [r.id for r in _registry(tmp_path).records()] == [good.id]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes")
@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root reads anything")
def test_an_unreadable_file_is_kept_and_never_overwritten(tmp_path: Path) -> None:
    path = tmp_path / COMPUTERS_FILE
    _registry(tmp_path).put(_record())
    before = path.read_bytes()
    path.chmod(0)
    try:
        reg = _registry(tmp_path)
        assert not reg.available
        with pytest.raises(OSError):
            reg.put(_record("mch_000000000002"))
    finally:
        path.chmod(0o600)
    assert path.read_bytes() == before


def test_remove_one_keeps_the_others(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    reg.put(_record("mch_000000000001"))
    reg.put(_record("mch_000000000002", machine_id=None))
    assert reg.remove("mch_000000000001") is True
    assert reg.remove("mch_000000000001") is False
    assert [r.id for r in _registry(tmp_path).records()] == ["mch_000000000002"]


def test_the_limit_refuses_a_new_record_but_not_an_update(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    for i in range(MAX_MACHINES):
        reg.put(_record(f"mch_{i:012x}", machine_id=None))
    assert reg.is_full
    with pytest.raises(MachineLimitError):
        reg.put(_record("mch_ffffffffffff", machine_id=None))
    reg.put(_record("mch_000000000000", name="renamed", machine_id=None))
    assert reg.get("mch_000000000000") is not None
    assert len(_registry(tmp_path)) == MAX_MACHINES


def test_last_seen_writes_at_most_once_a_minute(tmp_path: Path) -> None:
    clock = FakeClock()
    reg = _registry(tmp_path, clock)
    reg.put(_record())
    assert reg.touch_last_seen("mch_1a2b3c4d5e6f") is True
    clock.now += 30
    assert reg.touch_last_seen("mch_1a2b3c4d5e6f") is False
    assert reg.touch_last_seen("mch_1a2b3c4d5e6f", force=True) is True
    clock.now += 61
    assert reg.touch_last_seen("mch_1a2b3c4d5e6f") is True
    assert _registry(tmp_path).get("mch_1a2b3c4d5e6f").last_seen is not None  # type: ignore[union-attr]


# ── the keychain account ─────────────────────────────────────────────


def test_the_account_is_outside_the_credential_namespace() -> None:
    account = machine_key_account("mch_1a2b3c4d5e6f")
    assert account == "tst-machine.mch_1a2b3c4d5e6f"
    # Credential accounts are tst-<id>, and an id allows neither "." nor "_".
    assert not CREDENTIAL_ID_RE.match(account.removeprefix("tst-"))
    with pytest.raises(KeychainError):
        machine_key_account("../evil")


# ── the machine id ───────────────────────────────────────────────────


def test_the_machine_id_is_minted_once_and_kept(tmp_path: Path) -> None:
    first = load_or_mint_machine_id(tmp_path)
    assert MACHINE_ID_RE.match(first)
    assert load_or_mint_machine_id(tmp_path) == first
    if sys.platform != "win32":
        assert stat.S_IMODE(os.stat(tmp_path / MACHINE_ID_FILE).st_mode) == 0o600
    (tmp_path / MACHINE_ID_FILE).write_text("garbage", encoding="utf-8")
    replaced = load_or_mint_machine_id(tmp_path)
    assert replaced != first and MACHINE_ID_RE.match(replaced)


# ── add and remove order (pair_machine / forget_machine) ─────────────


class _Remote:
    """Stands in for the other computer: redeem and self-removal."""

    def __init__(self, machine_id: str | None = _MID, device_id: str = "dev_abcdef012345") -> None:
        self.machine_id = machine_id
        self.device_id = device_id
        self.redeemed: list[dict[str, Any]] = []
        self.removed: list[tuple[str, int, str, str]] = []

    async def redeem(self, target: PairTarget, **kw: Any) -> PairOutcome:
        self.redeemed.append({"host": target.host, "port": target.port, **kw})
        return PairOutcome(
            device_id=self.device_id,
            device_name="hub",
            token=_KEY,
            desktop_name="tower",
            machine_id=self.machine_id,
        )

    async def remove_self(self, host: str, port: int, key: str, device_id: str, **_: Any) -> bool:
        self.removed.append((host, port, key, device_id))
        return True


@pytest.fixture
def remote(monkeypatch: pytest.MonkeyPatch) -> _Remote:
    fake = _Remote()
    monkeypatch.setattr(bridge_pair, "redeem", fake.redeem)
    monkeypatch.setattr(bridge_pair, "remove_self", fake.remove_self)
    return fake


@pytest.fixture
def keychain(monkeypatch: pytest.MonkeyPatch) -> FakeKeychain:
    return FakeKeychain().install(monkeypatch)


def _target(host: str = "100.64.7.7", port: int = 39079) -> PairTarget:
    return PairTarget(host=host, port=port, code="ABCD2345", name="tower")


async def _pair(reg: MachineRegistry, keys: MachineKeys, target: PairTarget) -> MachineRecord:
    spawned: list[Any] = []

    def spawn(coro: Any) -> None:
        spawned.append(coro)

    record = await pair_machine(reg, keys, target, policy=LOCAL, wall=lambda: 0.0, spawn=spawn)
    for coro in spawned:
        await coro
    return record


async def test_pairing_writes_keychain_then_file(
    tmp_path: Path, remote: _Remote, keychain: FakeKeychain
) -> None:
    reg = _registry(tmp_path)
    record = await _pair(reg, MachineKeys(), _target())
    assert keychain.keys == {record.id: _KEY}
    assert _registry(tmp_path).get(record.id) == record
    assert remote.redeemed[0]["replaces"] is None
    assert remote.removed == []


async def test_a_keychain_failure_leaves_no_entry_and_cleans_the_other_computer(
    tmp_path: Path, remote: _Remote, keychain: FakeKeychain
) -> None:
    keychain.locked = True
    reg = _registry(tmp_path)
    with pytest.raises(PairError) as raised:
        await _pair(reg, MachineKeys(), _target())
    assert raised.value.code == "keychain_locked"
    assert len(_registry(tmp_path)) == 0 and keychain.keys == {}
    assert remote.removed == [("100.64.7.7", 39079, _KEY, "dev_abcdef012345")]


async def test_a_file_failure_deletes_the_key_and_cleans_the_other_computer(
    tmp_path: Path, remote: _Remote, keychain: FakeKeychain, monkeypatch: pytest.MonkeyPatch
) -> None:
    reg = _registry(tmp_path)

    def boom(_records: object) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(reg, "_write", boom)
    with pytest.raises(PairError) as raised:
        await _pair(reg, MachineKeys(), _target())
    assert raised.value.code == "pair_failed"
    assert keychain.keys == {} and len(reg) == 0
    assert remote.removed == [("100.64.7.7", 39079, _KEY, "dev_abcdef012345")]


async def test_the_limit_is_refused_after_cleaning_the_other_computer(
    tmp_path: Path, remote: _Remote, keychain: FakeKeychain
) -> None:
    reg = _registry(tmp_path)
    for i in range(MAX_MACHINES):
        reg.put(_record(f"mch_{i:012x}", machine_id=None, host=f"100.64.0.{i + 1}"))
    with pytest.raises(PairError) as raised:
        await _pair(reg, MachineKeys(), _target())
    assert raised.value.code == "machine_limit"
    assert len(remote.removed) == 1 and keychain.keys == {}


async def test_re_pairing_the_same_machine_id_at_a_new_address_keeps_the_mch(
    tmp_path: Path, remote: _Remote, keychain: FakeKeychain
) -> None:
    reg = _registry(tmp_path)
    keys = MachineKeys()
    first = await _pair(reg, keys, _target())
    keychain.keys[first.id] = "old" * 20
    keys.drop(first.id)
    remote.device_id = "dev_000000000777"
    second = await _pair(reg, keys, _target(host="tower.tail1234.ts.net"))
    assert second.id == first.id and second.paired_at == first.paired_at
    assert second.host == "tower.tail1234.ts.net" and second.device_id == "dev_000000000777"
    assert len(reg) == 1 and keychain.keys[first.id] == _KEY
    # A new address sends no ``replaces``; the old entry there goes with the old key.
    assert remote.redeemed[1]["replaces"] is None
    assert remote.removed == [("tower.tail1234.ts.net", 39079, "old" * 20, "dev_abcdef012345")]


async def test_re_pairing_the_same_address_sends_replaces(
    tmp_path: Path, remote: _Remote, keychain: FakeKeychain
) -> None:
    reg = _registry(tmp_path)
    keys = MachineKeys()
    first = await _pair(reg, keys, _target())
    second = await _pair(reg, keys, _target())
    assert second.id == first.id
    assert remote.redeemed[1]["replaces"] == _KEY
    assert remote.removed == []


async def test_forget_deletes_keychain_then_file_and_tells_the_other_computer(
    tmp_path: Path, remote: _Remote, keychain: FakeKeychain
) -> None:
    reg = _registry(tmp_path)
    keys = MachineKeys()
    record = await _pair(reg, keys, _target())
    spawned: list[Any] = []
    assert (await forget_machine(reg, keys, record, policy=LOCAL, spawn=spawned.append)) is None
    for coro in spawned:
        await coro
    assert keychain.keys == {} and len(_registry(tmp_path)) == 0
    assert remote.removed == [("100.64.7.7", 39079, _KEY, "dev_abcdef012345")]


async def test_forget_with_a_locked_keychain_changes_nothing(
    tmp_path: Path, remote: _Remote, keychain: FakeKeychain
) -> None:
    reg = _registry(tmp_path)
    keys = MachineKeys()
    record = await _pair(reg, keys, _target())
    keychain.locked = True
    error = await forget_machine(reg, keys, record, policy=LOCAL, spawn=lambda c: c.close())
    assert error is not None and '"keychain_locked"' in error
    assert len(_registry(tmp_path)) == 1 and remote.removed == []


async def test_keys_are_read_once_per_run(keychain: FakeKeychain) -> None:
    keychain.keys["mch_1a2b3c4d5e6f"] = _KEY
    keys = MachineKeys()
    assert await keys.get("mch_1a2b3c4d5e6f") == _KEY
    assert await keys.get("mch_1a2b3c4d5e6f") == _KEY
    assert keychain.reads == 1
    assert await keys.get_or_none("mch_000000000000") is None
