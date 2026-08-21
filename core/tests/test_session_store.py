"""Tests for the durable session registry snapshot (TD-1002)."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

from tstd.session_store import SessionStore


class TestSessionStore:
    async def test_upsert_then_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp))
            await store.upsert("s1", "/ws/one", "idle")
            recs = store.records()
            assert len(recs) == 1
            assert recs[0].session_id == "s1"
            assert recs[0].workspace_path == "/ws/one"
            assert recs[0].state == "idle"

    async def test_update_state_preserves_created_at(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp))
            await store.upsert("s1", "/ws", "idle", created_at="t0")
            await store.update_state("s1", "running")
            rec = store.get("s1")
            assert rec is not None
            assert rec.state == "running"
            assert rec.created_at == "t0"

    async def test_remove(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp))
            await store.upsert("s1", "/ws", "idle")
            await store.remove("s1")
            assert store.get("s1") is None
            assert store.records() == []

    async def test_restricted_mode(self) -> None:
        """0o600 on POSIX; on Windows the chmod is a no-op, by decision.

        TD-1406 chose the documented no-op over shelling out to ``icacls``:
        the store lives under ``%LOCALAPPDATA%``, whose ACL already grants
        the user, SYSTEM and Administrators and nobody else, so the
        directory is what protects the file and 0o600 never was.  The
        no-op is asserted rather than skipped so it stays a decision on
        the record instead of an untested silence — see ``docs/windows.md``.
        """
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp))
            await store.upsert("s1", "/ws", "idle")
            path = Path(tmp) / "sessions.json"
            assert path.exists()
            mode = path.stat().st_mode & 0o777
            if sys.platform == "win32":
                # os.chmod can only toggle the read-only attribute, and
                # 0o600 carries a write bit, so the file stays writable
                # and stat reports the Windows default.
                assert mode == 0o666, f"expected the Windows no-op 0o666, got {oct(mode)}"
                assert path.read_text()  # still owner-readable, write did not raise
            else:
                assert mode == 0o600, f"expected 0o600, got {oct(mode)}"

    async def test_writes_valid_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            await SessionStore(Path(tmp)).upsert("s1", "/ws", "idle")
            data = json.loads((Path(tmp) / "sessions.json").read_text())
            assert data[0]["session_id"] == "s1"
            assert "created_at" in data[0]
            assert "updated_at" in data[0]

    async def test_persists_across_instances(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            await SessionStore(Path(tmp)).upsert("s1", "/ws", "running")
            store2 = SessionStore(Path(tmp))
            rec = store2.get("s1")
            assert rec is not None
            assert rec.state == "running"
            assert rec.workspace_path == "/ws"

    async def test_missing_store_loads_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp))
            assert store.records() == []

    async def test_corrupt_store_loads_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "sessions.json").write_text("{not valid json")
            store = SessionStore(Path(tmp))
            assert store.records() == []


class TestLifecycleMetadata:
    """Archive + move-to-project durability (TD-1715)."""

    async def test_archived_flag_persists_across_instances(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp))
            await store.upsert("s1", "/ws", "idle")
            assert await store.set_archived("s1", True) is True

            reloaded = SessionStore(Path(tmp))
            rec = reloaded.get("s1")
            assert rec is not None
            assert rec.archived is True

    async def test_unarchive_persists_too(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp))
            await store.upsert("s1", "/ws", "idle")
            await store.set_archived("s1", True)
            await store.set_archived("s1", False)
            rec = SessionStore(Path(tmp)).get("s1")
            assert rec is not None
            assert rec.archived is False

    async def test_a_state_refresh_does_not_unfile_a_session(self) -> None:
        """A live session's state changes constantly; archiving must survive it."""
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp))
            await store.upsert("s1", "/ws", "idle")
            await store.set_archived("s1", True)
            await store.update_state("s1", "running")
            await store.upsert("s1", "/ws", "complete")
            rec = store.get("s1")
            assert rec is not None
            assert rec.archived is True

    async def test_workspace_reassignment_persists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp))
            await store.upsert("s1", "/ws/origin", "idle")
            created = store.get("s1")
            assert created is not None
            created_at = created.created_at

            assert await store.set_workspace("s1", "/ws/target") is True
            rec = SessionStore(Path(tmp)).get("s1")
            assert rec is not None
            assert rec.workspace_path == "/ws/target"
            # A move is not a new session: identity and birthday are kept.
            assert rec.session_id == "s1"
            assert rec.created_at == created_at

    async def test_unknown_ids_report_rather_than_pretending(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp))
            assert await store.set_archived("nope", True) is False
            assert await store.set_workspace("nope", "/ws") is False

    async def test_a_store_written_before_this_field_still_loads(self) -> None:
        """Older snapshots have no `archived` key; they must load, not drop."""
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "sessions.json").write_text(
                json.dumps(
                    [
                        {
                            "session_id": "s1",
                            "workspace_path": "/ws",
                            "state": "complete",
                            "created_at": "2026-08-13T10:00:00Z",
                            "updated_at": "2026-08-13T10:00:00Z",
                        }
                    ]
                )
            )
            rec = SessionStore(Path(tmp)).get("s1")
            assert rec is not None
            assert rec.archived is False
            assert rec.title is None


class TestSessionTitle:
    """Auto-title from the first non-empty user message (TD-3001)."""

    async def test_first_message_titles_and_later_ones_do_not(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp))
            await store.upsert("s1", "/ws", "idle")
            assert await store.maybe_set_title("s1", "  Fix the rail titles  ") is True
            rec = store.get("s1")
            assert rec is not None
            assert rec.title == "Fix the rail titles"
            assert rec.auto_title == "Fix the rail titles"

            assert await store.maybe_set_title("s1", "a later message") is False
            assert store.get("s1") is not None
            assert store.get("s1").title == "Fix the rail titles"  # type: ignore[union-attr]
            assert store.get("s1").auto_title == "Fix the rail titles"  # type: ignore[union-attr]

    async def test_empty_and_whitespace_stay_untitled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp))
            await store.upsert("s1", "/ws", "idle")
            assert await store.maybe_set_title("s1", "") is False
            assert await store.maybe_set_title("s1", "   \n\t  ") is False
            rec = store.get("s1")
            assert rec is not None
            assert rec.title is None
            assert rec.auto_title is None

    async def test_title_is_first_line_collapsed_and_capped(self) -> None:
        from tstd.session_store import SESSION_TITLE_MAX_LEN, title_from_user_message

        assert title_from_user_message("hello\nworld") == "hello"
        assert title_from_user_message("  lots   of\tspace  ") == "lots of space"
        long = "x" * (SESSION_TITLE_MAX_LEN + 20)
        assert title_from_user_message(long) == "x" * SESSION_TITLE_MAX_LEN
        assert title_from_user_message("") is None

    async def test_title_survives_upsert_and_reload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp))
            await store.upsert("s1", "/ws", "idle")
            await store.maybe_set_title("s1", "Keep this title")
            await store.update_state("s1", "running")
            await store.upsert("s1", "/ws", "complete")
            rec = SessionStore(Path(tmp)).get("s1")
            assert rec is not None
            assert rec.title == "Keep this title"
            assert rec.auto_title == "Keep this title"

    async def test_a_store_written_before_title_still_loads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "sessions.json").write_text(
                json.dumps(
                    [
                        {
                            "session_id": "s1",
                            "workspace_path": "/ws",
                            "state": "idle",
                            "created_at": "2026-08-13T10:00:00Z",
                            "updated_at": "2026-08-13T10:00:00Z",
                            "archived": False,
                        }
                    ]
                )
            )
            rec = SessionStore(Path(tmp)).get("s1")
            assert rec is not None
            assert rec.title is None
            assert rec.auto_title is None


class TestSessionRename:
    """Display title vs auto_title (TD-3002)."""

    async def test_set_title_writes_display_without_touching_auto_title(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp))
            await store.upsert("s1", "/ws", "idle")
            await store.maybe_set_title("s1", "First message title")
            assert await store.set_title("s1", "  My custom name  ") is True
            rec = store.get("s1")
            assert rec is not None
            assert rec.title == "My custom name"
            assert rec.auto_title == "First message title"

    async def test_empty_set_title_restores_auto_title(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp))
            await store.upsert("s1", "/ws", "idle")
            await store.maybe_set_title("s1", "First message title")
            await store.set_title("s1", "Custom")
            assert await store.set_title("s1", "") is True
            rec = store.get("s1")
            assert rec is not None
            assert rec.title == "First message title"
            assert rec.auto_title == "First message title"
            await store.set_title("s1", "   \n\t  ")
            assert store.get("s1").title == "First message title"  # type: ignore[union-attr]
            await store.set_title("s1", None)
            assert store.get("s1").title == "First message title"  # type: ignore[union-attr]

    async def test_empty_set_title_without_auto_title_clears_display(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp))
            await store.upsert("s1", "/ws", "idle")
            await store.set_title("s1", "Custom")
            await store.set_title("s1", "")
            rec = store.get("s1")
            assert rec is not None
            assert rec.title is None
            assert rec.auto_title is None

    async def test_rename_then_first_message_keeps_custom_title(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp))
            await store.upsert("s1", "/ws", "idle")
            await store.set_title("s1", "Custom")
            assert await store.maybe_set_title("s1", "First message") is True
            rec = store.get("s1")
            assert rec is not None
            assert rec.title == "Custom"
            assert rec.auto_title == "First message"

    async def test_set_title_is_first_line_collapsed_and_capped(self) -> None:
        from tstd.session_store import SESSION_TITLE_MAX_LEN

        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp))
            await store.upsert("s1", "/ws", "idle")
            await store.set_title("s1", "hello\nworld")
            assert store.get("s1").title == "hello"  # type: ignore[union-attr]
            long = "x" * (SESSION_TITLE_MAX_LEN + 20)
            await store.set_title("s1", long)
            assert store.get("s1").title == "x" * SESSION_TITLE_MAX_LEN  # type: ignore[union-attr]

    async def test_rename_survives_upsert_and_reload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp))
            await store.upsert("s1", "/ws", "idle")
            await store.maybe_set_title("s1", "Auto")
            await store.set_title("s1", "Custom")
            await store.update_state("s1", "running")
            await store.upsert("s1", "/ws", "complete")
            rec = SessionStore(Path(tmp)).get("s1")
            assert rec is not None
            assert rec.title == "Custom"
            assert rec.auto_title == "Auto"

    async def test_a_store_written_before_auto_title_treats_title_as_auto(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "sessions.json").write_text(
                json.dumps(
                    [
                        {
                            "session_id": "s1",
                            "workspace_path": "/ws",
                            "state": "idle",
                            "created_at": "2026-08-13T10:00:00Z",
                            "updated_at": "2026-08-13T10:00:00Z",
                            "archived": False,
                            "title": "Old title",
                        }
                    ]
                )
            )
            rec = SessionStore(Path(tmp)).get("s1")
            assert rec is not None
            assert rec.title == "Old title"
            assert rec.auto_title == "Old title"

    async def test_unknown_id_reports_rather_than_pretending(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp))
            assert await store.set_title("nope", "x") is False
