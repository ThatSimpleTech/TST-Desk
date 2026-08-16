"""Live-provider leg of the headless harness (TD-1803).

TD-1401 proves the agent loop against a scripted ``MockProvider`` —
deterministic, offline, and still the default.  This module adds the other
half: the same harness pass driven by a real OpenAI-compatible endpoint, so
the M1.5 promise (a local model actually completing a task) is demonstrated
rather than assumed.

Three things separate a live pass from the mock pass, and all three live
here rather than leaking into TD-1401's path:

* **The approval gate really fires.**  The live workspace declares an
  ``fs_write → ask`` policy rule, so the write parks on an
  ``approval_request``.  The harness approves on *that* event, never on
  ``tool_call``: TD-802 registers the pending approval when it emits the
  request, so approving on the earlier event answers a gate that has not
  opened yet.
* **Failures are attributable.**  :class:`LiveProvider` records every
  ``ProviderError`` the endpoint produces, so a broken shim or an
  unreachable model raises :class:`ProviderContractError` instead of
  surfacing as an agent-loop assertion failure.
* **Nothing is asserted about spend.**  A local preset prices every tier at
  zero, so the live pass requires real token counts in the ledger and an
  honest cost of zero — the mock pass keeps its ``cost > 0`` assertion.

The endpoint is loopback-only by design.  TD-1801 made a loopback tier
keyless, so a live pass needs no credential and can never bill anyone; a
remote ``--live-endpoint`` is refused in :func:`live_preflight` rather than
quietly spending the user's money from a test harness.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx

from .config import cached_config, is_loopback_url
from .e2e_plan import HarnessPlan
from .policy import PolicyConfig, PolicyRule
from .provider import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    ProviderClient,
    ProviderError,
    RetryConfig,
    StreamChunk,
    TimeoutConfig,
)

# The greeting the live task asks for.  Checked case-insensitively as a
# substring: a real model chooses its own newlines and capitalisation, and
# pinning those would fail the harness for something the story never
# promised.  The mock pass keeps its exact-match assertion.
_LIVE_MARKER = "hello"
_LIVE_FILE = "hello.txt"

_LIVE_STEERING = (
    "# Harness workspace\n"
    "\n"
    "Always pass absolute paths to file tools. A relative path resolves\n"
    "against the daemon's working directory, lands outside the workspace,\n"
    "and is refused at the boundary.\n"
    "\n"
    "Keep the greeting in hello.txt short.\n"
)

# A local model at single-digit tokens per second needs room that the mock
# pass never does: ~70s for a turn, plus ~18s if the server has to load the
# model first, times the two model calls a tool round-trip costs.
_LIVE_READ_TIMEOUT = 600.0
_LIVE_TURN_TIMEOUT = 900.0
_LIVE_BUDGET = 900.0

# One retry, not three: a retry after a ten-minute read timeout costs
# another ten minutes and tells us nothing the first attempt did not.
_LIVE_RETRIES = 1

_PROBE_TIMEOUT = 10.0


class ProviderContractError(Exception):
    """The endpoint broke the OpenAI-compatible contract.

    Raised instead of an assertion so a live failure says *which side*
    broke: this exception means the provider shim, the server, or the
    network failed, and none of the harness's agent-loop checks were ever
    given a fair chance to run.  An ``AssertionError`` from the same test
    means the opposite — the provider held up its end and the loop did not.
    """


class LiveProvider:
    """A recording ``ProviderLike`` over a real endpoint.

    Records every request (``calls``, mirroring ``MockProvider`` so the
    harness's steering check works unchanged) and every ``ProviderError``
    seen on the way back (``errors``).  Recording rather than raising keeps
    the loop's own error handling on the normal path — the loop still turns
    a provider failure into a failed turn — while giving the caller the
    evidence to attribute that failure.
    """

    def __init__(self, endpoint: str) -> None:
        self._client = ProviderClient(
            base_url=endpoint,
            api_key=None,  # loopback endpoints take no credential (TD-1801)
            timeout=TimeoutConfig(read=_LIVE_READ_TIMEOUT, total=_LIVE_BUDGET),
            retry_config=RetryConfig(max_retries=_LIVE_RETRIES),
        )
        self.calls: list[ChatCompletionRequest] = []
        self.errors: list[str] = []

    def _record(self, error: ProviderError) -> None:
        self.errors.append(f"{error.code}: {error.message}")

    async def chat_completion(
        self, request: ChatCompletionRequest
    ) -> ChatCompletionResponse | ProviderError:
        self.calls.append(request)
        response = await self._client.chat_completion(request)
        if isinstance(response, ProviderError):
            self._record(response)
        return response

    async def chat_completion_stream(
        self, request: ChatCompletionRequest
    ) -> AsyncIterator[StreamChunk | ProviderError]:
        self.calls.append(request)
        async for chunk in self._client.chat_completion_stream(request):
            if isinstance(chunk, ProviderError):
                self._record(chunk)
            yield chunk

    async def aclose(self) -> None:
        await self._client.close()


def _live_prompt(target: Path) -> str:
    """The one scripted task, stated so a small model cannot mis-aim it.

    The absolute path is spelled out because a relative one classifies C
    (``path-outside-workspace``) and is then refused by the boundary guard —
    a failure that looks like the loop breaking when it is really the model
    answering a badly-posed question.
    """
    return (
        f"Use the fs_write tool once to create the file {target}. "
        f"Its content must be the single line: {_LIVE_MARKER} from a local model\n"
        "Pass the path exactly as written above — an absolute path. "
        "Do not ask any questions first, and do not call any other tool."
    )


def _content_ok(text: str) -> bool:
    return _LIVE_MARKER in text.lower()


def live_plan(workspace: Path, provider: LiveProvider) -> HarnessPlan:
    """The harness plan for a live pass against *provider*.

    The ``fs_write → ask`` rule is the point of the exercise.  Without it an
    in-workspace absolute write classifies A, policy resolves ``auto``, and
    no ``approval_request`` is ever emitted — the gate would go untested
    while the pass still went green.
    """
    return HarnessPlan(
        provider=provider,
        steering=_LIVE_STEERING,
        prompt=_live_prompt(workspace / _LIVE_FILE),
        approve_on="approval_request",
        content_ok=_content_ok,
        expect_spend=False,
        turn_timeout=_LIVE_TURN_TIMEOUT,
        budget_secs=_LIVE_BUDGET,
        policy=PolicyConfig(rules=[PolicyRule(tool="fs_write", args="**", effect="ask")]),
    )


def off_box_refusal(endpoint: str) -> str | None:
    """Why *endpoint* may not be contacted at all, or ``None`` if it may.

    Split out of :func:`live_preflight` so it can be asked *before* anything
    is sent.  Model discovery (TD-1805) also has to reach the endpoint, and
    it ran ahead of this check — so pointing ``--live-endpoint`` at a remote
    host sent it a request and only then refused the run.  A refusal that
    fires after the packet has left is not a refusal.
    """
    if not is_loopback_url(endpoint):
        return (
            f"{endpoint} is not a loopback endpoint; the live harness targets "
            "an on-box model server so a test run can never spend money"
        )
    return None


async def live_preflight(endpoint: str, model: str) -> str | None:
    """Why a live pass cannot run against *endpoint*, or ``None`` if it can.

    Every reason is a "not run", not a failure: an absent local model server
    is a fact about the machine, and reporting it as a broken agent loop
    would be a lie.  The caller decides how to say so — the test skips, the
    CLI exits without pretending it passed.
    """
    off_box = off_box_refusal(endpoint)
    if off_box is not None:
        return off_box

    wire_model = cached_config().tier("brain").slug
    if wire_model is not None and wire_model != model:
        # The daemon takes the slug from config, not from the harness, so a
        # mismatch means we would probe for one model and request another.
        # An unset slug is not a mismatch: the daemon will discover the same
        # model from the same endpoint the caller just resolved (TD-1805).
        return (
            f"the active preset's brain tier requests {wire_model!r}, not {model!r} — "
            "switch the active preset or pass the matching --live-model"
        )

    try:
        async with httpx.AsyncClient(timeout=_PROBE_TIMEOUT) as client:
            response = await client.get(f"{endpoint.rstrip('/')}/models")
    except httpx.HTTPError as e:
        return f"no OpenAI-compatible endpoint reachable at {endpoint}: {e}"

    if response.status_code != 200:
        return f"{endpoint}/models returned HTTP {response.status_code}"

    try:
        served = {str(entry.get("id", "")) for entry in response.json().get("data", [])}
    except (ValueError, AttributeError, TypeError) as e:
        return f"{endpoint}/models did not return an OpenAI model list: {e}"

    if model not in served:
        return (
            f"{endpoint} does not serve {model!r} (serves: {', '.join(sorted(served)) or 'none'})"
        )
    return None


def raise_on_contract_failure(provider: LiveProvider) -> None:
    """Raise :class:`ProviderContractError` if the endpoint misbehaved.

    Call this *before* asserting on the harness's checks.  Ordering is the
    whole point: a provider failure makes every downstream check fail too,
    and reporting those first would blame the loop for the endpoint.
    """
    if provider.errors:
        raise ProviderContractError(
            "the endpoint broke the OpenAI-compatible contract, so the agent "
            "loop was never fairly exercised: " + "; ".join(provider.errors)
        )
