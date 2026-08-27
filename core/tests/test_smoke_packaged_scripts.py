"""Packaged clean-guest smoke scripts (TD-4906)."""

from __future__ import annotations

import asyncio
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS = ROOT / "core" / "scripts"
E2E = SCRIPTS / "smoke_linux_e2e.py"


def test_smoke_scripts_exist() -> None:
    assert (SCRIPTS / "smoke_linux_bundle.sh").is_file()
    assert (SCRIPTS / "smoke_macos_bundle.sh").is_file()
    assert (SCRIPTS / "smoke_windows_bundle.ps1").is_file()
    assert E2E.is_file()


def test_probe_keychain_flag_requires_port_file(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(E2E),
            "--probe-keychain",
            "--workspace",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert "--port-file" in result.stderr


def test_macos_bundle_refuses_non_darwin() -> None:
    if sys.platform == "darwin":
        return
    result = subprocess.run(
        ["bash", str(SCRIPTS / "smoke_macos_bundle.sh")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "macOS" in result.stderr


def _load_e2e_module():
    spec = importlib.util.spec_from_file_location("smoke_linux_e2e", E2E)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.asyncio
async def test_probe_keychain_on_clean_daemon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--probe-keychain`` must finish typed on a daemon with no stored key."""
    from tests.test_setup_state import (
        FakeKeychain,
        FakeProviderClient,
        _start_daemon,
        _stop_daemon,
    )
    from tstd.config import cached_config
    from tstd.keychain import KeychainError

    fk = FakeKeychain()

    async def _get(provider_name: str = "openrouter") -> str:
        return await fk.get(provider_name)

    async def _store(api_key: str, provider_name: str = "openrouter") -> None:
        await fk.store(api_key, provider_name)

    async def _delete(provider_name: str = "openrouter") -> None:
        await fk.delete(provider_name)

    monkeypatch.setattr("tstd.daemon.get_api_key", _get)
    monkeypatch.setattr("tstd.daemon.store_api_key", _store)
    monkeypatch.setattr("tstd.daemon.delete_api_key", _delete)
    monkeypatch.setattr("tstd.daemon.ProviderClient", FakeProviderClient)
    FakeProviderClient.reset()
    FakeProviderClient.raise_on_build = KeychainError("API key not found in keychain.")
    cached_config.cache_clear()

    e2e = _load_e2e_module()
    _daemon, task = await _start_daemon(tmp_path)
    try:
        port_file = tmp_path / "port.json"
        for _ in range(50):
            if port_file.is_file():
                break
            await asyncio.sleep(0.05)
        assert port_file.is_file()
        await asyncio.to_thread(e2e.probe_keychain, port_file)
    finally:
        await _stop_daemon(task)
