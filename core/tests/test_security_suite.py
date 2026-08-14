"""Cross-cutting security suite (TD-1402).

Release blockers: path-escape attempts across the TD-602 vectors, steering-file
write refusal at guard level, secret redaction in logs, non-loopback bind
refusal, and classifier-bypass attacks on the dispatch chokepoint.

Skipped with reasons — gaps, not verdicts:
- Tool-level steering refusal needs TD-604 (fs_write has no handler yet; a
  dispatched write returns no_handler before the guard runs).
- Environment sanitization needs TD-605 (no child-process code exists).
- Redaction of audit/event and error-message surfaces has no implementation:
  the logging filter is the only chokepoint, and ToolCallEvent.arguments /
  ToolResultEvent.output bypass it. No story covers this yet.
"""

from __future__ import annotations

import inspect
import json
import logging
import os
import re
from io import StringIO
from pathlib import Path
from typing import Any

import pytest

from tstd.autonomy import (
    AmbiguousClassifier,
    Boundary,
    DecisionClass,
    DecisionClassifier,
    DecisionRequest,
)
from tstd.autonomy.classifier import RULE_TABLE, is_steering_write
from tstd.logging import SECRET_PATTERNS, JSONFormatter, SecretsRedactionFilter
from tstd.tools import (
    ToolDispatcher,
    UnclassifiedToolCall,
    create_registry,
    register_builtin_handlers,
)
from tstd.tools.boundary import PathGuard, RefusalError
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
    dispatcher = ToolDispatcher(
        create_registry(),
        classifier=classifier,
        path_guard=PathGuard(boundary) if with_guard else None,
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


# ── 1. Path escape attempts (TD-602 vectors) ─────────────────────────────

TRAVERSAL_VECTORS = [
    "../outside.txt",
    "../../etc/passwd",
    "a/b/../../../outside.txt",
    "..",
]


@pytest.mark.parametrize("vector", TRAVERSAL_VECTORS)
def test_traversal_refused_for_read_and_write(ws: Path, vector: str) -> None:
    # The guard has no notion of relative-to-workspace: raw paths resolve
    # against the process CWD, so vectors are anchored at the workspace.
    g = guard(ws)
    for check in (g.check_read, g.check_write):
        with pytest.raises(RefusalError) as excinfo:
            check(str(ws / vector))
        assert excinfo.value.code == "outside_workspace"


def test_absolute_path_outside_refused(ws: Path) -> None:
    with pytest.raises(RefusalError) as excinfo:
        guard(ws).check_read("/etc/passwd")
    assert excinfo.value.code == "outside_workspace"


def test_symlinked_file_escape_refused(ws: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    (ws / "link.txt").symlink_to(outside)
    with pytest.raises(RefusalError) as excinfo:
        guard(ws).check_read(str(ws / "link.txt"))
    assert excinfo.value.code == "outside_workspace"


def test_symlinked_parent_dir_escape_refused(ws: Path, tmp_path: Path) -> None:
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    (outside_dir / "secret.txt").write_text("secret")
    (ws / "dirlink").symlink_to(outside_dir, target_is_directory=True)
    with pytest.raises(RefusalError) as excinfo:
        guard(ws).check_read(str(ws / "dirlink" / "secret.txt"))
    assert excinfo.value.code == "outside_workspace"


WINDOWS_VECTORS = [
    "C:\\Windows\\system.ini",  # drive absolute
    "C:relative.txt",  # drive relative
    "\\\\server\\share\\file.txt",  # UNC
    "PROGRA~1.txt",  # 8.3 short name
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


def test_percent_encoded_traversal_is_inert(ws: Path) -> None:
    """The guard must not decode: %2f is a literal filename inside the
    workspace, never a smuggled separator."""
    g = guard(ws)
    resolved = g.check_read(str(ws / "..%2f..%2fetc"))
    assert g.canonicalize(ws) in resolved.parents


def test_hardlink_write_refused(ws: Path) -> None:
    original = ws / "original.txt"
    original.write_text("x")
    os.link(original, ws / "alias.txt")
    with pytest.raises(RefusalError) as excinfo:
        guard(ws).check_write(str(original))
    assert excinfo.value.code == "hardlink"


def test_writable_patterns_enforced_for_writes_only(ws: Path) -> None:
    g = guard(ws, writable=("src/**",))
    with pytest.raises(RefusalError) as excinfo:
        g.check_write(str(ws / "docs" / "notes.md"))
    assert excinfo.value.code == "outside_writable_paths"
    # Reads must not be narrowed by write policy.
    allowed_read = ws / "docs" / "notes.md"
    allowed_write = ws / "src" / "ok.py"
    assert g.check_read(str(allowed_read)) == g.canonicalize(allowed_read)
    assert g.check_write(str(allowed_write)) == g.canonicalize(allowed_write)


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
]

STEERING_NEGATIVE = [
    "AGENTS.md.bak",
    "MYAGENTS.md",
    "notes.md",
    ".tst/config.yaml",
    ".tst/rules.md",  # a file named rules.md is not the rules dir
]


@pytest.mark.parametrize("relative", STEERING_POSITIVE)
def test_steering_targets_detected(ws: Path, relative: str) -> None:
    assert is_steering_write(Boundary(workspace_root=ws), ws / relative)


@pytest.mark.parametrize("relative", STEERING_NEGATIVE)
def test_steering_lookalikes_not_detected(ws: Path, relative: str) -> None:
    assert not is_steering_write(Boundary(workspace_root=ws), ws / relative)


@pytest.mark.parametrize("relative", STEERING_POSITIVE)
def test_steering_write_refused_at_guard(ws: Path, relative: str) -> None:
    with pytest.raises(RefusalError) as excinfo:
        guard(ws).check_write(str(ws / relative))
    assert excinfo.value.code == "steering_file"


@pytest.mark.skip(
    reason="TD-604: fs_write has no handler, so a dispatched write is rejected "
    "with no_handler before the guard runs. Unskip when fs_write lands; expect "
    "boundary_refusal and decision_class C here."
)
async def test_steering_write_refused_at_tool_level(ws: Path) -> None:
    dispatcher = make_dispatcher(ws)
    result = await dispatcher.dispatch("c1", "fs_write", {"path": str(ws / "AGENTS.md")})
    assert result.status == "error"
    assert result.error_code == "boundary_refusal"
    assert result.decision_class is DecisionClass.C


# ── 3. Environment sanitization for child processes ──────────────────────


@pytest.mark.skip(
    reason="TD-605: no shell-exec implementation exists to sanitize. The "
    "sanitizer must strip secret-bearing env vars (API keys, tokens, "
    "credentials) from every child process; write this test against its "
    "interface when it lands."
)
def test_child_process_env_sanitized() -> None:
    """Child processes must inherit a scrubbed environment, never the
    daemon's secrets."""


# ── 4. Secret redaction ───────────────────────────────────────────────────

FAKE_SECRETS = [
    "sk-PROJEXAMPLEKEYfakefake0000abcd",  # tst-secret-ok
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


@pytest.mark.skip(
    reason="No redaction chokepoint exists for the audit/event pipeline: "
    "ToolCallEvent.arguments and ToolResultEvent.output are stored verbatim. "
    "Needs an implementation story; criterion 4 makes this a release blocker."
)
def test_secret_in_tool_arguments_redacted_from_audit() -> None:
    """A secret-shaped tool argument must never reach the audit store or the
    event log unredacted."""


@pytest.mark.skip(
    reason="Error strings bypass redaction: handler exceptions and keychain "
    "stderr are embedded verbatim. Needs the same chokepoint story as the "
    "audit-surface test above."
)
def test_secret_in_error_output_redacted() -> None:
    """A secret surfaced inside an exception or stderr must never appear in a
    user-visible error string."""


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
    """Structural guard: the server hardcodes loopback. If a host parameter is
    ever added, it must route through validate_interface — this test forces
    that conversation instead of letting the parameter slip in."""
    assert "host" not in inspect.signature(WebSocketServer.start).parameters


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


async def test_poisoned_worker_cannot_downgrade_static_class(ws: Path) -> None:
    """A worker answering A for a request the static table calls C must be
    ignored entirely — the worker is only consulted when no rule fires."""
    spy = SpyWorker("A")
    classifier = AmbiguousClassifier(
        static=DecisionClassifier(Boundary(workspace_root=ws)),
        call_worker=spy,
    )
    classification = await classifier.classify(
        DecisionRequest(tool_name="fs_write", writes=(ws / "AGENTS.md",), is_mutation=True)
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
    allowed = {"loop.py", "dispatch.py"}
    callers = set()
    for py_file in source_root.rglob("*.py"):
        text = py_file.read_text()
        if re.search(r"\.dispatch\(|dispatch_many\(", text):
            callers.add(py_file.name)
    assert callers <= allowed, f"unexpected dispatch call sites: {sorted(callers - allowed)}"
