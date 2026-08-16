"""M1.5 exit test: the harness must pass against a real model (TD-1803).

Deselected by default (``addopts = -m 'not live'``) because it needs an
OpenAI-compatible model server on this machine; CI has none.  Run it with::

    uv run pytest -m live

It is the live twin of ``test_e2e_headless.py`` and asserts the same chain
— message → tool call → classification → approval → execution → ledger →
cost — with two deliberate differences: approval is granted on
``approval_request`` (TD-802 registers the pending approval when it emits
that event, not on ``tool_call``), and a zero-price local preset is expected
to bill nothing while still recording real token counts.

Failure modes are kept apart on purpose.  No endpoint, or an endpoint that
does not serve the configured slug, is a fact about the machine and skips.
An endpoint that answers but breaks the OpenAI-compatible contract raises
``ProviderContractError``.  Only an ``AssertionError`` here means the agent
loop itself is broken.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tstd.config import cached_config, save_active_preset
from tstd.e2e_harness import run
from tstd.e2e_live import (
    LiveProvider,
    live_plan,
    live_preflight,
    raise_on_contract_failure,
)

pytestmark = pytest.mark.live

LOCAL_PRESET = "local"


@pytest.fixture
def local_preset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Activate the shipped local preset in a throwaway user config.

    The developer's real config is never read or written — the run must be
    reproducible from the shipped defaults, and flipping someone's active
    preset from a test would be a rude side effect that survives the run.
    """
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.delenv("APPDATA", raising=False)
    cached_config.cache_clear()
    save_active_preset(LOCAL_PRESET)
    cached_config.cache_clear()


@pytest.mark.usefixtures("local_preset")
async def test_headless_harness_live(tmp_path: Path) -> None:
    brain = cached_config().tier("brain")
    reason = await live_preflight(brain.base_url, brain.slug)
    if reason is not None:
        pytest.skip(f"live harness not run: {reason}")

    workspace = tmp_path / "workspace"
    provider = LiveProvider(brain.base_url)
    try:
        result = await run(workspace, tmp_path / "data", live_plan(workspace, provider))
    finally:
        await provider.aclose()

    # Attribution before verdict: a provider failure fails every check that
    # depends on it, so asserting first would blame the loop for the shim.
    raise_on_contract_failure(provider)
    assert result.ok, f"harness failed:\n{result.report()}"

    # §2.7 on a live wire: the slug the daemon requested came from config,
    # not from the harness.  Nothing in the report covers this — the checks
    # see events, not requests.
    assert provider.calls[0].model == brain.slug
