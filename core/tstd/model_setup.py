"""The "Add a model" walkthrough's daemon side (TD-5172).

Three jobs, each a narrow message: list what a server offers
(``list_endpoint_models``), write a single-model preset from the user's pick
(``add_model``, via :func:`tstd.config_write.save_model_preset`), and send one
short test message (``test_model``). The key is the user's, sent only to the
URL the user typed, and never echoed back.
"""

from __future__ import annotations

from typing import Any

import httpx

from .config import ConfigError, allocate_credential_id, is_loopback_url
from .net import direct_transport
from .protocol import EndpointModel, EndpointModels

LIST_TIMEOUT = 15.0
"""Seconds to wait on ``/models``. A hosted list can be a few hundred rows."""

# A local server often serves far less context than a model's ceiling, so
# the walkthrough's default for one is the conservative floor the old
# local presets used. Hosted models below 128k are rare.
LOOPBACK_CONTEXT_WINDOW = 32768
HOSTED_CONTEXT_WINDOW = 128000
MAX_OUTPUT_TOKENS = 8192

TEST_PROMPT = "Reply with one short sentence to say you are working."
TEST_MAX_TOKENS = 64


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value > 0:
        return value
    return None


def _per_million(value: Any) -> float | None:
    """OpenRouter-style ``pricing`` is dollars per token, as a string."""
    try:
        price = float(value)
    except (TypeError, ValueError):
        return None
    if price < 0:
        return None
    return round(price * 1_000_000, 6)


def parse_model_list(payload: Any) -> list[EndpointModel] | None:
    """Rows from an OpenAI ``/models`` body, or ``None`` if it is not one.

    Picks up what servers commonly report beside the id: a context length
    (``context_length``; or the served window, vLLM's ``max_model_len`` or
    llama.cpp's ``meta.n_ctx``, which wins), per-token prices
    including the cache-read rate, and OpenRouter's output cap
    (``top_provider.max_completion_tokens``, TD-5185).
    """
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        return None
    rows: list[EndpointModel] = []
    for entry in payload["data"]:
        if not isinstance(entry, dict):
            continue
        model_id = entry.get("id")
        if not isinstance(model_id, str) or not model_id:
            continue
        raw_pricing = entry.get("pricing")
        pricing: dict[str, Any] = raw_pricing if isinstance(raw_pricing, dict) else {}
        raw_top = entry.get("top_provider")
        top: dict[str, Any] = raw_top if isinstance(raw_top, dict) else {}
        raw_meta = entry.get("meta")
        meta: dict[str, Any] = raw_meta if isinstance(raw_meta, dict) else {}
        # TD-5189: vLLM's max_model_len and llama.cpp's meta.n_ctx are what
        # the server was started with (llama.cpp: per request slot), not the
        # model's ceiling. meta.n_ctx_train is the ceiling and is not read.
        served = _positive_int(entry.get("max_model_len")) or _positive_int(meta.get("n_ctx"))
        rows.append(
            EndpointModel(
                id=model_id,
                context_window=served or _positive_int(entry.get("context_length")),
                served_window=served,
                input_price=_per_million(pricing.get("prompt")),
                output_price=_per_million(pricing.get("completion")),
                cache_read_price=_per_million(pricing.get("input_cache_read")),
                max_output_tokens=_positive_int(top.get("max_completion_tokens")),
            )
        )
    rows.sort(key=lambda row: row.id.lower())
    return rows


async def list_endpoint_models(
    base_url: str,
    api_key: str | None,
    *,
    client: httpx.AsyncClient | None = None,
) -> EndpointModels:
    """``GET {base_url}/models`` with *api_key* as the bearer token, if any."""
    endpoint = base_url.strip().rstrip("/")
    if not endpoint.startswith(("http://", "https://")):
        return EndpointModels(
            base_url=endpoint, error="The URL must start with http:// or https://"
        )
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    owned = client is None
    http = client or httpx.AsyncClient(timeout=LIST_TIMEOUT, transport=direct_transport(endpoint))
    try:
        response = await http.get(f"{endpoint}/models", headers=headers)
    except httpx.HTTPError as e:
        return EndpointModels(
            base_url=endpoint,
            error=f"No server answered at {endpoint} ({type(e).__name__}). Check the URL.",
        )
    finally:
        if owned:
            await http.aclose()
    if response.status_code in (401, 403):
        return EndpointModels(
            base_url=endpoint, error="The server refused the key. Check it and try again."
        )
    if response.status_code != 200:
        return EndpointModels(
            base_url=endpoint,
            error=(
                f"{endpoint}/models returned HTTP {response.status_code}. Most servers "
                "want the URL that ends in /v1."
            ),
        )
    try:
        rows = parse_model_list(response.json())
    except ValueError:
        rows = None
    if rows is None:
        return EndpointModels(
            base_url=endpoint,
            error=(
                "That URL answered, but not with a model list. "
                "Use the API URL, often ending in /v1."
            ),
        )
    if not rows:
        return EndpointModels(base_url=endpoint, error="The server lists no models.")
    # A gateway that lists bare ids (LiteLLM) may keep windows on /model/info.
    # A list that already carries windows (OpenRouter) is not asked twice.
    if any(row.context_window is None for row in rows):
        rows = await _with_gateway_windows(endpoint, headers, rows, client)
    return EndpointModels(base_url=endpoint, models=rows)


def parse_gateway_windows(payload: Any) -> dict[str, int]:
    """``model_name`` → window from a LiteLLM ``/model/info`` body (TD-5189).

    The gateway's ``model_info.max_input_tokens`` is the window the gateway's
    owner says the route serves. A name with several deployments gets the
    smallest, since a request may land on any of them.
    """
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        return {}
    windows: dict[str, int] = {}
    for entry in payload["data"]:
        if not isinstance(entry, dict):
            continue
        name = entry.get("model_name")
        info = entry.get("model_info")
        if not isinstance(name, str) or not isinstance(info, dict):
            continue
        window = _positive_int(info.get("max_input_tokens"))
        if window is not None:
            windows[name] = min(window, windows.get(name, window))
    return windows


async def _with_gateway_windows(
    endpoint: str,
    headers: dict[str, str],
    rows: list[EndpointModel],
    client: httpx.AsyncClient | None,
) -> list[EndpointModel]:
    """Fill windows a gateway keeps off ``/models`` from its ``/model/info``.

    LiteLLM lists only ids on ``/models``. Any failure leaves *rows* as they
    were: most servers have no such route, and that is not an error.
    """
    owned = client is None
    http = client or httpx.AsyncClient(timeout=LIST_TIMEOUT, transport=direct_transport(endpoint))
    try:
        response = await http.get(f"{endpoint}/model/info", headers=headers)
        windows = parse_gateway_windows(response.json()) if response.status_code == 200 else {}
    except (httpx.HTTPError, ValueError):
        windows = {}
    finally:
        if owned:
            await http.aclose()
    if not windows:
        return rows
    return [
        row.model_copy(update={"context_window": windows[row.id], "served_window": windows[row.id]})
        if row.served_window is None and row.id in windows
        else row
        for row in rows
    ]


def preset_name_for(label: str, existing: set[str]) -> str:
    """A free preset key for *label*: its slug, with a suffix on a clash."""
    try:
        return allocate_credential_id(label, existing)
    except ConfigError as e:
        raise ConfigError(f"Could not name a model entry for {label!r}") from e


def default_context_window(base_url: str) -> int:
    """The compaction budget for a model whose server did not report one."""
    return LOOPBACK_CONTEXT_WINDOW if is_loopback_url(base_url) else HOSTED_CONTEXT_WINDOW


def default_max_output(context_window: int) -> int:
    """Leave most of the window for the conversation."""
    return max(256, min(MAX_OUTPUT_TOKENS, context_window // 4))
