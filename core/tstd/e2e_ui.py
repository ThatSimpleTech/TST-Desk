"""Headless daemon for the UI walk.

Playwright drives the Svelte window. This process is the other half: a
real daemon, a mock model, and an in-memory keychain, bound to
127.0.0.1. No API key, no network, no Tauri host.

One JSON object is written to ``--runtime-file`` when ``port.json``
exists. The process then serves until SIGTERM.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import signal
import subprocess
import tempfile
from pathlib import Path

from .daemon import Daemon
from .keychain import KeychainBackend, KeychainError, store_api_key
from .mock import MockProvider, Script

_REPLY = "E2E reply from the mock model."


class MemoryKeychain(KeychainBackend):
    """Process-local keychain. The walk must not touch the login keychain."""

    def __init__(self) -> None:
        self._secrets: dict[str, str] = {}

    def _key(self, account: str, service: str) -> str:
        return f"{service}:{account}"

    async def get_secret(self, account: str, service: str = "com.thatsimpletech.tstdesk") -> str:
        key = self._key(account, service)
        if key not in self._secrets:
            raise KeychainError("API key not found in keychain.")
        return self._secrets[key]

    async def set_secret(
        self, account: str, secret: str, service: str = "com.thatsimpletech.tstdesk"
    ) -> None:
        self._secrets[self._key(account, service)] = secret

    async def delete_secret(
        self, account: str, service: str = "com.thatsimpletech.tstdesk"
    ) -> None:
        key = self._key(account, service)
        if key not in self._secrets:
            raise KeychainError("Failed to delete keychain secret.")
        del self._secrets[key]


def _prepare_workspace(workspace: Path) -> None:
    workspace.mkdir(parents=True)
    (workspace / "AGENTS.md").write_text(
        "# UI walk\n\nAnswer in one short sentence.\n", encoding="utf-8"
    )
    (workspace / "hello.txt").write_text("hello\n", encoding="utf-8")

    def git(*args: str) -> None:
        subprocess.run(
            [
                "git",
                "-C",
                str(workspace),
                "-c",
                "user.name=TST Desk Harness",
                "-c",
                "user.email=harness@localhost",
                *args,
            ],
            check=True,
            capture_output=True,
        )

    git("init", "-q")
    git("add", "AGENTS.md", "hello.txt")
    git("commit", "-q", "-m", "baseline")


def _write_runtime(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _install_keychain() -> None:
    import tstd.keychain as keychain

    keychain._backend = MemoryKeychain()


async def _wait_for_port(data_dir: Path) -> dict[str, object]:
    port_file = data_dir / "port.json"
    for _ in range(200):
        if port_file.exists():
            loaded = json.loads(port_file.read_text(encoding="utf-8"))
            if isinstance(loaded, dict) and "port" in loaded and "token" in loaded:
                return loaded
        await asyncio.sleep(0.05)
    raise TimeoutError("daemon did not write port.json")


async def serve(runtime_file: Path) -> None:
    root = Path(tempfile.mkdtemp(prefix="tstd-e2e-ui-"))
    workspace = root / "workspace"
    data_dir = root / "data"
    data_dir.mkdir()
    _prepare_workspace(workspace)
    _install_keychain()
    await store_api_key("sk-e2e-not-a-real-key")

    daemon = Daemon(
        data_dir=data_dir,
        provider=MockProvider(default=Script(kind="stream", content=_REPLY)),
    )
    task = asyncio.create_task(daemon.run())
    try:
        info = await _wait_for_port(data_dir)
        payload = {
            "port": info["port"],
            "token": info["token"],
            "workspace": str(workspace),
            "reply": _REPLY,
        }
        await asyncio.to_thread(_write_runtime, runtime_file, payload)
        print(json.dumps(payload), flush=True)

        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
        await stop.wait()
    finally:
        daemon._request_shutdown("e2e")
        await asyncio.wait_for(task, timeout=8.0)


def main() -> None:
    parser = argparse.ArgumentParser(description="UI walk daemon")
    parser.add_argument("--runtime-file", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(serve(args.runtime_file))


if __name__ == "__main__":
    main()
