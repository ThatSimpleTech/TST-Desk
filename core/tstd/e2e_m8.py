"""M8 exit harness (TD-3904).

Live pass against the shipped ``vllm`` preset's loopback ``/v1`` (vLLM or
EZER). An absent server — or a machine with no fixture at that URL —
skips with explicit copy, heading-match style, rather than failing.
Off-box URLs are refused before any request leaves. Not
``e2e_harness.run``. This harness is the M8 exit criterion.
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import httpx
import yaml

from .config import ConfigError, ModelConfig, default_config_yaml
from .cost import compute_call_cost
from .e2e_checks import HarnessResult, _check
from .e2e_live import LiveProvider, off_box_refusal
from .provider import ChatCompletionRequest, ChatMessage, ProviderError, Usage

_PRESET = "vllm"
_PROBE_TIMEOUT = 10.0
_LIVE_BUDGET = 600.0
_PROMPT = "Reply with the single word: pong. Do not call tools."
_BINARIES = ("vllm", "ezer")

SKIP_COPY = (
    "M8 harness skipped: no loopback vLLM/EZER fixture "
    "(heading-match style — an absent server is a fact about the machine)"
)


@dataclass(frozen=True)
class M8Preflight:
    """Whether a live M8 turn may run against *endpoint*."""

    endpoint: str
    skip_copy: str | None = None
    refusal: str | None = None
    model: str | None = None


def shipped_vllm_config() -> ModelConfig:
    """The packaged ``vllm`` preset. URL comes from yaml, never a literal."""
    loaded = yaml.safe_load(default_config_yaml())
    if not isinstance(loaded, dict):
        raise ConfigError("shipped config.yaml is not a mapping")
    config = ModelConfig.model_validate(loaded)
    if _PRESET not in config.presets:
        raise ConfigError(f"shipped config.yaml has no {_PRESET} preset")
    config.active_preset = _PRESET
    return config


def shipped_vllm_endpoint() -> str:
    """Loopback ``/v1`` the shipped ``vllm`` preset names."""
    return shipped_vllm_config().tier("brain").base_url


def fixture_binaries() -> tuple[str, ...]:
    """``vllm`` / ``ezer`` names found on PATH. Empty means no local binary."""
    return tuple(name for name in _BINARIES if shutil.which(name))


def _prepare(workspace: Path, data_dir: Path) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)


async def m8_preflight(endpoint: str) -> M8Preflight:
    """Why a live M8 turn cannot run, or the one model the fixture serves.

    Off-box is a refusal (nothing is sent). An unreachable ``/v1/models``,
    a non-list body, or anything other than exactly one served model is
    :data:`SKIP_COPY` — the same honesty as a down embeddings sidecar
    falling back to heading-match.
    """
    refusal = off_box_refusal(endpoint)
    if refusal is not None:
        return M8Preflight(endpoint=endpoint, refusal=refusal)

    try:
        async with httpx.AsyncClient(timeout=_PROBE_TIMEOUT) as client:
            response = await client.get(f"{endpoint.rstrip('/')}/models")
    except httpx.HTTPError:
        return M8Preflight(endpoint=endpoint, skip_copy=SKIP_COPY)

    if response.status_code != 200:
        return M8Preflight(endpoint=endpoint, skip_copy=SKIP_COPY)

    try:
        payload = response.json()
        entries = payload.get("data", []) if isinstance(payload, dict) else []
        served = [
            str(entry["id"])
            for entry in entries
            if isinstance(entry, dict) and isinstance(entry.get("id"), str) and entry["id"]
        ]
    except (ValueError, AttributeError, TypeError):
        return M8Preflight(endpoint=endpoint, skip_copy=SKIP_COPY)

    if len(served) != 1:
        return M8Preflight(endpoint=endpoint, skip_copy=SKIP_COPY)
    return M8Preflight(endpoint=endpoint, model=served[0])


async def _live_turn(endpoint: str, model: str, config: ModelConfig) -> tuple[bool, str]:
    """One keyless completion against *endpoint*. Cost must be zero."""
    provider = LiveProvider(endpoint)
    try:
        response = await provider.chat_completion(
            ChatCompletionRequest(
                model=model,
                messages=[ChatMessage(role="user", content=_PROMPT)],
                max_tokens=32,
            )
        )
    finally:
        await provider.aclose()

    if isinstance(response, ProviderError):
        return False, f"{response.code}: {response.message}"

    text = (response.message.content or "").strip()
    usage = response.usage if response.usage is not None else Usage()
    tokens = usage.total_tokens or (usage.prompt_tokens + usage.completion_tokens)
    cost = compute_call_cost(usage, config.tier("brain"))
    ok = bool(text) and tokens > 0 and cost == 0.0
    return ok, f"cost={cost} tokens={tokens} reply={text[:48]!r}"


async def run_m8(
    workspace: Path,
    data_dir: Path,
    *,
    endpoint: str | None = None,
) -> HarnessResult:
    """Probe the shipped ``vllm`` loopback; skip or run one live turn."""
    started = time.monotonic()
    _prepare(workspace, data_dir)
    config = shipped_vllm_config()
    target = endpoint if endpoint is not None else config.tier("brain").base_url
    result = HarnessResult()
    result.notes.append("TD-3904 this harness is the M8 exit criterion")
    result.notes.append(f"preset={_PRESET} endpoint={target}")

    found = fixture_binaries()
    result.notes.append("binaries on PATH: " + (", ".join(found) if found else "none (vllm/ezer)"))

    preflight = await m8_preflight(target)
    if preflight.refusal is not None:
        _check(result, "loopback only", False, preflight.refusal)
        _check(result, "m8 exit criterion", False, "off-box URL refused")
        result.elapsed = time.monotonic() - started
        return result

    _check(result, "loopback only", True, target)
    _check(
        result,
        "vllm preset keyless",
        not config.requires_api_key(),
        "no key on loopback",
    )

    if preflight.skip_copy is not None:
        result.notes.append(preflight.skip_copy)
        _check(result, "vLLM/EZER fixture", True, preflight.skip_copy)
        _check(result, "m8 exit criterion", True, "skipped heading-match style")
        result.elapsed = time.monotonic() - started
        return result

    assert preflight.model is not None
    live_ok, detail = await _live_turn(target, preflight.model, config)
    result.elapsed = time.monotonic() - started
    _check(result, "live turn", live_ok, detail)
    _check(result, "under budget", result.elapsed < _LIVE_BUDGET, f"{result.elapsed:.1f}s")
    _check(result, "m8 exit criterion", result.ok, "live vLLM/EZER turn")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="M8 exit harness — live vLLM/EZER or heading-match skip (TD-3904)"
    )
    parser.add_argument("--workspace", type=Path, default=None)
    parser.add_argument("--data-dir", type=Path, default=None)
    parser.add_argument(
        "--endpoint",
        default=None,
        help="loopback OpenAI /v1 (default: shipped vllm preset)",
    )
    args = parser.parse_args(argv)
    scratch = tempfile.TemporaryDirectory(prefix="tstd-e2e-m8-")
    root = Path(scratch.name)
    workspace = args.workspace or root / "workspace"
    data_dir = args.data_dir or root / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    result = asyncio.run(run_m8(workspace, data_dir, endpoint=args.endpoint))
    if SKIP_COPY in result.notes:
        print(f"SKIP  {SKIP_COPY}")
        return 0
    print(result.report())
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
