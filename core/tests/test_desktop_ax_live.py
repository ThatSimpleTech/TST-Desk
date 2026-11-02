"""Live accessibility run against a GTK form this test opens (TD-5220).

Opt-in twice: the ``desktop`` marker selects it (``-m desktop``) and
``TST_AX_LIVE=1`` lets it run. A marker is a selector, not a guard (see
the 2026-08-18 DECISIONS correction), so the variable is the guard.

It cannot touch another window: every ref comes from a snapshot of the
window whose pid is the form this test started, and every action is a
headless accessibility call (no pointer, no keyboard focus).

Needs a graphical session with the AT-SPI bus, PyGObject for the form's
Python (``TST_AX_FORM_PYTHON``, default ``/usr/bin/python3``) and the CLI
(``TST_AX_BINARY``, else ``PATH``) at the pinned release or the digest
in ``TST_AX_SHA256``.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from tstd.cu_policy import CuPolicy
from tstd.desktop import DesktopError
from tstd.desktop.ax_cli import AgentDesktopCli, child_env, resolve_binary
from tstd.desktop.ax_driver import AccessibilityDriver

pytestmark = [
    pytest.mark.desktop,
    pytest.mark.skipif(
        os.environ.get("TST_AX_LIVE") != "1", reason="set TST_AX_LIVE=1 to drive a real desktop"
    ),
]

FORM = Path(__file__).parent / "fixtures" / "agent_desktop" / "testform.py"


def _wait_for(predicate: object, seconds: float = 10.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():  # type: ignore[operator]
            return True
        time.sleep(0.2)
    return False


@pytest.fixture
def form(tmp_path: Path) -> Iterator[tuple[subprocess.Popen[bytes], Path]]:
    log = tmp_path / "form.log"
    python = os.environ.get("TST_AX_FORM_PYTHON", "/usr/bin/python3")
    proc = subprocess.Popen(
        [python, str(FORM), str(log)],
        env=child_env(os.environ, None),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        assert _wait_for(lambda: log.exists() and "ready" in log.read_text()), "form never started"
        yield proc, log
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def _driver(tmp_path: Path) -> AccessibilityDriver:
    binary = resolve_binary(os.environ.get("TST_AX_BINARY", ""))
    cli = AgentDesktopCli(
        binary, sha256=os.environ.get("TST_AX_SHA256", ""), state_dir=tmp_path / "state"
    )
    return AccessibilityDriver(cli, policy=lambda: CuPolicy())


def _ref(text: str, role: str, name: str) -> str:
    match = re.search(rf'^\s*{role} "{name}" (@\S+)', text, re.MULTILINE)
    assert match is not None, f"{role} {name!r} not in:\n{text}"
    return match.group(1)


async def test_drives_the_form_and_refuses_the_password(
    tmp_path: Path, form: tuple[subprocess.Popen[bytes], Path]
) -> None:
    proc, log = form
    driver = _driver(tmp_path)

    window_id = None
    for _ in range(25):
        rows = json.loads(await driver.windows())["windows"]
        mine = [r for r in rows if r["pid"] == proc.pid]
        if mine:
            window_id = mine[0]["window_id"]
            break
        await asyncio.sleep(0.4)
    assert window_id, "the form's window never reached the accessibility bus"

    text = await driver.snapshot(window_id=window_id)
    assert "TST Desk Test Form" in text.splitlines()[0]
    username = _ref(text, "textfield", "Username")
    password = _ref(text, "textfield", "Password")
    remember = _ref(text, "checkbox", "Remember me")
    sign_in = _ref(text, "button", "Sign in")
    assert re.search(r'"Password" @\S+ \[[^\]]*secure', text)

    typed = json.loads(await driver.act(username, "type", "tst-user"))
    assert typed["post_state"]["value"] == "[8 chars]"
    assert _wait_for(lambda: "username='tst-user'" in log.read_text(), 5)

    for action in ("type", "set_value"):
        with pytest.raises(DesktopError) as refused:
            await driver.act(password, action, "not-a-password")
        assert refused.value.code == "ax_secure_field"

    await driver.act(remember, "toggle")
    await driver.act(sign_in, "click")
    assert _wait_for(lambda: "clicked=Sign in" in log.read_text(), 5)

    state = await driver.state_for_ref(username)
    assert state is not None and '"tst-user"' in state and "@s" not in state

    driver.set_killed(True)
    with pytest.raises(DesktopError) as killed:
        await driver.act(sign_in, "click")
    assert killed.value.code == "cu_killed"

    lines = log.read_text().splitlines()
    assert "remember=True" in lines
    assert lines.count("clicked=Sign in") == 1
    assert not any(line.startswith("password_len") for line in lines)
