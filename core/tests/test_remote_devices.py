"""TD-5014: the paired-device registry and the one-time pairing code.

Registry: per-device keys stored as sha256 only, in an atomic 0600 file
that a damaged copy can never stop the daemon over. Code: Crockford
base32, normalized the same way everywhere, 10 minutes, single use,
burned after 5 wrong tries.
"""

from __future__ import annotations

import hashlib
import logging
import re
import sys
from pathlib import Path

import pytest
import yaml

from tstd.remote_devices import (
    CODE_ALPHABET,
    LAST_SEEN_THROTTLE_SECONDS,
    MAX_DEVICES,
    REMOTE_DEVICES_FILE,
    DeviceLimitError,
    PairingCodes,
    RemoteDeviceRegistry,
    display_code,
    normalize_code,
    pairing_code_event,
    pairing_link,
    pairing_qr_svg,
    sanitize_device_name,
)

_T0 = 1_791_974_400.0  # 2026-10-14T10:40:00Z


class FakeClock:
    def __init__(self, now: float = _T0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _registry(tmp: Path, clock: FakeClock | None = None) -> RemoteDeviceRegistry:
    reg = RemoteDeviceRegistry(tmp, clock=clock or FakeClock())
    reg.load()
    return reg


def _file(tmp: Path) -> dict[str, object]:
    data = yaml.safe_load((tmp / REMOTE_DEVICES_FILE).read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


# ── code normalization ───────────────────────────────────────────────


@pytest.mark.parametrize(
    ("typed", "want"),
    [
        ("ABCD2345", "ABCD2345"),
        ("abcd-2345", "ABCD2345"),
        (" ab cd - 23 45 ", "ABCD2345"),
        ("OIL0-1111", "0110" + "1111"),
        ("oilo2345", "01102345"),
    ],
)
def test_codes_normalize_the_way_a_person_types_them(typed: str, want: str) -> None:
    assert normalize_code(typed) == want


@pytest.mark.parametrize("bad", ["", "ABCD234", "ABCD23456", "ABCD234U", "ABCD 23!5"])
def test_a_code_that_cannot_be_one_normalizes_to_none(bad: str) -> None:
    assert normalize_code(bad) is None


def test_display_form_splits_the_code() -> None:
    assert display_code("ABCD2345") == "ABCD-2345"


def test_a_started_code_is_eight_alphabet_characters() -> None:
    codes = PairingCodes(clock=FakeClock())
    code, expires_at = codes.start()
    assert len(code) == 8 and all(ch in CODE_ALPHABET for ch in code)
    assert expires_at == _T0 + 600
    assert normalize_code(display_code(code)) == code


# ── code state ───────────────────────────────────────────────────────


def test_a_code_redeems_once() -> None:
    codes = PairingCodes(clock=FakeClock())
    code, _ = codes.start()
    assert codes.redeem(display_code(code).lower()) == "ok"
    assert codes.redeem(code) == "failed"
    assert not codes.active


def test_no_active_code_fails() -> None:
    assert PairingCodes(clock=FakeClock()).redeem("ABCD2345") == "failed"


def test_a_code_expires_after_ten_minutes() -> None:
    clock = FakeClock()
    codes = PairingCodes(clock=clock)
    code, _ = codes.start()
    clock.now += 599
    assert codes.active
    clock.now += 1
    assert codes.redeem(code) == "failed"
    assert not codes.active


def test_five_wrong_tries_burn_the_code() -> None:
    codes = PairingCodes(clock=FakeClock())
    code, _ = codes.start()
    wrong = "0000000" + ("1" if code[-1] != "1" else "2")
    results = [codes.redeem(wrong) for _ in range(5)]
    assert results == ["failed"] * 4 + ["locked"]
    # Even the right code is refused now, until a new start.
    assert codes.redeem(code) == "locked"
    fresh, _ = codes.start()
    assert codes.redeem(fresh) == "ok"


def test_a_new_start_replaces_the_old_code_and_cancel_clears_it() -> None:
    codes = PairingCodes(clock=FakeClock())
    first, _ = codes.start()
    second, _ = codes.start()
    if first != second:
        assert codes.redeem(first) == "failed"
    codes.start()
    codes.cancel()
    assert not codes.active
    assert codes.redeem(second) == "failed"


# ── registry ─────────────────────────────────────────────────────────


def test_add_returns_a_key_and_stores_only_its_hash(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    summary, token = reg.add("Pixel 11 Pro Fold")
    assert re.fullmatch(r"dev_[0-9a-f]{12}", summary.id)
    assert re.fullmatch(r"[0-9a-f]{64}", token)
    assert summary.name == "Pixel 11 Pro Fold"
    assert summary.paired_at == "2026-10-14T10:40:00Z"
    assert summary.last_seen is None and summary.connected is False

    text = (tmp_path / REMOTE_DEVICES_FILE).read_text(encoding="utf-8")
    assert token not in text
    data = _file(tmp_path)
    assert data["version"] == 1
    devices = data["devices"]
    assert isinstance(devices, list)
    assert devices[0]["token_sha256"] == hashlib.sha256(token.encode()).hexdigest()
    assert set(devices[0]) == {"id", "name", "token_sha256", "paired_at", "last_seen"}


def test_verify_finds_the_device_and_nothing_else(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    a, key_a = reg.add("A")
    b, key_b = reg.add("B")
    assert reg.verify(key_a) == a.id
    assert reg.verify(key_b) == b.id
    assert reg.verify("ab" * 32) is None
    assert reg.verify("not hex at all, ünïcode") is None


def test_a_key_never_equals_a_token_it_must_avoid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    taken = "cd" * 32
    minted = iter([taken, taken, "ef" * 32])
    monkeypatch.setattr(
        "tstd.remote_devices.secrets.token_hex",
        lambda n: next(minted) if n == 32 else "0" * (2 * n),
    )
    _, token = _registry(tmp_path).add("Phone", avoid=(taken,))
    assert token == "ef" * 32


def test_registry_survives_a_reload(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    summary, token = reg.add("Phone")
    again = _registry(tmp_path)
    assert again.verify(token) == summary.id
    assert [s.model_dump() for s in again.summaries()] == [summary.model_dump()]


def test_remove_forgets_the_device(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    a, key_a = reg.add("A")
    b, _ = reg.add("B")
    assert reg.remove(a.id)
    assert not reg.remove(a.id)
    assert reg.verify(key_a) is None
    assert [s.id for s in _registry(tmp_path).summaries()] == [b.id]


def test_a_failed_remove_keeps_the_device(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reg = _registry(tmp_path)
    a, key_a = reg.add("A")
    before = (tmp_path / REMOTE_DEVICES_FILE).read_text(encoding="utf-8")

    def boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("tstd.remote_devices.os.replace", boom)
    with pytest.raises(OSError):
        reg.remove(a.id)
    # Memory still matches the file, which still holds the key.
    assert reg.verify(key_a) == a.id and a.id in reg
    assert (tmp_path / REMOTE_DEVICES_FILE).read_text(encoding="utf-8") == before


def test_an_unreadable_file_is_kept_and_never_written_over(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    reg = _registry(tmp_path)
    summary, key = reg.add("Phone")
    path = tmp_path / REMOTE_DEVICES_FILE
    saved = tmp_path / "saved.yaml"
    path.rename(saved)
    path.mkdir()  # reading a directory is an OSError, as a permission error is
    with caplog.at_level(logging.WARNING, logger="tstd.remote_devices"):
        again = _registry(tmp_path)
        again.load()
    assert not again.available and len(again) == 0
    assert again.verify(key) is None
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1, "one warning, not one per retry"
    assert "set aside" not in warnings[0].getMessage()
    with pytest.raises(OSError):
        again.add("Another")
    assert len(again) == 0
    assert path.is_dir() and not list(tmp_path.glob(f"{REMOTE_DEVICES_FILE}.corrupt-*"))
    # Readable again: the next load recovers without a restart.
    path.rmdir()
    saved.rename(path)
    again.load()
    assert again.available and again.verify(key) == summary.id


def test_the_thirty_third_device_is_refused(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    for i in range(MAX_DEVICES):
        reg.add(f"Phone {i}")
    assert reg.is_full
    with pytest.raises(DeviceLimitError):
        reg.add("One too many")
    assert len(_registry(tmp_path)) == MAX_DEVICES


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_the_file_is_private_and_written_atomically(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    reg.add("Phone")
    path = tmp_path / REMOTE_DEVICES_FILE
    assert path.stat().st_mode & 0o777 == 0o600
    # No temp file is left beside it.
    assert sorted(p.name for p in tmp_path.iterdir()) == [REMOTE_DEVICES_FILE]


def test_a_failed_write_keeps_the_old_file_and_hands_out_no_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reg = _registry(tmp_path)
    first, _ = reg.add("First")
    before = (tmp_path / REMOTE_DEVICES_FILE).read_text(encoding="utf-8")

    def boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("tstd.remote_devices.os.replace", boom)
    with pytest.raises(OSError):
        reg.add("Second")
    assert (tmp_path / REMOTE_DEVICES_FILE).read_text(encoding="utf-8") == before
    assert [s.id for s in reg.summaries()] == [first.id]
    assert [p.name for p in tmp_path.iterdir()] == [REMOTE_DEVICES_FILE]


@pytest.mark.parametrize(
    "content",
    [
        ":::: not yaml [",
        "- just a list\n",
        "version: 2\ndevices: []\n",
        "version: 1\ndevices: nope\n",
        "",
    ],
)
def test_a_corrupt_file_is_set_aside_and_the_list_starts_empty(
    tmp_path: Path, content: str, caplog: pytest.LogCaptureFixture
) -> None:
    (tmp_path / REMOTE_DEVICES_FILE).write_text(content, encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="tstd.remote_devices"):
        reg = _registry(tmp_path)
    assert len(reg) == 0
    assert not (tmp_path / REMOTE_DEVICES_FILE).exists()
    aside = tmp_path / f"{REMOTE_DEVICES_FILE}.corrupt-{int(_T0)}"
    assert aside.read_text(encoding="utf-8") == content
    assert any("set aside" in r.getMessage() for r in caplog.records)
    # The next pairing writes a fresh file.
    reg.add("Phone")
    assert _file(tmp_path)["version"] == 1


def test_an_undecodable_file_is_set_aside_too(tmp_path: Path) -> None:
    (tmp_path / REMOTE_DEVICES_FILE).write_bytes(b"\xff\xfe\x00garbage")
    assert len(_registry(tmp_path)) == 0
    assert (tmp_path / f"{REMOTE_DEVICES_FILE}.corrupt-{int(_T0)}").exists()


def test_malformed_entries_are_skipped_with_one_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    good = {
        "id": "dev_0123456789ab",
        "name": "Pixel",
        "token_sha256": "a" * 64,
        "paired_at": "2026-10-14T10:40:00Z",
        "last_seen": None,
    }
    entries = [
        good,
        {**good, "id": "dev_0123456789ab"},  # duplicate id
        {**good, "id": "phone-1"},
        {**good, "id": "dev_aaaaaaaaaaaa", "token_sha256": "plaintext-key"},
        {**good, "id": "dev_bbbbbbbbbbbb", "paired_at": None},
        {**good, "id": "dev_cccccccccccc", "last_seen": 12},
        "not a mapping",
    ]
    (tmp_path / REMOTE_DEVICES_FILE).write_text(
        yaml.safe_dump({"version": 1, "devices": entries}), encoding="utf-8"
    )
    with caplog.at_level(logging.WARNING, logger="tstd.remote_devices"):
        reg = _registry(tmp_path)
    assert [s.id for s in reg.summaries()] == ["dev_0123456789ab"]
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert (tmp_path / REMOTE_DEVICES_FILE).exists()


def test_last_seen_is_written_at_most_once_a_minute(tmp_path: Path) -> None:
    clock = FakeClock()
    reg = _registry(tmp_path, clock)
    summary, _ = reg.add("Phone")
    assert reg.touch_last_seen(summary.id)
    assert reg.summaries()[0].last_seen == "2026-10-14T10:40:00Z"

    clock.now += LAST_SEEN_THROTTLE_SECONDS - 1
    assert not reg.touch_last_seen(summary.id)
    assert _registry(tmp_path).summaries()[0].last_seen == "2026-10-14T10:40:00Z"

    clock.now += 1
    assert reg.touch_last_seen(summary.id)
    assert _registry(tmp_path).summaries()[0].last_seen == "2026-10-14T10:41:00Z"
    assert not reg.touch_last_seen("dev_ffffffffffff")


def test_summaries_carry_the_connected_flag_and_never_a_hash(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    a, _ = reg.add("A")
    b, _ = reg.add("B")
    rows = {s.id: s.model_dump() for s in reg.summaries(connected={a.id})}
    assert rows[a.id]["connected"] is True and rows[b.id]["connected"] is False
    for row in rows.values():
        assert set(row) == {"id", "name", "paired_at", "last_seen", "connected"}


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        ("  Pixel 11 Pro Fold  ", "Pixel 11 Pro Fold"),
        ("Pix\x00el\n\t", "Pixel"),
        ("", "Unnamed device"),
        (" \x07 ", "Unnamed device"),
        ("x" * 100, "x" * 64),
        ("Adam\u2019s phone \U0001f4f1", "Adam\u2019s phone \U0001f4f1"),
    ],
)
def test_device_names_are_cleaned(raw: str, want: str) -> None:
    assert sanitize_device_name(raw) == want


# ── link and QR ──────────────────────────────────────────────────────


def test_the_link_carries_host_port_code_and_an_encoded_name() -> None:
    link = pairing_link("100.64.7.7", 39079, "ABCD2345", "ezer box/2")
    assert link == "tstdesk://pair?h=100.64.7.7&p=39079&c=ABCD2345&n=ezer%20box%2F2"


def test_the_qr_is_a_plain_standalone_svg() -> None:
    svg = pairing_qr_svg("tstdesk://pair?h=100.64.7.7&p=39079&c=ABCD2345&n=ezer")
    assert svg.startswith("<svg") and svg.endswith("</svg>")
    assert "<?xml" not in svg and "<script" not in svg.lower()
    assert "viewBox" in svg and "#000" in svg and "#fff" in svg


def test_the_pairing_code_event_matches_the_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("tstd.remote_devices.platform.node", lambda: "ezer.tail1234.ts.net")
    event = pairing_code_event("ABCD2345", _T0 + 600, "100.64.7.7", 39079)
    assert event.model_dump(exclude={"qr_svg"}) == {
        "type": "pairing_code",
        "seq": 1,
        "code": "ABCD2345",
        "display_code": "ABCD-2345",
        "host": "100.64.7.7",
        "port": 39079,
        "desktop_name": "ezer",
        "link": "tstdesk://pair?h=100.64.7.7&p=39079&c=ABCD2345&n=ezer",
        "expires_at": "2026-10-14T10:50:00Z",
    }
