"""M8 exit test: live vLLM/EZER fixture or heading-match skip (TD-3904).

The harness lives in ``tstd.e2e_m8``. CI green is the skip path — an
absent loopback server is a fact about the machine. The live turn is
``@pytest.mark.live``. This harness is the M8 exit criterion.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tstd.config import is_loopback_url
from tstd.e2e_m8 import (
    SKIP_COPY,
    m8_preflight,
    run_m8,
    shipped_vllm_config,
    shipped_vllm_endpoint,
)

# Discard protocol: nothing listens, the probe fails immediately.
_DEAD_LOOPBACK = "http://127.0.0.1:9/v1"
_OFF_BOX = "https://example.invalid/v1"


def test_shipped_vllm_endpoint_is_loopback() -> None:
    endpoint = shipped_vllm_endpoint()
    assert is_loopback_url(endpoint)
    assert shipped_vllm_config().requires_api_key() is False


async def test_m8_skip_copy_when_probe_fails() -> None:
    """Hermetic: no live server. The skip copy is the CI path."""
    preflight = await m8_preflight(_DEAD_LOOPBACK)
    assert preflight.refusal is None
    assert preflight.model is None
    assert preflight.skip_copy == SKIP_COPY


async def test_m8_refuses_off_box_url() -> None:
    """Refuse before send — a remote URL is not a skip."""
    preflight = await m8_preflight(_OFF_BOX)
    assert preflight.refusal is not None
    assert preflight.skip_copy is None
    assert preflight.model is None
    assert "not a loopback" in preflight.refusal


async def test_m8_run_skip_when_probe_fails(tmp_path: Path) -> None:
    result = await run_m8(tmp_path / "workspace", tmp_path / "data", endpoint=_DEAD_LOOPBACK)
    assert SKIP_COPY in result.notes
    assert result.ok, result.report()


@pytest.mark.live
async def test_m8_exit_harness(tmp_path: Path) -> None:
    result = await run_m8(tmp_path / "workspace", tmp_path / "data")
    if SKIP_COPY in result.notes:
        pytest.skip(SKIP_COPY)
    assert result.ok, f"M8 harness failed:\n{result.report()}"
    assert result.elapsed < 600.0
