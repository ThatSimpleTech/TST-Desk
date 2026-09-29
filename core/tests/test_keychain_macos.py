"""macOS keychain trust (TD-4838).

Every spawn and every Security-framework call is faked. These tests must
never touch the login keychain: the autouse fixture raises if one slips
through.
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any

import pytest

import tstd.keychain as kc_mod
import tstd.keychain_macos as keychain_macos
from tstd.keychain import KeychainError, KeychainLockedError, MacOSKeychain, api_key_is_stored

_SERVICE = "com.thatsimpletech.tstdesk"
_C_SPACE = frozenset(" \t\n\v\f\r")
# Captured before the autouse fixture replaces the framework seams.
_COPY_SRC = inspect.getsource(keychain_macos._sec_item_copy)
_DELETE_SRC = inspect.getsource(keychain_macos._sec_item_delete)


def _split_security_line(line: str) -> list[str]:
    """Replica of SecurityTool ``split_line`` (backslash escapes in both quotes)."""
    chars = list(line)
    i = 0
    n = len(chars)
    args: list[str] = []
    buf: list[str] = []
    state = "SKIP_WS"
    quote = ""
    while i < n:
        ch = chars[i]
        if state == "SKIP_WS":
            if ch in _C_SPACE:
                i += 1
                continue
            if ch in "\"'":
                quote = ch
                state = "QUOTED_ARG"
                buf = []
                i += 1
                continue
            state = "READ_ARG"
            buf = []
        if state == "READ_ARG":
            if ch == "\\":
                state = "READ_ARG_ESCAPED"
                i += 1
                continue
            if ch in _C_SPACE:
                args.append("".join(buf))
                state = "SKIP_WS"
                i += 1
                if len(args) >= 32:
                    break
                continue
            buf.append(ch)
            i += 1
            continue
        if state == "QUOTED_ARG":
            if ch == "\\":
                state = "QUOTED_ARG_ESCAPED"
                i += 1
                continue
            if ch == quote:
                args.append("".join(buf))
                state = "SKIP_WS"
                i += 1
                if len(args) >= 32:
                    break
                continue
            buf.append(ch)
            i += 1
            continue
        if state == "READ_ARG_ESCAPED":
            buf.append(ch)
            state = "READ_ARG"
            i += 1
            continue
        if state == "QUOTED_ARG_ESCAPED":
            buf.append(ch)
            state = "QUOTED_ARG"
            i += 1
            continue
        i += 1
    if state != "SKIP_WS":
        args.append("".join(buf))
    return args


class _Script:
    """Scripted ``security`` spawns. Stdin is recorded; argv never has to carry a secret."""

    def __init__(self, steps: list[tuple[int, bytes, bytes]]) -> None:
        self.steps = steps
        self.argv: list[tuple[str, ...]] = []
        self.stdin: list[bytes | None] = []
        self._i = 0

    def install(self, monkeypatch: pytest.MonkeyPatch) -> _Script:
        script = self

        async def fake_exec(*argv: str, **_kwargs: Any) -> Any:
            idx = script._i
            script._i += 1
            if idx >= len(script.steps):
                raise AssertionError(f"unexpected spawn {argv!r}")
            code, out, err = script.steps[idx]
            script.argv.append(argv)

            class Proc:
                returncode = code

                async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:
                    script.stdin.append(input)
                    return out, err

            return Proc()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        return self


@pytest.fixture(autouse=True)
def _never_the_host_keychain(monkeypatch: pytest.MonkeyPatch) -> None:
    async def refuse_spawn(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("test tried to spawn a real keychain helper")

    def refuse_framework(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("test tried to touch the Security framework")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", refuse_spawn)
    monkeypatch.setattr(keychain_macos, "_sec_item_delete", refuse_framework)
    monkeypatch.setattr(keychain_macos, "_sec_item_copy", refuse_framework)
    monkeypatch.setattr(kc_mod, "_backend", None)


def _allow_delete(monkeypatch: pytest.MonkeyPatch, status: int = -25300) -> list[tuple[str, str]]:
    seen: list[tuple[str, str]] = []

    def _delete(service: str, account: str) -> int:
        seen.append((service, account))
        return status

    monkeypatch.setattr(keychain_macos, "_sec_item_delete", _delete)
    return seen


class TestInteractiveWrite:
    async def test_argv_is_security_interactive_and_secret_is_only_stdin(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        secret = "sk-stdin-only"
        deleted = _allow_delete(monkeypatch)
        script = _Script([(0, b"", b""), (0, b"", b"")]).install(monkeypatch)
        await MacOSKeychain().set_secret("tst-ezer", secret, _SERVICE)
        assert deleted == [(_SERVICE, "tst-ezer")]
        assert script.argv[0] == (
            "security",
            "delete-generic-password",
            "-a",
            "tst-ezer",
            "-s",
            _SERVICE,
        )
        assert script.argv[1] == ("security", "-i")
        assert script.stdin[1] == keychain_macos.interactive_add_payload(
            "tst-ezer", secret, _SERVICE
        )
        assert secret not in repr(script.argv)
        assert secret.encode() in (script.stdin[1] or b"")

    @pytest.mark.parametrize(
        "secret",
        [
            "has space",
            "it's",
            'say "hi"',
            "both ' and \"",
            "back\\slash",
            "cost $5",
            "héllo",
            "密钥",
            "",
            "mix ' \" \\ $ spáce",
            "a\rb",
        ],
    )
    def test_quote_round_trip(self, secret: str) -> None:
        payload = keychain_macos.interactive_add_payload("tst-ezer", secret, _SERVICE)
        assert payload.endswith(b"\n")
        assert payload.count(b"\n") == 1
        args = _split_security_line(payload.decode("utf-8"))
        assert args[args.index("-w") + 1] == secret
        assert args[args.index("-a") + 1] == "tst-ezer"
        assert args[args.index("-l") + 1] == "TST Desk tst-ezer"
        assert args[0] == "add-generic-password"
        assert "-U" in args
        assert "-A" not in args
        assert "-T" not in args

    @pytest.mark.parametrize("secret", ["line\nsecret-sentinel", "nul\x00secret-sentinel"])
    async def test_newline_and_nul_are_refused(
        self, secret: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with pytest.raises(KeychainError, match="newline or NUL") as exc:
            await MacOSKeychain().set_secret("tst-ezer", secret)
        assert "secret-sentinel" not in str(exc.value)
        assert "\n" not in str(exc.value)
        assert "\x00" not in str(exc.value)

    def test_overlong_secret_is_refused(self) -> None:
        secret = "A" * 5000
        with pytest.raises(KeychainError, match="too long") as exc:
            keychain_macos.interactive_add_payload("tst-ezer", secret, _SERVICE)
        assert "AAAA" not in str(exc.value)

    async def test_failed_subcommand_with_exit_zero_is_an_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        secret = "sk-stderr-sentinel"
        _allow_delete(monkeypatch)
        stderr = b"add-generic-password: returned 1\nsecurity: err sk-stderr-sentinel\n"
        _Script([(0, b"", b""), (0, stderr, stderr)]).install(monkeypatch)
        # returncode 0 on the second step: the failure is the stderr.
        with pytest.raises(KeychainError, match="Failed to store") as exc:
            await MacOSKeychain().set_secret("tst-ezer", secret)
        assert "sk-stderr-sentinel" not in str(exc.value)
        assert "add-generic-password -U" not in str(exc.value)

    async def test_missing_item_is_deleted_then_added(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        deleted = _allow_delete(monkeypatch, keychain_macos._ERR_SEC_ITEM_NOT_FOUND)
        not_found = b"security: The specified item could not be found in the keychain.\n"
        script = _Script([(1, b"", not_found), (0, b"", b"")]).install(monkeypatch)
        await MacOSKeychain().set_secret("tst-ezer", "sk-replaced")
        assert deleted == [(_SERVICE, "tst-ezer")]
        assert script.argv[1] == ("security", "-i")
        assert "sk-replaced" not in repr(script.argv)

    async def test_locked_delete_does_not_add(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _allow_delete(monkeypatch, keychain_macos._ERR_SEC_INTERACTION_NOT_ALLOWED)
        script = _Script(
            [(1, b"", b"security: The user name or passphrase you entered is not correct.\n")]
        ).install(monkeypatch)
        with pytest.raises(KeychainLockedError, match="login keychain is locked"):
            await MacOSKeychain().set_secret("tst-ezer", "sk-locked-sentinel")
        assert len(script.argv) == 1
        assert script.argv[0][1] == "delete-generic-password"

    async def test_hung_add_is_a_locked_keychain(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _allow_delete(monkeypatch)
        monkeypatch.setattr(kc_mod, "_CLI_TIMEOUT_SECS", 0.05)

        class Hung:
            returncode = None
            killed = False

            def kill(self) -> None:
                self.killed = True

            async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:
                await asyncio.sleep(30)
                return b"", b""

            async def wait(self) -> int:
                return 0

        hung = Hung()
        calls = 0

        async def fake_exec(*_argv: str, **_kwargs: Any) -> Any:
            nonlocal calls
            calls += 1
            if calls == 1:

                class Ok:
                    returncode = 0

                    async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:
                        return b"", b""

                return Ok()
            return hung

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        with pytest.raises(KeychainLockedError, match="did not respond") as exc:
            await MacOSKeychain().set_secret("tst-ezer", "sk-hung-sentinel")
        assert hung.killed
        assert "sk-hung-sentinel" not in str(exc.value)


class TestReadFallback:
    def test_copy_and_delete_force_the_ui_off(self) -> None:
        assert keychain_macos._AUTH_UI == "kSecUseAuthenticationUIFail"
        assert "_AUTH_UI" in _COPY_SRC
        assert "kSecReturnData" in _COPY_SRC
        assert "kSecMatchLimitOne" in _COPY_SRC
        assert "kSecUseAuthenticationUIAllow" not in _COPY_SRC
        assert "_AUTH_UI" in _DELETE_SRC
        assert "kSecUseAuthenticationUIAllow" not in _DELETE_SRC

    async def test_cli_success_does_not_copy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        script = _Script([(0, b"from-cli\n", b"")]).install(monkeypatch)
        assert await MacOSKeychain().get_secret("tst-ezer") == "from-cli"
        assert script.argv == [
            ("security", "find-generic-password", "-a", "tst-ezer", "-s", _SERVICE, "-w")
        ]

    async def test_not_found_does_not_copy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stderr = b"The specified item could not be found in the keychain. sk-leak-sentinel\n"
        _Script([(1, b"", stderr)]).install(monkeypatch)
        with pytest.raises(KeychainError, match="not found") as exc:
            await MacOSKeychain().get_secret("tst-ezer")
        assert "sk-leak-sentinel" not in str(exc.value)

    async def test_acl_denial_falls_back_to_copy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        copied: list[tuple[str, str]] = []

        def _copy(service: str, account: str) -> bytes:
            copied.append((service, account))
            return b"from-framework\n"

        monkeypatch.setattr(keychain_macos, "_sec_item_copy", _copy)
        stderr = b"security: User interaction is not allowed.\n"
        script = _Script(
            [
                (1, b"", stderr),
                (0, b"password: sk-attr-sentinel\n", b""),
            ]
        ).install(monkeypatch)
        assert await MacOSKeychain().get_secret("tst-ezer") == "from-framework\n"
        assert "-w" in script.argv[0]
        assert "-w" not in script.argv[1]
        assert copied == [(_SERVICE, "tst-ezer")]

    async def test_timeout_then_attributes_then_copy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(kc_mod, "_CLI_TIMEOUT_SECS", 0.05)
        monkeypatch.setattr(keychain_macos, "_sec_item_copy", lambda *_a: b"from-framework")
        calls = 0

        class Hung:
            returncode = None

            def kill(self) -> None:
                self.killed = True

            async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:
                await asyncio.sleep(30)
                return b"", b""

            async def wait(self) -> int:
                return 0

        async def fake_exec(*argv: str, **_kwargs: Any) -> Any:
            nonlocal calls
            calls += 1
            if calls == 1:
                assert "-w" in argv
                return Hung()

            class Ok:
                returncode = 0

                async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:
                    assert "-w" not in argv
                    return b"", b""

            return Ok()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        assert await MacOSKeychain().get_secret("tst-ezer") == "from-framework"

    async def test_unreadable_item_says_resave(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(keychain_macos, "_sec_item_copy", lambda *_a: None)
        stderr = b"security: User interaction is not allowed. sk-leak-sentinel\n"
        _Script([(1, b"", stderr), (0, b"attributes\n", b"")]).install(monkeypatch)
        with pytest.raises(KeychainError, match="Re-save the key") as exc:
            await MacOSKeychain().get_secret("tst-ezer")
        text = str(exc.value)
        assert "Settings" in text
        assert "API keys" in text
        assert "sk-leak-sentinel" not in text

    async def test_real_lock_is_not_a_resave(self, monkeypatch: pytest.MonkeyPatch) -> None:
        locked = b"security: User interaction is not allowed.\n"
        _Script([(1, b"", locked), (1, b"", locked)]).install(monkeypatch)
        with pytest.raises(KeychainLockedError, match="login keychain is locked") as exc:
            await MacOSKeychain().get_secret("tst-ezer")
        assert "Re-save" not in str(exc.value)


class TestPresence:
    async def test_probe_never_passes_w_or_reads_the_secret(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        script = _Script([(0, b"attributes only\n", b"")]).install(monkeypatch)
        monkeypatch.setattr(kc_mod, "_get_backend", lambda: MacOSKeychain())

        async def explode(*_args: object, **_kwargs: object) -> str:
            raise AssertionError("presence read the secret")

        monkeypatch.setattr(kc_mod, "get_api_key", explode)
        monkeypatch.setattr(MacOSKeychain, "get_secret", explode)
        assert await api_key_is_stored("ezer") is True
        assert script.argv == [
            ("security", "find-generic-password", "-a", "tst-ezer", "-s", _SERVICE)
        ]
        assert "-w" not in script.argv[0]

    async def test_missing_item_is_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stderr = b"security: The specified item could not be found in the keychain.\n"
        script = _Script([(1, b"", stderr)]).install(monkeypatch)
        monkeypatch.setattr(kc_mod, "_get_backend", lambda: MacOSKeychain())
        assert await api_key_is_stored("ezer") is False
        assert "-w" not in script.argv[0]

    async def test_locked_probe_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stderr = b"security: The user name or passphrase you entered is not correct.\n"
        _Script([(1, b"", stderr)]).install(monkeypatch)
        monkeypatch.setattr(kc_mod, "_get_backend", lambda: MacOSKeychain())
        with pytest.raises(KeychainLockedError):
            await api_key_is_stored("ezer")
