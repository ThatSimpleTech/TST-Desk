"""Always allow never replaces a stricter rule saved for the same call (TD-5149).

Found in the TD-5141 review: Always allow saved the call's raw argument
summary, and ``add_rule`` first deleted any rule with the same
``(tool, args)``.  So a read of a file literally named ``*.env*`` wrote
``fs_read: *.env* -> auto`` over the user's documented guard rule.  The
summary is escaped since TD-5101; these tests pin the rest: the guard is
never replaced, the card does not offer what it cannot save, a click that
races the rule is answered the way the rule says (the rule may be in the
session or only in the file, edited by hand while the session is open), the
save merges into the file instead of writing the session's copy over it, and
an escaped rule reads as the path it was for.
"""

from __future__ import annotations

import asyncio
import fnmatch
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

from tests.test_approval_flow import _open_workspace, _park, _recv_until
from tstd.autonomy.classifier import DecisionClass
from tstd.daemon import Daemon
from tstd.policy import (
    PolicyConfig,
    PolicyRule,
    StricterRuleError,
    _literal,
    add_rule,
    exact_text,
    load_policy,
    propose_always_allow,
    resolve,
    rule_summary,
    save_policy,
    stricter_rule,
)
from tstd.protocol import ApprovalRequest as ApprovalRequestEvent
from tstd.session import Session
from tstd.tools import Tool

B = DecisionClass.B
FS_READ = Tool(name="fs_read", path_fields=("path",))
SHELL = Tool(name="shell")


def _rule(tool: str, args: str, effect: str) -> PolicyRule:
    return PolicyRule.model_validate({"tool": tool, "args": args, "effect": effect})


# ── add_rule never loosens ──────────────────────────────────────────────


class TestAddRule:
    @pytest.mark.parametrize("effect", ["ask", "never"])
    def test_refuses_to_replace_a_stricter_rule(self, effect: str) -> None:
        guard = _rule("fs_read", "*.env*", effect)
        config = PolicyConfig(rules=[guard])
        with pytest.raises(StricterRuleError) as raised:
            add_rule(config, _rule("fs_read", "*.env*", "auto"))
        assert raised.value.existing == guard
        assert config.rules == [guard]

    def test_an_ask_rule_is_not_replaced_by_auto_or_never_by_ask(self) -> None:
        config = PolicyConfig(rules=[_rule("shell", "npm test", "never")])
        with pytest.raises(StricterRuleError):
            add_rule(config, _rule("shell", "npm test", "ask"))
        assert config.rules == [_rule("shell", "npm test", "never")]

    def test_the_same_or_a_stricter_effect_still_replaces(self) -> None:
        config = PolicyConfig(rules=[_rule("shell", "npm test", "auto")])
        add_rule(config, _rule("shell", "npm test", "auto"))
        assert config.rules == [_rule("shell", "npm test", "auto")]
        add_rule(config, _rule("shell", "npm test", "never"))
        assert config.rules == [_rule("shell", "npm test", "never")]

    def test_another_identity_is_added_beside_the_guard(self) -> None:
        guard = _rule("fs_read", "*.env*", "never")
        config = PolicyConfig(rules=[guard, _rule("fs_*", "*.env*", "ask")])
        assert stricter_rule(config, _rule("fs_read", "[*].env[*]", "auto")) is None
        add_rule(config, _rule("fs_read", "[*].env[*]", "auto"))
        assert guard in config.rules
        assert len(config.rules) == 3


# ── The story's file: a read of a file literally named `*.env*` ────────


class TestTheGuardRuleSurvives:
    def test_always_allow_on_a_star_env_star_file_keeps_the_guard(self, tmp_path: Path) -> None:
        path = str(tmp_path / "*.env*")
        proposed = propose_always_allow(FS_READ, {"path": path}, B, tmp_path)
        assert proposed == _rule("fs_read", "[*].env[*]", "auto")

        config = PolicyConfig(rules=[_rule("fs_read", "*.env*", "ask")])
        add_rule(config, proposed)
        assert _rule("fs_read", "*.env*", "ask") in config.rules
        # The guard still asks for every other env file.
        for name in (".env", ".env.local", "prod.env.bak"):
            other = str(tmp_path / name)
            assert resolve(config, FS_READ, {"path": other}, B, tmp_path) == "ask", name

    def test_a_next_js_path_rule_matches_only_that_file(self, tmp_path: Path) -> None:
        path = str(tmp_path / "app" / "[id]" / "page.tsx")
        proposed = propose_always_allow(FS_READ, {"path": path}, B, tmp_path)
        assert proposed is not None
        assert proposed.args == "app/[[]id]/page.tsx"
        config = PolicyConfig(rules=[proposed])
        assert resolve(config, FS_READ, {"path": path}, B, tmp_path) == "auto"
        for other in ("app/i/page.tsx", "app/d/page.tsx", "app/[ix]/page.tsx"):
            assert resolve(config, FS_READ, {"path": str(tmp_path / other)}, B, tmp_path) == "ask"
        # Settings and the card read it as the path, not the escaped pattern.
        assert rule_summary(proposed).exact == "app/[id]/page.tsx"


# ── exact_text reads Always allow's escapes back ───────────────────────


class TestExactText:
    @pytest.mark.parametrize(
        "text",
        [
            "src/app.py",
            "app/[id]/page.tsx",
            "*.env*",
            "what?.md",
            "a[[]b]",
            "[]",
            "]x[",
            "ls *.py",
            "",
        ],
    )
    def test_it_inverts_the_escape_and_the_pattern_matches_only_that_text(self, text: str) -> None:
        pattern = _literal(text)
        assert exact_text(pattern) == text
        assert fnmatch.fnmatchcase(text, pattern)
        for near in (text + "x", "x" + text, text.replace("[", "a").replace("*", "b")):
            if near != text:
                assert not fnmatch.fnmatchcase(near, pattern), (text, near)

    @pytest.mark.parametrize(
        "pattern", ["**", "*.env*", "src/*.py", "file?.txt", "app/[id]/page.tsx", "[!a]", "[*]*"]
    )
    def test_a_glob_has_no_exact_text(self, pattern: str) -> None:
        assert exact_text(pattern) is None

    def test_rule_summary_carries_it(self) -> None:
        assert rule_summary(_rule("fs_read", "[*].env[*]", "auto")).exact == "*.env*"
        assert rule_summary(_rule("fs_read", "*.env*", "never")).exact is None
        assert rule_summary(_rule("shell", "npm test", "auto")).exact == "npm test"

    def test_a_guard_is_never_shown_as_exact(self) -> None:
        """``ask`` and ``never`` rules match in any case too (TD-5141, TD-5158)."""
        assert rule_summary(_rule("fs_read", ".env", "never")).exact is None
        assert rule_summary(_rule("fs_read", "app/[[]id]/page.tsx", "ask")).exact is None


# ── The daemon: no offer, and a racing click answered as the rule says ─


@asynccontextmanager
async def _daemon(tmp_path: Path) -> AsyncIterator[Daemon]:
    daemon = Daemon(data_dir=tmp_path / "data")
    task = asyncio.create_task(daemon.run())
    for _ in range(50):
        if daemon.ws_server.port:
            break
        await asyncio.sleep(0.05)
    try:
        yield daemon
    finally:
        daemon._shutdown_event.set()
        await asyncio.wait_for(task, timeout=3)


def _card(session: Session) -> ApprovalRequestEvent:
    return next(e for e in session.event_log.all_events if isinstance(e, ApprovalRequestEvent))


async def _always_allow(ws: Any, session: Session) -> dict[str, Any]:
    await ws.send(
        json.dumps({"type": "always_allow", "session_id": session.id, "tool_call_id": "tc-1"})
    )
    return await _recv_until(ws, "error")


class TestDaemon:
    @pytest.mark.asyncio
    async def test_an_ask_rule_for_the_call_is_kept_and_the_call_runs_once(
        self, tmp_path: Path
    ) -> None:
        guard = _rule("shell", "npm test", "ask")
        save_policy(tmp_path, PolicyConfig(rules=[guard]))
        async with _daemon(tmp_path) as daemon:
            ws, session = await _open_workspace(daemon, tmp_path)
            parked = await _park(session, "tc-1", SHELL, {"command": "npm test"}, B)
            # Saving would replace the user's rule, so the card doesn't offer it.
            assert _card(session).proposed_always_allow is None

            err = await _always_allow(ws, session)  # a client that sends it anyway
            assert err["code"] == "policy_rule_stricter"
            assert err["message"].startswith("Approved once.")
            assert "`shell: npm test` → ask" in err["message"]
            outcome = await asyncio.wait_for(parked, timeout=2)
            assert outcome.approved
            assert load_policy(tmp_path).rules == [guard]
            assert session.policy.rules == [guard]
            await ws.close()

    @pytest.mark.asyncio
    async def test_a_never_rule_added_while_the_card_waits_refuses_the_call(
        self, tmp_path: Path
    ) -> None:
        async with _daemon(tmp_path) as daemon:
            ws, session = await _open_workspace(daemon, tmp_path)
            parked = await _park(session, "tc-1", SHELL, {"command": "npm test"}, B)
            assert _card(session).proposed_always_allow is not None

            guard = _rule("shell", "npm test", "never")
            session.policy = PolicyConfig(rules=[guard])
            err = await _always_allow(ws, session)
            assert err["code"] == "policy_rule_stricter"
            assert err["message"].startswith("Denied.")
            outcome = await asyncio.wait_for(parked, timeout=2)
            assert not outcome.approved
            assert "`shell: npm test` → never" in outcome.message
            assert session.policy.rules == [guard]
            assert load_policy(tmp_path).rules == []
            await ws.close()

    @pytest.mark.parametrize(("effect", "approved"), [("never", False), ("ask", True)])
    @pytest.mark.asyncio
    async def test_a_rule_written_to_the_file_while_the_card_waits_is_kept(
        self, tmp_path: Path, effect: str, approved: bool
    ) -> None:
        """The realistic race: the user edits .tst/config.yaml by hand while
        the session is open. Nothing reloads the session's policy, so the
        check must read the file, or the save writes the stale copy over it."""
        async with _daemon(tmp_path) as daemon:
            ws, session = await _open_workspace(daemon, tmp_path)
            parked = await _park(session, "tc-1", SHELL, {"command": "npm test"}, B)
            assert _card(session).proposed_always_allow is not None

            guard = _rule("shell", "npm test", effect)
            save_policy(tmp_path, PolicyConfig(rules=[guard]))  # the hand edit
            err = await _always_allow(ws, session)
            assert err["code"] == "policy_rule_stricter"
            assert err["message"].startswith("Approved once." if approved else "Denied.")
            assert f"`shell: npm test` → {effect}" in err["message"]
            outcome = await asyncio.wait_for(parked, timeout=2)
            assert outcome.approved is approved
            assert load_policy(tmp_path).rules == [guard]
            await ws.close()

    @pytest.mark.asyncio
    async def test_always_allow_keeps_rules_written_to_the_file_since_the_session_opened(
        self, tmp_path: Path
    ) -> None:
        """A rule for another call added by hand is merged, not overwritten,
        and the session runs under what was saved from then on."""
        async with _daemon(tmp_path) as daemon:
            ws, session = await _open_workspace(daemon, tmp_path)
            live = session.policy  # the object the dispatcher holds
            parked = await _park(session, "tc-1", SHELL, {"command": "npm test"}, B)

            other = _rule("shell", "git push*", "never")
            save_policy(tmp_path, PolicyConfig(rules=[other]))  # the hand edit
            await ws.send(
                json.dumps(
                    {"type": "always_allow", "session_id": session.id, "tool_call_id": "tc-1"}
                )
            )
            assert (await asyncio.wait_for(parked, timeout=2)).approved
            saved = [other, _rule("shell", "npm test", "auto")]
            assert load_policy(tmp_path).rules == saved
            assert session.policy is live
            assert session.policy.rules == saved
            await ws.close()

    @pytest.mark.asyncio
    async def test_revoke_keeps_rules_written_to_the_file_since_the_session_opened(
        self, tmp_path: Path
    ) -> None:
        """The same for Revoke in Settings → Policy: it removes one rule from
        the file as it is now, not from the copy the session opened with."""
        mine = _rule("shell", "npm test", "ask")
        save_policy(tmp_path, PolicyConfig(rules=[mine]))
        async with _daemon(tmp_path) as daemon:
            ws, session = await _open_workspace(daemon, tmp_path)
            assert session.policy.rules == [mine]
            other = _rule("shell", "git push*", "never")
            save_policy(tmp_path, PolicyConfig(rules=[mine, other]))  # the hand edit
            await ws.send(
                json.dumps(
                    {
                        "type": "revoke_policy_rule",
                        "session_id": session.id,
                        "tool": "shell",
                        "args": "npm test",
                    }
                )
            )
            listed = await _recv_until(ws, "policy_rules")
            assert load_policy(tmp_path).rules == [other]
            assert [(r["tool"], r["args"]) for r in listed["rules"]] == [("shell", "git push*")]
            await ws.close()

    @pytest.mark.asyncio
    async def test_revoking_a_rule_already_removed_by_hand_writes_nothing(
        self, tmp_path: Path
    ) -> None:
        mine = _rule("shell", "npm test", "ask")
        save_policy(tmp_path, PolicyConfig(rules=[mine]))
        async with _daemon(tmp_path) as daemon:
            ws, session = await _open_workspace(daemon, tmp_path)
            config = tmp_path / ".tst" / "config.yaml"
            config.write_text("# emptied by hand\n", encoding="utf-8")
            await ws.send(
                json.dumps(
                    {
                        "type": "revoke_policy_rule",
                        "session_id": session.id,
                        "tool": "shell",
                        "args": "npm test",
                    }
                )
            )
            listed = await _recv_until(ws, "policy_rules")
            assert listed["rules"] == []
            assert session.policy.rules == []
            assert config.read_text(encoding="utf-8") == "# emptied by hand\n"
            await ws.close()

    @pytest.mark.asyncio
    async def test_a_policy_section_it_cannot_read_is_not_written_over(
        self, tmp_path: Path
    ) -> None:
        """Merging needs the file's rules; one it cannot parse is left for the
        user to fix, and the call runs once, as for any failed save."""
        config = tmp_path / ".tst" / "config.yaml"
        config.parent.mkdir()
        config.write_text("policy:\n  rules: not-a-list\n", encoding="utf-8")
        async with _daemon(tmp_path) as daemon:
            ws, session = await _open_workspace(daemon, tmp_path)
            parked = await _park(session, "tc-1", SHELL, {"command": "npm test"}, B)
            err = await _always_allow(ws, session)
            assert err["code"] == "policy_save_failed"
            assert err["message"].startswith("Approved once;")
            assert (await asyncio.wait_for(parked, timeout=2)).approved
            assert config.read_text(encoding="utf-8") == "policy:\n  rules: not-a-list\n"
            await ws.close()

    @pytest.mark.asyncio
    async def test_a_star_env_star_file_saves_beside_the_guard_and_lists_readably(
        self, tmp_path: Path
    ) -> None:
        guard = _rule("fs_read", "*.env*", "ask")
        save_policy(tmp_path, PolicyConfig(rules=[guard]))
        async with _daemon(tmp_path) as daemon:
            ws, session = await _open_workspace(daemon, tmp_path)
            path = str(tmp_path / "*.env*")
            parked = await _park(session, "tc-1", FS_READ, {"path": path}, B)
            proposal = _card(session).proposed_always_allow
            assert proposal is not None
            assert (proposal.args, proposal.exact) == ("[*].env[*]", "*.env*")

            await ws.send(
                json.dumps(
                    {"type": "always_allow", "session_id": session.id, "tool_call_id": "tc-1"}
                )
            )
            assert (await asyncio.wait_for(parked, timeout=2)).approved
            assert load_policy(tmp_path).rules == [
                guard,
                _rule("fs_read", "[*].env[*]", "auto"),
            ]

            await ws.send(json.dumps({"type": "list_policy_rules", "session_id": session.id}))
            listed = await _recv_until(ws, "policy_rules")
            assert listed["rules"] == [
                {"tool": "fs_read", "args": "*.env*", "effect": "ask", "exact": None},
                {"tool": "fs_read", "args": "[*].env[*]", "effect": "auto", "exact": "*.env*"},
            ]
            await ws.close()
