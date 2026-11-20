"""``fs_edit`` replaces every occurrence, or several things at once (TD-5270).

A rename used to cost one call per occurrence. The tool stays one file per
call, on the same path the classifier already judges; what changes is how
much one call can do, and that a batch either all lands or none of it does.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.test_read_tools import make_dispatcher
from tstd.autonomy import DecisionClass
from tstd.tools import create_registry, fs_edit
from tstd.tools.write import MAX_EDITS, Edit, apply_edits, edits_from_arguments

TEXT = "alpha = 1\nbeta = alpha + 1\ngamma = alpha * beta\n"


def _file(tmp_path: Path, text: str = TEXT) -> Path:
    target = tmp_path / "a.py"
    target.write_text(text, encoding="utf-8")
    return target


class TestReplaceAll:
    async def test_every_occurrence_goes_and_the_count_is_reported(self, tmp_path: Path) -> None:
        target = _file(tmp_path)
        out = await fs_edit(None, str(target), "alpha", "first", replace_all=True)
        assert target.read_text() == TEXT.replace("alpha", "first")
        assert "replaced 3 occurrences" in out

    async def test_none_found_still_fails(self, tmp_path: Path) -> None:
        target = _file(tmp_path)
        with pytest.raises(ValueError, match="not found"):
            await fs_edit(None, str(target), "delta", "x", replace_all=True)
        assert target.read_text() == TEXT

    async def test_without_it_two_occurrences_are_still_ambiguous(self, tmp_path: Path) -> None:
        target = _file(tmp_path)
        with pytest.raises(ValueError, match="ambiguous: 3 occurrences") as caught:
            await fs_edit(None, str(target), "alpha", "x")
        assert "replace_all" in str(caught.value)
        assert target.read_text() == TEXT


class TestABatch:
    async def test_edits_apply_in_order_each_to_what_the_last_left(self, tmp_path: Path) -> None:
        target = _file(tmp_path)
        out = await fs_edit(
            None,
            str(target),
            edits=[
                {"old_string": "alpha", "new_string": "a", "replace_all": True},
                {"old_string": "beta = a + 1", "new_string": "beta = a + 2"},
                {"old_string": "gamma", "new_string": "c"},
            ],
        )
        assert target.read_text() == "a = 1\nbeta = a + 2\nc = a * beta\n"
        assert "3 edits" in out and "replaced 3, 1, 1" in out and "5 occurrences" in out

    @pytest.mark.parametrize(
        ("bad", "says"),
        [
            ({"old_string": "nowhere", "new_string": "x"}, "edit 2: target string not found"),
            ({"old_string": "a", "new_string": "x"}, "edit 2: target string is ambiguous"),
        ],
    )
    async def test_one_edit_that_cannot_apply_leaves_the_file_as_it_was(
        self, tmp_path: Path, bad: dict[str, Any], says: str
    ) -> None:
        target = _file(tmp_path)
        before = target.read_bytes()
        with pytest.raises(ValueError, match=says) as caught:
            await fs_edit(
                None,
                str(target),
                edits=[
                    {"old_string": "alpha = 1", "new_string": "alpha = 9"},
                    bad,
                    {"old_string": "gamma", "new_string": "c"},
                ],
            )
        assert "nothing was changed" in str(caught.value)
        assert target.read_bytes() == before

    async def test_the_file_is_written_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from tstd.tools import write as write_module

        writes: list[str] = []
        real = write_module._atomic_write

        def counting(path: Path, text: str) -> None:
            writes.append(text)
            real(path, text)

        monkeypatch.setattr(write_module, "_atomic_write", counting)
        target = _file(tmp_path)
        await fs_edit(
            None,
            str(target),
            edits=[
                {"old_string": "alpha = 1", "new_string": "alpha = 2"},
                {"old_string": "gamma", "new_string": "c"},
            ],
        )
        assert len(writes) == 1 and writes[0] == target.read_text()


class TestTheTwoForms:
    @pytest.mark.parametrize(
        ("kwargs", "says"),
        [
            ({}, "give old_string and new_string, or a list of edits"),
            ({"old_string": "alpha"}, "give old_string and new_string, or a list of edits"),
            (
                {
                    "old_string": "a",
                    "new_string": "b",
                    "edits": [{"old_string": "a", "new_string": "b"}],
                },
                "not both",
            ),
            ({"edits": []}, "edits is empty"),
            ({"edits": [{"old_string": "", "new_string": "x"}]}, "edit 1 needs old_string"),
            ({"edits": [{"new_string": "x"}]}, "edit 1 needs old_string"),
            (
                {"edits": [{"old_string": "a", "new_string": "b"}] * (MAX_EDITS + 1)},
                f"too many edits: {MAX_EDITS + 1}",
            ),
        ],
    )
    async def test_anything_else_changes_nothing(
        self, tmp_path: Path, kwargs: dict[str, Any], says: str
    ) -> None:
        target = _file(tmp_path)
        with pytest.raises(ValueError, match=says):
            await fs_edit(None, str(target), **kwargs)
        assert target.read_text() == TEXT

    def test_the_single_form_is_one_edit(self) -> None:
        assert edits_from_arguments("a", "b", False, None) == [Edit("a", "b", False)]
        assert edits_from_arguments("a", "", True, None) == [Edit("a", "", True)]

    def test_applying_is_pure(self) -> None:
        text, counts = apply_edits("x x y", [Edit("x", "z", True), Edit("y", "x")], "f")
        assert text == "z z x" and counts == [2, 1]


class TestTheSchemaAndTheGate:
    def test_the_tool_is_still_one_file_judged_by_its_path(self) -> None:
        tool = create_registry().require("fs_edit")
        assert tool.path_fields == ("path",)
        assert tool.side_effect_class == "ask" and tool.mutates is True
        props = tool.parameters["properties"]
        assert tool.parameters["required"] == ["path"]
        assert props["edits"]["maxItems"] == MAX_EDITS == 20
        assert props["edits"]["items"]["required"] == ["old_string", "new_string"]
        assert props["replace_all"]["type"] == "boolean"

    async def test_a_batch_goes_through_the_dispatcher_as_one_call(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        dispatcher = make_dispatcher(tmp_path)
        monkeypatch.chdir(tmp_path)
        (tmp_path / "a.txt").write_text("one two one\n", encoding="utf-8")
        result = await dispatcher.dispatch(
            "c1",
            "fs_edit",
            {
                "path": "a.txt",
                "edits": [
                    {"old_string": "one", "new_string": "1", "replace_all": True},
                    {"old_string": "two", "new_string": "2"},
                ],
            },
        )
        assert result.status == "success", result.output
        assert result.diff is not None and "-one two one" in result.diff
        assert (tmp_path / "a.txt").read_text() == "1 2 1\n"
        assert "2 edits" in result.output

    async def test_a_batch_on_a_steering_file_is_still_outside_the_wall(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        dispatcher = make_dispatcher(tmp_path)
        monkeypatch.chdir(tmp_path)
        (tmp_path / "AGENTS.md").write_text("rules\n", encoding="utf-8")
        result = await dispatcher.dispatch(
            "c1",
            "fs_edit",
            {"path": "AGENTS.md", "edits": [{"old_string": "rules", "new_string": "none"}]},
        )
        assert result.status != "success"
        assert (tmp_path / "AGENTS.md").read_text() == "rules\n"
        assert result.decision_class == DecisionClass.C.value
