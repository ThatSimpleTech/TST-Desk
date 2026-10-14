"""TD-5015: ``tst machines [add|remove]`` against two real daemons."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from tests.machine_helpers import FakeKeychain, Fleet, ask, eventually, window
from tstd import cli
from tstd.cli import CliError
from tstd.cli_machines import add_frame, add_machine, list_machines, remove_machine, split_address
from tstd.daemon import Daemon


class TestParser:
    def test_machines_lists_by_default(self) -> None:
        ns = cli.parse_args(["machines"])
        assert ns.command == "machines" and ns.machines_command is None

    @pytest.mark.parametrize(
        "argv",
        [
            ["machines", "add", "100.64.0.9:39079", "ABCD-2345", "--data-dir", "{d}"],
            ["machines", "--data-dir", "{d}", "add", "100.64.0.9:39079", "ABCD-2345"],
        ],
    )
    def test_add_keeps_data_dir_on_either_side(self, tmp_path: Path, argv: list[str]) -> None:
        ns = cli.parse_args([a.replace("{d}", str(tmp_path)) for a in argv])
        assert ns.machines_command == "add" and ns.target == "100.64.0.9:39079"
        assert ns.code == "ABCD-2345" and ns.data_dir == tmp_path

    def test_remove_takes_an_id(self) -> None:
        ns = cli.parse_args(["machines", "remove", "mch_0123456789ab"])
        assert ns.machines_command == "remove" and ns.machine_id == "mch_0123456789ab"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("100.64.0.9:39079", ("100.64.0.9", 39079)),
        ("[fd7a:115c:a1e0::9]:1", ("fd7a:115c:a1e0::9", 1)),
        ("ezer.tail1.ts.net:39079", ("ezer.tail1.ts.net", 39079)),
    ],
)
def test_split_address(text: str, expected: tuple[str, int]) -> None:
    assert split_address(text) == expected


@pytest.mark.parametrize("text", ["100.64.0.9", "fd7a::9:1", "[fd7a::9]1", "h:0", "h:x", ":1"])
def test_split_address_refuses(text: str) -> None:
    with pytest.raises(CliError):
        split_address(text)


def test_add_frame_forms() -> None:
    link = "tstdesk://pair?h=100.64.0.9&p=39079&c=ABCD2345"
    assert add_frame(link, None) == {"type": "add_machine", "link": link}
    assert add_frame("100.64.0.9:39079", "abcd-2345") == {
        "type": "add_machine",
        "host": "100.64.0.9",
        "port": 39079,
        "code": "abcd-2345",
    }
    with pytest.raises(CliError):
        add_frame(link, "ABCD2345")
    with pytest.raises(CliError):
        add_frame("100.64.0.9:39079", None)


# ── against real daemons ─────────────────────────────────────────────


@pytest.fixture
async def pair_of(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[tuple[Daemon, Daemon]]:
    FakeKeychain().install(monkeypatch)
    fleet = Fleet(monkeypatch)
    try:
        yield await fleet.start(tmp_path / "a"), await fleet.start(tmp_path / "b")
    finally:
        await fleet.close()


async def _code(other: Daemon) -> str:
    async with window(other) as screen:
        return str((await ask(screen, {"type": "start_pairing"}, "pairing_code"))["code"])


async def test_add_list_and_remove(
    pair_of: tuple[Daemon, Daemon], capsys: pytest.CaptureFixture[str]
) -> None:
    a, b = pair_of
    assert await list_machines(a.data_dir) == 0
    assert "No paired computers" in capsys.readouterr().out

    code = await _code(b)
    assert await add_machine(a.data_dir, f"127.0.0.1:{b.ws_server.port}", code) == 0
    out = capsys.readouterr().out
    assert "Pairing..." in out and "Paired: " in out
    leaked = code in out or f"{code[:4]}-{code[4:]}" in out
    assert not leaked  # a bool, so a failure never prints the code
    (record,) = a.hub.registry.records()

    assert await list_machines(a.data_dir) == 0
    listed = capsys.readouterr().out
    assert listed.startswith(f"{record.id}  {record.name}  127.0.0.1:{b.ws_server.port}  ")
    assert "last seen" in listed

    assert await remove_machine(a.data_dir, record.id) == 0
    assert capsys.readouterr().out.strip() == f"Removed: {record.name}"
    assert len(a.hub.registry) == 0
    await eventually(lambda: len(b.remote_devices) == 0, within=5)


async def test_a_wrong_code_and_an_unknown_id_fail_plainly(
    pair_of: tuple[Daemon, Daemon], capsys: pytest.CaptureFixture[str]
) -> None:
    a, b = pair_of
    await _code(b)
    assert await add_machine(a.data_dir, f"127.0.0.1:{b.ws_server.port}", "ZZZZ9999") == 1
    err = capsys.readouterr().err
    assert "didn't work" in err and "ZZZZ" not in err
    assert await remove_machine(a.data_dir, "mch_000000000000") == 1
    assert "No paired computer mch_000000000000" in capsys.readouterr().err
