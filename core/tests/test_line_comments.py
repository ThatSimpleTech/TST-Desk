"""TD-5132: line comments on the diff view go back to the agent.

The renderer on its own (what the model reads for each comment), the wire
model's checks, the held-message round trip, and a real daemon on loopback
with the mock provider for the verb end to end.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from tests.machine_helpers import Fleet, ask, recv_until, window
from tstd.line_comments import CONTEXT_LINES, LineCommentError, build_comments_turn
from tstd.mock import MockProvider, Script
from tstd.policy import PolicyConfig, PolicyRule
from tstd.protocol import LineComment, SubmitLineComments, parse_client_message
from tstd.queued_messages import messages_from_rows, queue_rows
from tstd.session import QueuedUserMessage

SOURCE = "\n".join(f"line {n}" for n in range(1, 21)) + "\n"


def _comment(
    path: Path | str, start: int, end: int, body: str = "rename this", **kw: Any
) -> LineComment:
    lines = kw.pop("lines", [f"line {n}" for n in range(start, end + 1)])
    return LineComment(
        id=kw.pop("id", "c1"),
        path=str(path),
        start_line=start,
        end_line=end,
        body=body,
        lines=lines,
    )


@pytest.fixture
def work(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    root.mkdir()
    (root / "src").mkdir()
    (root / "src" / "app.py").write_text(SOURCE, encoding="utf-8")
    return root


# ── What the model reads ─────────────────────────────────────────────


def test_a_comment_is_anchored_by_file_and_line_with_the_code_around_it(work: Path) -> None:
    turn = build_comments_turn([_comment(work / "src" / "app.py", 10, 10)], work, PolicyConfig())
    assert "1. src/app.py, line 10:" in turn.text
    assert "Comment: rename this" in turn.text
    # Three lines each side, the commented one marked.
    for n in range(10 - CONTEXT_LINES, 10 + CONTEXT_LINES + 1):
        assert f"{n} | line {n}" in turn.text
    assert "line 6\n" not in turn.text and "| line 14" not in turn.text
    assert ">" + " 10 | line 10" in turn.text
    assert "  9 | line 9" in turn.text
    assert [(a.path, a.start_line, a.end_line) for a in turn.anchors] == [
        (str((work / "src" / "app.py").resolve()), 10, 10)
    ]


def test_all_comments_go_as_one_message_in_order(work: Path) -> None:
    other = work / "b.txt"
    other.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
    comments = [
        _comment(work / "src" / "app.py", 2, 4, "split this", id="c1"),
        _comment(other, 2, 2, "typo", id="c2", lines=["beta"]),
    ]
    turn = build_comments_turn(comments, work, PolicyConfig())
    assert turn.text.startswith("Review comments on your changes (2 comments).")
    first = turn.text.index("1. src/app.py, lines 2-4:")
    second = turn.text.index("2. b.txt, line 2:")
    assert first < second
    assert "> 2 | line 2" in turn.text and "> 4 | line 4" in turn.text
    assert [a.id for a in turn.anchors] == ["c1", "c2"]


def test_a_file_changed_since_the_diff_sends_the_reviewed_lines_marked(work: Path) -> None:
    (work / "src" / "app.py").write_text("something else\n" * 20, encoding="utf-8")
    turn = build_comments_turn([_comment(work / "src" / "app.py", 5, 6)], work, PolicyConfig())
    assert "the file has changed since this diff" in turn.text
    assert "> 5 | line 5" in turn.text and "> 6 | line 6" in turn.text
    assert "something else" not in turn.text


def test_a_deleted_file_sends_the_reviewed_lines(work: Path) -> None:
    gone = work / "gone.py"
    turn = build_comments_turn([_comment(gone, 1, 1, lines=["x = 1"])], work, PolicyConfig())
    assert "could not be read now" in turn.text
    assert "> 1 | x = 1" in turn.text


def test_a_policy_rule_that_asks_keeps_the_rest_of_the_file_out(work: Path) -> None:
    policy = PolicyConfig(rules=[PolicyRule(tool="fs_read", args="src/**", effect="ask")])
    turn = build_comments_turn([_comment(work / "src" / "app.py", 10, 10)], work, policy)
    assert "a policy rule keeps the rest of this file out" in turn.text
    assert "line 9" not in turn.text and "line 11" not in turn.text


def test_a_path_outside_the_workspace_refuses_every_comment(work: Path, tmp_path: Path) -> None:
    outside = tmp_path / "secret.txt"
    outside.write_text("line 1\n", encoding="utf-8")
    comments = [_comment(work / "src" / "app.py", 1, 1), _comment(outside, 1, 1, id="c2")]
    with pytest.raises(LineCommentError) as caught:
        build_comments_turn(comments, work, PolicyConfig())
    assert caught.value.code == "line_comment_outside_workspace"


def test_dotdot_out_of_the_workspace_is_refused(work: Path) -> None:
    with pytest.raises(LineCommentError):
        build_comments_turn([_comment(work / ".." / "elsewhere.py", 1, 1)], work, PolicyConfig())


def test_code_with_backticks_cannot_close_the_fence(work: Path) -> None:
    target = work / "doc.md"
    target.write_text("```\nrun this\n```\n", encoding="utf-8")
    turn = build_comments_turn([_comment(target, 2, 2, lines=["run this"])], work, PolicyConfig())
    assert "\n````\n" in turn.text


def test_too_much_code_is_refused(work: Path) -> None:
    big = work / "big.txt"
    line = "x" * 3999
    big.write_text((line + "\n") * 200, encoding="utf-8")
    comments = [_comment(big, 1, 200, id=f"c{i}", lines=[line] * 200) for i in range(1, 2)]
    with pytest.raises(LineCommentError) as caught:
        build_comments_turn(comments, work, PolicyConfig())
    assert caught.value.code == "line_comments_too_large"


# ── The wire model ───────────────────────────────────────────────────


def test_a_range_must_be_ordered_and_match_its_lines() -> None:
    with pytest.raises(ValidationError):
        _comment("/w/a.py", 5, 4, lines=[])
    with pytest.raises(ValidationError):
        _comment("/w/a.py", 1, 2, lines=["only one"])
    with pytest.raises(ValidationError):
        _comment("/w/a.py", 1, 1, body="   ")


def test_submit_line_comments_parses_from_the_wire() -> None:
    raw = {
        "type": "submit_line_comments",
        "session_id": "s1",
        "comments": [
            {
                "id": "c1",
                "path": "/w/a.py",
                "start_line": 3,
                "end_line": 3,
                "body": "x",
                "lines": ["y"],
            }
        ],
    }
    msg = parse_client_message(json.dumps(raw))
    assert isinstance(msg, SubmitLineComments)
    assert msg.comments[0].start_line == 3


def test_a_held_message_keeps_its_comments_across_a_restart(work: Path) -> None:
    turn = build_comments_turn([_comment(work / "src" / "app.py", 3, 3)], work, PolicyConfig())
    message = QueuedUserMessage(display=turn.text, provider=turn.text, line_comments=turn.anchors)
    back = messages_from_rows(json.loads(json.dumps(queue_rows([message]))))
    assert back[0].line_comments == turn.anchors
    plain = messages_from_rows(queue_rows([QueuedUserMessage.plain("hi")]))
    assert plain[0].line_comments is None


# ── End to end on a real daemon ──────────────────────────────────────


@pytest.fixture
async def fleet(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Fleet]:
    made = Fleet(monkeypatch)
    try:
        yield made
    finally:
        await made.close()


def _user_texts(mock: MockProvider) -> list[str]:
    texts: list[str] = []
    for call in mock.calls:
        for message in call.messages:
            if message.role == "user" and isinstance(message.content, str):
                texts.append(message.content)
    return texts


async def test_submitted_comments_reach_the_model_as_one_turn(
    tmp_path: Path, work: Path, fleet: Fleet
) -> None:
    mock = MockProvider(default=Script(kind="stream", content="done"))
    daemon = await fleet.start(tmp_path / "d", provider=mock)
    target = str(work / "src" / "app.py")
    async with window(daemon) as ws:
        opened = await ask(ws, {"type": "open_workspace", "path": str(work)}, "session_state")
        sid = str(opened["session_id"])
        await ws.send(json.dumps({"type": "attach", "session_id": sid, "from_seq": 1}))
        comments = [
            {
                "id": "c1",
                "path": target,
                "start_line": 8,
                "end_line": 9,
                "body": "merge these",
                "lines": ["line 8", "line 9"],
            },
            {
                "id": "c2",
                "path": target,
                "start_line": 15,
                "end_line": 15,
                "body": "drop it",
                "lines": ["line 15"],
            },
        ]
        await ws.send(
            json.dumps({"type": "submit_line_comments", "session_id": sid, "comments": comments})
        )
        turn = await recv_until(ws, lambda f: f.get("type") == "user_turn")
        assert [c["id"] for c in turn["line_comments"]] == ["c1", "c2"]
        assert turn["line_comments"][0]["start_line"] == 8
        await recv_until(ws, lambda f: f.get("type") == "turn_complete", within=10)
    sent = [t for t in _user_texts(mock) if "Review comments" in t]
    assert len(sent) == 1, "one message for all the comments"
    assert "src/app.py, lines 8-9" in sent[0] and "src/app.py, line 15" in sent[0]
    assert "  5 | line 5" in sent[0], "the code around the comment"


async def test_a_refused_submission_names_the_verb_and_queues_nothing(
    tmp_path: Path, work: Path, fleet: Fleet
) -> None:
    mock = MockProvider(default=Script(kind="stream", content="done"))
    daemon = await fleet.start(tmp_path / "d", provider=mock)
    outside = tmp_path / "other.txt"
    outside.write_text("x\n", encoding="utf-8")
    async with window(daemon) as ws:
        opened = await ask(ws, {"type": "open_workspace", "path": str(work)}, "session_state")
        sid = str(opened["session_id"])
        frame = {
            "type": "submit_line_comments",
            "session_id": sid,
            "comments": [
                {
                    "id": "c1",
                    "path": str(outside),
                    "start_line": 1,
                    "end_line": 1,
                    "body": "b",
                    "lines": ["x"],
                }
            ],
        }
        error = await ask(ws, frame, "error")
        assert error["code"] == "line_comment_outside_workspace"
        assert error["in_reply_to"] == "submit_line_comments"
    assert mock.calls == []
