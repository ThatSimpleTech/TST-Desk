"""One switch for the app's own background network calls (DOCS-28, ENTERPRISE-04).

The release check (host) and the model price refresh (daemon) sit behind
`automatic` in the data folder's update.yaml, which Settings, About writes.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

import pytest

from tests.machine_helpers import Fleet
from tstd.background_checks import background_checks_on


@pytest.mark.parametrize(
    ("text", "on"),
    [
        (None, True),
        ("automatic: true\n", True),
        ("automatic: false\n", False),
        ("automatic: maybe\n", True),
        ("- a list\n", True),
        (": not yaml [\n", True),
    ],
)
def test_the_switch_reads_like_the_host_does(tmp_path: Path, text: str | None, on: bool) -> None:
    if text is not None:
        (tmp_path / "update.yaml").write_text(text, encoding="utf-8")
    assert background_checks_on(tmp_path) is on


@pytest.mark.parametrize("on", [True, False])
async def test_the_price_refresh_follows_the_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, on: bool
) -> None:
    data = tmp_path / "d"
    data.mkdir()
    (data / "update.yaml").write_text(f"automatic: {str(on).lower()}\n", encoding="utf-8")
    monkeypatch.setattr("tstd.daemon.CARD_REFRESH_FIRST_DELAY", 0.01)
    ran = asyncio.Event()

    async def refresh(self: object) -> None:
        ran.set()

    monkeypatch.setattr("tstd.daemon.Daemon._auto_refresh_cards", refresh)
    fleet = Fleet(monkeypatch)
    try:
        await fleet.start(data)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(ran.wait(), timeout=1.0)
        assert ran.is_set() is on
    finally:
        await fleet.close()
