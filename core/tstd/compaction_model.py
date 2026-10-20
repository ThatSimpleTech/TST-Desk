"""Model-written compaction summaries (TD-5128).

The extractive summary keeps 200 characters of each folded message, which
on a 32k-65k window loses the goal, the decisions and the files within a
couple of compactions. With ``compaction.summary: model`` the worker tier
writes a structured summary instead (goal, decisions, files touched, open
items), from ``compaction.summary_prompt`` or the built-in prompt. A worker
that fails, runs past ``summary_timeout_seconds``, is cut off at its token
cap, or answers without the built-in prompt's four headings leaves the
extractive summary in its place: compaction never waits on, or fails with,
the worker.

So that the turn that crosses the budget need not wait, the summary can be
written ahead (``background_percent``): once a request passes that share of
the compaction budget, the turns the *next* turn's compaction would fold
(everything before the last user turn) are summarized in the background,
while the turn runs. A compaction that folds exactly those turns takes the
summary ready-made; one that folds other turns writes its own. Tool results
are left out of the match, because clearing stale results (TD-5115) changes
them after the summary was written and the summary is no worse for having
seen them whole.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

from .compaction import (
    MAX_GUIDANCE_CHARS,
    CompactionStats,
    auto_span,
    budget_threshold,
    compact_on_request,
    estimate_tokens,
    find_compaction_point,
    manual_span,
    maybe_compact,
)
from .compaction_files import FileAccess, read_recent_files, recent_files
from .compaction_text import is_summary, summary_core
from .config import CompactionConfig, TierConfig
from .context.tokens import TokenCounter
from .logging import get_logger
from .provider import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatMessage,
    ProviderError,
    ToolDefinition,
    content_as_text,
)

log = get_logger("tstd.compaction_model")

SUMMARY_HEADINGS = ("## Goal", "## Decisions", "## Files touched", "## Open items")

DEFAULT_SUMMARY_PROMPT = """\
You write the summary that replaces the earlier part of a coding agent's
conversation when its context window fills. The agent keeps working from
your summary and the turns after it, so keep what it needs to carry on and
drop the chatter. Answer in Markdown with exactly these four headings, in
this order, and nothing before the first:
## Goal
## Decisions
## Files touched
## Open items
Goal: what the user wants overall, in their words where it matters.
Decisions: what was chosen and why, and approaches ruled out.
Files touched: each file read, created or changed, by path, with one line
on what happened to it.
Open items: what is unfinished, failing, or promised next.
Write "none" under a heading with nothing to say. Do not call tools."""

# Per message, what the worker sees of a folded turn.
_CHARS_PER_TEXT = 4_000
_CHARS_PER_TOOL_RESULT = 1_500
_CHARS_PER_TOOL_ARGS = 300
# Conservative characters per token when sizing the transcript for the
# worker's window: code and paths run below the usual four.
_CHARS_PER_TOKEN = 3
# Room left in the worker's window for the prompt and message overhead.
_PROMPT_RESERVE_TOKENS = 1_000
_THINK = re.compile(r"<think>.*?</think>", re.DOTALL)

Complete = Callable[[ChatCompletionRequest], Awaitable[ChatCompletionResponse | ProviderError]]


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "…"


def _line(message: ChatMessage) -> str:
    text = content_as_text(message.content)
    if is_summary(message):
        return f"Summary of still earlier turns:\n{summary_core(text)}"
    if message.role == "user":
        return f"{'Done-check' if message.check else 'User'}: {_cut(text, _CHARS_PER_TEXT)}"
    if message.role == "tool":
        return f"Tool result: {_cut(text, _CHARS_PER_TOOL_RESULT)}"
    if message.role == "assistant":
        parts = [f"Assistant: {_cut(text, _CHARS_PER_TEXT)}"] if text.strip() else []
        for call in message.tool_calls or ():
            if call.function is not None:
                args = _cut(call.function.arguments or "", _CHARS_PER_TOOL_ARGS)
                parts.append(f"Assistant called {call.function.name}({args})")
        return "\n".join(parts)
    return _cut(text, _CHARS_PER_TEXT)


def transcript(dropped: Sequence[ChatMessage], max_chars: int) -> str:
    """The folded turns as the worker reads them, within *max_chars*.

    A prior summary is kept whole; when the rest is over, the oldest
    messages go first, so what the agent did last survives.
    """
    lines = [line for line in (_line(m) for m in dropped) if line]
    pinned = [i for i, m in enumerate(dropped) if is_summary(m)]
    removed = 0
    while sum(len(line) + 1 for line in lines) > max_chars:
        index = next((i for i in range(len(lines)) if i not in pinned), None)
        if index is None:
            break
        del lines[index]
        pinned = [p - 1 if p > index else p for p in pinned]
        removed += 1
    if removed:
        lines.insert(0, f"[{removed} earlier messages left out to fit]")
    return "\n".join(lines)[:max_chars]


def _fingerprint(dropped: Sequence[ChatMessage], guidance: str) -> str:
    """Which turns a summary stands for, tool result text aside."""
    digest = hashlib.sha256(guidance.encode("utf-8"))
    for message in dropped:
        calls = [
            (c.id, c.function.name, c.function.arguments)
            for c in message.tool_calls or ()
            if c.function is not None
        ]
        text = "" if message.role == "tool" else content_as_text(message.content)
        row = [message.role, message.tool_call_id or "", text, calls]
        digest.update(json.dumps(row, sort_keys=True).encode("utf-8"))
    return digest.hexdigest()


def usable_summary(text: str, *, custom_prompt: bool) -> str | None:
    """The worker's answer as a summary body, or None if it is not one.

    Thinking a model left inline is dropped. The built-in prompt's answer
    must carry its four headings, in order; a custom prompt's must only
    be non-empty, since the user chose its shape.
    """
    body = _THINK.sub("", text).strip()
    if not body:
        return None
    if custom_prompt:
        return body
    at = -1
    for heading in SUMMARY_HEADINGS:
        found = body.find(heading, at + 1)
        if found < 0:
            return None
        at = found
    return body[body.find(SUMMARY_HEADINGS[0]) :]


@dataclass
class _Ahead:
    key: str
    task: asyncio.Task[str | None]


class CompactionSummarizer:
    """Writes compaction summaries on the worker tier, and ahead of need.

    One per agent loop. *complete* is the worker call (cost is recorded by
    the caller); *access* is what the files read again may touch.
    """

    def __init__(
        self,
        settings: CompactionConfig,
        worker: TierConfig,
        complete: Complete,
        access: Callable[[], FileAccess],
    ) -> None:
        self.settings = settings
        self.worker = worker
        self._complete = complete
        self._access = access
        self._ahead: _Ahead | None = None

    @property
    def enabled(self) -> bool:
        return self.settings.summary == "model" and bool(self.worker.slug)

    def close(self) -> None:
        """Stop a summary being written ahead; the loop has ended."""
        if self._ahead is not None:
            self._ahead.task.cancel()
            self._ahead = None

    async def _write(self, dropped: Sequence[ChatMessage], guidance: str) -> str | None:
        settings = self.settings
        room = self.worker.context_window - settings.summary_max_tokens - _PROMPT_RESERVE_TOKENS
        payload = transcript(dropped, max(2_000, room * _CHARS_PER_TOKEN))
        if guidance:
            payload = f"The user asked you to keep this in view: {guidance}\n\n{payload}"
        prompt = settings.summary_prompt.strip()
        request = ChatCompletionRequest(
            model=self.worker.require_slug(),
            messages=[
                ChatMessage(role="system", content=prompt or DEFAULT_SUMMARY_PROMPT),
                ChatMessage(role="user", content=f"Conversation to summarize:\n{payload}"),
            ],
            tools=None,
            max_tokens=settings.summary_max_tokens,
            temperature=0.0,
            extra_body=self.worker.extra_body,
            retry_on_timeout=False,
        )
        try:
            response = await asyncio.wait_for(
                self._complete(request), settings.summary_timeout_seconds
            )
        except TimeoutError:
            return _fallback("timed out")
        except Exception as e:
            # The worker's client can fail before it sends (a keychain
            # error, say); the summary falls back rather than the turn.
            return _fallback(f"{type(e).__name__}: {e}")
        if isinstance(response, ProviderError):
            return _fallback(response.message)
        if response.finish_reason == "length":
            return _fallback("cut off at summary_max_tokens")
        body = usable_summary(content_as_text(response.message.content), custom_prompt=bool(prompt))
        return body if body is not None else _fallback("answer was not a summary")

    async def summarize(self, dropped: Sequence[ChatMessage], guidance: str = "") -> str | None:
        """The worker's summary of *dropped*, or None for the extractive one.

        A summary written ahead of exactly these turns is used as it is,
        or waited for if it is still being written.
        """
        if not self.enabled or not dropped:
            return None
        ahead = self._ahead
        if ahead is not None and ahead.key == _fingerprint(dropped, guidance):
            self._ahead = None
            return await ahead.task
        return await self._write(dropped, guidance)

    def prefetch(
        self,
        messages: list[ChatMessage],
        tier: TierConfig,
        counter: TokenCounter,
        tools: Sequence[ToolDefinition] | None,
    ) -> bool:
        """Start writing the next compaction's summary if it is near.

        Near means the prompt is over ``background_percent`` of the
        compaction budget. The turns are those before the last user turn:
        what the next turn's compaction, or ``/compact``, folds. Returns
        True when a summary was started.
        """
        percent = self.settings.background_percent
        if not self.enabled or percent <= 0:
            return False
        used = estimate_tokens(messages, counter, tools)[0]
        if used * 100 < budget_threshold(tier) * percent:
            return False
        cut = find_compaction_point(messages, keep_user_turns=1)
        if cut <= 1:
            return False
        dropped = list(messages[1:cut])
        key = _fingerprint(dropped, "")
        if self._ahead is not None:
            if self._ahead.key == key:
                return False
            self._ahead.task.cancel()
        self._ahead = _Ahead(key, asyncio.create_task(self._write(dropped, "")))
        return True

    async def files(
        self,
        dropped: Sequence[ChatMessage],
        kept: Sequence[ChatMessage],
        counter: TokenCounter,
        room: int,
    ) -> str:
        """The files read again under the summary, within *room* tokens."""
        settings = self.settings
        budget = min(settings.reinject_tokens, room)
        if settings.reinject_files <= 0 or budget <= 0:
            return ""
        paths = recent_files(dropped, kept)
        if not paths:
            return ""
        return await read_recent_files(
            paths,
            self._access(),
            counter,
            max_files=settings.reinject_files,
            max_tokens=budget,
        )


def _fallback(reason: str) -> str | None:
    log.warning(
        "compaction summary fell back to extractive",
        extra={"extra_fields": {"reason": reason}},
    )
    return None


def _room_for_files(tier: TierConfig | None, without: CompactionStats) -> int:
    """Tokens the files may take: half the headroom left under the budget.

    Half, so the files never bring the next request back to the budget
    the compaction just got it under.
    """
    if tier is None:
        return 0
    return max(0, budget_threshold(tier) - without.tokens_after) // 2


async def compact_auto(
    messages: list[ChatMessage],
    tier: TierConfig,
    counter: TokenCounter,
    tools: Sequence[ToolDefinition] | None,
    *,
    carry: str,
    summarizer: CompactionSummarizer | None,
) -> tuple[list[ChatMessage], CompactionStats | None]:
    """:func:`maybe_compact`, with the worker's summary and the files."""
    span = auto_span(messages, tier, counter, tools)
    if summarizer is None or span is None:
        return maybe_compact(messages, tier, counter, tools, carry=carry)
    start, end = span
    body = await summarizer.summarize(messages[start:end])
    plain = maybe_compact(messages, tier, counter, tools, carry=carry, body=body)
    if plain[1] is None:
        return plain
    files = await summarizer.files(
        messages[start:end], messages[end:], counter, _room_for_files(tier, plain[1])
    )
    if not files:
        return plain
    return maybe_compact(messages, tier, counter, tools, carry=carry, body=body, files=files)


async def compact_manual(
    messages: list[ChatMessage],
    tier: TierConfig | None,
    counter: TokenCounter,
    tools: Sequence[ToolDefinition] | None,
    *,
    guidance: str,
    from_position: int | None,
    carry: str,
    summarizer: CompactionSummarizer | None,
) -> tuple[list[ChatMessage], CompactionStats] | None:
    """:func:`compact_on_request`, with the worker's summary and the files."""
    span = manual_span(messages, from_position)
    if summarizer is None or span is None:
        return compact_on_request(
            messages, counter, tools, guidance=guidance, from_position=from_position, carry=carry
        )
    start, end = span
    guidance = guidance.strip()[:MAX_GUIDANCE_CHARS]
    dropped, kept = messages[start:end], messages[end:]
    body = await summarizer.summarize(dropped, guidance)

    def run(files: str = "") -> tuple[list[ChatMessage], CompactionStats] | None:
        return compact_on_request(
            messages,
            counter,
            tools,
            guidance=guidance,
            from_position=from_position,
            carry=carry,
            body=body,
            files=files,
        )

    plain = run()
    if plain is None:
        return None
    files = await summarizer.files(dropped, kept, counter, _room_for_files(tier, plain[1]))
    return run(files) if files else plain
