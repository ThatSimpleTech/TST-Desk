"""Every window hears when approvals are switched off or on (APPROVE-05).

``setup_state`` was only the reply to the window that flipped the switch,
so a second window, or a phone, kept showing approvals as on.
"""

from __future__ import annotations

from pathlib import Path

from tests.test_add_model import _ask, _stop
from tests.test_picker_groups import _next_setup_state, _second_window, _seeded


async def test_the_other_window_is_told_both_ways(tmp_path: Path) -> None:
    daemon, task, ws = await _seeded(tmp_path)
    other = await _second_window(daemon)
    try:
        await _ask(ws, {"type": "set_skip_all_approvals", "enabled": True}, "setup_state")
        assert (await _next_setup_state(other))["skip_all_approvals"] is True
        await _ask(ws, {"type": "set_skip_all_approvals", "enabled": False}, "setup_state")
        assert (await _next_setup_state(other))["skip_all_approvals"] is False
    finally:
        await other.close()
        await _stop(task, ws)
