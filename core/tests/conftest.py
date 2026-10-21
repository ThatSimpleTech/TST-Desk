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
import os
import sys
import tempfile
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.catalog_helpers import catalog_config_yaml
from tstd.config import ModelConfig, cached_config
from tstd.context import calibration as calibration_module
from tstd.context.calibration import CalibrationKey, CalibrationStore
from tstd.context.embeddings import EmbeddingsClient
from tstd.keychain import KeychainError
from tstd.logging import LINUX_DATA_DIR_NAME, LINUX_LEGACY_DATA_DIR_NAME, user_data_dir
from tstd.tools.web_page import PAGE_CACHE

_KEEP_REAL_PING_INTERVAL = {
    "test_default_interval_leaves_room_for_a_missed_frame",
}

# test_embeddings owns from_config against the shipped sidecar URL.
_KEEP_EMBEDDINGS_FROM_CONFIG = {
    "test_embeddings",
}


def _real_user_data_dirs() -> tuple[Path, ...]:
    """The developer's own data folders, worked out with no side effect (TD-5013).

    On Linux ``user_data_dir()`` renames a leftover reverse-DNS folder to
    ``tst-desk``, so calling it here would change the real data area
    just by collecting the suite. Both Linux names are guarded instead.
    The macOS and Windows branches only build a path.
    """
    if sys.platform in ("darwin", "win32"):
        return (user_data_dir(),)
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return (base / LINUX_DATA_DIR_NAME, base / LINUX_LEGACY_DATA_DIR_NAME)


# Read at import, before any fixture moves HOME. Nothing in the suite may
# persist into these (TD-5013).
_REAL_USER_DATA_DIRS = tuple(d.resolve() for d in _real_user_data_dirs())


@pytest.fixture(scope="session")
def _seeded_config(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The shipped config with the test catalog spliced over ``presets: {}``."""
    path = tmp_path_factory.mktemp("seed") / "config.yaml"
    path.write_text(catalog_config_yaml(), encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def _seed_test_catalog(
    request: pytest.FixtureRequest, _seeded_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """New user configs get the test catalog, since none ships (TD-5172).

    A test marked ``shipped_config`` sees the real, empty shipped catalog.
    """
    if request.node.get_closest_marker("shipped_config") is not None:
        return
    import tstd.config as config_module

    monkeypatch.setattr(config_module, "_seed_source", lambda: _seeded_config)


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


class _NonLearningStore(CalibrationStore):
    """A calibration store that never learns a ratio."""

    def observe(
        self, key: CalibrationKey, raw_estimate: int, prompt_tokens: int | None
    ) -> float | None:
        return None


@pytest.fixture(autouse=True)
def _no_token_calibration_from_canned_usage(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Loops in the suite do not learn token ratios by default (TD-5007).

    ``MockProvider`` reports the same canned ``prompt_tokens`` whatever
    was sent, so a ratio learned from it is not a measurement. It would
    also leak: the store is daemon-wide and most tests share one mock
    endpoint. Every test gets its own store that never learns. Tests of
    calibration inject a real ``CalibrationStore`` into ``agent_loop`` or
    patch this one back.

    A daemon started in-process turns persistence on for this per-test
    store (TD-5013). Its data dir is the test's own or, with HOME moved
    by ``_throwaway_home``, a throwaway one, and a store that never
    learns never saves. The teardown fails any test that left the
    daemon-wide store pointed into the developer's real data folder.
    """
    monkeypatch.setattr(calibration_module, "_PROCESS_STORE", _NonLearningStore())
    yield
    path = calibration_module.process_calibration_store().path
    assert path is None or not any(
        path.resolve().is_relative_to(real) for real in _REAL_USER_DATA_DIRS
    ), f"a test pointed token calibration at the real data folder: {path}"


@pytest.fixture(autouse=True)
def _empty_page_cache() -> Iterator[None]:
    """web_fetch's page cache is daemon-wide (TD-5114); tests share URLs."""
    PAGE_CACHE.clear()
    yield
    PAGE_CACHE.clear()


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
# probes every named credential; a locked Secret Service waits on a prompt
# and the suite hangs (TD-1105). test_keychain* opts out because those
# modules own the backends and fake every spawn themselves.
_KEYCHAIN_SEAMS = (
    "tstd.keychain.api_key_is_stored",
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
    # TD-5015: paired computers' keys. Callers reach them through the
    # module (``keychain.get_machine_key``), so these are the only names.
    "tstd.keychain.store_machine_key",
    "tstd.keychain.get_machine_key",
    "tstd.keychain.delete_machine_key",
    "tstd.keychain.get_smtp_password",
    "tstd.keychain.store_smtp_password",
    "tstd.keychain.smtp_password_is_stored",
    "tstd.keychain.get_github_oauth",
    "tstd.keychain.store_github_oauth",
    "tstd.keychain.delete_github_oauth",
    "tstd.keychain.github_oauth_is_stored",
    # TD-5110: MCP server values. Callers reach them through the module.
    "tstd.keychain_mcp.store_mcp_secret",
    "tstd.keychain_mcp.get_mcp_secret",
    "tstd.keychain_mcp.delete_mcp_secret",
    "tstd.keychain_mcp.mcp_secret_is_stored",
    "tstd.daemon.api_key_is_stored",
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
    module_name = request.module.__name__.rsplit(".", 1)[-1]
    if module_name == "test_keychain" or module_name.startswith("test_keychain_"):
        return
    isolated: Callable[..., Awaitable[Any]] = _isolated_keychain
    for seam in _KEYCHAIN_SEAMS:
        monkeypatch.setattr(seam, isolated)
