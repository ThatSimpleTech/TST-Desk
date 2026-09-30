"""OS keychain abstraction for secure credential storage.

API keys live in the OS keychain, never in config files or environment variables
(prime directive §2.2). This module provides a platform-agnostic interface for
reading and writing secrets.

Supported platforms:
- macOS: `security` CLI. Writes go through `security -i` with the add
  command on stdin (keychain_macos, TD-4838) so the secret never rides in
  argv and `/usr/bin/security` stays the trusted reader
- Linux: `secret-tool` CLI (libsecret); the secret travels over stdin
- Windows: Credential Manager via ctypes (keychain_windows, TD-1102)

The service name is always ``com.thatsimpletech.tstdesk``.
"""

from __future__ import annotations

import asyncio
import sys
from abc import ABC, abstractmethod

from .proc_lifecycle import finish_subprocess


class KeychainError(Exception):
    """Raised when a keychain operation fails."""


class KeychainLockedError(KeychainError):
    """The login keychain is locked or its password drifted (TD-1105).

    A macOS password change (notably on AD-bound Macs) leaves the login
    keychain on the old password: every ``security`` call fails with
    "user name or passphrase not correct" until it is re-keyed.  A locked
    Secret Service collection is the Linux equivalent.  The daemon maps
    this to the ``keychain_locked`` error code so the UI can show unlock
    guidance instead of raw CLI stderr.
    """


# stderr substrings that mean "keychain is locked / password drifted",
# matched case-insensitively.
_LOCKED_MARKERS = (
    "user name or passphrase",
    "interaction is not allowed",  # errSecInteractionNotAllowed — unlock UI barred
    "interaction not allowed",
    "is locked",
    "locked collection",
)

_LOCKED_GUIDANCE = (
    "The login keychain is locked — most often a macOS password change left "
    "the keychain on the old password. Open Keychain Access, unlock the login "
    "keychain (or update its password to the current login password), then retry."
)

# Bound so a locked Secret Service / security prompt cannot stall the
# daemon (or the suite) forever. Unlock-and-retry is the TD-1105 path.
_CLI_TIMEOUT_SECS = 5.0
_TIMEOUT_GUIDANCE = (
    "The login keychain did not respond in time. It is probably locked "
    "or waiting on an unlock prompt. Unlock it (Keychain Access on "
    "macOS, the login keyring on Linux) and retry."
)


async def _await_cli(
    proc: asyncio.subprocess.Process,
    stdin: bytes | None = None,
) -> tuple[bytes, bytes]:
    """``communicate`` with a deadline; a hung CLI is a locked keychain."""
    try:
        # The post-kill wait stays at one second: a CLI that ignores
        # SIGKILL is already a locked keychain, and the suite's hung-CLI
        # stand-in must not pick up the longer default grace.
        return await finish_subprocess(
            proc, timeout=_CLI_TIMEOUT_SECS, stdin=stdin, reap_timeout=1.0
        )
    except TimeoutError:
        raise KeychainLockedError(_TIMEOUT_GUIDANCE) from None


async def _spawn_cli(*args: str, stdin: bool = False) -> asyncio.subprocess.Process:
    """Start a keychain helper. A missing binary is a KeychainError, not a crash.

    ``setup_state`` probes every named credential. On a clean Linux guest
    ``secret-tool`` is often absent; FileNotFoundError used to kill the
    WebSocket handler and leave the window on Connecting….
    """
    try:
        if stdin:
            return await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.PIPE,
            )
        return await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        name = args[0] if args else "keychain helper"
        if name == "secret-tool":
            hint = "Install libsecret-tools and retry."
        elif name == "security":
            hint = "The macOS security CLI was not found."
        else:
            hint = f"{name} was not found."
        raise KeychainError(f"Keychain helper {name!r} is not installed. {hint}") from exc


def _classify_cli_failure(stderr_text: str, fallback: str) -> KeychainError:
    """Map keychain-CLI stderr to the right error type.

    Locked/drifted keychains get KeychainLockedError with unlock guidance;
    anything else keeps the raw stderr in a plain KeychainError.
    """
    low = stderr_text.lower()
    if any(marker in low for marker in _LOCKED_MARKERS):
        return KeychainLockedError(_LOCKED_GUIDANCE)
    return KeychainError(f"{fallback}: {stderr_text}")


class KeychainBackend(ABC):
    """Platform-specific keychain backend."""

    @abstractmethod
    async def get_secret(self, account: str, service: str = "com.thatsimpletech.tstdesk") -> str:
        """Retrieve a secret from the keychain.

        Raises:
            KeychainError: If the secret is not found or retrieval fails.
        """
        ...

    @abstractmethod
    async def set_secret(
        self, account: str, secret: str, service: str = "com.thatsimpletech.tstdesk"
    ) -> None:
        """Store a secret in the keychain.

        Raises:
            KeychainError: If storage fails.
        """
        ...

    @abstractmethod
    async def delete_secret(
        self, account: str, service: str = "com.thatsimpletech.tstdesk"
    ) -> None:
        """Delete a secret from the keychain.

        Raises:
            KeychainError: If the secret is not found or deletion fails.
        """
        ...

    async def has_secret(self, account: str, service: str = "com.thatsimpletech.tstdesk") -> bool:
        """Whether an item exists. The default reads the secret (Linux/Windows).

        macOS overrides this with an attributes-only lookup so presence never
        reads the password or raises an ACL prompt (TD-4838).
        """
        try:
            await self.get_secret(account, service)
        except KeychainError:
            return False
        return True


class MacOSKeychain(KeychainBackend):
    """macOS keychain via the `security` CLI, with trust `security` can read."""

    async def get_secret(self, account: str, service: str = "com.thatsimpletech.tstdesk") -> str:
        from . import keychain_macos

        return await keychain_macos.read_secret(account, service)

    async def set_secret(
        self, account: str, secret: str, service: str = "com.thatsimpletech.tstdesk"
    ) -> None:
        from . import keychain_macos

        await keychain_macos.write_secret(account, secret, service)

    async def has_secret(self, account: str, service: str = "com.thatsimpletech.tstdesk") -> bool:
        from . import keychain_macos

        return await keychain_macos.secret_exists(account, service)

    async def delete_secret(
        self, account: str, service: str = "com.thatsimpletech.tstdesk"
    ) -> None:
        proc = await _spawn_cli(
            "security",
            "delete-generic-password",
            "-a",
            account,
            "-s",
            service,
        )
        _, stderr = await _await_cli(proc)
        if proc.returncode != 0:
            raise _classify_cli_failure(stderr.decode().strip(), "Failed to delete keychain secret")


class LinuxSecretService(KeychainBackend):
    """Linux keychain via `secret-tool` (libsecret)."""

    async def get_secret(self, account: str, service: str = "com.thatsimpletech.tstdesk") -> str:
        proc = await _spawn_cli(
            "secret-tool",
            "lookup",
            "service",
            service,
            "account",
            account,
        )
        stdout, stderr = await _await_cli(proc)
        if proc.returncode != 0:
            stderr_text = stderr.decode().strip()
            if "not found" in stderr_text or "does not exist" in stderr_text:
                raise KeychainError(
                    f"API key not found in keychain. "
                    f"Run: secret-tool store "
                    f"--label='TST Desk {account}' "
                    f"service '{service}' account '{account}'"
                )
            raise _classify_cli_failure(stderr_text, "Failed to read keychain")
        value = stdout.decode().strip()
        if not value:
            raise KeychainError("API key not found in keychain (empty value).")
        return value

    async def set_secret(
        self, account: str, secret: str, service: str = "com.thatsimpletech.tstdesk"
    ) -> None:
        proc = await _spawn_cli(
            "secret-tool",
            "store",
            "--label",
            f"TST Desk {account}",
            "service",
            service,
            "account",
            account,
            stdin=True,
        )
        _, stderr = await _await_cli(proc, stdin=secret.encode())
        if proc.returncode != 0:
            raise _classify_cli_failure(stderr.decode().strip(), "Failed to store keychain secret")

    async def delete_secret(
        self, account: str, service: str = "com.thatsimpletech.tstdesk"
    ) -> None:
        proc = await _spawn_cli(
            "secret-tool",
            "clear",
            "service",
            service,
            "account",
            account,
        )
        _, stderr = await _await_cli(proc)
        if proc.returncode != 0:
            raise _classify_cli_failure(stderr.decode().strip(), "Failed to delete keychain secret")


def _detect_backend() -> KeychainBackend:
    """Detect the appropriate keychain backend for the current platform."""
    system = sys.platform
    if system == "darwin":
        return MacOSKeychain()

    # On Linux, check if secret-tool is available
    if system in ("linux", "linux2"):
        # Use a simpler check: just try to use secret-tool
        # The actual availability check happens at call time
        return LinuxSecretService()

    if system == "win32":
        # Lazy import: keychain_windows touches ctypes.windll at call time,
        # but keeping the import here too means non-Windows platforms never
        # load the module.
        from .keychain_windows import WindowsCredentialManager

        return WindowsCredentialManager()

    raise KeychainError(f"Unsupported platform: {system}.")


# Module-level singleton backend
_backend: KeychainBackend | None = None


def _get_backend() -> KeychainBackend:
    global _backend
    if _backend is None:
        _backend = _detect_backend()
    return _backend


# ── Public API ─────────────────────────────────────────────────────────


def _reject_api_key_value(secret: str) -> None:
    """Refuse a value ``security find-generic-password -w`` cannot echo.

    That flag prints any byte outside printable ASCII (0x20-0x7E) as hex.
    A real key can be all hex, so the read path must not decode it. An
    empty or whitespace-only value would leave the provider as
    ``Authorization: Bearer ``. The message never includes the secret.
    """
    if "\n" in secret or "\x00" in secret:
        raise KeychainError("A keychain value cannot contain a newline or NUL.")
    if not secret.strip():
        raise KeychainError("API key is empty")
    if any(ord(ch) < 0x20 or ord(ch) > 0x7E for ch in secret):
        raise KeychainError("API keys must be printable ASCII")


def _require_api_key_value(value: str, name: str) -> str:
    """Treat an empty or whitespace-only secret as missing.

    ``name`` is the provider id or the keychain account. It is not the secret.
    """
    if value.strip():
        return value
    raise KeychainError(
        f"the stored API key for '{name}' is empty — re-save it in Settings → API keys"
    )


async def api_key_is_stored(provider_name: str = "openrouter") -> bool:
    """Whether ``tst-{provider_name}`` exists, without reading the secret.

    A full read prompts when the item's ACL trusts only an older ``tstd``
    binary, which made a saved key look missing (TD-4838). Callers that
    need the value still use :func:`get_api_key`.
    """
    backend = _get_backend()
    return await backend.has_secret(f"tst-{provider_name}")


async def get_api_key(provider_name: str = "openrouter") -> str:
    """Retrieve an API key from the OS keychain.

    The key is stored with account name ``tst-{provider_name}``
    under the service ``com.thatsimpletech.tstdesk``.

    Args:
        provider_name: The provider name (e.g. ``openrouter``, ``openai``).

    Returns:
        The API key string.

    Raises:
        KeychainError: If the key is not found, empty, or retrieval fails.
            An empty or whitespace-only item uses the not-found family so it
            is never returned as a bearer token.
    """
    backend = _get_backend()
    value = await backend.get_secret(f"tst-{provider_name}")
    return _require_api_key_value(value, provider_name)


async def store_api_key(api_key: str, provider_name: str = "openrouter") -> None:
    """Store an API key in the OS keychain.

    The key is stored with account name ``tst-{provider_name}``
    under the service ``com.thatsimpletech.tstdesk``.

    Args:
        api_key: The API key to store.
        provider_name: The provider name (e.g. ``openrouter``, ``openai``).

    Raises:
        KeychainError: If the value is empty, not printable ASCII, or storage fails.
            The message does not include the secret.
    """
    _reject_api_key_value(api_key)
    backend = _get_backend()
    await backend.set_secret(f"tst-{provider_name}", api_key)


async def delete_api_key(provider_name: str = "openrouter") -> None:
    """Delete an API key from the OS keychain.

    Args:
        provider_name: The provider name (e.g. ``openrouter``, ``openai``).

    Raises:
        KeychainError: If deletion fails.
    """
    backend = _get_backend()
    await backend.delete_secret(f"tst-{provider_name}")


SLACK_WEBHOOK_ACCOUNT = "tst-slack-webhook"


async def get_slack_webhook_url() -> str:
    """Retrieve the Slack incoming-webhook URL from the OS keychain.

    Stored under account ``tst-slack-webhook``. The URL is a secret —
    callers must not write it to config, logs, or the audit database.
    """
    backend = _get_backend()
    return await backend.get_secret(SLACK_WEBHOOK_ACCOUNT)


async def store_slack_webhook_url(url: str) -> None:
    """Store the Slack incoming-webhook URL in the OS keychain."""
    backend = _get_backend()
    await backend.set_secret(SLACK_WEBHOOK_ACCOUNT, url)


async def delete_slack_webhook_url() -> None:
    """Delete the Slack incoming-webhook URL from the OS keychain."""
    backend = _get_backend()
    await backend.delete_secret(SLACK_WEBHOOK_ACCOUNT)


NTFY_TOPIC_ACCOUNT = "tst-ntfy-topic"


async def get_ntfy_topic_url() -> str:
    """Retrieve the ntfy topic URL from the OS keychain.

    Stored under account ``tst-ntfy-topic``. The URL is a secret —
    callers must not write it to config, logs, or the audit database.
    """
    backend = _get_backend()
    return await backend.get_secret(NTFY_TOPIC_ACCOUNT)


async def store_ntfy_topic_url(url: str) -> None:
    """Store the ntfy topic URL in the OS keychain."""
    backend = _get_backend()
    await backend.set_secret(NTFY_TOPIC_ACCOUNT, url)


async def delete_ntfy_topic_url() -> None:
    """Delete the ntfy topic URL from the OS keychain."""
    backend = _get_backend()
    await backend.delete_secret(NTFY_TOPIC_ACCOUNT)


DISCORD_WEBHOOK_ACCOUNT = "tst-discord-webhook"


async def get_discord_webhook_url() -> str:
    """Retrieve the Discord incoming-webhook URL from the OS keychain.

    Stored under account ``tst-discord-webhook``. The URL is a secret —
    callers must not write it to config, logs, or the audit database.
    """
    backend = _get_backend()
    return await backend.get_secret(DISCORD_WEBHOOK_ACCOUNT)


async def store_discord_webhook_url(url: str) -> None:
    """Store the Discord incoming-webhook URL in the OS keychain."""
    backend = _get_backend()
    await backend.set_secret(DISCORD_WEBHOOK_ACCOUNT, url)


async def delete_discord_webhook_url() -> None:
    """Delete the Discord incoming-webhook URL from the OS keychain."""
    backend = _get_backend()
    await backend.delete_secret(DISCORD_WEBHOOK_ACCOUNT)


TELEGRAM_BOT_ACCOUNT = "tst-telegram-bot"


async def get_telegram_bot_url() -> str:
    """Retrieve the Telegram bot sendMessage URL from the OS keychain.

    Stored under account ``tst-telegram-bot``. The URL is a secret —
    callers must not write it to config, logs, or the audit database.
    """
    backend = _get_backend()
    return await backend.get_secret(TELEGRAM_BOT_ACCOUNT)


async def store_telegram_bot_url(url: str) -> None:
    """Store the Telegram bot sendMessage URL in the OS keychain."""
    backend = _get_backend()
    await backend.set_secret(TELEGRAM_BOT_ACCOUNT, url)


async def delete_telegram_bot_url() -> None:
    """Delete the Telegram bot sendMessage URL from the OS keychain."""
    backend = _get_backend()
    await backend.delete_secret(TELEGRAM_BOT_ACCOUNT)


def has_keychain_backend() -> bool:
    """Check if a keychain backend is available for this platform."""
    try:
        _detect_backend()
        return True
    except (KeychainError, ImportError, OSError):
        return False
