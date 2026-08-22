"""macOS Security-framework write path (TD-4813).

``security add-generic-password -w <secret>`` puts the API key in the
process argument list, where any same-user process can read it with ``ps``
for the lifetime of the call. This module writes through the Security
framework instead — ``SecItemAdd`` with the secret as in-memory CFData, no
subprocess on the write path at all. Zero-dependency like every other
backend: raw ``ctypes`` against the system frameworks, matching the
Windows backend's precedent in ``keychain_windows``.

Only the write lives here. Reads and deletes stay on the ``security`` CLI
in :mod:`tstd.keychain` — their argv carries only account and service
names, which are not secrets. The item lands in the default (login)
keychain, the same store the CLI reads, so both paths see one item.

Imported lazily by ``keychain.MacOSKeychain.set_secret``. The module itself
must import cleanly on every platform — all framework loading happens
inside functions, so tests elsewhere can monkeypatch the ``_sec_add`` seam
without a Mac.
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import c_char_p, c_int32, c_long, c_uint32, c_void_p
from typing import Any

from .keychain import _LOCKED_GUIDANCE, KeychainError, KeychainLockedError

# OSStatus codes from SecBase.h.
_ERR_SEC_SUCCESS = 0
_ERR_SEC_DUPLICATE_ITEM = -25299
_ERR_SEC_INTERACTION_NOT_ALLOWED = -25308  # locked keychain, unlock UI barred

# kCFStringEncodingUTF8.
_K_CF_ENCODING_UTF8 = 0x08000100

_core_foundation: Any = None
_security: Any = None


def _libs() -> tuple[Any, Any]:
    """Load CoreFoundation + Security with signatures declared, once.

    Loading happens here rather than at import so non-Mac platforms never
    touch the frameworks and tests can patch the seams freely.
    """
    global _core_foundation, _security
    if _security is None:
        if sys.platform != "darwin":
            raise KeychainError("the macOS keychain write path requires darwin")
        cf = ctypes.cdll.LoadLibrary(
            "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
        )
        sec = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/Security.framework/Security")
        cf.CFStringCreateWithCString.argtypes = [c_void_p, c_char_p, c_uint32]
        cf.CFStringCreateWithCString.restype = c_void_p
        cf.CFDataCreate.argtypes = [c_void_p, c_char_p, c_long]
        cf.CFDataCreate.restype = c_void_p
        cf.CFDictionaryCreateMutable.argtypes = [c_void_p, c_long, c_void_p, c_void_p]
        cf.CFDictionaryCreateMutable.restype = c_void_p
        cf.CFDictionarySetValue.argtypes = [c_void_p, c_void_p, c_void_p]
        cf.CFDictionarySetValue.restype = None
        cf.CFRelease.argtypes = [c_void_p]
        cf.CFRelease.restype = None
        sec.SecItemAdd.argtypes = [c_void_p, c_void_p]
        sec.SecItemAdd.restype = c_int32
        _core_foundation, _security = cf, sec
    return _core_foundation, _security


def _sec_item_add(service: str, account: str, secret: str) -> int:
    """Build the generic-password query dict and call ``SecItemAdd``.

    Returns the raw ``OSStatus`` so the exception mapping stays testable off
    macOS. CF objects created here are released in ``finally`` — the
    dictionary retains its values under ``kCFTypeDictionaryValueCallBacks``,
    so these owned references are ours alone.
    """
    cf, sec = _libs()
    query_keys = [
        c_void_p.in_dll(sec, name)
        for name in ("kSecClass", "kSecAttrService", "kSecAttrAccount", "kSecValueData")
    ]
    query = cf.CFDictionaryCreateMutable(
        None,
        0,
        c_void_p.in_dll(cf, "kCFCopyStringDictionaryKeyCallBacks"),
        c_void_p.in_dll(cf, "kCFTypeDictionaryValueCallBacks"),
    )
    service_ref = cf.CFStringCreateWithCString(None, service.encode("utf-8"), _K_CF_ENCODING_UTF8)
    account_ref = cf.CFStringCreateWithCString(None, account.encode("utf-8"), _K_CF_ENCODING_UTF8)
    secret_bytes = secret.encode("utf-8")
    secret_ref = cf.CFDataCreate(None, secret_bytes, len(secret_bytes))
    try:
        cf.CFDictionarySetValue(
            query, query_keys[0], c_void_p.in_dll(sec, "kSecClassGenericPassword")
        )
        cf.CFDictionarySetValue(query, query_keys[1], service_ref)
        cf.CFDictionarySetValue(query, query_keys[2], account_ref)
        cf.CFDictionarySetValue(query, query_keys[3], secret_ref)
        status: int = sec.SecItemAdd(query, None)
    finally:
        for ref in (query, service_ref, account_ref, secret_ref):
            if ref:
                cf.CFRelease(ref)
    return status


def _sec_add(service: str, account: str, secret: str) -> None:
    """Store one generic password via the framework; map failures to our errors."""
    status = _sec_item_add(service, account, secret)
    if status == _ERR_SEC_SUCCESS:
        return
    if status == _ERR_SEC_INTERACTION_NOT_ALLOWED:
        raise KeychainLockedError(_LOCKED_GUIDANCE)
    if status == _ERR_SEC_DUPLICATE_ITEM:
        # The caller deletes first, so this means the pre-clean failed and an
        # old entry is still in the way — say so rather than looping.
        raise KeychainError(
            f"keychain already holds an entry for {account!r} that could not be replaced"
        )
    raise KeychainError(f"Failed to store keychain secret: OSStatus {status}")
