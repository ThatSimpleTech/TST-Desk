"""Approval summaries show hidden and reordering characters (RENDERSEC-02)."""

from __future__ import annotations

from tstd.protocol import ApprovalRequest
from tstd.reveal import reveal

RLO = chr(0x202E)
ZWSP = chr(0x200B)
ESC = chr(0x1B)


def test_a_right_to_left_override_is_shown_in_logical_order() -> None:
    shown = reveal(f"rm -rf {RLO}/emoh")
    assert shown == "rm -rf ⟨RLO U+202E⟩/emoh"
    assert RLO not in shown


def test_zero_width_and_terminal_codes_are_named() -> None:
    assert reveal(f"cu{ZWSP}rl") == "cu⟨ZWSP U+200B⟩rl"
    assert reveal(f"{ESC}[2Jok") == "⟨ESC U+001B⟩[2Jok"
    assert reveal(chr(0x7)) == "⟨U+0007⟩"


def test_ordinary_text_is_untouched() -> None:
    text = "Run `npm test`\n\tthen 日本語 and émigré — done"
    assert reveal(text) == text


def test_every_approval_summary_is_revealed_and_the_arguments_are_not() -> None:
    command = f"rm -rf {RLO}/emoh"
    event = ApprovalRequest(
        session_id="s1",
        tool_call_id="tc-1",
        tool_name="shell",
        arguments={"command": command},
        decision_class="B",
        summary=f"Run `{command}`",
        reason="asked",
        seq=1,
    )
    assert RLO not in event.summary and "⟨RLO U+202E⟩" in event.summary
    assert event.arguments["command"] == command  # what runs is unchanged
