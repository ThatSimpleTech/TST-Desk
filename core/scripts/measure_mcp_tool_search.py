"""Measure prompt tokens with many MCP servers, before and after tool search.

TD-5127. Loads the six catalogs of ``mcp_catalog_server.py`` (or the first
``--servers`` of them) through the daemon's own ``McpSupervisor``, builds
the first request of a turn as the loop builds it, and counts its prompt
tokens three ways: every schema sent (``mode: off``, the old behaviour),
deferred (``mode: auto``), and deferred after one ``tool_search`` loaded
a few tools.

The count is TST Desk's own estimate, and, with ``--tokenize-url``, the
exact count from a vLLM-style ``/tokenize`` endpoint that takes chat
``messages`` and ``tools`` (no generation runs). On the GPU server:

    uv run python scripts/measure_mcp_tool_search.py \\
        --tokenize-url http://127.0.0.1:8002/tokenize --model local \\
        --context-window 32768
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import tempfile
from pathlib import Path
from typing import Any

import httpx

from tstd.compaction import estimate_tokens
from tstd.config import McpConfig, McpServerConfig, McpToolSearchConfig, TierConfig
from tstd.context.prompt import PromptAssembler
from tstd.context.tokens import make_token_counter
from tstd.mcp.deferral import McpToolDeferral
from tstd.mcp.loader import McpSupervisor
from tstd.provider import ChatMessage, ToolDefinition, content_as_text, tool_definition_payload
from tstd.session import Session
from tstd.tools import ToolDispatcher, create_registry
from tstd.tools.tool_search import tool_search

SERVER = Path(__file__).with_name("mcp_catalog_server.py")
CATALOGS = ("tracker", "wiki", "calendar", "mail", "sql", "storage")
QUESTION = "Find a free hour with Ana next week and put it on my calendar."


def _tier(window: int, slug: str) -> TierConfig:
    return TierConfig(
        slug=slug,
        base_url="http://127.0.0.1:9/v1",
        input_price=0,
        output_price=0,
        cache_read_price=0,
        context_window=window,
        max_output_tokens=4096,
    )


async def _exact(url: str, model: str, messages: list[ChatMessage], tools: Any) -> int | None:
    body = {
        "model": model,
        "messages": [{"role": m.role, "content": content_as_text(m.content)} for m in messages],
        "tools": [tool_definition_payload(t) for t in tools],
    }
    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post(url, json=body)
    if response.status_code != 200:
        print(f"tokenize failed: {response.status_code} {response.text[:200]}", file=sys.stderr)
        return None
    count = response.json().get("count")
    return count if isinstance(count, int) else None


async def measure(args: argparse.Namespace) -> None:
    catalogs = CATALOGS[: args.servers]
    servers = {
        name: McpServerConfig(transport="stdio", command=[sys.executable, str(SERVER), name])
        for name in catalogs
    }
    supervisor = McpSupervisor(McpConfig(servers=servers), handshake_timeout=20.0)
    registry = create_registry()
    try:
        rows = await supervisor.ensure_loaded()
        supervisor.attach(registry, ToolDispatcher(registry))
    finally:
        await supervisor.aclose()
    failed = [r for r in rows if r.status != "ok"]
    if failed:
        raise SystemExit(f"servers failed: {[(r.server_id, r.detail) for r in failed]}")

    with tempfile.TemporaryDirectory() as tmp:
        ws = Path(tmp) / "ws"
        ws.mkdir()
        assembled = await PromptAssembler(ws, home_dir=Path(tmp) / "home").assemble("brain")
    messages = [
        ChatMessage(role="system", content=assembled.text),
        ChatMessage(role="user", content=QUESTION),
    ]
    tier_cfg = _tier(args.context_window, args.model)
    counter = make_token_counter(args.model)
    definitions = registry.to_provider_definitions()
    session = Session("/tmp/measure")

    def offered(config: McpToolSearchConfig) -> list[ToolDefinition]:
        out = McpToolDeferral(registry, config).offer(
            definitions,
            session=session,
            tier="brain",
            tier_cfg=tier_cfg,
            counter=counter,
            messages=messages,
        )
        return out or []

    before = offered(McpToolSearchConfig(mode="off"))
    search = McpToolSearchConfig(threshold_percent=args.threshold_percent)
    after = offered(search)
    found = await tool_search(registry=registry, session=session, query=args.query)
    loaded = offered(search)

    mcp_count = sum(1 for t in registry.list_tools() if (t.provenance or "").startswith("mcp:"))
    print(f"servers: {len(catalogs)} ({', '.join(catalogs)}); MCP tools: {mcp_count}")
    print(f"window: {args.context_window}; threshold: {args.threshold_percent}%")
    print(f"tool_search({args.query!r}) loaded: {', '.join(session.loaded_mcp_tools)}")
    print()
    print("| request | tools | estimate | exact |")
    print("|---|---|---|---|")
    for label, tools in (
        ("every schema (before)", before),
        ("deferred (after)", after),
        ("after one tool_search", loaded),
    ):
        estimate, _ = estimate_tokens(messages, counter, tools)
        exact = (
            await _exact(args.tokenize_url, args.model, messages, tools)
            if args.tokenize_url
            else None
        )
        shown = "-" if exact is None else str(exact)
        print(f"| {label} | {len(tools)} | {estimate} | {shown} |")
    if args.verbose:
        print()
        print(found)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--servers", type=int, default=len(CATALOGS), choices=range(1, 7))
    parser.add_argument("--context-window", type=int, default=65_536)
    parser.add_argument("--threshold-percent", type=float, default=10.0)
    parser.add_argument("--model", default="local", help="model name sent to the tokenizer")
    parser.add_argument("--tokenize-url", default="", help="vLLM-style /tokenize URL")
    parser.add_argument("--query", default="calendar free time event")
    parser.add_argument("--verbose", action="store_true")
    asyncio.run(measure(parser.parse_args()))


if __name__ == "__main__":
    main()
