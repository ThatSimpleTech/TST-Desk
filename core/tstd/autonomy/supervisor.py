"""Validator drift check — detect and report only (TD-4201, spec §12.6).

Every N autonomy turns (``check_every``, default 5) or on Class B this
turn, the validator answers the three intent-guardian questions. No
revert (TD-4202), no circuit breakers (TD-4203). Not a user turn.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..logging import get_logger
from ..provider import ChatCompletionRequest, ChatMessage, ProviderError
from .checkpoint import auto_branch
from .ledger import DecisionLedger, format_entry

if TYPE_CHECKING:
    from ..config import ModelConfig, TierConfig
    from ..cost import CostTracker
    from ..session import Session

log = get_logger("tstd.autonomy.supervisor")

_GIT_TIMEOUT = 30.0
NO_TEST_OUTPUT = "no test output"
_INSTRUCTIONS = (
    "You are the intent guardian for an autonomous run. "
    "Answer the three questions. Do not propose edits."
)
_QUESTIONS = (
    "1. Does the work still serve the objective? (YES/NO)",
    "2. Have the accumulated Class A decisions drifted from the stated intent? (YES/NO/DRIFT)",
    "3. Is progress real, or is it thrashing? (YES/NO/THRASHING)",
)
_JSON_HINT = (
    'Reply as JSON {"serves_objective": true|false, '
    '"class_a_drifted": true|false, "progress_real": true|false} '
    "or three YES/NO/DRIFT lines in order."
)
_LINE_TOKEN = re.compile(
    r"^(?:\d+[.)]\s*)?(YES|NO|DRIFT|THRASHING|TRUE|FALSE)\s*$",
    re.IGNORECASE,
)
_TRUTHY = frozenset({"YES", "TRUE"})
_FALSY = frozenset({"NO", "FALSE"})
_DRIFT = frozenset({"YES", "TRUE", "DRIFT"})
_NOT_PROGRESS = frozenset({"NO", "FALSE", "THRASHING"})

ClientFactory = Callable[["TierConfig"], Awaitable[Any]]


@dataclass(frozen=True)
class DriftCheckResult:
    """Parsed §12.6 answers. Detect and report only."""

    serves_objective: bool | None
    class_a_drifted: bool | None
    progress_real: bool | None
    drift_detected: bool
    raw: str
    unparseable: bool = False


def should_check(*, autonomy: bool, turns: int, class_b: bool, check_every: int) -> bool:
    """True when this finished turn is due a validator check."""
    if not autonomy or turns < 1 or check_every < 1:
        return False
    return class_b or turns % check_every == 0


def parse_drift_answer(text: str) -> DriftCheckResult:
    """Parse JSON or three YES/NO/DRIFT lines. Unparseable is drift."""
    parsed = _try_json(text) or _try_lines(text)
    if parsed is None or None in parsed:
        return DriftCheckResult(None, None, None, True, text, unparseable=True)
    serves, drifted, progress = parsed
    assert serves is not None and drifted is not None and progress is not None
    return DriftCheckResult(serves, drifted, progress, not serves or drifted or not progress, text)


def _coerce(value: object, truthy: frozenset[str], falsy: frozenset[str]) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        token = value.strip().upper()
        if token in truthy:
            return True
        if token in falsy:
            return False
    return None


def _try_json(text: str) -> tuple[bool | None, bool | None, bool | None] | None:
    blob = _json_object(text)
    if blob is None:
        return None
    return (
        _coerce(blob.get("serves_objective"), _TRUTHY, _FALSY),
        _coerce(blob.get("class_a_drifted"), _DRIFT, _FALSY),
        _coerce(blob.get("progress_real"), _TRUTHY, _NOT_PROGRESS),
    )


def _json_object(text: str) -> dict[str, Any] | None:
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if len(lines) >= 3 and lines[-1].strip().startswith("```"):
            stripped = "\n".join(lines[1:-1]).strip()
    try:
        data: object = json.loads(stripped)
    except json.JSONDecodeError:
        start, end = stripped.find("{"), stripped.rfind("}")
        if start == -1 or end <= start:
            return None
        try:
            data = json.loads(stripped[start : end + 1])
        except json.JSONDecodeError:
            return None
    return data if isinstance(data, dict) else None


def _try_lines(text: str) -> tuple[bool | None, bool | None, bool | None] | None:
    tokens = [
        m.group(1).upper() for line in text.splitlines() if (m := _LINE_TOKEN.match(line.strip()))
    ]
    if len(tokens) < 3:
        return None
    return (
        _coerce(tokens[0], _TRUTHY, _FALSY),
        _coerce(tokens[1], _DRIFT, _FALSY),
        _coerce(tokens[2], _TRUTHY, _NOT_PROGRESS),
    )


def attach_drift_check(
    session: Session,
    *,
    config: ModelConfig,
    client_for: ClientFactory,
    tracker: CostTracker,
) -> None:
    """Wire the validator client and ``check_every`` onto *session*."""
    session.autonomy_check_every = config.autonomy.check_every

    async def _call(prompt: str) -> str:
        return await invoke_validator(
            prompt, config=config, client_for=client_for, tracker=tracker, session=session
        )

    session.validator_call = _call


async def invoke_validator(
    prompt: str,
    *,
    config: ModelConfig,
    client_for: ClientFactory,
    tracker: CostTracker | None,
    session: Session,
) -> str:
    """Call the validator tier. Not a user turn — do not touch the router."""
    from ..local_worker import effective_tier

    tier_cfg = effective_tier(config, "validator", cu_heavy=False)
    client = await client_for(tier_cfg)
    resp = await client.chat_completion(
        ChatCompletionRequest(
            model=tier_cfg.require_slug(),
            messages=[ChatMessage(role="user", content=prompt)],
            max_tokens=tier_cfg.max_output_tokens,
            temperature=0.0,
        )
    )
    if isinstance(resp, ProviderError):
        log.warning(
            "validator drift check provider error",
            extra={"extra_fields": {"session_id": session.id, "error": resp.message}},
        )
        return ""
    if resp.usage is not None and tracker is not None:
        tracker.record_off_turn("validator", resp.usage, tier_cfg)
        await session.event_log.add(tracker.emit_cost_update(session.id))
    return resp.message.content or ""


async def maybe_check_drift(session: Session) -> DriftCheckResult | None:
    """Run a check when due. Interactive sessions never check."""
    if not session.autonomy or session.charter is None or session.validator_call is None:
        return None
    if not should_check(
        autonomy=True,
        turns=session.autonomy_turns,
        class_b=session.autonomy_class_b,
        check_every=session.autonomy_check_every,
    ):
        return None
    result = await run_drift_check(session)
    session.last_drift_check = result
    session.autonomy_class_b = False
    log.info(
        "drift check",
        extra={
            "extra_fields": {
                "session_id": session.id,
                "turn": session.autonomy_turns,
                "serves_objective": result.serves_objective,
                "class_a_drifted": result.class_a_drifted,
                "progress_real": result.progress_real,
                "drift_detected": result.drift_detected,
                "unparseable": result.unparseable,
            }
        },
    )
    return result


async def run_drift_check(session: Session) -> DriftCheckResult:
    """Assemble context, call the validator, parse. Fail closed on errors."""
    call = session.validator_call
    if call is None:
        return parse_drift_answer("")
    prompt = await assemble_check_prompt(session)
    try:
        raw = await call(prompt)
    except Exception:
        log.exception("validator drift check call failed")
        raw = ""
    return parse_drift_answer(raw)


async def assemble_check_prompt(session: Session) -> str:
    """Charter + live source_of_truth + diff + new ledger + tests."""
    charter = session.charter
    if charter is None:
        return _INSTRUCTIONS
    sources = await asyncio.to_thread(
        _read_sources_sync, session.workspace_path, list(charter.source_of_truth)
    )
    diff, tip = await _diff_since(session)
    if tip is not None:
        session.autonomy_last_check_sha = tip
    ledger = await asyncio.to_thread(_new_ledger, session)
    tests = _test_output(session)
    assembled = await _tier_context(Path(session.workspace_path), diff, tests)
    return "\n\n".join(
        [
            _INSTRUCTIONS,
            "## Charter\n" + _charter_text(charter),
            "## Source of truth\n" + sources,
            "## Diff\n" + diff,
            "## Ledger\n" + ledger,
            "## Tests\n" + tests,
            assembled,
            "\n".join(_QUESTIONS),
            _JSON_HINT,
        ]
    )


def _charter_text(charter: Any) -> str:
    done = "\n".join(f"- {item}" for item in charter.definition_of_done)
    truths = "\n".join(f"- {p}" for p in charter.source_of_truth) or "- (none)"
    stops = "\n".join(f"- {s}" for s in charter.stop_conditions) or "- (none)"
    return (
        f"objective: {charter.objective}\n"
        f"definition_of_done:\n{done}\n"
        f"source_of_truth:\n{truths}\n"
        f"stop_conditions:\n{stops}"
    )


def _read_sources_sync(workspace: str, paths: list[str]) -> str:
    """Re-read ``source_of_truth`` from disk. Skip paths that fail the wall."""
    # Local import: tools.boundary → tools package → dispatch → autonomy.
    from ..tools.boundary import PathGuard, RefusalError
    from .classifier import Boundary

    if not paths:
        return "(none)"
    guard = PathGuard(Boundary(workspace_root=Path(workspace).resolve()))
    parts: list[str] = []
    for raw in paths:
        try:
            target = guard.check_read(raw)
        except RefusalError:
            continue
        if not target.is_file():
            continue
        try:
            parts.append(f"### {raw}\n{target.read_text(encoding='utf-8')}")
        except OSError:
            continue
    return "\n\n".join(parts) if parts else "(none readable)"


def _new_ledger(session: Session) -> str:
    entries = DecisionLedger(session.workspace_path).read()
    new = entries[session.autonomy_ledger_seen :]
    session.autonomy_ledger_seen = len(entries)
    if not new:
        return "(no new ledger entries)"
    return "\n\n".join(format_entry(entry) for entry in new)


def _test_output(session: Session) -> str:
    results = getattr(session.last_dod_poll, "results", None)
    if not results:
        return NO_TEST_OUTPUT
    lines: list[str] = []
    for item in results:
        lines.append(f"{'GREEN' if item.green else 'RED'} ({item.via}): {item.item}")
        if item.detail:
            lines.append(item.detail)
    return "\n".join(lines) if lines else NO_TEST_OUTPUT


async def _tier_context(workspace: Path, diff: str, test_output: str) -> str:
    from ..context.tier import assemble_for_tier

    try:
        ctx = await assemble_for_tier(
            "validator", workspace_path=workspace, diff=diff, test_output=test_output
        )
    except Exception:
        log.exception("validator tier assemble failed")
        return ""
    return ctx.text


async def _diff_since(session: Session) -> tuple[str, str | None]:
    """Working-tree diff since the last check SHA, else last checkpoint."""
    workspace = Path(session.workspace_path)
    branch: str | None = None
    if session.charter is not None:
        try:
            branch = auto_branch(session.charter.slug)
        except ValueError:
            branch = None
    since = session.autonomy_last_check_sha or await _resolve_sha(workspace, branch)
    tip = await _resolve_sha(workspace, branch)
    if since is None:
        return "", tip
    rc, out = await _git(workspace, "diff", since)
    return (out if rc == 0 else ""), tip


async def _resolve_sha(workspace: Path, branch: str | None) -> str | None:
    if branch:
        rc, out = await _git(workspace, "rev-parse", "--verify", "-q", f"refs/heads/{branch}")
        if rc == 0 and out.strip():
            return out.strip()
    rc, out = await _git(workspace, "rev-parse", "--verify", "-q", "HEAD")
    return out.strip() if rc == 0 and out.strip() else None


async def _git(workspace: Path, *args: str) -> tuple[int, str]:
    from ..tools.shell import sanitized_env

    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            str(workspace),
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=sanitized_env(),
        )
    except OSError:
        return 1, ""
    try:
        out_b, _err = await asyncio.wait_for(proc.communicate(), timeout=_GIT_TIMEOUT)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return 1, ""
    rc = proc.returncode if proc.returncode is not None else 1
    return rc, out_b.decode("utf-8", errors="replace")
