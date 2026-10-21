"""The after-write review prompt fits the validator's window (TD-5153).

The interactive review (TD-4204) sent every diff and every shell output
of the turn. A local validator processes the whole prompt before the
first streamed chunk, so a prompt longer than the slot reads within the
tier's ``read_timeout`` times out (and TD-5146 does not resend it), and
one longer than the slot fails outright.

So the prompt gets a token budget: ``autonomy.verify_max_prompt_tokens``,
or ``DEFAULT_PROMPT_SHARE`` of the validator tier's ``context_window``
when that is null, and never more than the window less the tier's
``max_output_tokens``. The whole request is estimated as the turn's own
requests are (``estimate_tokens``), with a counter scaled by the ratio
learned for the validator's endpoint (TD-5007). The review's own usage
teaches that ratio too, so a validator on an endpoint the turn never
calls is measured after its first review.

When the turn's material is over the budget, it is cut in this order:

1. Shell output loses its head. Each output keeps its tail, where a test
   run prints its result, down to a floor: ``TAIL_FLOOR_CHARS`` each,
   and the floors together at most ``TAIL_FLOOR_SHARE`` of the room.
2. The oldest diffs go whole. The newest diff stays.
3. Shell output goes below its floor, down to its markers.
4. Shell output goes altogether, then the newest diff loses its end.

Each cut leaves a marker where it was made (``(N earlier lines
omitted)``, ``(N earlier diffs omitted)``), the review is told that
parts were left out, and the verdict's summary says what was. A prompt
that is still over the budget with everything cut, or that has no room
left for any of the diff, is not sent.
"""

from __future__ import annotations

import math
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

from ..compaction import estimate_tokens
from ..config import TierConfig
from ..context.calibration import (
    CalibratedTokenCounter,
    CalibrationStore,
    calibration_key,
    effective_chars_per_token,
)
from ..context.tokens import TokenCounter, make_token_counter
from ..logging import get_logger
from ..provider import ChatMessage, Usage

log = get_logger(__name__)

#: The budget when ``autonomy.verify_max_prompt_tokens`` is null. On
#: a local GPU server's 65,536-token slots that is about 39k tokens, which the slowest
#: measured prompt rate (~390 tokens/s) reads in about 100 s, inside the
#: tier's 120 s read timeout.
DEFAULT_PROMPT_SHARE = 0.6
#: Characters of each shell output's tail kept before any diff is dropped.
TAIL_FLOOR_CHARS = 2_000
#: The floors together never take more than this share of the room, so
#: many shell calls cannot crowd out the diffs.
TAIL_FLOOR_SHARE = 0.25
#: Room each marker line is given, which every marker fits in.
MARKER_CHARS = 64
#: Re-measures after a cut. Each round cuts the estimate's excess plus
#: ``FIT_SLACK_TOKENS``, so the second round almost always fits.
MAX_FIT_ROUNDS = 5
FIT_SLACK_TOKENS = 64

NO_DIFF = "(no diff)"
NO_TEST_OUTPUT = "(no test output)"
_SEP = "\n\n"

REVIEW_USER = (
    "Review the workspace diff and any test output. "
    "Reply with a first line of `verdict: pass` or `verdict: fail`, "
    "then a short summary. Do not propose a revert."
)
#: Appended to :data:`REVIEW_USER` when anything was cut.
TRIMMED_USER = (
    " Parts of the turn's diffs or shell output were left out to fit "
    "your context; the `omitted` markers say where. Judge what is shown, "
    "and do not fail the review for what was left out."
)


def _plural(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


@dataclass(frozen=True)
class ReviewMaterial:
    """The diff and test-output blocks the review sends, and what was cut."""

    diff: str
    test_output: str
    #: Oldest diffs left out whole.
    omitted_diffs: int = 0
    #: Lines at the end of the newest diff left out.
    cut_diff_lines: int = 0
    #: Shell-output lines left out, from heads and from whole outputs.
    omitted_lines: int = 0
    #: The turn had a diff and none of it is kept.
    diff_lost: bool = False

    @property
    def size(self) -> int:
        return len(self.diff) + len(self.test_output)

    @property
    def trimmed(self) -> bool:
        return bool(self.omitted_diffs or self.cut_diff_lines or self.omitted_lines)

    def note(self) -> str:
        """One line for the verdict's summary; empty when nothing was cut."""
        parts: list[str] = []
        if self.omitted_diffs:
            parts.append(_plural(self.omitted_diffs, "earlier diff"))
        if self.cut_diff_lines:
            parts.append(f"the last {_plural(self.cut_diff_lines, 'line')} of the newest diff")
        if self.omitted_lines:
            parts.append(f"{_plural(self.omitted_lines, 'line')} of shell output")
        if not parts:
            return ""
        return "Left out to fit the validator's window: " + ", ".join(parts) + "."


def review_budget(tier: TierConfig, cap: int | None) -> int:
    """Estimated prompt tokens the review may send to *tier*.

    *cap* is ``autonomy.verify_max_prompt_tokens``; null is
    ``DEFAULT_PROMPT_SHARE`` of the window. The window must also hold
    the reply, so the budget never passes the window less
    ``max_output_tokens``.
    """
    target = cap if cap is not None else int(tier.context_window * DEFAULT_PROMPT_SHARE)
    return max(0, min(target, tier.context_window - tier.max_output_tokens))


def review_counter(store: CalibrationStore, tier: TierConfig) -> TokenCounter:
    """The counter for prompts sent to *tier*, scaled by its learned ratio.

    *tier* is the effective tier (credential host applied), keyed exactly
    as the turn's counter keys it. A tier whose slug is not resolved yet
    has no key, so its counter is the uncalibrated heuristic.
    """
    if tier.slug is None:
        return make_token_counter(None)
    return CalibratedTokenCounter(make_token_counter(tier.slug), store, calibration_key(tier))


def observe_review_usage(
    counter: TokenCounter, messages: list[ChatMessage], usage: Usage | None
) -> None:
    """Teach the validator's endpoint ratio from this review's usage.

    The raw estimate comes from the base counter over exactly what was
    sent; a calibrated one would compound the ratio.
    """
    if usage is None or not isinstance(counter, CalibratedTokenCounter):
        return
    raw_estimate, _ = estimate_tokens(messages, counter.base)
    counter.observe(raw_estimate, usage.prompt_tokens)


def review_messages(system: str, material: ReviewMaterial) -> list[ChatMessage]:
    user = REVIEW_USER + (TRIMMED_USER if material.trimmed else "")
    return [
        ChatMessage(role="system", content=system),
        ChatMessage(role="user", content=user),
    ]


# ── Cutting the material to a character room ─────────────────────────


def _keep_tail(text: str, keep: int) -> tuple[str, int]:
    """The end of *text* in at most *keep* characters, from a line start
    where one fits, and how many lines are not shown whole."""
    if len(text) <= keep:
        return text, 0
    total = len(text.splitlines())
    if keep <= 0:
        return "", total
    start = len(text) - keep
    if text[start - 1] != "\n":
        newline = text.find("\n", start)
        if newline == -1 or newline + 1 >= len(text):
            # One long last line: keep its end, marked as partial.
            return "…" + text[start + 1 :], total
        start = newline + 1
    tail = text[start:]
    return tail, total - len(tail.splitlines())


def _keep_head(text: str, keep: int) -> tuple[str, int]:
    """The start of *text* in at most *keep* characters, to a line end
    where one fits, and how many lines are not shown whole."""
    if len(text) <= keep:
        return text, 0
    total = len(text.splitlines())
    if keep <= 1:
        return "", total
    newline = text.rfind("\n", 0, keep)
    if newline < 1:
        # One long first line: keep its start, marked as partial.
        return text[: keep - 1] + "…", total
    head = text[:newline]
    return head, total - len(head.splitlines())


def _outputs_cost(outputs: Sequence[str], cap: int) -> int:
    """Characters the outputs take with each tail capped at *cap*.

    An output that fits in its cap plus a marker is kept whole: cutting it
    would save nothing.
    """
    return sum(min(len(o), cap + MARKER_CHARS) + len(_SEP) for o in outputs)


def _largest_cap(outputs: Sequence[str], room: int, low: int) -> int:
    """The largest tail cap from *low* whose outputs fit *room*."""
    high = max((len(o) for o in outputs), default=low)
    if high <= low:
        return low
    while low < high:
        mid = (low + high + 1) // 2
        if _outputs_cost(outputs, mid) <= room:
            low = mid
        else:
            high = mid - 1
    return low


def _render_outputs(outputs: Sequence[str], cap: int) -> tuple[str, int]:
    rendered: list[str] = []
    omitted = 0
    for output in outputs:
        if len(output) <= cap + MARKER_CHARS:
            rendered.append(output)
            continue
        tail, lines = _keep_tail(output, cap)
        omitted += lines
        if tail:
            rendered.append(f"({_plural(lines, 'earlier line')} omitted)\n{tail}")
        else:
            rendered.append(f"({_plural(lines, 'line')} omitted)")
    return _SEP.join(rendered), omitted


def _render_diffs(diffs: Sequence[str], keep: int) -> str:
    """The newest *keep* diffs, with a marker for the older ones."""
    dropped = len(diffs) - keep
    text = _SEP.join(diffs[dropped:])
    if dropped:
        return f"({_plural(dropped, 'earlier diff')} omitted){_SEP}{text}"
    return text


def _with_outputs(
    diffs: Sequence[str], keep: int, outputs: Sequence[str], cap: int
) -> ReviewMaterial:
    test_output, omitted_lines = _render_outputs(outputs, cap)
    return ReviewMaterial(
        diff=_render_diffs(diffs, keep),
        test_output=test_output,
        omitted_diffs=len(diffs) - keep,
        omitted_lines=omitted_lines,
    )


def _diffs_cost(diffs: Sequence[str], keep: int) -> int:
    marker = MARKER_CHARS if keep < len(diffs) else 0
    return sum(len(d) + len(_SEP) for d in diffs[len(diffs) - keep :]) + marker


def fit_material(diffs: Sequence[str], outputs: Sequence[str], room: int | None) -> ReviewMaterial:
    """The turn's diffs and shell outputs cut to *room* characters.

    ``None`` is no limit. The order of cuts is the module docstring's.
    """
    diffs = [d for d in diffs if d]
    outputs = [o for o in outputs if o]
    whole = ReviewMaterial(diff=_SEP.join(diffs), test_output=_SEP.join(outputs))
    if room is None or whole.size <= room:
        return whole
    room = max(0, room)
    every = len(diffs)

    # 1. Shell output keeps its tails; every diff stays.
    floor = min(TAIL_FLOOR_CHARS, int(room * TAIL_FLOOR_SHARE) // max(1, len(outputs)))
    if _outputs_cost(outputs, floor) <= room - _diffs_cost(diffs, every):
        cap = _largest_cap(outputs, room - _diffs_cost(diffs, every), floor)
        return _with_outputs(diffs, every, outputs, cap)

    # 2. The oldest diffs go; the newest stays.
    keep = every
    while keep > 1 and _diffs_cost(diffs, keep) > room - _outputs_cost(outputs, floor):
        keep -= 1
    if _diffs_cost(diffs, keep) <= room - _outputs_cost(outputs, floor):
        cap = _largest_cap(outputs, room - _diffs_cost(diffs, keep), floor)
        return _with_outputs(diffs, keep, outputs, cap)

    # 3. Shell output goes below its floor, down to its markers.
    if _outputs_cost(outputs, 0) <= room - _diffs_cost(diffs, keep):
        cap = _largest_cap(outputs, room - _diffs_cost(diffs, keep), 0)
        return _with_outputs(diffs, keep, outputs, cap)

    # 4. Shell output goes altogether; the newest diff loses its end.
    test_output = f"({_plural(len(outputs), 'shell output')} omitted)" if outputs else ""
    output_lines = sum(len(o.splitlines()) for o in outputs)
    if not diffs:
        return ReviewMaterial(diff="", test_output=test_output, omitted_lines=output_lines)
    dropped = every - 1
    head_room = room - len(test_output) - (MARKER_CHARS if dropped else 0) - MARKER_CHARS
    head, cut = _keep_head(diffs[-1], head_room)
    if head and cut:
        head += f"\n({_plural(cut, 'later line')} of this diff omitted)"
    diff = (f"({_plural(dropped, 'earlier diff')} omitted){_SEP}" if dropped else "") + head
    return ReviewMaterial(
        diff=diff,
        test_output=test_output,
        omitted_diffs=dropped,
        cut_diff_lines=cut,
        omitted_lines=output_lines,
        diff_lost=not head,
    )


# ── Fitting the whole prompt to the token budget ──────────────────────


@dataclass(frozen=True)
class ReviewPrompt:
    """The review request's messages and how they measured."""

    messages: list[ChatMessage]
    material: ReviewMaterial
    tokens: int
    budget: int

    @property
    def sendable(self) -> bool:
        return self.tokens <= self.budget and not self.material.diff_lost

    def refusal(self) -> str:
        """Why the prompt is not sent, for the verdict's summary."""
        if self.material.diff_lost:
            return (
                "Not reviewed: the validator's window has no room for this turn's diff "
                f"(budget {self.budget:,} tokens)."
            )
        return (
            f"Not reviewed: the review prompt is about {self.tokens:,} tokens with the "
            f"turn's diffs and output cut, over the validator's budget of {self.budget:,}."
        )


AssembleReview = Callable[[str, str], Awaitable[str]]
"""``(diff, test_output) -> system prompt`` for the validator tier."""


async def fit_review_prompt(
    assemble: AssembleReview,
    diffs: Sequence[str],
    outputs: Sequence[str],
    counter: TokenCounter,
    budget: int,
) -> ReviewPrompt:
    """Assemble the review and cut its material until it fits *budget*.

    The estimate covers the whole request, so the steering subset and
    the instructions are paid for before the material. A cut aims at the
    estimate's excess, converted to characters at the counter's ratio.
    """
    material = fit_material(diffs, outputs, None)
    prompt = await _measure(assemble, material, counter, budget)
    for _ in range(MAX_FIT_ROUNDS - 1):
        if prompt.tokens <= budget or material.size == 0:
            break
        excess = prompt.tokens - budget + FIT_SLACK_TOKENS
        room = material.size - math.ceil(excess * effective_chars_per_token(counter))
        smaller = fit_material(diffs, outputs, max(0, room))
        if smaller.size >= material.size:
            break  # Everything that can go has gone.
        material = smaller
        prompt = await _measure(assemble, material, counter, budget)
    return prompt


async def _measure(
    assemble: AssembleReview, material: ReviewMaterial, counter: TokenCounter, budget: int
) -> ReviewPrompt:
    system = await assemble(material.diff or NO_DIFF, material.test_output or NO_TEST_OUTPUT)
    messages = review_messages(system, material)
    tokens, _ = estimate_tokens(messages, counter)
    return ReviewPrompt(messages=messages, material=material, tokens=tokens, budget=budget)


def log_review_fit(session_id: str, prompt: ReviewPrompt) -> None:
    """One log line when the review was cut or is not sent."""
    material = prompt.material
    if not material.trimmed and prompt.sendable:
        return
    log.info(
        "verify prompt cut to the validator budget",
        extra={
            "extra_fields": {
                "session_id": session_id,
                "tokens": prompt.tokens,
                "budget": prompt.budget,
                "omitted_diffs": material.omitted_diffs,
                "cut_diff_lines": material.cut_diff_lines,
                "omitted_output_lines": material.omitted_lines,
                "sent": prompt.sendable,
            }
        },
    )
