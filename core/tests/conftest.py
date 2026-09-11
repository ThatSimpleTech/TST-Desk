"""Suite-wide isolation from the developer's machine.

``load_config()`` with no path reads ``user_data_dir()/config.yaml``. On CI
that is the shipped default, created on first use; on a developer's Mac it
is whatever the app last saved — an ``engine.kind`` of ``grok`` sends every
daemon test to the real Grok CLI instead of the mock provider. Redirect
``HOME`` to a throwaway per-test directory, the idiom
``test_local_preset_paths._install_config`` already uses, so a local run
sees what CI sees and never rewrites the developer's own files. A module
that redirects ``HOME`` itself still wins: its fixtures run after this one.

The application ping (TD-1716) is a liveness frame, not a reply. A loaded
suite can stall a settings-wire test past 15s, and ``await ws.recv()`` then
returns ``ping`` instead of the ack. Production clients skip it. Tests that
are not about the cadence should not see it either.
"""

from __future__ import annotations

import hashlib
import sys
import tempfile
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from tstd.config import ModelConfig, cached_config
from tstd.context.embeddings import EmbeddingsClient
from tstd.keychain import KeychainError

_KEEP_REAL_PING_INTERVAL = {
    "test_default_interval_leaves_room_for_a_missed_frame",
}

# test_embeddings owns from_config against the shipped sidecar URL.
_KEEP_EMBEDDINGS_FROM_CONFIG = {
    "test_embeddings",
}


@pytest.fixture(autouse=True)
def _throwaway_home(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    # A sibling of tmp_path, not inside it: tests assert tmp_path ends up empty.
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.delenv("APPDATA", raising=False)
    cached_config.cache_clear()
    yield
    cached_config.cache_clear()


@pytest.fixture(autouse=True)
def _quiet_application_pings(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stop the 15s ping unless the test is pinning the real cadence."""
    if request.node.name in _KEEP_REAL_PING_INTERVAL:
        return
    monkeypatch.setattr("tstd.ws.PING_INTERVAL_SECONDS", 0.0)


@pytest.fixture(autouse=True)
def _disable_embeddings_sidecar(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Do not POST the shipped embeddings sidecar from ordinary tests.

    Session open plants ``.tst/memory/*.md`` topics. A brain turn then
    POSTs ``embeddings.base_url`` (``http://127.0.0.1:8080/v1``) with a
    2s connect timeout. CI has no sidecar, so every such turn paid 2s
    and Windows tests with a 3s ``approval_request`` deadline lost the
    race (``test_remote_approve_unparks_the_agent_loop``).
    """
    if request.module.__name__.rsplit(".", 1)[-1] in _KEEP_EMBEDDINGS_FROM_CONFIG:
        return

    def _disabled(_cls: type[EmbeddingsClient], _config: ModelConfig) -> EmbeddingsClient:
        return EmbeddingsClient("", "", timeout_seconds=0.1)

    monkeypatch.setattr(EmbeddingsClient, "from_config", classmethod(_disabled))


@pytest.fixture(autouse=True)
def _redirect_windows_user_data(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``user_data_dir()`` follows ``%APPDATA%`` on Windows, not ``HOME``."""
    if sys.platform != "win32":
        return
    digest = hashlib.sha256(request.node.nodeid.encode()).hexdigest()[:16]
    profile = Path(tempfile.gettempdir()) / "tstd-pytest-profiles" / digest
    roaming = profile / "AppData" / "Roaming"
    local = profile / "AppData" / "Local"
    roaming.mkdir(parents=True, exist_ok=True)
    local.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("USERPROFILE", str(profile))
    monkeypatch.setenv("APPDATA", str(roaming))
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    cached_config.cache_clear()


# Seams that would otherwise call the developer's live keychain. setup_state
# probes every named credential via get_api_key; a locked Secret Service
# waits on a prompt and the suite hangs (TD-1105). test_keychain opts out
# because it is the module that owns the real backends.
_KEYCHAIN_SEAMS = (
    "tstd.keychain.get_api_key",
    "tstd.keychain.store_api_key",
    "tstd.keychain.delete_api_key",
    "tstd.keychain.get_slack_webhook_url",
    "tstd.keychain.store_slack_webhook_url",
    "tstd.keychain.delete_slack_webhook_url",
    "tstd.keychain.get_ntfy_topic_url",
    "tstd.keychain.store_ntfy_topic_url",
    "tstd.keychain.delete_ntfy_topic_url",
    "tstd.keychain.get_discord_webhook_url",
    "tstd.keychain.store_discord_webhook_url",
    "tstd.keychain.delete_discord_webhook_url",
    "tstd.keychain.get_telegram_bot_url",
    "tstd.keychain.store_telegram_bot_url",
    "tstd.keychain.delete_telegram_bot_url",
    "tstd.daemon.get_api_key",
    "tstd.notify.slack.get_slack_webhook_url",
    "tstd.notify.ntfy.get_ntfy_topic_url",
    "tstd.notify.discord.get_discord_webhook_url",
    "tstd.notify.telegram.get_telegram_bot_url",
)


async def _isolated_keychain(*_args: object, **_kwargs: object) -> Any:
    raise KeychainError("keychain isolated in tests")


@pytest.fixture(autouse=True)
def _isolate_live_keychain(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the suite off the host Secret Service / Keychain / CredMan."""
    if request.module.__name__.endswith("test_keychain"):
        return
    isolated: Callable[..., Awaitable[Any]] = _isolated_keychain
    for seam in _KEYCHAIN_SEAMS:
        monkeypatch.setattr(seam, isolated)
