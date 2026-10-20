"""On-demand MCP tool search (TD-5127).

Acceptance criteria covered here:

* Above ``mcp.tool_search.threshold_percent`` of the tier's window, MCP
  tools are left out of the request and the builtin ``tool_search`` is
  offered; the tools it finds load for the rest of the session.  Proved
  through a real loop turn: a stdio MCP server, the real dispatcher and
  classifier, and an HTTP endpoint that sees the wire body.
* It needs no provider feature: every request is a plain OpenAI-shaped
  body with plain ``tools`` entries, accepted by the strict endpoint
  mock from TD-5104.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.test_dispatch import make_classifier, make_config, wait_for_turn
from tests.test_mcp_schema_sanitize import _strict_problem
from tstd.autonomy.classifier import (
    RULE_TABLE,
    Boundary,
    DecisionClass,
    DecisionClassifier,
    DecisionRequest,
)
from tstd.config import McpConfig, McpServerConfig, McpToolSearchConfig, ModelConfig, TierConfig
from tstd.context.prompt import PromptAssembler
from tstd.context.tokens import HeuristicTokenCounter
from tstd.loop import agent_loop
from tstd.mcp.deferral import McpToolDeferral, catalog_text
from tstd.mcp.loader import McpSupervisor
from tstd.policy import ApprovalOutcome, PolicyConfig
from tstd.provider import (
    ChatMessage,
    FunctionCall,
    ProviderClient,
    ToolCall,
    ToolDefinition,
)
from tstd.router import TierRouter
from tstd.session import Session, SessionRunner
from tstd.tools import ToolDispatcher, create_registry
from tstd.tools.handlers import register_builtin_handlers
from tstd.tools.registry import Tool, ToolRegistry
from tstd.tools.tool_search import (
    TOOL_SEARCH_NAME,
    TOOL_SEARCH_TOOL,
    search_tools,
    tool_search,
)

COUNTER = HeuristicTokenCounter()

# ── A registry with MCP tools ───────────────────────────────────────────


def _mcp_tool(server: str, name: str, description: str, **props: str) -> Tool:
    return Tool(
        name=f"{server}__{name}",
        description=description,
        parameters={
            "type": "object",
            "properties": {k: {"type": "string", "description": v} for k, v in props.items()},
        },
        side_effect_class="ask",
        parallel_safe=True,
        provenance=f"mcp:{server}",
    )


CATALOG = [
    _mcp_tool("tracker", "create_issue", "Create a new issue in a project.", title="Summary"),
    _mcp_tool("tracker", "list_issues", "List open issues, newest first.", project="Key"),
    _mcp_tool("tracker", "add_comment", "Comment on an issue.", key="Issue key"),
    _mcp_tool("wx", "get_forecast", "Weather forecast for a city.", city="City name"),
    _mcp_tool("wx", "get_alerts", "Severe weather alerts for a region.", region="Region"),
    _mcp_tool("files", "upload_file", "Upload a file to cloud storage.", path="Local path"),
]


def _registry(tools: Sequence[Tool] = CATALOG) -> ToolRegistry:
    registry = create_registry()
    for tool in tools:
        registry.register(tool)
    return registry


def _tier(window: int) -> TierConfig:
    return TierConfig(
        slug="m",
        base_url="http://127.0.0.1:9/v1",
        input_price=0,
        output_price=0,
        cache_read_price=0,
        context_window=window,
        max_output_tokens=100,
    )


def _names(definitions: Sequence[ToolDefinition] | None) -> list[str]:
    return [d.function.name for d in definitions or () if d.function is not None]


def _offer(
    deferral: McpToolDeferral,
    registry: ToolRegistry,
    session: Session,
    *,
    window: int,
    messages: Sequence[ChatMessage] = (),
    tier: str = "brain",
    counter: Any = COUNTER,
) -> list[ToolDefinition] | None:
    return deferral.offer(
        registry.to_provider_definitions(),
        session=session,
        tier=tier,
        tier_cfg=_tier(window),
        counter=counter,
        messages=messages,
    )


def _mcp_tokens(registry: ToolRegistry) -> int:
    from tstd.compaction import estimate_tokens

    defs = [d for d in registry.to_provider_definitions() if "__" in _names([d])[0]]
    return estimate_tokens([], COUNTER, defs)[0]


# ── Search ──────────────────────────────────────────────────────────────


class TestSearch:
    def test_keywords_rank_name_matches_first(self) -> None:
        found = search_tools(CATALOG, "create issue", 5)
        assert found[0].name == "tracker__create_issue"
        assert {t.name for t in found} >= {"tracker__list_issues", "tracker__add_comment"}

    def test_plural_and_description_words_match(self) -> None:
        assert search_tools(CATALOG, "issues", 1)[0].name in {
            "tracker__create_issue",
            "tracker__list_issues",
        }
        assert [t.name for t in search_tools(CATALOG, "weather", 5)] == [
            "wx__get_alerts",
            "wx__get_forecast",
        ]

    def test_server_name_finds_its_tools(self) -> None:
        assert {t.name for t in search_tools(CATALOG, "wx", 5)} == {
            "wx__get_forecast",
            "wx__get_alerts",
        }

    def test_select_takes_exact_and_short_names(self) -> None:
        found = search_tools(CATALOG, "select:wx__get_alerts, upload_file,nope", 5)
        assert [t.name for t in found] == ["wx__get_alerts", "files__upload_file"]

    def test_a_short_name_two_servers_use_selects_both(self) -> None:
        tools = [
            *CATALOG,
            _mcp_tool("wiki", "search", "Search wiki pages."),
            _mcp_tool("mail", "search", "Search mail."),
        ]
        found = search_tools(tools, "select:search", 5)
        assert sorted(t.name for t in found) == ["mail__search", "wiki__search"]
        assert [t.name for t in search_tools(tools, "select:wiki__search", 5)] == ["wiki__search"]

    def test_limit_and_no_match(self) -> None:
        assert len(search_tools(CATALOG, "issue", 1)) == 1
        assert search_tools(CATALOG, "zebra", 5) == []
        assert search_tools(CATALOG, "  ", 5) == []


class TestCatalog:
    def test_names_by_server(self) -> None:
        text = catalog_text(CATALOG)
        assert "- tracker: create_issue, list_issues, add_comment" in text
        assert "- wx: get_forecast, get_alerts" in text

    def test_a_server_past_the_budget_is_counted(self) -> None:
        text = catalog_text(CATALOG, budget=90)
        assert "- files: upload_file" in text
        assert "- tracker: 3 tools, find them by keyword" in text


# ── What each call offers ───────────────────────────────────────────────


class TestOffer:
    def test_below_the_threshold_every_tool_goes_and_search_does_not(self) -> None:
        registry = _registry()
        window = _mcp_tokens(registry) * 100  # MCP tools are 1% of it
        deferral = McpToolDeferral(registry, McpToolSearchConfig(threshold_percent=10))
        names = _names(_offer(deferral, registry, Session("/tmp/ws"), window=window))
        assert {t.name for t in CATALOG} <= set(names)
        assert TOOL_SEARCH_NAME not in names

    def test_above_the_threshold_mcp_tools_stay_out(self) -> None:
        registry = _registry()
        window = _mcp_tokens(registry) * 5  # MCP tools are 20% of it
        deferral = McpToolDeferral(registry, McpToolSearchConfig(threshold_percent=10))
        offered = _offer(deferral, registry, Session("/tmp/ws"), window=window)
        names = _names(offered)
        assert not {t.name for t in CATALOG} & set(names)
        assert "fs_read" in names and TOOL_SEARCH_NAME in names
        search = next(d for d in offered or () if _names([d]) == [TOOL_SEARCH_NAME])
        assert search.function is not None
        assert "- wx: get_alerts, get_forecast" in search.function.description

    def test_off_and_always(self) -> None:
        registry = _registry()
        huge = _mcp_tokens(registry) * 1000
        off = McpToolDeferral(registry, McpToolSearchConfig(mode="off"))
        names = _names(_offer(off, registry, Session("/tmp/ws"), window=1000))
        assert {t.name for t in CATALOG} <= set(names) and TOOL_SEARCH_NAME not in names
        always = McpToolDeferral(registry, McpToolSearchConfig(mode="always"))
        names = _names(_offer(always, registry, Session("/tmp/ws"), window=huge))
        assert not {t.name for t in CATALOG} & set(names) and TOOL_SEARCH_NAME in names

    def test_no_mcp_tools_means_no_search(self) -> None:
        registry = create_registry()
        deferral = McpToolDeferral(registry, McpToolSearchConfig(mode="always"))
        names = _names(_offer(deferral, registry, Session("/tmp/ws"), window=1000))
        everything = _names(registry.to_provider_definitions())
        assert names == [n for n in everything if n != TOOL_SEARCH_NAME]

    def test_the_decision_holds_for_the_session(self) -> None:
        """A tier's learned token ratio must not make the tool list flicker."""

        class Scaled:
            def __init__(self, ratio: float) -> None:
                self.ratio = ratio

            def count(self, text: str) -> Any:
                base = COUNTER.count(text)
                return type(base)(count=int(base.count * self.ratio), method=base.method)

        registry = _registry()
        window = _mcp_tokens(registry) * 5
        counter = Scaled(1.0)
        deferral = McpToolDeferral(registry, McpToolSearchConfig(threshold_percent=10))
        session = Session("/tmp/ws")
        assert TOOL_SEARCH_NAME in _names(
            _offer(deferral, registry, session, window=window, counter=counter)
        )
        counter.ratio = 0.01  # now far under the threshold
        assert TOOL_SEARCH_NAME in _names(
            _offer(deferral, registry, session, window=window, counter=counter)
        )
        # A tier with a bigger window decides for itself.
        names = _names(_offer(deferral, registry, session, window=window * 100, tier="worker"))
        assert TOOL_SEARCH_NAME not in names

    def test_loaded_tools_go_last_in_load_order(self) -> None:
        registry = _registry()
        deferral = McpToolDeferral(registry, McpToolSearchConfig(mode="always"))
        session = Session("/tmp/ws")
        before = _offer(deferral, registry, session, window=100_000)
        session.loaded_mcp_tools.extend(["wx__get_forecast", "tracker__create_issue"])
        after = _offer(deferral, registry, session, window=100_000)
        assert after is not None and before is not None
        assert _names(after[-2:]) == ["wx__get_forecast", "tracker__create_issue"]
        # Everything before them is byte-identical, so a cached prefix holds.
        assert after[:-2] == before

    def test_a_tool_the_conversation_called_stays_offered(self) -> None:
        """After a restart the session is rebuilt from its conversation."""
        registry = _registry()
        deferral = McpToolDeferral(registry, McpToolSearchConfig(mode="always"))
        session = Session("/tmp/ws")
        call = ToolCall(
            id="c1",
            type="function",
            function=FunctionCall(name="wx__get_alerts", arguments="{}"),
        )
        messages = [ChatMessage(role="assistant", content="", tool_calls=[call])]
        names = _names(_offer(deferral, registry, session, window=100_000, messages=messages))
        assert names[-1] == "wx__get_alerts"
        assert session.loaded_mcp_tools == ["wx__get_alerts"]


# ── A restart keeps what was loaded ─────────────────────────────────────


async def test_loaded_tools_survive_a_restart(tmp_path: Path) -> None:
    """Compaction folds away the calls, so the history alone cannot say."""
    from tests.test_queued_restart import SID, _seed, _stop
    from tstd.daemon import Daemon
    from tstd.mock import MockProvider, Script
    from tstd.session_persist import SessionPersist

    data_dir = tmp_path / "data"
    workspace = tmp_path / "ws"
    data_dir.mkdir()
    workspace.mkdir()
    await _seed(data_dir, workspace, [], [])
    persist = SessionPersist(data_dir)
    persist.save_loaded_tools(SID, ["wx__get_forecast", "tracker__create_issue"])

    daemon = Daemon(data_dir=data_dir, provider=MockProvider(default=Script(kind="stream")))
    await daemon._restore_sessions()
    try:
        session = daemon.session_registry.get(SID)
        assert session is not None
        assert session.loaded_mcp_tools == ["wx__get_forecast", "tracker__create_issue"]
        # A later load is saved with the next conversation snapshot.
        session.loaded_mcp_tools.append("wx__get_alerts")
        await session.conversation_changed()
        assert persist.load_loaded_tools(SID)[-1] == "wx__get_alerts"
        session.loaded_mcp_tools.clear()
        await session.conversation_changed()
        assert not (persist.dir_for(SID) / "mcp_tools.json").exists()
    finally:
        await _stop(daemon)


def test_an_unreadable_list_reads_as_none(tmp_path: Path) -> None:
    from tstd.session_persist import SessionPersist

    persist = SessionPersist(tmp_path)
    persist.prepare("s1")
    (persist.dir_for("s1") / "mcp_tools.json").write_text("{not json")
    assert persist.load_loaded_tools("s1") == []
    (persist.dir_for("s1") / "mcp_tools.json").write_text('["a", 3, "b"]')
    assert persist.load_loaded_tools("s1") == ["a", "b"]


# ── The handler ─────────────────────────────────────────────────────────


async def test_the_handler_loads_matches_once() -> None:
    registry = _registry()
    session = Session("/tmp/ws")
    out = await tool_search(registry=registry, session=session, query="weather forecast")
    assert session.loaded_mcp_tools[0] == "wx__get_forecast"
    assert "Loaded 2 tool(s)" in out and "- wx__get_forecast: Weather forecast" in out
    again = await tool_search(registry=registry, session=session, query="select:get_forecast")
    assert "(already loaded)" in again
    assert session.loaded_mcp_tools.count("wx__get_forecast") == 1


async def test_the_handler_says_when_nothing_matches() -> None:
    session = Session("/tmp/ws")
    out = await tool_search(registry=_registry(), session=session, query="zebra")
    assert "No MCP tool matched" in out and session.loaded_mcp_tools == []
    none = await tool_search(registry=create_registry(), session=session, query="issue")
    assert "No MCP tools are configured" in none


async def test_without_a_session_the_handler_refuses() -> None:
    from tstd.tools.results import HandlerRefusal

    with pytest.raises(HandlerRefusal):
        await tool_search(registry=_registry(), session=None, query="issue")


def test_the_tool_is_a_strict_builtin() -> None:
    registry = create_registry()
    assert registry.require(TOOL_SEARCH_NAME) is TOOL_SEARCH_TOOL
    (definition,) = [
        d for d in registry.to_provider_definitions() if _names([d]) == [TOOL_SEARCH_NAME]
    ]
    from tstd.provider import tool_definition_payload

    assert _strict_problem(tool_definition_payload(definition)) is None


# ── The classifier ──────────────────────────────────────────────────────


def _classify(request: DecisionRequest) -> Any:
    return DecisionClassifier(Boundary(workspace_root=Path("/work/proj"))).classify(request)


def test_the_builtin_is_class_a() -> None:
    decision = _classify(DecisionRequest(tool_name=TOOL_SEARCH_NAME))
    assert decision.decision_class is DecisionClass.A
    assert decision.rule is not None and decision.rule.id == "tool-search"


def test_only_the_builtin_shape_matches() -> None:
    for request in (
        DecisionRequest(tool_name=TOOL_SEARCH_NAME, provenance="mcp:srv"),
        DecisionRequest(tool_name=TOOL_SEARCH_NAME, provenance="plugin:dist"),
        DecisionRequest(tool_name=TOOL_SEARCH_NAME, side_effect_class="ask"),
        DecisionRequest(tool_name=TOOL_SEARCH_NAME, reads=(Path("/work/proj/a"),)),
        DecisionRequest(tool_name=TOOL_SEARCH_NAME, hosts=("example.com",)),
        DecisionRequest(tool_name="srv__tool_search", provenance="mcp:srv"),
    ):
        decision = _classify(request)
        assert decision.rule is None or decision.rule.id != "tool-search", request


def test_order_pinned() -> None:
    ids = [rule.id for rule in RULE_TABLE]
    at = ids.index("tool-search")
    assert ids.index("cap-exceeded") < at
    assert ids.index("side-effect-never") < at
    assert ids.index("mcp-undeclared-fields") < at
    assert at < ids.index("side-effect-ask-floor")


# ── End to end: a real turn against a strict endpoint ───────────────────

_SERVER = r"""
import json
import sys

FILLER = (
    "Return the station report for one numbered observation site, with hourly "
    "temperature, humidity, pressure, wind speed and direction, and the data quality flags."
)
TOOLS = [
    {"name": "get_forecast", "description": "Weather forecast for a city, by day.",
     "inputSchema": {"type": "object", "properties": {"city": {"type": "string"}},
                     "required": ["city"]}},
] + [
    {"name": f"station_report_{i:02d}", "description": FILLER,
     "inputSchema": {"type": "object", "properties": {
         "station": {"type": "string", "description": "Station id, e.g. KSEA"},
         "hours": {"type": "integer", "description": "Hours of history (1-72)"}}}}
    for i in range(24)
]

for line in sys.stdin:
    msg = json.loads(line)
    if "id" not in msg:
        continue
    if msg["method"] == "initialize":
        result = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
                  "serverInfo": {"name": "wx", "version": "1"}}
    elif msg["method"] == "tools/list":
        result = {"tools": TOOLS}
    else:
        city = msg["params"]["arguments"].get("city", "?")
        result = {"content": [{"type": "text", "text": f"Sunny in {city}"}]}
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result}) + "\n")
    sys.stdout.flush()
"""

#: Every top-level key an OpenAI chat completion body may carry here.
_OPENAI_KEYS = {"model", "messages", "tools", "tool_choice", "stream", "stream_options"}
_OPENAI_KEYS |= {"max_tokens", "temperature", "top_p", "stop"}


def _sse(delta: dict[str, Any], finish: str) -> httpx.Response:
    chunks = [
        {"id": "c1", "choices": [{"index": 0, "delta": delta}]},
        {"id": "c1", "choices": [{"index": 0, "delta": {}, "finish_reason": finish}]},
    ]
    body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body.encode())


def _call(name: str, arguments: dict[str, Any], call_id: str) -> httpx.Response:
    delta = {
        "role": "assistant",
        "tool_calls": [
            {
                "index": 0,
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments)},
            }
        ],
    }
    return _sse(delta, "tool_calls")


class _Endpoint:
    """A strict OpenAI-compatible endpoint that scripts the model's moves."""

    def __init__(self, replies: list[httpx.Response]) -> None:
        self.replies = replies
        self.bodies: list[dict[str, Any]] = []

    def tools(self, n: int) -> list[str]:
        return [t["function"]["name"] for t in self.bodies[n].get("tools", [])]

    def handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.bodies.append(body)
        unknown = set(body) - _OPENAI_KEYS
        if unknown:
            return httpx.Response(400, json={"error": {"message": f"unknown {unknown}"}})
        for tool in body.get("tools", []):
            if set(tool) != {"type", "function"} or set(tool["function"]) != {
                "name",
                "description",
                "parameters",
            }:
                return httpx.Response(400, json={"error": {"message": f"odd tool {tool}"}})
            problem = _strict_problem(tool)
            if problem:
                return httpx.Response(400, json={"error": {"message": problem}})
        return self.replies.pop(0)


async def _run_turns(
    tmp_path: Path, endpoint: _Endpoint, search: McpToolSearchConfig, turns: int
) -> tuple[Session, list[tuple[Any, ...]]]:
    ws = tmp_path / "ws"
    ws.mkdir()
    script = tmp_path / "server.py"
    script.write_text(_SERVER)
    server = McpServerConfig(transport="stdio", command=[sys.executable, str(script)])
    supervisor = McpSupervisor(
        McpConfig(servers={"wx": server}),
        handshake_timeout=10.0,
        call_timeout=10.0,
    )
    registry = create_registry()
    dispatcher = ToolDispatcher(registry, classifier=make_classifier(str(ws)), workspace=ws)
    dispatcher.policy = PolicyConfig()
    approvals: list[tuple[Any, ...]] = []

    async def _approve(*args: Any) -> ApprovalOutcome:
        approvals.append(args)
        return ApprovalOutcome(True)

    dispatcher.approval_handler = _approve
    register_builtin_handlers(dispatcher)
    config: ModelConfig = make_config()
    config.mcp = McpConfig(tool_search=search)
    client = ProviderClient(
        base_url="http://127.0.0.1:9/v1",
        api_key=None,
        client=httpx.AsyncClient(transport=httpx.MockTransport(endpoint.handle)),
    )

    async def factory(*_args: object) -> ProviderClient:
        return client

    session = Session(str(ws))
    try:
        await supervisor.ensure_loaded()
        supervisor.attach(registry, dispatcher)
        runner = SessionRunner(
            session,
            loop_factory=lambda s: agent_loop(
                s,
                TierRouter(),
                factory,
                config,
                tool_registry=registry,
                tool_dispatcher=dispatcher,
                prompt_assembler=PromptAssembler(ws, home_dir=tmp_path / "home"),
            ),
        )
        await runner.start()
        try:
            for n in range(1, turns + 1):
                await session.add_user_message(f"message {n}")
                turn = await wait_for_turn(session, n, 15.0)
                assert turn.failed is False, turn.error_code
        finally:
            await runner.cancel()
    finally:
        await supervisor.aclose()
        await client.close()
    return session, approvals


async def test_a_turn_searches_loads_and_calls_a_deferred_tool(tmp_path: Path) -> None:
    endpoint = _Endpoint(
        [
            _call(TOOL_SEARCH_NAME, {"query": "weather forecast"}, "call_1"),
            _call("wx__get_forecast", {"city": "Quito"}, "call_2"),
            _sse({"role": "assistant", "content": "Sunny."}, "stop"),
            _sse({"role": "assistant", "content": "Still sunny."}, "stop"),
        ]
    )
    # 1% of the 100k test window: the 25 wx tools are well over it.
    session, approvals = await _run_turns(
        tmp_path, endpoint, McpToolSearchConfig(threshold_percent=1), turns=2
    )

    first = endpoint.tools(0)
    assert TOOL_SEARCH_NAME in first and "fs_read" in first
    assert not [n for n in first if n.startswith("wx__")], "MCP schemas left out"
    # The search's own description names what it can load.
    search = next(t for t in endpoint.bodies[0]["tools"] if t["function"]["name"] == "tool_search")
    assert "get_forecast" in search["function"]["description"]

    # Loaded from the very next call, after every other tool, and kept
    # for the rest of the session: the next user turn still has it.
    for n in (1, 2, 3):
        tools = endpoint.tools(n)
        assert tools[-1] == "wx__get_forecast", n
        assert [t for t in tools if t.startswith("wx__")] == ["wx__get_forecast"], n
    assert endpoint.tools(1)[:-1] == first
    tool_reply = endpoint.bodies[2]["messages"][-1]
    assert tool_reply["role"] == "tool" and "Sunny in Quito" in tool_reply["content"]

    # tool_search never asked; the MCP call itself still went through
    # the classifier and asked (Class B), so loading granted nothing.
    asked = [a for a in approvals if "wx__get_forecast" in repr(a)]
    assert len(asked) == 1 and len(approvals) == 1
    assert session.loaded_mcp_tools[0] == "wx__get_forecast"


async def test_under_the_threshold_the_request_is_unchanged(tmp_path: Path) -> None:
    endpoint = _Endpoint([_sse({"role": "assistant", "content": "Hi."}, "stop")])
    await _run_turns(tmp_path, endpoint, McpToolSearchConfig(threshold_percent=50), turns=1)
    tools = endpoint.tools(0)
    assert len([n for n in tools if n.startswith("wx__")]) == 25
    assert TOOL_SEARCH_NAME not in tools


# ── The measurement script (run on EZER for the story) ──────────────────

_MEASURE = Path(__file__).resolve().parent.parent / "scripts" / "measure_mcp_tool_search.py"


async def test_the_measurement_script_runs_without_an_endpoint(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The script the story's numbers came from still runs and still defers."""
    import argparse
    import importlib.util

    spec = importlib.util.spec_from_file_location("_measure_mcp_tool_search", _MEASURE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    args = argparse.Namespace(
        servers=6,
        context_window=32_768,
        threshold_percent=10.0,
        model="local",
        tokenize_url="",
        query="calendar free time event",
        verbose=False,
    )
    await module.measure(args)
    out = capsys.readouterr().out
    assert "servers: 6" in out and "MCP tools: 70" in out
    rows = {
        line.split("|")[1].strip(): int(line.split("|")[3])
        for line in out.splitlines()
        if line.startswith("| ") and line.split("|")[3].strip().isdigit()
    }
    before = rows["every schema (before)"]
    after = rows["deferred (after)"]
    assert after < before * 0.7, rows
    assert after < rows["after one tool_search"] < before
