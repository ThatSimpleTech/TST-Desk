"""Cross-cutting security suite (TD-1402).

Release blockers: path-escape attempts across the TD-602 vectors, steering-file
write refusal at guard level, secret redaction in logs and across the
audit/event pipeline (TD-1405), non-loopback bind refusal, and
classifier-bypass attacks on the dispatch chokepoint.
"""

from __future__ import annotations

import inspect
import json
import logging
import os
import re
import sqlite3
import sys
import tempfile
from enum import StrEnum
from io import StringIO
from pathlib import Path
from types import UnionType
from typing import Any, Literal, Union, get_args, get_origin

import pytest
from pydantic import BaseModel, ValidationError

from tests.test_dispatch import (
    attach_auto_approver,
    make_config,
    start_loop,
    wait_for_turn,
)
from tstd.audit import AuditStore
from tstd.audit_writer import AuditWriter
from tstd.autonomy import (
    AmbiguousClassifier,
    Boundary,
    DecisionClass,
    DecisionClassifier,
    DecisionRequest,
)
from tstd.autonomy.classifier import RULE_TABLE, is_steering_write
from tstd.logging import SECRET_PATTERNS, JSONFormatter, SecretsRedactionFilter
from tstd.mock import MockProvider, Script
from tstd.protocol import DaemonEvent, ShellOutput, build_error, scrub_wire_json
from tstd.protocol import ToolCall as ToolCallEvent
from tstd.protocol import ToolResult as ToolResultEvent
from tstd.router import TierRouter
from tstd.session import EventSubscriber, Session, SessionEventLog, _redact_event
from tstd.tools import (
    Tool,
    ToolDispatcher,
    UnclassifiedToolCall,
    create_registry,
    register_builtin_handlers,
)
from tstd.tools.boundary import PathGuard, RefusalError
from tstd.tools.shell import run_shell, sanitized_env
from tstd.ws import WebSocketServer, validate_interface


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    root.mkdir()
    return root


def guard(workspace: Path, writable: tuple[str, ...] = ("**",)) -> PathGuard:
    return PathGuard(Boundary(workspace_root=workspace, writable_patterns=writable))


def make_dispatcher(
    workspace: Path,
    *,
    with_classifier: bool = True,
    with_guard: bool = True,
) -> ToolDispatcher:
    boundary = Boundary(workspace_root=workspace)
    classifier = (
        AmbiguousClassifier(
            static=DecisionClassifier(boundary),
            call_worker=SpyWorker("B"),
        )
        if with_classifier
        else None
    )
    dispatcher = attach_auto_approver(  # TD-802: mechanics tests auto-approve
        ToolDispatcher(
            create_registry(),
            classifier=classifier,
            path_guard=PathGuard(boundary) if with_guard else None,
        )
    )
    register_builtin_handlers(dispatcher)
    return dispatcher


class SpyWorker:
    """Worker-tier classifier stub: records prompts, returns ``response``."""

    def __init__(self, response: str = "A") -> None:
        self.response = response
        self.calls: list[str] = []

    async def __call__(self, prompt: str) -> str:
        self.calls.append(prompt)
        return self.response


def _read(path: Path) -> str:
    """Sync read: ASYNC240 keeps Path calls out of async test functions."""
    return path.read_text()


# ── 1. Path escape attempts (TD-602 vectors) ─────────────────────────────

TRAVERSAL_VECTORS = [
    "../outside.txt",
    "../../etc/passwd",
    "a/b/../../../outside.txt",
    "..",
]


@pytest.mark.parametrize("vector", TRAVERSAL_VECTORS)
def test_traversal_refused_for_read_and_write(
    ws: Path, monkeypatch: pytest.MonkeyPatch, vector: str
) -> None:
    # TD-1406: raw guard inputs resolve against the process CWD, so chdir
    # into the workspace and feed workspace-relative vectors — an absolute
    # tmp_path-anchored input is a drive-letter path on Windows, refused
    # fail-closed as windows_unsafe before the traversal logic runs.
    monkeypatch.chdir(ws)
    g = guard(ws)
    for check in (g.check_read, g.check_write):
        with pytest.raises(RefusalError) as excinfo:
            check(vector)
        assert excinfo.value.code == "outside_workspace"


def test_absolute_path_outside_refused(ws: Path) -> None:
    with pytest.raises(RefusalError) as excinfo:
        guard(ws).check_read("/etc/passwd")
    assert excinfo.value.code == "outside_workspace"


def test_symlinked_file_escape_refused(
    ws: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Workspace-relative guard input from inside the workspace — TD-1406
    # (see test_traversal_refused_for_read_and_write).
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    (ws / "link.txt").symlink_to(outside)
    monkeypatch.chdir(ws)
    with pytest.raises(RefusalError) as excinfo:
        guard(ws).check_read("link.txt")
    assert excinfo.value.code == "outside_workspace"


def test_symlinked_parent_dir_escape_refused(
    ws: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Workspace-relative guard input from inside the workspace — TD-1406.
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    (outside_dir / "secret.txt").write_text("secret")
    (ws / "dirlink").symlink_to(outside_dir, target_is_directory=True)
    monkeypatch.chdir(ws)
    with pytest.raises(RefusalError) as excinfo:
        guard(ws).check_read("dirlink/secret.txt")
    assert excinfo.value.code == "outside_workspace"


WINDOWS_VECTORS = [
    "C:relative.txt",  # drive relative
    "\\\\server\\share\\file.txt",  # UNC
    "PROGRA~1.txt",  # 8.3 short name, naming nothing that can expand
    "notes.txt:hidden",  # alternate data stream
]


@pytest.mark.parametrize("vector", WINDOWS_VECTORS)
def test_windows_unsafe_refused_on_every_platform(ws: Path, vector: str) -> None:
    """Windows-unsafe forms are refused fail-closed even on POSIX hosts."""
    g = guard(ws)
    for check in (g.check_read, g.check_write):
        with pytest.raises(RefusalError) as excinfo:
            check(vector)
        assert excinfo.value.code == "windows_unsafe"


def test_drive_absolute_outside_workspace_refused(ws: Path) -> None:
    """Drive-absolute paths are refused everywhere — for different reasons.

    TD-1406 made the drive-absolute form legal on Windows, where it is the
    only way to write an absolute path; it then faces the workspace wall
    like any other absolute path.  Off Windows it stays a form refusal,
    because no POSIX host can resolve a drive letter.  Either way the
    system32 path does not get written.
    """
    g = guard(ws)
    expected = "outside_workspace" if sys.platform == "win32" else "windows_unsafe"
    for check in (g.check_read, g.check_write):
        with pytest.raises(RefusalError) as excinfo:
            check("C:\\Windows\\system.ini")
        assert excinfo.value.code == expected


def test_percent_encoded_traversal_is_inert(ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The guard must not decode: %2f is a literal filename inside the
    workspace, never a smuggled separator."""
    # Workspace-relative guard input from inside the workspace — TD-1406.
    monkeypatch.chdir(ws)
    g = guard(ws)
    resolved = g.check_read("..%2f..%2fetc")
    assert g.canonicalize(ws) in resolved.parents


def test_hardlink_write_refused(ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    original = ws / "original.txt"
    original.write_text("x")
    os.link(original, ws / "alias.txt")
    # Workspace-relative guard input from inside the workspace — TD-1406.
    monkeypatch.chdir(ws)
    with pytest.raises(RefusalError) as excinfo:
        guard(ws).check_write("original.txt")
    assert excinfo.value.code == "hardlink"


def test_writable_patterns_enforced_for_writes_only(
    ws: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Workspace-relative guard inputs from inside the workspace — TD-1406.
    monkeypatch.chdir(ws)
    g = guard(ws, writable=("src/**",))
    with pytest.raises(RefusalError) as excinfo:
        g.check_write("docs/notes.md")
    assert excinfo.value.code == "outside_writable_paths"
    # Reads must not be narrowed by write policy.
    assert g.check_read("docs/notes.md") == g.canonicalize(ws / "docs" / "notes.md")
    assert g.check_write("src/ok.py") == g.canonicalize(ws / "src" / "ok.py")


def test_no_workspace_root_refuses_writes() -> None:
    g = PathGuard(Boundary(workspace_root=None))
    with pytest.raises(RefusalError) as excinfo:
        g.check_write("anything.txt")
    assert excinfo.value.code == "outside_workspace"


# ── 2. Steering-file write refusal ────────────────────────────────────────

STEERING_POSITIVE = [
    "AGENTS.md",
    "CLAUDE.md",
    "agents.md",  # case-insensitive
    "docs/AGENTS.md",
    ".tst/rules/style.md",
    ".tst/rules/deep/style.md",
    ".tst/rules",  # the rules dir itself
    ".tst/config.yaml",  # the approval policy is steering-adjacent (TD-4803)
]

STEERING_NEGATIVE = [
    "AGENTS.md.bak",
    "MYAGENTS.md",
    "notes.md",
    ".tst/rules.md",  # a file named rules.md is not the rules dir
    ".tst/memory/MEMORY.md",  # memory is the carve-out (TD-2102)
    ".tst/memory/gotchas.md",
]


@pytest.mark.parametrize("relative", STEERING_POSITIVE)
def test_steering_targets_detected(ws: Path, relative: str) -> None:
    assert is_steering_write(Boundary(workspace_root=ws), ws / relative)


@pytest.mark.parametrize("relative", STEERING_NEGATIVE)
def test_steering_lookalikes_not_detected(ws: Path, relative: str) -> None:
    assert not is_steering_write(Boundary(workspace_root=ws), ws / relative)


@pytest.mark.parametrize("relative", STEERING_POSITIVE)
def test_steering_write_refused_at_guard(
    ws: Path, monkeypatch: pytest.MonkeyPatch, relative: str
) -> None:
    # Workspace-relative guard input from inside the workspace — TD-1406
    # (see test_traversal_refused_for_read_and_write).
    monkeypatch.chdir(ws)
    with pytest.raises(RefusalError) as excinfo:
        guard(ws).check_write(relative)
    assert excinfo.value.code == "steering_file"


@pytest.mark.parametrize("relative", STEERING_POSITIVE)
async def test_steering_write_refused_at_tool_level(ws: Path, relative: str) -> None:
    """Every steering shape refuses at tool level too — before any approval
    machinery could say yes."""
    dispatcher = make_dispatcher(ws)
    result = await dispatcher.dispatch(
        "c1", "fs_write", {"path": str(ws / relative), "content": "override"}
    )
    assert result.status == "error"
    assert result.error_code == "boundary_refusal"
    assert result.decision_class is DecisionClass.C
    assert not (ws / relative).exists() or _read(ws / relative) != "override"


async def test_fs_write_dispatch_refuses_traversal(ws: Path) -> None:
    dispatcher = make_dispatcher(ws)
    result = await dispatcher.dispatch(
        "c1", "fs_write", {"path": str(ws / ".." / "escape.txt"), "content": "x"}
    )
    assert result.status == "error"
    assert result.error_code == "boundary_refusal"
    assert result.decision_class is DecisionClass.C


async def test_fs_edit_dispatch_refuses_absolute_escape(ws: Path) -> None:
    dispatcher = make_dispatcher(ws)
    result = await dispatcher.dispatch(
        "c1", "fs_edit", {"path": "/etc/passwd", "old_string": "root", "new_string": "x"}
    )
    assert result.status == "error"
    assert result.error_code == "boundary_refusal"
    assert result.decision_class is DecisionClass.C


async def test_legit_write_succeeds_proving_refusals_are_targeted(
    ws: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Positive control: in-workspace writes execute — refusals are surgical,
    not a piece of machinery that rejects everything."""
    # Workspace-relative tool argument from inside the workspace — TD-1406:
    # an absolute tmp_path argument is a drive-letter path on Windows,
    # refused fail-closed before the in-workspace success path under test.
    monkeypatch.chdir(ws)
    dispatcher = make_dispatcher(ws)
    result = await dispatcher.dispatch("c1", "fs_write", {"path": "src/app.py", "content": "hi\n"})
    assert result.status == "success"
    assert result.decision_class is DecisionClass.A
    assert _read(ws / "src" / "app.py") == "hi\n"


# ── 3. Environment sanitization for child processes ──────────────────────


def test_sanitized_env_drops_secret_names(monkeypatch: pytest.MonkeyPatch) -> None:
    """Secret-shaped variable names are stripped from the child environment;
    ordinary variables pass through (TD-605)."""
    monkeypatch.setenv("TSTD_PLANTED_API_KEY", "sk-planted-secret-value")  # tst-secret-ok
    monkeypatch.setenv("TSTD_PLANTED_DB_PASSWORD", "hunter2")  # tst-secret-ok
    monkeypatch.setenv("TSTD_PLANTED_VISIBLE", "still-here")
    env = sanitized_env()
    assert "TSTD_PLANTED_API_KEY" not in env
    assert "TSTD_PLANTED_DB_PASSWORD" not in env
    assert env["TSTD_PLANTED_VISIBLE"] == "still-here"


async def test_shell_child_never_sees_daemon_secrets(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """End-to-end: a shell command that dumps its environment echoes the
    planted safe variable but never the planted secret (TD-605 + TD-1402)."""
    secret_value = "sk-planted-secret-value"  # tst-secret-ok
    monkeypatch.setenv("TSTD_PLANTED_API_KEY", secret_value)
    monkeypatch.setenv("TSTD_PLANTED_VISIBLE", "still-here")
    session = Session(str(tmp_path))
    out = await run_shell(session, "printenv TSTD_PLANTED_API_KEY; printenv TSTD_PLANTED_VISIBLE")
    assert secret_value not in out
    assert "still-here" in out


# ── 3b. Shell hardening: static steering classification, env net (TD-4805) ──


def _shell_decision(ws: Path, command: str) -> Any:
    """Static classification of a shell call — no worker tier involved."""
    return DecisionClassifier(Boundary(workspace_root=ws)).classify(
        DecisionRequest(tool_name="shell", arguments={"command": command}, is_mutation=True)
    )


@pytest.mark.parametrize(
    "command",
    [
        "echo never > AGENTS.md",
        "echo never >> CLAUDE.md",
        "echo never > .tst/rules/evil.md",
        "echo never > .tst/config.yaml",
        "echo never > ./AGENTS.md",
        "tee .tst/rules/x.md",
        "tee -a AGENTS.md",
        "echo never > notes.md && tee CLAUDE.md",
        # TD-4817: the operator forms the extractor originally missed.
        "echo never >& AGENTS.md",
        "echo never >| .tst/config.yaml",
        "echo never >| .tst/rules/evil.md",
        "echo never >! CLAUDE.md",  # zsh clobber, attached
        "echo never >&! AGENTS.md",
        "echo never >! .tst/config.yaml",  # spaced clobber
    ],
)
def test_shell_write_to_steering_is_static_c(ws: Path, command: str) -> None:
    """A shell command writing a steering path is Class C from the static
    table alone — no model judgment involved (TD-4805)."""
    decision = _shell_decision(ws, command)
    assert decision.decision_class is DecisionClass.C
    assert decision.rule is not None
    assert decision.rule.id == "shell-steering-write"


@pytest.mark.parametrize(
    "command",
    [
        "cat AGENTS.md",  # reading steering is fine
        "echo hi > notes.md",  # benign redirect
        "git status",
        "echo done 2>&1",  # descriptor dup writes no file
        "echo hi >&2",  # stderr dup, not a file write (TD-4817)
        "exec 3>&1",  # fd juggling extracts a numeric non-target
        'echo "unclosed',  # unparseable → no targets, still not unsafe
    ],
)
def test_shell_without_steering_target_hits_the_b_floor(ws: Path, command: str) -> None:
    """Every other shell command is statically B: the table cannot see
    inside the string, so shell never gets a model-granted A (TD-4805)."""
    decision = _shell_decision(ws, command)
    assert decision.decision_class is DecisionClass.B
    assert decision.rule is not None
    assert decision.rule.id == "shell-floor"


def test_shell_floor_precedes_worker_consultation(ws: Path) -> None:
    """The floor is static, so the worker tier is never consulted for
    shell — its A cannot auto-run a command."""
    decision = _shell_decision(ws, "make deploy")
    assert decision.decision_class is not None  # static decided; worker not needed


@pytest.mark.parametrize(
    "name",
    ["DOCKER_AUTH_CONFIG", "MYSQL_PWD", "SOME_SERVICE_AUTH", "TSTD_PLANTED_AUTH_TOKEN"],
)
def test_sanitized_env_drops_widened_secret_names(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    """The TD-4805 net: AUTH word forms and PWD carriers drop too."""
    monkeypatch.setenv(name, "planted")  # tst-secret-ok
    assert name not in sanitized_env()


def test_sanitized_env_keeps_author_and_ssh_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    """AUTH as a substring must not eat GIT_AUTHOR_* (the child commits as
    the user) or SSH_AUTH_SOCK (a capability, not a secret value)."""
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Ada")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "ada@example.com")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/agent.sock")
    env = sanitized_env()
    assert env["GIT_AUTHOR_NAME"] == "Ada"
    assert env["GIT_AUTHOR_EMAIL"] == "ada@example.com"
    assert env["SSH_AUTH_SOCK"] == "/tmp/agent.sock"


def test_sanitized_env_drops_credential_url_values(monkeypatch: pytest.MonkeyPatch) -> None:
    """A URL with embedded credentials is a secret whatever the name."""
    monkeypatch.setenv("TSTD_PLANTED_DB", "postgres://user:hunter2@host/db")  # tst-secret-ok
    monkeypatch.setenv("TSTD_PLANTED_CLEAN", "postgres://host/db")
    env = sanitized_env()
    assert "TSTD_PLANTED_DB" not in env
    assert env["TSTD_PLANTED_CLEAN"] == "postgres://host/db"


@pytest.mark.parametrize(
    "value",
    [
        "redis://:hunter2@prod-cache:6379/0",  # tst-secret-ok
        "rediss://:hunter2@prod-cache:6380",  # tst-secret-ok
        "postgresql://:hunter2@db.internal/app",  # tst-secret-ok
        "mongodb://:hunter2@mongo.internal:27017",  # tst-secret-ok
    ],
)
def test_sanitized_env_drops_password_only_credential_urls(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """Heroku-style `scheme://:password@host` — the username is empty and
    the password is the whole secret (TD-4819)."""
    monkeypatch.setenv("TSTD_PLANTED_URL", value)
    assert "TSTD_PLANTED_URL" not in sanitized_env()


def test_allowlist_docs_name_the_escape_hatches() -> None:
    """The allowlist docstring names the re-exec hatches instead of
    implying containment (TD-4805)."""
    import tstd.tools.shell as shell_mod

    doc = shell_mod.__doc__ or ""
    for hatch in ("find -exec", "xargs", "sh -c"):
        assert hatch in doc


# ── 3c. The network rail reaches the web tools (TD-4808) ─────────────────
#
# ``network: deny`` and the host allowlist only bite if the classifier can
# see the host a call reaches.  web_fetch carries it in an argument;
# web_search's host lives in config (search.base_url).  Both must land in
# the DecisionRequest, or the rail is decorative.


def _classify_tool_call(
    ws: Path,
    tool_name: str,
    arguments: dict[str, Any],
    allowed_hosts: frozenset[str] = frozenset(),
) -> Any:
    """Static classification of a real registered tool call (TD-4808)."""
    from tstd.tools.dispatch import build_decision_request

    tool = create_registry().require(tool_name)
    request = build_decision_request(tool, arguments)
    return DecisionClassifier(Boundary(workspace_root=ws, allowed_hosts=allowed_hosts)).classify(
        request
    )


def test_web_fetch_host_is_classified_against_the_allowlist(ws: Path) -> None:
    decision = _classify_tool_call(ws, "web_fetch", {"url": "https://news.example.com/x"})
    assert decision.decision_class is DecisionClass.C
    assert decision.rule is not None
    assert decision.rule.id == "network-new-host"


def test_web_fetch_to_an_allowlisted_host_asks_not_refuses(ws: Path) -> None:
    decision = _classify_tool_call(
        ws,
        "web_fetch",
        {"url": "https://news.example.com/x"},
        allowed_hosts=frozenset({"news.example.com"}),
    )
    assert decision.decision_class is DecisionClass.B
    assert decision.rule is not None
    assert decision.rule.id == "side-effect-ask-floor"


def test_web_fetch_url_is_reduced_to_a_bare_host(ws: Path) -> None:
    # Scheme, case, and port must not defeat the allowlist match.
    decision = _classify_tool_call(
        ws,
        "web_fetch",
        {"url": "https://NEWS.example.com:8443/x?q=1"},
        allowed_hosts=frozenset({"news.example.com"}),
    )
    assert decision.rule is not None
    assert decision.rule.id != "network-new-host"


def test_web_search_host_comes_from_config(ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    import tstd.tools.web_search as web_search_mod

    monkeypatch.setattr(
        web_search_mod,
        "cached_config",
        lambda: SimpleNamespace(
            search=SimpleNamespace(base_url="https://searx.example.com/search")
        ),
    )
    denied = _classify_tool_call(ws, "web_search", {"query": "anything"})
    assert denied.decision_class is DecisionClass.C
    assert denied.rule is not None
    assert denied.rule.id == "network-new-host"

    allowed = _classify_tool_call(
        ws, "web_search", {"query": "anything"}, allowed_hosts=frozenset({"searx.example.com"})
    )
    assert allowed.decision_class is DecisionClass.B
    assert allowed.rule is not None
    assert allowed.rule.id == "side-effect-ask-floor"


def test_web_search_unconfigured_reaches_no_host(ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    import tstd.tools.web_search as web_search_mod

    monkeypatch.setattr(
        web_search_mod,
        "cached_config",
        lambda: SimpleNamespace(search=SimpleNamespace(base_url="")),
    )
    decision = _classify_tool_call(ws, "web_search", {"query": "anything"})
    assert decision.decision_class is DecisionClass.B
    assert decision.rule is not None
    assert decision.rule.id == "side-effect-ask-floor"


def test_host_of_reduction() -> None:
    from tstd.tools.dispatch import host_of

    assert host_of("https://Example.COM:8443/a?b=c") == "example.com"
    assert host_of("http://user:pass@example.com/") == "example.com"
    assert host_of("example.com") == "example.com"
    # Non-network schemes reach no host — not a network call.
    assert host_of("about:blank") is None
    assert host_of("file:///etc/passwd") is None
    assert host_of("data:text/html,x") is None


def test_web_tools_keep_their_rails_declared() -> None:
    """Drift guard: the rail is only wired while the declarations stay."""
    registry = create_registry()
    assert registry.require("web_fetch").host_fields == ("url",)
    assert registry.require("web_search").host_resolver is not None
    # And the floor stays declared — removal is an AC-level decision,
    # not a refactor side effect.
    assert registry.require("web_fetch").side_effect_class == "ask"
    assert registry.require("web_search").side_effect_class == "ask"


# ── 4. Secret redaction ───────────────────────────────────────────────────

FAKE_SECRETS = [
    "sk-PROJEXAMPLEKEYfakefake0000abcd",  # tst-secret-ok
    # Dashed forms — the shapes the shipped presets actually hold
    # (TD-4801). The pre-TD-4801 pattern required 20+ alphanumerics with
    # no dashes and missed all three of these.
    "sk-or-v1-0000000000000000000000000000000000000000000000abcd",  # tst-secret-ok
    "sk-proj-FAKEFAKE0_fake-fake-fake-fake-fake-fake-fake0abcd",  # tst-secret-ok
    "sk-ant-api03-FAKEFAKE0fakefakefakefakefakefakefake0abcd",  # tst-secret-ok
    "github_pat_11FAKEFAKE0abcdefghijklmnopqrstuvwxyz01",  # tst-secret-ok
    "ghp_FAKEFAKEFAKEFAKE00000000000000000abcd",  # tst-secret-ok
    "AKIAIOSFODNN7EXAMPLE",  # tst-secret-ok
    "-----BEGIN RSA PRIVATE KEY-----",  # tst-secret-ok
]


def capture_logger(name: str) -> tuple[logging.Logger, StringIO]:
    logger = logging.getLogger(name)
    logger.handlers.clear()
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    buf = StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(JSONFormatter())
    handler.addFilter(SecretsRedactionFilter())
    logger.addHandler(handler)
    return logger, buf


@pytest.mark.parametrize("secret", FAKE_SECRETS)
def test_log_message_redacts_each_known_secret_shape(secret: str) -> None:
    # Every pattern in SECRET_PATTERNS must have a fake here.
    assert any(pattern.search(secret) for pattern in SECRET_PATTERNS)
    logger, buf = capture_logger(f"test.security.{len(secret)}")
    logger.info("using credential %s", secret)
    rendered = json.loads(buf.getvalue())["message"]
    assert "[REDACTED]" in rendered
    assert secret not in rendered


def test_clean_log_message_not_redacted() -> None:
    logger, buf = capture_logger("test.security.clean")
    logger.info("reading config.yaml, 42 entries")
    assert json.loads(buf.getvalue())["message"] == "reading config.yaml, 42 entries"


PLANTED = FAKE_SECRETS[0]


def event_spy() -> tuple[list[DaemonEvent], EventSubscriber]:
    """A broadcast-stream spy: records the events a client would receive."""
    seen: list[DaemonEvent] = []

    async def _spy(event: DaemonEvent, _log: SessionEventLog) -> None:
        seen.append(event)

    return seen, _spy


def make_boom_dispatcher(workspace: Path) -> ToolDispatcher:
    """A production-wired dispatcher plus a tool whose handler fails with a
    secret embedded in the exception message."""
    boundary = Boundary(workspace_root=workspace)
    dispatcher = attach_auto_approver(  # TD-802: mechanics tests auto-approve
        ToolDispatcher(
            create_registry(),
            classifier=AmbiguousClassifier(
                static=DecisionClassifier(boundary),
                call_worker=SpyWorker("B"),
            ),
            path_guard=PathGuard(boundary),
        )
    )
    register_builtin_handlers(dispatcher)
    dispatcher.registry.register(
        Tool(
            name="boom",
            description="Always fails, echoing the upstream message",
            parameters={"type": "object", "properties": {}, "required": []},
            side_effect_class="auto",
            parallel_safe=True,
        )
    )

    async def boom_handler(session: object, tool_call_id: str = "") -> str:
        raise RuntimeError(f"upstream refused key {PLANTED}")

    dispatcher.register_handler("boom", boom_handler)
    return dispatcher


def scripted_write(path: Path, content: str, reply: str) -> MockProvider:
    """One-turn brain script: fs_write the file, then stream the reply."""
    return MockProvider(
        sequences={
            "test-brain": [
                Script(
                    kind="tool_call",
                    tool_name="fs_write",
                    tool_arguments=json.dumps({"path": str(path), "content": content}),
                ),
                Script(kind="stream", content=reply),
            ]
        },
        default=Script(kind="stream", content="(unused)"),
    )


def _query_one(db_path: Path, sql: str) -> tuple[Any, ...] | None:
    """Sync query: keeps sqlite3 calls out of async test functions (ASYNC240)."""
    return sqlite3.connect(db_path).execute(sql).fetchone()


async def test_secret_in_tool_arguments_redacted_from_audit(
    ws: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A secret-shaped tool argument must never reach the audit store or the
    event log unredacted — and the write itself keeps the exact bytes."""
    # Workspace-relative tool argument from inside the workspace — TD-1406:
    # an absolute tmp_path argument is a drive-letter path on Windows,
    # refused fail-closed so the file never lands (and the redaction
    # pipeline never sees the write under test).
    monkeypatch.chdir(ws)
    session = Session(str(ws))
    seen, spy = event_spy()
    session.event_log.subscribe(spy)
    store = AuditStore(tmp_path / "audit.db")
    writer = AuditWriter(store)
    writer.start()
    writer.attach_session(session)

    dispatcher = make_dispatcher(ws)
    content = f"api_key = {PLANTED}\n"
    mock = scripted_write(Path("notes.txt"), content, "done")
    await start_loop(session, TierRouter(), mock, make_config(), dispatcher.registry, dispatcher)
    await session.add_user_message("write the key file")
    await wait_for_turn(session, 1)

    # Redaction never mutates execution: the file holds the exact bytes.
    assert _read(ws / "notes.txt") == content

    calls = [e for e in session.event_log.all_events if isinstance(e, ToolCallEvent)]
    assert calls, "loop did not log a tool_call event"
    logged_args = json.dumps(calls[0].arguments)
    assert PLANTED not in logged_args
    assert "[REDACTED]" in logged_args

    # The broadcast stream carries the same redacted copy replay serves.
    seen_calls = [e for e in seen if isinstance(e, ToolCallEvent)]
    assert seen_calls and PLANTED not in json.dumps(seen_calls[0].arguments)

    results = [e for e in session.event_log.all_events if isinstance(e, ToolResultEvent)]
    assert results and results[0].status == "success"
    assert results[0].diff is not None
    assert PLANTED not in results[0].diff

    await writer.close()
    row = _query_one(
        tmp_path / "audit.db", "SELECT arguments FROM tool_calls WHERE name = 'fs_write'"
    )
    assert row is not None
    assert PLANTED not in row[0]
    assert "[REDACTED]" in row[0]


async def test_secret_in_error_output_redacted(ws: Path) -> None:
    """A secret surfaced inside an exception must never appear in a
    user-visible error string — stored events, broadcast, or build_error."""
    session = Session(str(ws))
    dispatcher = make_boom_dispatcher(ws)
    mock = MockProvider(
        sequences={
            "test-brain": [
                Script(kind="tool_call", tool_name="boom", tool_arguments="{}"),
                Script(kind="stream", content="recovered"),
            ]
        },
        default=Script(kind="stream", content="(unused)"),
    )
    await start_loop(session, TierRouter(), mock, make_config(), dispatcher.registry, dispatcher)
    await session.add_user_message("try the thing")
    await wait_for_turn(session, 1)

    results = [e for e in session.event_log.all_events if isinstance(e, ToolResultEvent)]
    assert results and results[0].status == "error"
    assert PLANTED not in results[0].output
    assert "[REDACTED]" in results[0].output

    # build_error bypasses the event log (straight to the socket), so it
    # scrubs at construction instead.
    payload = json.loads(build_error("upstream", f"failed with {PLANTED}"))
    assert PLANTED not in payload["message"]
    assert "[REDACTED]" in payload["message"]
    assert json.loads(build_error("upstream", "plain failure"))["message"] == "plain failure"


async def test_benign_event_text_survives_byte_identical(
    ws: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Positive control: benign arguments, output, and diffs pass through
    the redaction chokepoint unchanged."""
    # Workspace-relative tool argument from inside the workspace — TD-1406
    # (see test_secret_in_tool_arguments_redacted_from_audit).
    monkeypatch.chdir(ws)
    session = Session(str(ws))
    dispatcher = make_dispatcher(ws)
    content = "release notes: all quiet\n"
    mock = scripted_write(Path("notes.txt"), content, "done")
    await start_loop(session, TierRouter(), mock, make_config(), dispatcher.registry, dispatcher)
    await session.add_user_message("write the notes")
    await wait_for_turn(session, 1)

    calls = [e for e in session.event_log.all_events if isinstance(e, ToolCallEvent)]
    assert calls and calls[0].arguments == {"path": "notes.txt", "content": content}

    results = [e for e in session.event_log.all_events if isinstance(e, ToolResultEvent)]
    assert results and "[REDACTED]" not in results[0].output
    assert results[0].diff is not None
    assert content.strip() in results[0].diff
    assert "[REDACTED]" not in results[0].diff


async def test_shell_output_chunks_redacted(ws: Path) -> None:
    """Streamed shell output passes through the same chokepoint (TD-605)."""
    session = Session(str(ws))
    await run_shell(session, f"echo {PLANTED}")
    chunks = [e for e in session.event_log.all_events if isinstance(e, ShellOutput)]
    assert chunks, "shell handler did not stream output events"
    assert all(PLANTED not in c.chunk for c in chunks)
    assert any("[REDACTED]" in c.chunk for c in chunks)


# ── 5. Non-loopback bind refusal ──────────────────────────────────────────

HOSTILE_HOSTS = ["0.0.0.0", "::", "192.168.1.10", "example.com", "", "*"]


@pytest.mark.parametrize("host", HOSTILE_HOSTS)
def test_non_loopback_bind_refused(host: str) -> None:
    with pytest.raises(ValueError, match="loopback only"):
        validate_interface(host)


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost"])
def test_loopback_bind_allowed(host: str) -> None:
    validate_interface(host)


def test_server_start_exposes_no_host_parameter() -> None:
    """start() takes no host. Opt-in Tailscale bind is __init__(bind=)
    and still routes through validate_interface / resolve_remote_bind."""
    assert "host" not in inspect.signature(WebSocketServer.start).parameters
    assert "bind" in inspect.signature(WebSocketServer.__init__).parameters


def test_unspecified_refused_even_as_extra_allowed() -> None:
    """0.0.0.0 / :: are never a legal extra Tailscale host (TD-3601)."""
    with pytest.raises(ValueError, match="loopback only"):
        validate_interface("0.0.0.0", extra_allowed="0.0.0.0")
    with pytest.raises(ValueError, match="loopback only"):
        validate_interface("::", extra_allowed="::")


def test_tailscale_extra_allowed_is_exact_address() -> None:
    """A resolved Tailscale host is allowed; a different LAN host is not."""
    validate_interface("100.64.1.5", extra_allowed="100.64.1.5")
    with pytest.raises(ValueError, match="loopback only"):
        validate_interface("192.168.1.10", extra_allowed="100.64.1.5")


# ── 6. Classifier bypass attempts (TD-702 chokepoint) ─────────────────────


async def test_dispatch_without_classifier_raises(ws: Path) -> None:
    dispatcher = make_dispatcher(ws, with_classifier=False)
    with pytest.raises(UnclassifiedToolCall):
        await dispatcher.dispatch("c1", "fs_read", {"path": "x.txt"})


async def test_path_tool_without_guard_raises(ws: Path) -> None:
    dispatcher = make_dispatcher(ws, with_guard=False)
    with pytest.raises(UnclassifiedToolCall):
        await dispatcher.dispatch("c1", "fs_read", {"path": "x.txt"})


def ambiguous_request(**arguments: Any) -> DecisionRequest:
    """No static rule can fire: no paths, no hosts, no mutation."""
    return DecisionRequest(tool_name="some_tool", arguments=arguments)


async def test_worker_garbage_never_auto_approves(ws: Path) -> None:
    # parse_decision legitimately normalizes a bare letter (case, stray
    # punctuation); these responses must still be rejected outright.
    for junk in ("yes", "A B", "", "C and also A", "maybe", "1"):
        classifier = AmbiguousClassifier(
            static=DecisionClassifier(Boundary(workspace_root=ws)),
            call_worker=SpyWorker(junk),
        )
        classification = await classifier.classify(ambiguous_request(q=1))
        assert classification.decision_class is DecisionClass.B


async def test_failing_worker_fails_toward_ask(ws: Path) -> None:
    async def boom(_prompt: str) -> str:
        raise RuntimeError("worker unreachable")

    classifier = AmbiguousClassifier(
        static=DecisionClassifier(Boundary(workspace_root=ws)),
        call_worker=boom,
    )
    classification = await classifier.classify(ambiguous_request(q=1))
    assert classification.decision_class is DecisionClass.B


async def test_poisoned_worker_cannot_downgrade_static_class(
    ws: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A worker answering A for a request the static table calls C must be
    ignored entirely — the worker is only consulted when no rule fires."""
    # Workspace-relative write target from inside the workspace — TD-1406:
    # an absolute tmp_path target is a drive-letter path on Windows, where
    # the boundary-unsafe-path rule fires before the steering-file rule
    # under test.
    monkeypatch.chdir(ws)
    spy = SpyWorker("A")
    classifier = AmbiguousClassifier(
        static=DecisionClassifier(Boundary(workspace_root=ws)),
        call_worker=spy,
    )
    classification = await classifier.classify(
        DecisionRequest(tool_name="fs_write", writes=(Path("AGENTS.md"),), is_mutation=True)
    )
    assert classification.decision_class is DecisionClass.C
    assert classification.rule is not None
    assert classification.rule.id == "steering-file-write"
    assert spy.calls == []


async def test_worker_cache_keyed_by_argument_shape(ws: Path) -> None:
    """A cached class must apply only to the same argument shape — different
    arguments re-consult the worker instead of inheriting a decision."""
    spy = SpyWorker("B")
    classifier = AmbiguousClassifier(
        static=DecisionClassifier(Boundary(workspace_root=ws)),
        call_worker=spy,
    )
    await classifier.classify(ambiguous_request(a=1))
    await classifier.classify(ambiguous_request(a=1))
    assert len(spy.calls) == 1  # cached
    await classifier.classify(ambiguous_request(a=2))
    assert len(spy.calls) == 2  # different shape: re-consulted


def test_class_c_rules_precede_class_a_rules() -> None:
    """Every C rule must evaluate before the A rule; an A that fires first is
    an auto-approval bypass."""
    positions = {rule.id: i for i, rule in enumerate(RULE_TABLE)}
    first_a = positions["in-workspace-edit"]
    for rule in RULE_TABLE:
        if rule.decision_class is DecisionClass.C:
            assert positions[rule.id] < first_a, f"{rule.id} must precede 'in-workspace-edit'"


def test_dispatch_call_sites_confined() -> None:
    """Calls into the dispatcher are confined to the known set; a new call
    site is a new chokepoint risk and must be added deliberately."""
    source_root = Path(__file__).resolve().parent.parent / "tstd"
    allowed = {"loop.py", "dispatch.py", "dod.py"}
    callers = set()
    for py_file in source_root.rglob("*.py"):
        # utf-8 explicitly: the platform default is cp1252 on Windows, which
        # cannot decode the UTF-8 punctuation in tstd sources (TD-1406).
        text = py_file.read_text(encoding="utf-8")
        if re.search(r"\.dispatch\(|dispatch_many\(", text):
            callers.add(py_file.name)
    assert callers <= allowed, f"unexpected dispatch call sites: {sorted(callers - allowed)}"


# ── 6. Whole-event redaction (TD-4802) ─────────────────────────────────
#
# TD-1405's chokepoint redacted a hand-listed set of event types; every type
# added later (ApprovalRequest, DecisionLogged, the assistant deltas) skipped
# it. The scrub is now whole-event, and these tests make that categorical:
# the enumeration plants a canary in every string field of every event type
# and requires none to survive.


def _carries_text(annotation: Any) -> bool:
    """Whether the annotation can hold free text somewhere in its shape."""
    if annotation is str:
        return True
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return True
    origin = get_origin(annotation)
    if origin in (Union, UnionType):
        return any(a is not type(None) and _carries_text(a) for a in get_args(annotation))
    if origin in (list, tuple, set, frozenset):
        args = get_args(annotation)
        return bool(args) and _carries_text(args[0])
    if origin is dict:
        args = get_args(annotation)
        return len(args) == 2 and _carries_text(args[1])
    return False


def _synth(annotation: Any, *, canary: bool) -> Any:
    """A valid value for a field annotation, planting PLANTED where it fits."""
    if annotation is str:
        return PLANTED if canary else "x"
    origin = get_origin(annotation)
    if origin is Literal:
        return get_args(annotation)[0]
    if origin in (Union, UnionType):
        for arg in get_args(annotation):
            if arg is not type(None):
                return _synth(arg, canary=canary)
        return None
    if origin in (list, tuple, set, frozenset):
        args = get_args(annotation)
        item = args[0] if args else None
        if isinstance(item, type) and issubclass(item, BaseModel):
            return [item(**_canary_kwargs(item))]
        if item is str:
            return [PLANTED if canary else "x"]
        return []
    if origin is dict:
        args = get_args(annotation)
        val = args[1] if len(args) == 2 else None
        if val is str:
            return {"k": PLANTED if canary else "x"}
        if isinstance(val, type) and issubclass(val, BaseModel):
            return {"k": val(**_canary_kwargs(val))}
        return {}
    if annotation is int:
        return 1
    if annotation is float:
        return 0.5
    if annotation is bool:
        return False
    if isinstance(annotation, type):
        if issubclass(annotation, BaseModel):
            return annotation(**_canary_kwargs(annotation))
        if issubclass(annotation, StrEnum):
            return next(iter(annotation))
    return None


def _canary_kwargs(cls: type[BaseModel]) -> dict[str, Any]:
    """Constructor kwargs with PLANTED in every string field that accepts it.

    A field that rejects the canary under validation is shape-constrained —
    it cannot carry a secret — and excludes itself.
    """
    kwargs = {
        name: _synth(field.annotation, canary=False)
        for name, field in cls.model_fields.items()
        if field.is_required()
    }
    cls(**kwargs)  # synthesizer sanity: a failure here is a bug in this test
    for name, field in cls.model_fields.items():
        if not _carries_text(field.annotation):
            continue
        probe = {**kwargs, name: _synth(field.annotation, canary=True)}
        try:
            cls(**probe)
        except ValidationError:
            continue
        kwargs = probe
    return kwargs


@pytest.mark.parametrize(
    "event_cls",
    sorted(DaemonEvent.__subclasses__(), key=lambda c: c.__name__),
    ids=lambda c: c.__name__,
)
def test_every_event_type_scrubs_every_text_field(event_cls: type[DaemonEvent]) -> None:
    """A canary planted in any string field of any event type never survives
    insertion redaction. New event types join this test automatically — a
    type that skips the scrub fails here the day it is added."""
    event = event_cls(**_canary_kwargs(event_cls))
    scrubbed = _redact_event(event)
    assert PLANTED not in json.dumps(scrubbed.model_dump())


def test_benign_event_returns_same_object() -> None:
    """Fast path: nothing matched, so the caller's object is stored as-is."""
    event = ToolCallEvent(
        seq=1, session_id="s1", tool_call_id="t1", name="fs_read", arguments={"path": "a.md"}
    )
    assert _redact_event(event) is event


def test_previously_skipped_fields_are_scrubbed() -> None:
    """The named TD-4802 regressions, pinned directly: the pre-fix chokepoint
    let each of these through raw. Also proves the enumeration test's canary
    planting is not vacuous — each constructed event really carries it."""
    from tstd.protocol import ApprovalRequest, AssistantDelta, DecisionLogged

    cases = [
        ApprovalRequest(
            seq=1,
            session_id="s1",
            tool_call_id="t1",
            tool_name="shell",
            arguments={"command": f"deploy {PLANTED}"},
            decision_class="B",
            summary=f"Run deploy with {PLANTED}",
            reason=f"class B, key {PLANTED}",
        ),
        DecisionLogged(
            seq=1, session_id="s1", decision_class="B", what=f"write {PLANTED}", why="asked"
        ),
        AssistantDelta(seq=1, session_id="s1", delta=f"your key is {PLANTED}"),
    ]
    for event in cases:
        assert PLANTED in json.dumps(event.model_dump()), "canary planting went vacuous"
        scrubbed = _redact_event(event)
        assert PLANTED not in json.dumps(scrubbed.model_dump())


def test_scrub_wire_json_redacts_nested_reply_text() -> None:
    """The reply funnel scrubs nested structures — a session title is the
    first user message and rode the wire raw before TD-4802."""
    payload = json.dumps(
        {
            "type": "session_list",
            "seq": 1,
            "sessions": [
                {
                    "session_id": "s1",
                    "workspace_path": "/tmp/ws",
                    "state": "idle",
                    "created_at": "2026-08-21",
                    "updated_at": "2026-08-21",
                    "event_count": 1,
                    "title": f"rotate the key {PLANTED}",
                }
            ],
        }
    )
    out = scrub_wire_json(payload)
    assert PLANTED not in out
    assert "[REDACTED]" in out
    # Benign frames pass semantically unchanged.
    benign = json.dumps({"type": "ready", "seq": 1, "version": "0.1.0", "protocol_version": 1})
    assert json.loads(scrub_wire_json(benign)) == json.loads(benign)


async def test_connection_scoped_reply_scrubs_memory_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end: memory file content (user text) in a list_memory reply
    never reaches the socket raw."""
    from tests.test_setup_state import _ask, _connect_and_handshake, _start_daemon, _stop_daemon

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    workspace = tmp_path / "ws"
    (workspace / ".tst" / "memory").mkdir(parents=True)
    (workspace / ".tst" / "memory" / "notes.md").write_text(
        f"the key is {PLANTED}\n", encoding="utf-8"
    )

    with tempfile.TemporaryDirectory() as data:
        daemon, task = await _start_daemon(Path(data))
        try:
            ws = await _connect_and_handshake(
                f"ws://127.0.0.1:{daemon.ws_server.port}", daemon.ws_server.token
            )
            resp = await _ask(ws, {"type": "list_memory", "workspace_path": str(workspace)})
            assert resp["type"] == "memory_files"
            assert PLANTED not in json.dumps(resp)
            assert "[REDACTED]" in json.dumps(resp)
            await ws.close()
        finally:
            await _stop_daemon(task)


def test_append_decision_scrubs_free_text(tmp_path: Path) -> None:
    """The decisions ledger scrubbed nothing while tool_calls scrubbed
    everything (TD-4802)."""
    store = AuditStore(tmp_path / "audit.db")
    store.append_session("s1", str(tmp_path), 1.0)
    store.append_decision(
        "s1", DecisionClass.B, f"write the key {PLANTED}", f"because {PLANTED} was asked", None
    )
    row = _query_one(tmp_path / "audit.db", "SELECT what, why FROM decisions")
    store.close()
    assert row is not None
    assert PLANTED not in row[0] and PLANTED not in row[1]
    assert "[REDACTED]" in row[0] and "[REDACTED]" in row[1]


def test_formatter_redacts_extra_fields_and_traceback() -> None:
    """The redaction filter runs on msg/args before formatting; extra_fields
    and the traceback are merged after — the formatter scrubs them itself."""
    logger, buf = capture_logger("test.security.formatter")
    try:
        raise ValueError(f"bad key {PLANTED}")
    except ValueError:
        logger.exception("boom", extra={"extra_fields": {"detail": PLANTED, "n": 1}})
    rendered = json.loads(buf.getvalue())
    assert rendered["detail"] == "[REDACTED]"
    assert rendered["n"] == 1
    assert PLANTED not in rendered["exception"]
    assert PLANTED not in buf.getvalue()
