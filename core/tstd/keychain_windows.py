"""Windows Credential Manager backend (TD-1102).

Zero-dependency, matching the CLI backends' precedent: advapi32
``CredReadW`` / ``CredWriteW`` / ``CredDeleteW`` via stdlib ``ctypes``.
Secrets are generic credentials targeted ``{service}:{account}`` with the
blob stored UTF-16-LE, as the Win32 credential APIs expect.

Imported lazily by ``keychain._detect_backend`` on win32 only. The module
itself must import cleanly on every platform — all ``ctypes.windll``
access happens inside functions, so tests elsewhere can monkeypatch the
``_cred_*`` seam without a Windows machine.
"""

from __future__ import annotations

import asyncio
import ctypes
import sys
from ctypes import wintypes
from typing import Any, cast

from .keychain import KeychainBackend, KeychainError

_CRED_TYPE_GENERIC = 1
_CRED_PERSIST_LOCAL_MACHINE = 2
# win32: ERROR_NOT_FOUND — no credential with that target name.
_ERROR_NOT_FOUND = 1168


class _FILETIME(ctypes.Structure):
    _fields_ = [
        ("dwLowDateTime", wintypes.DWORD),
        ("dwHighDateTime", wintypes.DWORD),
    ]


class _CREDENTIAL(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR),
        ("LastWritten", _FILETIME),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.c_void_p),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


_PCREDENTIAL = ctypes.POINTER(_CREDENTIAL)

# Windows-only ctypes attributes (WinDLL, get_last_error) are platform-gated
# in typeshed: direct access fails off-Windows type checks even though the
# code paths using them only run on Windows (_adv raises first). Reach them
# through an Any alias — the runtime guard stays in _adv.
_winctypes = cast(Any, ctypes)

_advapi32: Any = None


def _adv() -> Any:
    """advapi32 loaded with ``use_last_error`` so GetLastError is reliable,
    with signatures declared so ctypes converts arguments.

    ``WinDLL`` only exists on Windows; it is reached via the ``_winctypes``
    alias so this module imports and type-checks everywhere.
    """
    global _advapi32
    if _advapi32 is None:
        if sys.platform != "win32":
            raise KeychainError("Windows Credential Manager requires win32")
        adv = _winctypes.WinDLL("advapi32", use_last_error=True)
        adv.CredReadW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(_PCREDENTIAL),
        ]
        adv.CredReadW.restype = wintypes.BOOL
        adv.CredWriteW.argtypes = [_PCREDENTIAL, wintypes.DWORD]
        adv.CredWriteW.restype = wintypes.BOOL
        adv.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
        adv.CredDeleteW.restype = wintypes.BOOL
        adv.CredFree.argtypes = [ctypes.c_void_p]
        adv.CredFree.restype = None
        _advapi32 = adv
    return _advapi32


def _cred_read(target: str) -> bytes:
    adv = _adv()
    pcred = _PCREDENTIAL()
    ok = adv.CredReadW(target, _CRED_TYPE_GENERIC, 0, ctypes.byref(pcred))
    if not ok:
        err = _winctypes.get_last_error()
        if err == _ERROR_NOT_FOUND:
            raise KeychainError(
                f"API key not found in Windows Credential Manager (target {target!r})."
            )
        raise KeychainError(f"CredRead failed: win32 error {err}")
    try:
        return ctypes.string_at(pcred.contents.CredentialBlob, pcred.contents.CredentialBlobSize)
    finally:
        adv.CredFree(ctypes.cast(pcred, ctypes.c_void_p))


def _cred_write(target: str, blob: bytes, username: str) -> None:
    adv = _adv()
    # Keep the buffer alive for the duration of the call.
    buf = ctypes.create_string_buffer(blob, len(blob))
    cred = _CREDENTIAL(
        Flags=0,
        Type=_CRED_TYPE_GENERIC,
        TargetName=target,
        Comment="TST Desk API key",
        LastWritten=_FILETIME(0, 0),
        CredentialBlobSize=len(blob),
        CredentialBlob=ctypes.cast(buf, ctypes.c_void_p),
        Persist=_CRED_PERSIST_LOCAL_MACHINE,
        AttributeCount=0,
        Attributes=None,
        TargetAlias=None,
        UserName=username,
    )
    if not adv.CredWriteW(ctypes.byref(cred), 0):
        err = _winctypes.get_last_error()
        raise KeychainError(f"CredWrite failed: win32 error {err}")


def _cred_delete(target: str) -> None:
    adv = _adv()
    if not adv.CredDeleteW(target, _CRED_TYPE_GENERIC, 0):
        err = _winctypes.get_last_error()
        if err == _ERROR_NOT_FOUND:
            raise KeychainError(
                f"API key not found in Windows Credential Manager (target {target!r})."
            )
        raise KeychainError(f"CredDelete failed: win32 error {err}")


class WindowsCredentialManager(KeychainBackend):
    """Windows keychain via Credential Manager generic credentials."""

    @staticmethod
    def _target(account: str, service: str) -> str:
        return f"{service}:{account}"

    async def get_secret(self, account: str, service: str = "com.thatsimpletech.tstdesk") -> str:
        blob = await asyncio.to_thread(_cred_read, self._target(account, service))
        # rstrip: credentials written by other tools may carry a terminator.
        value = blob.decode("utf-16-le").rstrip("\x00")
        if not value:
            raise KeychainError("API key not found in Windows Credential Manager (empty value).")
        return value

    async def set_secret(
        self, account: str, secret: str, service: str = "com.thatsimpletech.tstdesk"
    ) -> None:
        await asyncio.to_thread(
            _cred_write, self._target(account, service), secret.encode("utf-16-le"), account
        )

    async def delete_secret(
        self, account: str, service: str = "com.thatsimpletech.tstdesk"
    ) -> None:
        await asyncio.to_thread(_cred_delete, self._target(account, service))
