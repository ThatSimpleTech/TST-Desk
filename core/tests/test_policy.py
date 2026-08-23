"""Tests for the approval policy model (TD-801).

Covers: (tool, argument pattern) → effect mapping; decision-class defaults
(A → auto, B → ask, C → ask-or-never per config); persistence in
``.tst/config.yaml`` with other sections preserved; most-specific-wins
precedence; and the invariant that policy never grants what the boundary
forbids (a class-C call never resolves to ``auto``).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tstd.autonomy.classifier import DecisionClass
from tstd.boundary_config import load_workspace_boundary
from tstd.config import ConfigError
from tstd.policy import (
    PolicyConfig,
    PolicyRule,
    add_rule,
    load_policy,
    load_skip_all,
    propose_always_allow,
    remove_rule,
    resolve,
    resolve_explained,
    save_policy,
    save_skip_all,
    summarize_arguments,
)
from tstd.tools import Tool

A, B, C = DecisionClass.A, DecisionClass.B, DecisionClass.C

FS_WRITE = Tool(name="fs_write", path_fields=("path",), mutates=True)
FS_READ = Tool(name="fs_read", path_fields=("path",))
SHELL = Tool(name="shell")
FETCH = Tool(name="http_fetch", host_fields=("host",))


def _rule(tool: str, args: str = "**", effect: str = "auto") -> PolicyRule:
    return PolicyRule.model_validate({"tool": tool, "args": args, "effect": effect})


# ── (tool, argument pattern) → effect ───────────────────────────────────


class TestMapping:
    def test_matching_rule_applies(self, tmp_path: Path) -> None:
        cfg = PolicyConfig(rules=[_rule("fs_write", "src/**", "never")])
        target = str(tmp_path / "src" / "app.py")
        assert resolve(cfg, FS_WRITE, {"path": target}, A, tmp_path) == "never"

    def test_default_args_pattern_matches_any_call(self) -> None:
        cfg = PolicyConfig(rules=[_rule("fs_read")])
        assert resolve(cfg, FS_READ, {"path": "/anywhere.txt"}, A) == "auto"

    def test_unmatched_tool_falls_through_to_class_default(self, tmp_path: Path) -> None:
        cfg = PolicyConfig(rules=[_rule("fs_write", "**", "never")])
        target = str(tmp_path / "a.txt")
        assert resolve(cfg, FS_READ, {"path": target}, A, tmp_path) == "auto"
        assert resolve(cfg, FS_READ, {"path": target}, B, tmp_path) == "ask"

    def test_glob_tool_pattern(self) -> None:
        cfg = PolicyConfig(rules=[_rule("fs_*", "etc/**", "never")])
        assert resolve(cfg, FS_READ, {"path": "etc/hosts"}, A) == "never"

    def test_command_pattern_matches_shell_summary(self) -> None:
        cfg = PolicyConfig(rules=[_rule("shell", "npm test*", "auto")])
        assert resolve(cfg, SHELL, {"command": "npm test -- --runInBand"}, B) == "auto"
        assert resolve(cfg, SHELL, {"command": "rm -rf build/"}, B) == "ask"


# ── Defaults derive from decision class ─────────────────────────────────


class TestClassDefaults:
    @pytest.mark.parametrize(
        ("decision_class", "expected"),
        [(A, "auto"), (B, "ask"), (C, "ask")],
    )
    def test_defaults_without_rules(self, decision_class: DecisionClass, expected: str) -> None:
        assert resolve(PolicyConfig(), SHELL, {"command": "ls"}, decision_class) == expected

    def test_class_c_configurable_to_never(self) -> None:
        cfg = PolicyConfig(class_c_default="never")
        assert resolve(cfg, SHELL, {"command": "ls"}, C) == "never"
        # A and B defaults are unaffected by the C knob.
        assert resolve(cfg, SHELL, {"command": "ls"}, A) == "auto"
        assert resolve(cfg, SHELL, {"command": "ls"}, B) == "ask"

    def test_non_matching_rules_still_yield_class_default(self) -> None:
        cfg = PolicyConfig(rules=[_rule("shell", "cargo *", "auto")], class_c_default="never")
        assert resolve(cfg, SHELL, {"command": "ls"}, C) == "never"


# ── Most-specific pattern wins ──────────────────────────────────────────


class TestPrecedence:
    def test_exact_tool_beats_glob_tool(self) -> None:
        cfg = PolicyConfig(rules=[_rule("fs_*", "**", "ask"), _rule("fs_write", "**", "never")])
        assert resolve(cfg, FS_WRITE, {"path": "x"}, A) == "never"

    @pytest.mark.parametrize(
        ("winning_args", "losing_args", "probe"),
        [
            ("src/app.py", "src/*", "src/app.py"),  # literal beats wildcard
            ("src/*", "**", "src/app.py"),  # fewer wildcards beats more
            ("src/deep/*", "src/*", "src/deep/x.py"),  # longer wins among equal wildcards
        ],
    )
    def test_more_specific_args_pattern_wins(
        self, winning_args: str, losing_args: str, probe: str
    ) -> None:
        """The probe path matches both patterns; the more specific one decides."""
        cfg = PolicyConfig(
            rules=[_rule("fs_write", losing_args, "ask"), _rule("fs_write", winning_args, "never")]
        )
        assert resolve(cfg, FS_WRITE, {"path": probe}, A) == "never"

    def test_rule_order_does_not_decide(self) -> None:
        """Specificity, not declaration order, determines the winner."""
        ordered = PolicyConfig(
            rules=[_rule("fs_write", "**", "ask"), _rule("fs_write", "src/*", "never")]
        )
        reversed_ = PolicyConfig(
            rules=[_rule("fs_write", "src/*", "never"), _rule("fs_write", "**", "ask")]
        )
        assert resolve(ordered, FS_WRITE, {"path": "src/x"}, A) == "never"
        assert resolve(reversed_, FS_WRITE, {"path": "src/x"}, A) == "never"

    @pytest.mark.parametrize(
        ("effects", "expected"),
        [
            (["auto", "ask"], "ask"),
            (["auto", "never"], "never"),
            (["ask", "never"], "never"),
        ],
    )
    def test_exact_tie_breaks_to_most_restrictive(self, effects: list[str], expected: str) -> None:
        rules = [_rule("fs_write", "src/*", effect) for effect in effects]
        assert resolve(PolicyConfig(rules=rules), FS_WRITE, {"path": "src/x"}, A) == expected


# ── Policy never grants what the boundary forbids ───────────────────────


class TestBoundaryWins:
    def test_auto_rule_cannot_downgrade_class_c(self) -> None:
        cfg = PolicyConfig(rules=[_rule("fs_write", "**", "auto")])
        assert resolve(cfg, FS_WRITE, {"path": "x"}, C) == "ask"

    def test_auto_rule_cannot_downgrade_class_c_configured_never(self) -> None:
        cfg = PolicyConfig(rules=[_rule("fs_write", "**", "auto")], class_c_default="never")
        assert resolve(cfg, FS_WRITE, {"path": "x"}, C) == "never"

    def test_policy_can_tighten_but_not_loosen(self) -> None:
        """A never rule binds even a class-A call; loosening past C does not."""
        cfg = PolicyConfig(rules=[_rule("fs_read", "**", "never")])
        assert resolve(cfg, FS_READ, {"path": "x"}, A) == "never"


# ── Argument summaries ──────────────────────────────────────────────────


class TestSummarize:
    def test_path_tool_summarizes_workspace_relative(self, tmp_path: Path) -> None:
        target = tmp_path / "src" / "app.py"
        assert summarize_arguments(FS_WRITE, {"path": str(target)}, tmp_path) == "src/app.py"

    def test_path_outside_workspace_matches_absolute(self, tmp_path: Path) -> None:
        summary = summarize_arguments(FS_WRITE, {"path": "/etc/hosts"}, tmp_path)
        assert summary == "/etc/hosts"

    def test_host_tool_summarizes_host(self) -> None:
        assert summarize_arguments(FETCH, {"host": "api.example.com"}) == "api.example.com"

    def test_command_argument_is_the_summary(self) -> None:
        assert summarize_arguments(SHELL, {"command": "npm test"}) == "npm test"

    def test_fallback_is_canonical_json(self) -> None:
        summary = summarize_arguments(Tool(name="other"), {"b": 1, "a": 2})
        assert summary == '{"a":2,"b":1}'


# ── Persistence in .tst/config.yaml ─────────────────────────────────────


class TestPersistence:
    def test_absent_file_returns_defaults(self, tmp_path: Path) -> None:
        cfg = load_policy(tmp_path)
        assert cfg.rules == []
        assert cfg.class_c_default == "ask"

    def test_comment_only_file_returns_defaults(self, tmp_path: Path) -> None:
        # The TD-1103 scaffolded template is comment-only: YAML parses it
        # as None, which means defaults, not ConfigError — and save_policy
        # must round-trip through that same file.
        config_path = tmp_path / ".tst" / "config.yaml"
        config_path.parent.mkdir(parents=True)
        config_path.write_text("# boundary settings\n# nothing yet\n", encoding="utf-8")
        cfg = load_policy(tmp_path)
        assert cfg.rules == []
        save_policy(tmp_path, PolicyConfig(rules=[_rule("shell", "ls", "auto")]))
        assert load_policy(tmp_path).rules == [_rule("shell", "ls", "auto")]

    def test_round_trip(self, tmp_path: Path) -> None:
        cfg = PolicyConfig(
            rules=[_rule("fs_write", "src/**", "ask"), _rule("shell", "npm *", "auto")],
            class_c_default="never",
        )
        save_policy(tmp_path, cfg)
        assert load_policy(tmp_path) == cfg
        # It really lands in .tst/config.yaml, not some other file.
        assert (tmp_path / ".tst" / "config.yaml").exists()

    def test_save_preserves_other_sections(self, tmp_path: Path) -> None:
        config_path = tmp_path / ".tst" / "config.yaml"
        config_path.parent.mkdir(parents=True)
        config_path.write_text(
            "boundary:\n  network: deny\ncaps:\n  spend_usd: 5.0\n", encoding="utf-8"
        )
        save_policy(tmp_path, PolicyConfig(rules=[_rule("shell", "git *", "ask")]))

        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        assert raw["boundary"] == {"network": "deny"}
        assert raw["caps"] == {"spend_usd": 5.0}
        assert raw["policy"]["rules"] == [{"tool": "shell", "args": "git *", "effect": "ask"}]
        # The TD-706 loader must still accept the file.
        assert load_workspace_boundary(tmp_path).caps.spend_usd == 5.0

    def test_policy_section_coexists_with_boundary_loader(self, tmp_path: Path) -> None:
        """A file carrying a policy section does not disturb boundary loading."""
        save_policy(tmp_path, PolicyConfig(rules=[_rule("fs_read")]))
        boundary = load_workspace_boundary(tmp_path)
        assert boundary.boundary.writable_paths == ["**"]

    def test_invalid_effect_names_key(self, tmp_path: Path) -> None:
        config_path = tmp_path / ".tst" / "config.yaml"
        config_path.parent.mkdir(parents=True)
        config_path.write_text(
            "policy:\n  rules:\n    - tool: shell\n      effect: yolo\n", encoding="utf-8"
        )
        with pytest.raises(ConfigError, match="policy"):
            load_policy(tmp_path)

    def test_non_mapping_policy_section(self, tmp_path: Path) -> None:
        config_path = tmp_path / ".tst" / "config.yaml"
        config_path.parent.mkdir(parents=True)
        config_path.write_text("policy: just-a-string\n", encoding="utf-8")
        with pytest.raises(ConfigError, match="policy"):
            load_policy(tmp_path)

    def test_invalid_class_c_default(self, tmp_path: Path) -> None:
        config_path = tmp_path / ".tst" / "config.yaml"
        config_path.parent.mkdir(parents=True)
        config_path.write_text("policy:\n  class_c_default: maybe\n", encoding="utf-8")
        with pytest.raises(ConfigError, match="class_c_default"):
            load_policy(tmp_path)


# ── Always-allow rule generation and revocation (TD-803) ───────────────


class TestAlwaysAllow:
    def test_proposes_exact_command_not_blanket(self) -> None:
        rule = propose_always_allow(SHELL, {"command": "rm -rf build/"}, B)
        assert rule is not None
        assert rule.tool == "shell"
        assert rule.args == "rm -rf build/"  # exact command, never a `**` grant
        assert rule.effect == "auto"

    def test_proposes_workspace_relative_path(self, tmp_path: Path) -> None:
        target = tmp_path / "src" / "app.py"
        rule = propose_always_allow(FS_WRITE, {"path": str(target)}, B, tmp_path)
        assert rule is not None
        assert rule.tool == "fs_write"
        assert rule.args == "src/app.py"

    def test_class_c_is_never_always_allowable(self) -> None:
        assert propose_always_allow(SHELL, {"command": "rm -rf build/"}, C) is None

    def test_saved_rule_resolves_same_call_to_auto(self) -> None:
        config = PolicyConfig()
        rule = propose_always_allow(SHELL, {"command": "npm test"}, B)
        assert rule is not None
        add_rule(config, rule)
        assert resolve(config, SHELL, {"command": "npm test"}, B) == "auto"
        # Narrow scope: a different command still requires approval.
        assert resolve(config, SHELL, {"command": "npm run build"}, B) == "ask"

    def test_add_rule_is_idempotent(self) -> None:
        config = PolicyConfig()
        add_rule(config, _rule("shell", "npm test", "auto"))
        add_rule(config, _rule("shell", "npm test", "auto"))
        assert config.rules == [_rule("shell", "npm test", "auto")]

    def test_add_rule_replaces_same_tool_args(self) -> None:
        config = PolicyConfig(rules=[_rule("shell", "npm test", "ask")])
        add_rule(config, _rule("shell", "npm test", "auto"))
        assert len(config.rules) == 1
        assert config.rules[0].effect == "auto"

    def test_remove_rule_by_tool_args(self) -> None:
        config = PolicyConfig(rules=[_rule("shell", "npm test"), _rule("fs_*", "src/**")])
        assert remove_rule(config, "shell", "npm test") is True
        assert config.rules == [_rule("fs_*", "src/**")]
        assert remove_rule(config, "shell", "npm test") is False  # already gone


# ── Skip-all (TD-804) ───────────────────────────────────────────────────


class TestSkipAll:
    def test_promotes_class_b_ask_to_auto(self) -> None:
        decision = resolve_explained(
            PolicyConfig(), FS_WRITE, {"path": "src/x.py"}, B, skip_all=True
        )
        assert decision.effect == "auto"
        assert decision.reason == "skip-all approvals is on"

    def test_promotes_an_ask_rule_for_class_b(self) -> None:
        cfg = PolicyConfig(rules=[_rule("fs_write", "**", "ask")])
        assert resolve(cfg, FS_WRITE, {"path": "src/x.py"}, B, skip_all=True) == "auto"

    def test_promotes_shell_class_b(self) -> None:
        # TD-805: skip-all is the yolo bit. Shell B is included.
        decision = resolve_explained(PolicyConfig(), SHELL, {"command": "ls"}, B, skip_all=True)
        assert decision.effect == "auto"
        assert decision.reason == "skip-all approvals is on"

    def test_promotes_a_shell_ask_rule(self) -> None:
        cfg = PolicyConfig(rules=[_rule("shell", "**", "ask")])
        assert resolve(cfg, SHELL, {"command": "ls"}, B, skip_all=True) == "auto"

    def test_shell_explicit_auto_rule_still_runs(self) -> None:
        cfg = PolicyConfig(rules=[_rule("shell", "git status", "auto")])
        assert resolve(cfg, SHELL, {"command": "git status"}, B, skip_all=True) == "auto"

    def test_promotes_class_c_ask(self) -> None:
        decision = resolve_explained(PolicyConfig(), SHELL, {"command": "ls"}, C, skip_all=True)
        assert decision.effect == "auto"
        assert decision.reason == "skip-all approvals is on"

    def test_class_c_configured_never_still_never(self) -> None:
        cfg = PolicyConfig(class_c_default="never")
        assert resolve(cfg, SHELL, {"command": "ls"}, C, skip_all=True) == "never"

    def test_auto_rule_for_class_c_runs_under_skip_all(self) -> None:
        cfg = PolicyConfig(rules=[_rule("shell", "**", "auto")])
        assert resolve(cfg, SHELL, {"command": "ls"}, C, skip_all=True) == "auto"

    def test_class_c_wall_stands_when_skip_all_is_off(self) -> None:
        cfg = PolicyConfig(rules=[_rule("shell", "**", "auto")])
        assert resolve(cfg, SHELL, {"command": "ls"}, C, skip_all=False) == "ask"

    def test_never_rule_still_refuses(self) -> None:
        cfg = PolicyConfig(rules=[_rule("shell", "**", "never")])
        assert resolve(cfg, SHELL, {"command": "ls"}, B, skip_all=True) == "never"

    def test_off_leaves_class_b_asking(self) -> None:
        assert resolve(PolicyConfig(), SHELL, {"command": "ls"}, B) == "ask"
        assert resolve(PolicyConfig(), SHELL, {"command": "ls"}, B, skip_all=False) == "ask"


class TestAutonomy:
    def test_class_a_stays_auto(self) -> None:
        assert resolve(PolicyConfig(), FS_WRITE, {"path": "src/x.py"}, A, autonomy=True) == "auto"

    def test_class_b_never_prompts(self) -> None:
        decision = resolve_explained(PolicyConfig(), SHELL, {"command": "ls"}, B, autonomy=True)
        assert decision.effect == "auto"
        assert "autonomy" in decision.reason

    def test_class_c_never_even_under_skip_all(self) -> None:
        decision = resolve_explained(
            PolicyConfig(), SHELL, {"command": "ls"}, C, skip_all=True, autonomy=True
        )
        assert decision.effect == "never"
        assert "Class C" in decision.reason

    def test_class_c_auto_rule_still_stops(self) -> None:
        cfg = PolicyConfig(rules=[_rule("shell", "**", "auto")])
        assert resolve(cfg, SHELL, {"command": "ls"}, C, skip_all=True, autonomy=True) == "never"


class TestSkipAllPersist:
    def test_absent_is_off(self, tmp_path: Path) -> None:
        assert load_skip_all(tmp_path) is False

    def test_round_trip(self, tmp_path: Path) -> None:
        save_skip_all(tmp_path, True)
        assert load_skip_all(tmp_path) is True
        save_skip_all(tmp_path, False)
        assert load_skip_all(tmp_path) is False

    def test_lands_in_user_data_not_workspace_config(self, tmp_path: Path) -> None:
        data = tmp_path / "data"
        workspace = tmp_path / "ws"
        (workspace / ".tst").mkdir(parents=True)
        workspace_config = workspace / ".tst" / "config.yaml"
        workspace_config.write_text("policy:\n  rules: []\n", encoding="utf-8")
        before = workspace_config.read_text(encoding="utf-8")

        save_skip_all(data, True)

        assert (data / "approvals.yaml").exists()
        assert not (data / ".tst").exists()
        assert workspace_config.read_text(encoding="utf-8") == before
        assert "skip_all" not in before

    def test_unreadable_or_junk_is_off(self, tmp_path: Path) -> None:
        path = tmp_path / "approvals.yaml"
        path.write_text(":::: not yaml", encoding="utf-8")
        assert load_skip_all(tmp_path) is False
        path.write_text("- just a list\n", encoding="utf-8")
        assert load_skip_all(tmp_path) is False
        path.write_text("skip_all: false\n", encoding="utf-8")
        assert load_skip_all(tmp_path) is False
        path.write_text('skip_all: "false"\n', encoding="utf-8")
        assert load_skip_all(tmp_path) is False
