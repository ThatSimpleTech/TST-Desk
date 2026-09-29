"""macOS keychain trust for CLI reads (TD-4838).

TD-4813 stored the secret with ``SecItemAdd`` inside ``tstd`` so it never
appeared in argv. The item's ACL then trusted only that binary. This app
is ad-hoc signed, so that partition is not stable across rebuilds, and
every read still shells out to ``/usr/bin/security``, which was not on the
ACL. A saved key existed and could not be read.

Writes now run ``security -i`` (argv is exactly those two words) and send
one ``add-generic-password`` command on stdin. The creating binary is
``/usr/bin/security``, whose apple-tool partition the CLI can read after a
rebuild. The secret stays off argv. Quoting matches SecurityTool
``split_line``: backslash is an escape inside both quote styles, and
``$`` is not expanded.

A TD-4813 item is deleted in-process first (``tstd`` is the trusted app
for that ACL) and then via the CLI. ``-U`` updates the password but does
not replace an ACL, so delete-then-add is what a re-save repairs. Reads
stay on the CLI. When that fails for an item that exists, an in-process
copy with the UI forced off tries the current binary; if that also fails
the user is told to re-save. Nothing here logs or raises the secret.
"""

from __future__ import annotations

import asyncio
import ctypes
import sys
from ctypes import POINTER, c_char_p, c_int32, c_long, c_uint32, c_void_p
from typing import Any

from .keychain import (
    KeychainError,
    KeychainLockedError,
    _await_cli,
    _classify_cli_failure,
    _spawn_cli,
)

# OSStatus. Not-found is success for the pre-add delete.
_ERR_SEC_SUCCESS = 0
_ERR_SEC_ITEM_NOT_FOUND = -25300
_ERR_SEC_INTERACTION_NOT_ALLOWED = -25308

# kCFStringEncodingUTF8.
_K_CF_ENCODING_UTF8 = 0x08000100

# readline's buffer is 4096 including the NUL it writes. A longer stdin
# line is split and the tail can be stored as a second, truncated item.
_MAX_INTERACTIVE_LINE = 4095

_SERVICE = "com.thatsimpletech.tstdesk"

# Referenced by name inside the copy and delete queries so a test can prove
# the prompt is forced off without loading the Security framework.
_AUTH_UI = "kSecUseAuthenticationUIFail"

_RESAVE = (
    "The stored API key is not readable by this build. Re-save the key in Settings → API keys."
)

_core_foundation: Any = None
_security: Any = None


def quote_arg(value: str) -> str:
    """Quote one word for ``security -i``.

    ``split_line`` treats ``\\`` as an escape inside both quote styles and
    does not expand ``$``. A single-quoted word with ``\\`` and ``'``
    escaped round-trips every other character, including spaces, double
    quotes, and non-ASCII. Newline and NUL cannot be one argument.
    """
    escaped = value.replace("\\", "\\\\").replace("'", "\\'")
    return f"'{escaped}'"


def _reject_unquotable(value: str) -> None:
    if "\n" in value or "\x00" in value:
        raise KeychainError("A keychain value cannot contain a newline or NUL.")


def interactive_add_payload(account: str, secret: str, service: str = _SERVICE) -> bytes:
    """One newline-terminated ``add-generic-password`` command.

    Exactly one line: a missing newline makes readline hit EOF and drop
    the command (exit 0, nothing stored), and a second blank line resets
    ``security -i``'s status so a failed add can exit 0.
    """
    for value in (account, service, secret):
        _reject_unquotable(value)
    label = f"TST Desk {account}"
    line = (
        "add-generic-password -U "
        f"-a {quote_arg(account)} "
        f"-s {quote_arg(service)} "
        f"-l {quote_arg(label)} "
        f"-w {quote_arg(secret)}"
    )
    encoded = line.encode("utf-8")
    if len(encoded) > _MAX_INTERACTIVE_LINE:
        raise KeychainError("API key is too long for the macOS keychain CLI.")
    return encoded + b"\n"


def _redact(text: str, secret: str, payload: bytes) -> str:
    """Drop the secret and the stdin line before any text becomes an error."""
    if secret:
        text = text.replace(secret, "")
    leaked = payload.decode("utf-8", errors="replace")
    return text.replace(leaked, "")


def _raise_if_add_failed(
    returncode: int | None, stderr: bytes, secret: str, payload: bytes
) -> None:
    # A successful add is silent. ``security -i`` can still exit 0 when the
    # sub-command failed, and that failure is the stderr line.
    if returncode == 0 and not stderr.strip():
        return
    text = _redact(stderr.decode(errors="replace"), secret, payload)
    raise _classify_cli_failure(text.strip(), "Failed to store keychain secret")


def _not_found(stderr_text: str) -> bool:
    return "could not be found" in stderr_text or "The specified item" in stderr_text


def _missing_message(account: str, service: str) -> str:
    return (
        "API key not found in keychain. "
        f"Run: security add-generic-password -a '{account}' -s '{service}' -w"
    )


async def _cli_delete_missing_ok(account: str, service: str) -> None:
    """CLI delete. A missing item is the expected case after SecItemDelete.

    Only "could not be found" is success. "The specified item" also matches
    other Security errors, and swallowing those would hide a locked keychain
    and then mis-report the add.
    """
    proc = await _spawn_cli(
        "security",
        "delete-generic-password",
        "-a",
        account,
        "-s",
        service,
    )
    _stdout, stderr = await _await_cli(proc)
    if proc.returncode == 0:
        return
    text = stderr.decode(errors="replace")
    if "could not be found" in text:
        return
    raise _classify_cli_failure(text.strip(), "Failed to delete keychain secret")


async def _delete_in_process(service: str, account: str) -> None:
    # tstd can delete the items it created. Anything else (not trusted,
    # framework missing) falls through to the CLI, which can delete items
    # this binary is not on the ACL for. Never prompt.
    try:
        await asyncio.to_thread(_sec_item_delete, service, account)
    except (KeychainError, OSError, AttributeError, ValueError, TypeError):
        return


async def write_secret(account: str, secret: str, service: str = _SERVICE) -> None:
    """Replace the item with one ``/usr/bin/security`` can read."""
    payload = interactive_add_payload(account, secret, service)
    await _delete_in_process(service, account)
    await _cli_delete_missing_ok(account, service)
    proc = await _spawn_cli("security", "-i", stdin=True)
    _stdout, stderr = await _await_cli(proc, stdin=payload)
    _raise_if_add_failed(proc.returncode, stderr, secret, payload)


async def secret_exists(account: str, service: str = _SERVICE) -> bool:
    """Attributes-only lookup. No ``-w``, so this does not prompt or return the secret."""
    proc = await _spawn_cli(
        "security",
        "find-generic-password",
        "-a",
        account,
        "-s",
        service,
    )
    _stdout, stderr = await _await_cli(proc)
    if proc.returncode == 0:
        return True
    text = stderr.decode(errors="replace")
    # "The specified item" also matches errors that are not a missing item.
    # Only the not-found phrase means the account is absent.
    if "could not be found" in text:
        return False
    raise _classify_cli_failure(text.strip(), "Failed to read keychain")


async def _copy_if_present(account: str, service: str, original: KeychainError) -> str:
    try:
        exists = await secret_exists(account, service)
    except KeychainLockedError:
        raise
    except KeychainError:
        raise original from None
    if not exists:
        raise original from None
    try:
        blob = await asyncio.to_thread(_sec_item_copy, service, account)
    except (KeychainError, OSError, AttributeError, ValueError, TypeError):
        blob = None
    if blob is None:
        raise KeychainError(_RESAVE) from None
    try:
        return blob.decode("utf-8")
    except UnicodeDecodeError:
        raise KeychainError(_RESAVE) from None


async def read_secret(account: str, service: str = _SERVICE) -> str:
    """CLI read, then an in-process copy when the item exists but ``-w`` cannot.

    Not-found stays not-found: there is nothing to fall back to. A timeout
    or an ACL denial on ``-w`` tries the attributes probe first. If that
    probe is itself a lock, the lock wins over the re-save message.
    """
    proc = await _spawn_cli(
        "security",
        "find-generic-password",
        "-a",
        account,
        "-s",
        service,
        "-w",
    )
    try:
        stdout, stderr = await _await_cli(proc)
    except KeychainLockedError as locked:
        return await _copy_if_present(account, service, locked)
    if proc.returncode == 0:
        return stdout.decode().strip()
    stderr_text = stderr.decode(errors="replace").strip()
    if _not_found(stderr_text):
        raise KeychainError(_missing_message(account, service))
    err = _classify_cli_failure(stderr_text, "Failed to read keychain")
    return await _copy_if_present(account, service, err)


def _libs() -> tuple[Any, Any]:
    """Load CoreFoundation + Security once. Import stays clean off darwin."""
    global _core_foundation, _security
    if _security is None:
        if sys.platform != "darwin":
            raise KeychainError("the macOS keychain path requires darwin")
        cf = ctypes.cdll.LoadLibrary(
            "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
        )
        sec = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/Security.framework/Security")
        cf.CFStringCreateWithCString.argtypes = [c_void_p, c_char_p, c_uint32]
        cf.CFStringCreateWithCString.restype = c_void_p
        cf.CFDictionaryCreateMutable.argtypes = [c_void_p, c_long, c_void_p, c_void_p]
        cf.CFDictionaryCreateMutable.restype = c_void_p
        cf.CFDictionarySetValue.argtypes = [c_void_p, c_void_p, c_void_p]
        cf.CFDictionarySetValue.restype = None
        cf.CFDataGetBytePtr.argtypes = [c_void_p]
        cf.CFDataGetBytePtr.restype = c_void_p
        cf.CFDataGetLength.argtypes = [c_void_p]
        cf.CFDataGetLength.restype = c_long
        cf.CFRelease.argtypes = [c_void_p]
        cf.CFRelease.restype = None
        sec.SecItemDelete.argtypes = [c_void_p]
        sec.SecItemDelete.restype = c_int32
        sec.SecItemCopyMatching.argtypes = [c_void_p, POINTER(c_void_p)]
        sec.SecItemCopyMatching.restype = c_int32
        _core_foundation, _security = cf, sec
    return _core_foundation, _security


def _put(cf: Any, query: Any, sec: Any, key: str, value: Any) -> None:
    cf.CFDictionarySetValue(query, c_void_p.in_dll(sec, key), value)


def _base_query(cf: Any, sec: Any, service: str, account: str) -> tuple[Any, list[Any]]:
    query = cf.CFDictionaryCreateMutable(
        None,
        0,
        c_void_p.in_dll(cf, "kCFCopyStringDictionaryKeyCallBacks"),
        c_void_p.in_dll(cf, "kCFTypeDictionaryValueCallBacks"),
    )
    service_ref = cf.CFStringCreateWithCString(None, service.encode("utf-8"), _K_CF_ENCODING_UTF8)
    account_ref = cf.CFStringCreateWithCString(None, account.encode("utf-8"), _K_CF_ENCODING_UTF8)
    owned: list[Any] = [query, service_ref, account_ref]
    _put(cf, query, sec, "kSecClass", c_void_p.in_dll(sec, "kSecClassGenericPassword"))
    _put(cf, query, sec, "kSecAttrService", service_ref)
    _put(cf, query, sec, "kSecAttrAccount", account_ref)
    return query, owned


def _release(cf: Any, refs: list[Any]) -> None:
    for ref in refs:
        if ref:
            cf.CFRelease(ref)


def _sec_item_delete(service: str, account: str) -> int:
    """Delete without a GUI prompt. The caller treats every status as non-fatal."""
    cf, sec = _libs()
    query, owned = _base_query(cf, sec, service, account)
    try:
        _put(cf, query, sec, "kSecUseAuthenticationUI", c_void_p.in_dll(sec, _AUTH_UI))
        return int(sec.SecItemDelete(query))
    finally:
        _release(cf, owned)


def _sec_item_copy(service: str, account: str) -> bytes | None:
    """Return the secret bytes, or None when the item cannot be read without a prompt.

    ``kSecUseAuthenticationUIFail`` is mandatory: a GUI prompt stalls the
    daemon the same way a hung ``security -w`` does.
    """
    cf, sec = _libs()
    query, owned = _base_query(cf, sec, service, account)
    result = c_void_p()
    try:
        _put(cf, query, sec, "kSecReturnData", c_void_p.in_dll(cf, "kCFBooleanTrue"))
        _put(cf, query, sec, "kSecMatchLimit", c_void_p.in_dll(sec, "kSecMatchLimitOne"))
        _put(cf, query, sec, "kSecUseAuthenticationUI", c_void_p.in_dll(sec, _AUTH_UI))
        status = int(sec.SecItemCopyMatching(query, ctypes.byref(result)))
        if status != _ERR_SEC_SUCCESS or not result.value:
            return None
        length = int(cf.CFDataGetLength(result))
        if length <= 0:
            return b"" if length == 0 else None
        ptr = cf.CFDataGetBytePtr(result)
        if not ptr:
            return None
        return bytes(ctypes.string_at(ptr, length))
    finally:
        if result.value:
            cf.CFRelease(result)
        _release(cf, owned)
