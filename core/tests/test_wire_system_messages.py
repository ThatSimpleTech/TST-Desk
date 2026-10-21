"""TD-5142: a compacted session still goes over the wire with one system message.

Compaction stores its summary as a second system message. Qwen's chat
template on gateway-model (llama.cpp) raises "System message must be at the
beginning." for that, the server answers 500, and every later request in
the session failed the same way after eight retries. The wire now merges
or folds stray system messages, and a template refusal is never retried.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx

from tests.test_compaction import pad, run_session, tiny_config
from tests.test_context_fit import _forge_config, _run
from tests.test_loop import wait_for_turn
from tstd.compaction import compact_on_request
from tstd.context.tokens import HeuristicTokenCounter
from tstd.context_fit import transcript_failure_text
from tstd.mock import MockProvider, Script
from tstd.protocol import ContextCompacted
from tstd.provider import (
    ChatCompletionRequest,
    ChatMessage,
    FunctionCall,
    ProviderClient,
    ProviderError,
    RetryConfig,
    ToolCall,
    template_error_reason,
    wire_messages,
)
from tstd.scheduler.retry import is_transient_failure
from tstd.session import Session

COUNTER = HeuristicTokenCounter()

# What LiteLLM relayed from llama.cpp on 2026-10-14 (gateway-model, Qwen3.8).
LITELLM_TEMPLATE_500 = {
    "error": {
        "message": (
            "litellm.InternalServerError: InternalServerError: Hosted_vllmException - "
            '{"error":{"code":500,"message":"\\n------------\\nWhile executing CallExpression '
            "at line 106, column 32 in source:\\n...first %}↵            {{- raise_exception("
            "'System message must be at the beginnin...\\n"
            "                                           ^\\nError: Jinja Exception: System "
            'message must be at the beginning.","type":"server_error"}}. '
            "Received Model Group=gateway-model"
        ),
        "type": None,
        "param": None,
        "code": "500",
    }
}


def strict_template(messages: list[dict[str, Any]]) -> None:
    """The two checks Qwen's template makes before rendering anything.

    Raises ValueError with the template's own words, which is what
    llama.cpp turns into a 500.
    """
    for i, message in enumerate(messages):
        if message["role"] == "system" and i != 0:
            raise ValueError("System message must be at the beginning.")
    for message in reversed(messages):
        content = message.get("content")
        text = content if isinstance(content, str) else json.dumps(content)
        if message["role"] == "user" and not (
            text.startswith("<tool_response>") and text.endswith("</tool_response>")
        ):
            return
    raise ValueError("No user query found in messages.")


def body_messages(messages: list[ChatMessage]) -> list[dict[str, Any]]:
    body: list[dict[str, Any]] = ChatCompletionRequest(model="m", messages=messages).to_dict()[
        "messages"
    ]
    return body


def _turns(n: int) -> list[ChatMessage]:
    messages = [ChatMessage(role="system", content="PROMPT")]
    for i in range(1, n + 1):
        messages.append(ChatMessage(role="user", content=f"question {i}"))
        messages.append(ChatMessage(role="assistant", content=f"answer {i}"))
    return messages


# ── The wire shape ───────────────────────────────────────────────────


def test_conversation_without_a_stray_system_message_is_sent_as_is() -> None:
    messages = _turns(2)
    assert wire_messages(messages) == messages
    assert body_messages(messages) == [ChatCompletionRequest._message_to_dict(m) for m in messages]


def test_summary_after_the_prompt_merges_into_one_system_message() -> None:
    messages = [
        ChatMessage(role="system", content="PROMPT"),
        ChatMessage(role="system", content="[Earlier conversation compacted]\nUser: hi"),
        ChatMessage(role="user", content="next"),
    ]
    sent = body_messages(messages)
    assert [m["role"] for m in sent] == ["system", "user"]
    assert sent[0]["content"] == "PROMPT\n\n[Earlier conversation compacted]\nUser: hi"
    strict_template(sent)


def test_wire_leaves_the_stored_conversation_alone() -> None:
    """Persisted sessions keep the summary where compaction put it, so
    the fix needs no migration and a fork still sees the summary."""
    messages = [
        ChatMessage(role="system", content="PROMPT"),
        ChatMessage(role="system", content="SUMMARY"),
        ChatMessage(role="user", content="next"),
    ]
    request = ChatCompletionRequest(model="m", messages=messages)
    request.to_dict()
    assert [m.role for m in request.messages] == ["system", "system", "user"]
    assert request.messages[1].content == "SUMMARY"


def test_mid_conversation_summary_goes_ahead_of_the_next_user_message() -> None:
    """A summary of later turns (what a /compact "from here" leaves, TD-5109)
    stays between the turns kept verbatim and the new message."""
    compacted = [
        *_turns(1),
        ChatMessage(role="system", content="[Earlier conversation compacted]\nUser: question 2"),
        ChatMessage(role="user", content="carry on"),
    ]

    sent = body_messages(compacted)
    strict_template(sent)
    assert [m["role"] for m in sent] == ["system", "user", "assistant", "user"]
    assert sent[0]["content"] == "PROMPT"
    assert sent[-1]["content"].startswith("[Earlier conversation compacted]")
    assert sent[-1]["content"].endswith("\n\ncarry on")


def test_folded_summary_keeps_image_parts() -> None:
    image = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}
    messages = [
        ChatMessage(role="system", content="PROMPT"),
        ChatMessage(role="user", content="q1"),
        ChatMessage(role="assistant", content="a1"),
        ChatMessage(role="system", content="SUMMARY"),
        ChatMessage(role="user", content=[{"type": "text", "text": "look"}, image]),
    ]
    sent = body_messages(messages)
    strict_template(sent)
    assert sent[-1]["content"] == [
        {"type": "text", "text": "SUMMARY"},
        {"type": "text", "text": "look"},
        image,
    ]


def test_stray_system_before_a_tool_result_never_splits_the_call() -> None:
    """A user message between a tool call and its result breaks the
    OpenAI contract, so anything not followed by a user message joins
    the first system message instead."""
    call = ToolCall(id="c1", function=FunctionCall(name="fs_read", arguments="{}"))
    messages = [
        ChatMessage(role="system", content="PROMPT"),
        ChatMessage(role="user", content="read it"),
        ChatMessage(role="assistant", content=None, tool_calls=[call]),
        ChatMessage(role="system", content="NOTE"),
        ChatMessage(role="tool", content="file text", tool_call_id="c1"),
        ChatMessage(role="system", content="TAIL"),
    ]
    sent = body_messages(messages)
    strict_template(sent)
    assert [m["role"] for m in sent] == ["system", "user", "assistant", "tool"]
    assert sent[0]["content"] == "PROMPT\n\nNOTE\n\nTAIL"


def test_no_leading_system_message_and_a_later_one() -> None:
    messages = [
        ChatMessage(role="user", content="q1"),
        ChatMessage(role="assistant", content="a1"),
        ChatMessage(role="system", content="SUMMARY"),
        ChatMessage(role="user", content="q2"),
    ]
    sent = body_messages(messages)
    strict_template(sent)
    assert [m["role"] for m in sent] == ["user", "assistant", "user"]
    assert sent[-1]["content"] == "SUMMARY\n\nq2"


def test_empty_stray_system_message_adds_no_blank_lines() -> None:
    messages = [
        ChatMessage(role="system", content="PROMPT"),
        ChatMessage(role="system", content=""),
        ChatMessage(role="user", content="q"),
    ]
    sent = body_messages(messages)
    assert sent == [{"role": "system", "content": "PROMPT"}, {"role": "user", "content": "q"}]


def test_echoed_reasoning_details_survive_the_system_merge() -> None:
    """TD-1903's echo rides on the assistant message, which the merge
    passes through untouched, so one system message and the array coexist."""
    details = [{"type": "reasoning.encrypted", "data": "opaque", "index": 0}]
    call = ToolCall(id="c1", function=FunctionCall(name="fs_read", arguments="{}"))
    messages = [
        ChatMessage(role="system", content="PROMPT"),
        ChatMessage(role="system", content="SUMMARY"),
        ChatMessage(role="user", content="read it"),
        ChatMessage(role="assistant", content=None, tool_calls=[call], reasoning_details=details),
        ChatMessage(role="tool", content="file text", tool_call_id="c1"),
    ]
    sent = body_messages(messages)
    strict_template(sent)
    assert [m["role"] for m in sent] == ["system", "user", "assistant", "tool"]
    assert sent[2]["reasoning_details"] == details


# ── Through the real loop ────────────────────────────────────────────


async def test_request_after_a_real_compaction_passes_the_strict_template(
    tmp_path: Path,
) -> None:
    """The bug end to end: the loop compacts, and the body that would go
    to the server has one system message, first, and a user query."""
    session, mock, runner, _hashes = await run_session(
        tmp_path / "ws",
        tmp_path / "home",
        [pad("turn1", 800), pad("turn2", 800), pad("turn3", 800)],
        tiny_config(),
    )
    assert any(isinstance(e, ContextCompacted) for e in session.event_log.all_events)
    stored = mock.calls[-1].messages
    assert [m.role for m in stored[:2]] == ["system", "system"]  # what used to go out

    sent = mock.calls[-1].to_dict()["messages"]
    strict_template(sent)
    assert sum(m["role"] == "system" for m in sent) == 1
    assert "Test workspace steering" in sent[0]["content"]
    assert "[Earlier conversation compacted]" in sent[0]["content"]
    await runner.cancel()


def _one_system_message_first(sent: list[dict[str, Any]]) -> None:
    strict_template(sent)
    assert [m["role"] for m in sent].count("system") == 1
    assert sent[0]["role"] == "system"


def test_real_from_here_compaction_goes_out_with_one_system_message() -> None:
    """TD-5154: the real ``compact_on_request(..., from_position=...)``
    output, not a hand-built copy, through ``wire_messages`` and the
    template's checks, from every user turn of the conversation."""
    original = _turns(3)
    positions = [i for i, m in enumerate(original) if m.role == "user"]
    assert positions == [1, 3, 5]
    for position in positions:
        result = compact_on_request(
            original, COUNTER, guidance="keep the answers", from_position=position
        )
        assert result is not None, position
        compacted, stats = result
        # What is stored: the summary is a second system message, after the
        # turns kept verbatim and with nothing after it yet.
        assert [m.role for m in compacted].count("system") == 2
        assert compacted[-1].role == "system"
        assert stats.dropped_messages == len(original) - position

        compacted.append(ChatMessage(role="user", content="carry on"))
        wire = wire_messages(compacted)
        assert [m.role for m in wire].count("system") == 1 and wire[0].role == "system"
        assert wire[1:-1] == original[1:position]  # earlier turns go out verbatim
        sent = body_messages(compacted)
        _one_system_message_first(sent)
        # Right after the prompt the summary joins it; after kept turns it
        # goes ahead of the next user message.
        summary = sent[0]["content"] if position == 1 else sent[-1]["content"]
        assert summary.startswith("PROMPT\n\n[Earlier" if position == 1 else "[Earlier")
        assert "keep the answers" in summary
        assert sent[-1]["content"].endswith("carry on")
        assert (sent[-1]["content"] == "carry on") == (position == 1)


async def test_summarize_from_here_then_a_turn_passes_the_strict_template(
    tmp_path: Path,
) -> None:
    """End to end: three turns, "Summarize from here" on the second, and
    the next request the loop sends has one system message, first."""
    mock = MockProvider(default=Script(kind="stream", content="ok"))
    session = Session(str(tmp_path))
    runner = await _run(session, mock, _forge_config(), tmp_path)
    try:
        for n in range(3):
            await session.add_user_message(f"question {n}")
            await wait_for_turn(session, n + 1, 15.0)
        assert await session.compact_now("keep question 2", user_index=1) is None
        await session.add_user_message("question 3")
        await wait_for_turn(session, 4, 15.0)
    finally:
        await runner.cancel()

    stored = mock.calls[-1].messages
    assert [m.role for m in stored].count("system") == 2  # what used to go out
    sent = mock.calls[-1].to_dict()["messages"]
    _one_system_message_first(sent)
    users = [m["content"] for m in sent if m["role"] == "user"]
    assert users[0] == "question 0"
    assert users[-1].startswith("[Earlier conversation compacted] (2 user turns)")
    assert users[-1].endswith("\n\nquestion 3")


# ── A template refusal fails once, in plain words ────────────────────


def _client(handler: Any, retries: int = 8) -> tuple[ProviderClient, list[int]]:
    hits: list[int] = []

    def counted(request: httpx.Request) -> httpx.Response:
        hits.append(1)
        response: httpx.Response = handler(request)
        return response

    client = ProviderClient(
        base_url="http://forge.test/v1",
        api_key=None,
        client=httpx.AsyncClient(transport=httpx.MockTransport(counted)),
        retry_config=RetryConfig(max_retries=retries, initial_delay=0.001, max_delay=0.001),
    )
    return client, hits


def _template_500(request: httpx.Request) -> httpx.Response:
    return httpx.Response(500, json=LITELLM_TEMPLATE_500)


async def test_template_refusal_is_not_retried() -> None:
    client, hits = _client(_template_500)
    request = ChatCompletionRequest(model="gateway-model", messages=_turns(1))
    result = await client.chat_completion(request)
    assert isinstance(result, ProviderError)
    assert result.code == "template_error"
    assert result.retryable is False
    assert len(hits) == 1
    assert "System message must be at the beginning" in result.message
    assert "won't help" in result.message
    assert '{"' not in result.message


async def test_template_refusal_on_a_stream_is_not_retried() -> None:
    client, hits = _client(_template_500)
    request = ChatCompletionRequest(model="gateway-model", messages=_turns(1), stream=True)
    items = [item async for item in client.chat_completion_stream(request)]
    errors = [i for i in items if isinstance(i, ProviderError)]
    assert len(errors) == 1
    assert errors[0].code == "template_error"
    assert len(hits) == 1


async def test_an_ordinary_500_is_still_retried() -> None:
    client, hits = _client(
        lambda _r: httpx.Response(500, json={"error": {"message": "upstream fell over"}}),
        retries=2,
    )
    result = await client.chat_completion(ChatCompletionRequest(model="m", messages=_turns(1)))
    assert isinstance(result, ProviderError)
    assert result.code == "server_error"
    assert len(hits) == 3


def test_template_reasons_from_each_server() -> None:
    lite = str(LITELLM_TEMPLATE_500["error"]["message"])
    assert template_error_reason(lite) == "System message must be at the beginning."
    assert (
        template_error_reason(
            "jinja2.exceptions.TemplateError: Conversation roles must alternate user/assistant"
        )
        == "Conversation roles must alternate user/assistant"
    )
    assert template_error_reason("Error: Jinja Exception: ") == ""
    assert template_error_reason("upstream timeout") is None
    assert template_error_reason("") is None


def test_transcript_shows_the_reason_not_provider_rejected() -> None:
    message = (
        "The model server's chat template refused this conversation: "
        "System message must be at the beginning. Resending won't help."
    )
    text = transcript_failure_text(
        "template_error", message, model="gateway-model", context_window=65536
    )
    assert "provider rejected" not in text
    assert "System message must be at the beginning" in text


def test_scheduler_does_not_retry_a_template_refusal() -> None:
    assert is_transient_failure("template_error") is False


def _litellm_body(status_text: str, own: str, fallback: str = "") -> dict[str, Any]:
    tail = f"\nError doing the fallback: {fallback}" if fallback else ""
    return {"error": {"message": f"litellm.{status_text}: {own}{tail}", "code": "0"}}


async def test_template_refusal_from_a_fallback_model_keeps_the_retries() -> None:
    """LiteLLM keeps the primary's status and appends the fallback's
    failure. A strict template on the fallback must not make the
    primary's 503, 429, or connection failure permanent."""
    refusal = (
        'Hosted_vllmException - {"error":{"code":500,"message":"Error: Jinja Exception: '
        'Conversation roles must alternate user/assistant/user/assistant/..."}}'
    )
    cases = ((503, "service_unavailable"), (429, "rate_limited"), (500, "server_error"))
    for status, code in cases:
        body = _litellm_body("ServiceUnavailableError", "Loading model", refusal)
        client, hits = _client(lambda _r, s=status, b=body: httpx.Response(s, json=b), retries=2)
        result = await client.chat_completion(ChatCompletionRequest(model="m", messages=_turns(1)))
        assert isinstance(result, ProviderError)
        assert result.code == code, status
        assert result.retryable is True
        assert len(hits) == 3


async def test_other_llama_cpp_render_failures_are_template_errors() -> None:
    """llama.cpp wraps every render failure, not only raise_exception."""
    raw = (
        "\\n------------\\nWhile executing FilterExpression at line 12, column 5 in source:"
        "\\n{{ x | items }}\\n         ^\\nError: Unknown (built-in) filter 'items' for type String"
    )
    body = _litellm_body(
        "InternalServerError",
        'Hosted_vllmException - {"error":{"code":500,"message":"' + raw + '"}}',
    )
    client, hits = _client(lambda _r: httpx.Response(500, json=body))
    result = await client.chat_completion(ChatCompletionRequest(model="m", messages=_turns(1)))
    assert isinstance(result, ProviderError)
    assert result.code == "template_error"
    assert result.retryable is False
    assert len(hits) == 1
    assert "Unknown (built-in) filter 'items' for type String" in result.message
