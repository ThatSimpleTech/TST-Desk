"""Bubblewrap sandbox for interactive shell commands (TD-5138).

Autonomy runs sit in a rootless container (``autonomy/sandbox.py``); an
interactive command used to run straight on the host. With ``sandbox:
enabled`` in ``.tst/config.yaml`` the shell tool runs each command under
``bwrap`` on Linux instead:

- the root is bound read-only, the workspace read-write and ``/tmp`` is a
  fresh private tmpfs, so writes land only in the workspace and temp;
- the steering files, ``.tst/`` and the whole ``.git`` are bound back
  read-only, so a command cannot edit policy, trust its own config or plant
  a hook or ``core.fsmonitor`` that later runs outside the sandbox. A bound
  path is a mount point, so it cannot be renamed away either; a guarded name
  that is missing or a symlink is checked after the run and anything the
  command put there is moved aside (``guard_snapshot`` / ``guard_restore``);
- every namespace is unshared (no TCP/IP, no abstract sockets), and a
  seccomp filter refuses ``socket(AF_UNIX)`` and io_uring, so a command
  cannot reach a host daemon through a socket file either (docker.sock,
  tailscaled, the system bus). ``socketpair`` still works;
- ``~/.ssh``, ``~/.aws``, the keyrings, TST Desk's own data dir and the
  user's runtime dir (D-Bus, where the Secret Service keychain answers,
  and the ssh and gpg agents) are covered by an empty mode-000 tmpfs.

``sandbox.auto_allow`` lets a command that would otherwise ask run without
asking, because it runs inside the walls. A command matching
``sandbox.exclude`` runs outside, on the host, through the normal gate.
The dispatcher promotes only a class default ``ask`` (never a rule's own
``ask``, never Class C), and the shell tool refuses a promoted command it
could not sandbox, so an auto-run never reaches the host.
"""

from __future__ import annotations

import contextlib
import fnmatch
import hashlib
import os
import platform
import shlex
import shutil
import struct
import sys
import tempfile
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .logging import user_data_dir
from .policy_shell import split_command

if TYPE_CHECKING:
    from .autonomy.classifier import DecisionClass
    from .policy import PolicyDecision

# Paths under the home directory a sandboxed command must not read:
# keys, cloud and registry credentials, keyrings and password stores.
DEFAULT_HIDDEN_HOME: tuple[str, ...] = (
    ".ssh",
    ".aws",
    ".gnupg",
    ".local/share/keyrings",
    ".password-store",
    ".netrc",
    ".git-credentials",
    ".config/gh",
    ".config/gcloud",
    ".azure",
    ".docker",
    ".kube",
    ".config/tst-secrets",
)
# Workspace paths bound back read-only: steering files (prime directive 4),
# TST Desk's own workspace config and state, and the whole git dir. Binding
# only .git/config and .git/hooks was not enough: the .git around them stayed
# writable, so a command could rename it away and write a fresh config with
# core.fsmonitor that the host's next ``git status`` runs (review of #63).
GUARDED_NAMES: tuple[str, ...] = (
    "AGENTS.md",
    "CLAUDE.md",
    ".tst",
    ".git",
)
# Variables that point at a socket the sandbox hides; leaving them set only
# makes tools wait on a socket that is not there.
_UNSET_ENV: tuple[str, ...] = (
    "DBUS_SESSION_BUS_ADDRESS",
    "SSH_AUTH_SOCK",
    "GPG_AGENT_INFO",
    "XDG_RUNTIME_DIR",
)
MAX_EXCLUDES = 100
MAX_HIDDEN = 100

SANDBOX_NOTE = (
    "ran in the shell sandbox: writes reach only the workspace and a private /tmp, "
    "no network or host sockets; .git and steering files are read-only"
)
AUTO_REASON = "runs inside the shell sandbox (sandbox.auto_allow)"


class SandboxSection(BaseModel):
    """The ``sandbox:`` section of ``.tst/config.yaml`` (TD-5138).

    Unknown keys are refused, like ``caps``: a typo must not quietly leave
    commands unsandboxed.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    auto_allow: bool = False
    exclude: list[str] = Field(default_factory=list, max_length=MAX_EXCLUDES)
    hidden_paths: list[str] = Field(default_factory=list, max_length=MAX_HIDDEN)

    @field_validator("exclude", "hidden_paths")
    @classmethod
    def _non_empty(cls, v: list[str]) -> list[str]:
        if any(not item.strip() for item in v):
            raise ValueError("entries must be non-empty strings")
        return [item.strip() for item in v]


def excluded(section: SandboxSection, command: str) -> bool:
    """True when any command of the line matches an ``exclude`` glob.

    Matched against every reading policy's ``ask`` and ``never`` rules see
    (TD-5101), so ``sudo docker run`` or ``a && docker run`` still match
    ``docker *``. Reading more sends more to the host gate, never less.
    """
    if not section.exclude:
        return False
    texts = split_command(command).deny_texts or (command.strip(),)
    return any(fnmatch.fnmatchcase(t, glob) for t in texts for glob in section.exclude)


def applies(section: SandboxSection, command: str, *, autonomy: bool) -> bool:
    """True when *command* must run inside the sandbox."""
    return section.enabled and not autonomy and not excluded(section, command)


def unavailable_reason() -> str | None:
    """Why this host cannot run the sandbox, or None when it can."""
    if not sys.platform.startswith("linux"):
        return "the shell sandbox needs Linux (bubblewrap); turn sandbox.enabled off here"
    if _seccomp_arch() is None:
        return (
            f"the shell sandbox supports x86_64 and aarch64, not {platform.machine()}; "
            "turn sandbox.enabled off here"
        )
    if shutil.which("bwrap") is None:
        return (
            "the shell sandbox is on but bubblewrap is not installed; install it "
            "(apt install bubblewrap / dnf install bubblewrap) or turn sandbox.enabled off"
        )
    return None


def promote(
    section: SandboxSection,
    command: object,
    decision: PolicyDecision,
    decision_class: DecisionClass,
    *,
    autonomy: bool,
) -> PolicyDecision | None:
    """An ``auto`` decision when a sandboxed command may skip its ask.

    Only the class default ``ask`` of a Class A/B call is promoted: a
    workspace ``ask`` rule is the user's own word and still asks, Class C
    still asks, and a line policy could not split with confidence asks.
    """
    from .autonomy.classifier import DecisionClass
    from .policy import PolicyDecision

    if not (section.enabled and section.auto_allow) or autonomy:
        return None
    if not isinstance(command, str) or decision.effect != "ask" or decision.rule is not None:
        return None
    if decision_class is DecisionClass.C:
        return None
    if not split_command(command).complete or not applies(section, command, autonomy=False):
        return None
    if unavailable_reason() is not None:
        return None
    return PolicyDecision("auto", AUTO_REASON, None)


# ── Seccomp: no AF_UNIX sockets ─────────────────────────────────────────
#
# A read-only mount does not stop connect() on a socket file, and the root
# is bound read-only, not hidden: /run/docker.sock, tailscaled's socket and
# the system bus would all answer (review of #63). Refusing socket(AF_UNIX)
# closes every pathname socket wherever it lives; io_uring is refused too,
# because IORING_OP_SOCKET would make one without the socket syscall.

# (audit arch, socket syscall number) per machine; io_uring_setup, _enter
# and _register share 425-427 on both.
_SECCOMP_ARCHES: dict[str, tuple[int, int]] = {
    "x86_64": (0xC000003E, 41),
    "amd64": (0xC000003E, 41),
    "aarch64": (0xC00000B7, 198),
    "arm64": (0xC00000B7, 198),
}
_IO_URING = (425, 426, 427)
_X32_BIT = 0x40000000
_AF_UNIX = 1
_EPERM = 1
_ENOSYS = 38
_SECCOMP_FD = 9


def _seccomp_arch() -> tuple[int, int] | None:
    return _SECCOMP_ARCHES.get(platform.machine().lower())


def seccomp_program(arch: int, socket_nr: int) -> bytes:
    """The classic-BPF filter bwrap installs with ``--seccomp``.

    A foreign-arch call (a 32-bit binary, x32) gets ENOSYS, so the
    socketcall path cannot slip past the native number check.
    """
    ld_abs, jeq, jge, ret = 0x20, 0x15, 0x35, 0x06
    allow, errno = 0x7FFF0000, 0x00050000
    prog: list[tuple[int, int, int, int]] = [
        (ld_abs, 0, 0, 4),  # 0: arch
        (jeq, 1, 0, arch),  # 1: native -> 3
        (ret, 0, 0, errno | _ENOSYS),  # 2
        (ld_abs, 0, 0, 0),  # 3: syscall number
        (jge, 0, 1, _X32_BIT),  # 4: x32 -> 5, else 6
        (ret, 0, 0, errno | _ENOSYS),  # 5
        (jeq, 6, 0, _IO_URING[0]),  # 6 -> 13
        (jeq, 5, 0, _IO_URING[1]),  # 7 -> 13
        (jeq, 4, 0, _IO_URING[2]),  # 8 -> 13
        (jeq, 0, 2, socket_nr),  # 9: socket -> 10, else 12
        (ld_abs, 0, 0, 16),  # 10: args[0], low word (little-endian)
        (jeq, 1, 0, _AF_UNIX),  # 11: AF_UNIX -> 13
        (ret, 0, 0, allow),  # 12
        (ret, 0, 0, errno | _EPERM),  # 13
    ]
    return b"".join(struct.pack("<HBBI", *ins) for ins in prog)


_seccomp_file: Path | None = None


def seccomp_file() -> Path:
    """A private copy of the filter for bwrap to read on fd 9.

    Kept in a fresh ``mkdtemp`` dir (mode 0700), rewritten if a temp
    cleaner removed it or its bytes changed.
    """
    global _seccomp_file
    found = _seccomp_arch()
    if found is None:
        raise ValueError("the shell sandbox has no seccomp filter for this machine")
    program = seccomp_program(*found)
    path = _seccomp_file
    with contextlib.suppress(OSError):
        if path is not None and path.read_bytes() == program:
            return path
    folder = Path(tempfile.mkdtemp(prefix="tstd-sandbox-"))
    path = folder / f"no-unix-{hashlib.sha256(program).hexdigest()[:12]}.bpf"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
    with os.fdopen(fd, "wb") as fh:
        fh.write(program)
    _seccomp_file = path
    return path


@dataclass(frozen=True)
class SandboxPlan:
    """What one sandboxed command is wrapped in."""

    argv: list[str]
    unset_env: tuple[str, ...] = _UNSET_ENV
    seccomp: Path | None = None

    def shell_text(self) -> str:
        """The line the host shell ``exec``s, so the spawned pid stays the
        process-group leader the timeout and cancel kills reach. The host
        shell opens the filter on fd 9 before the exec."""
        text = "exec " + shlex.join(self.argv)
        if self.seccomp is not None:
            text += f" {_SECCOMP_FD}< {shlex.quote(str(self.seccomp))}"
        return text


# ── Guarded names ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Seen:
    kind: str  # "missing", "link" or "real"
    link: str = ""
    ident: tuple[int, int] = (0, 0)


def _seen(path: Path) -> _Seen:
    try:
        st = path.lstat()
    except FileNotFoundError:
        return _Seen("missing")
    if path.is_symlink():
        return _Seen("link", link=os.readlink(path))
    return _Seen("real", ident=(st.st_dev, st.st_ino))


GuardSnapshot = dict[str, _Seen]
"""What :func:`guard_snapshot` saw, for :func:`guard_restore` after the run."""


def guard_snapshot(workspace: str) -> dict[str, _Seen]:
    """How each guarded name looks before a sandboxed run.

    A real file or dir is bound read-only at its own path, a mount point
    the command cannot rename or remove. A missing name, or a symlink
    (which bwrap can only pin at its target), can still be created or
    replaced from inside, so ``guard_restore`` checks it after the run.
    """
    root = Path(workspace)
    return {name: _seen(root / name) for name in GUARDED_NAMES}


def _aside(path: Path) -> Path:
    stamp = time.strftime("%Y%m%d%H%M%S")
    n = 0
    while True:
        dest = path.with_name(f"{path.name}.sandbox-blocked-{stamp}{f'-{n}' if n else ''}")
        if not os.path.lexists(dest):
            return dest
        n += 1


def guard_restore(workspace: str, before: dict[str, _Seen]) -> list[str]:
    """Undo what a sandboxed command did to a guarded name; one note each.

    Whatever the command put at a guarded name is moved aside, never
    deleted, and a symlink it replaced is put back. The sandbox's pid
    namespace dies with the command, so nothing can race this.
    """
    root = Path(workspace)
    notes: list[str] = []
    for name, was in before.items():
        path = root / name
        now = _seen(path)
        if now == was:
            continue
        moved = ""
        if now.kind != "missing":
            dest = _aside(path)
            try:
                os.rename(path, dest)
            except OSError as e:
                notes.append(f"the sandboxed command changed {name} and it could not be moved: {e}")
                continue
            moved = f"; moved what it left to {dest.name}"
        if was.kind == "link":
            with contextlib.suppress(OSError):
                os.symlink(was.link, path)
                moved += f"; restored the {name} symlink"
        notes.append(
            f"blocked: the sandboxed command changed {name}, which shell commands "
            f"may not write{moved}"
        )
    return notes


def _pins(real_ws: Path) -> list[Path]:
    """The paths bound back read-only, each once.

    A real guarded name is pinned where it stands (a mount point cannot be
    renamed). A symlink is pinned at its target. A ``.git`` file (a linked
    worktree) also pins the git dir it names when that lies inside the
    workspace; outside it, the read-only root already covers it.
    """
    out: list[Path] = []

    def add(p: Path) -> None:
        if p.exists() and p not in out:
            out.append(p)

    for name in GUARDED_NAMES:
        path = real_ws / name
        add(path.resolve() if path.is_symlink() else path)
    gitfile = real_ws / ".git"
    if gitfile.is_file():
        with contextlib.suppress(OSError, UnicodeDecodeError):
            first = gitfile.read_text().splitlines()[0]
            if first.startswith("gitdir:"):
                gitdir = (real_ws / first[len("gitdir:") :].strip()).resolve()
                if gitdir.is_relative_to(real_ws):
                    add(gitdir)
    return out


def hidden_paths(
    section: SandboxSection, home: Path | None = None, extra: Sequence[Path] = ()
) -> list[Path]:
    """Every existing path the sandbox covers, symlink targets included.

    *extra* is the daemon's own data dir when it is not the default one
    (``--data-dir``): sessions, the audit log and the trust store.
    """
    root = home if home is not None else Path.home()
    names: list[Path] = [root / rel for rel in DEFAULT_HIDDEN_HOME]
    names.append(user_data_dir())
    names += list(extra)
    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    names.append(Path(runtime))
    names += [Path(p).expanduser() for p in section.hidden_paths]
    out: list[Path] = []
    for name in names:
        for candidate in (name, name.resolve()):
            if candidate.exists() and candidate not in out:
                out.append(candidate)
    return out


def build_plan(
    workspace: str,
    command: str,
    section: SandboxSection,
    *,
    bwrap: str = "bwrap",
    home: Path | None = None,
    extra_hidden: Sequence[Path] = (),
    seccomp: Path | None = None,
) -> SandboxPlan:
    """The ``bwrap`` argv that runs *command* in *workspace*.

    Order matters: bwrap applies mounts in sequence, so the workspace bind
    comes after the read-only root and the masks come last, which keeps
    ``~/.ssh`` covered even when the workspace is the home directory.
    *seccomp* defaults to ``seccomp_file()``.
    """
    real_ws = Path(workspace).resolve()
    filter_path = seccomp if seccomp is not None else seccomp_file()
    argv = [
        bwrap,
        "--die-with-parent",
        "--new-session",
        "--unshare-all",
        "--seccomp",
        str(_SECCOMP_FD),
        "--ro-bind",
        "/",
        "/",
        "--dev",
        "/dev",
        "--proc",
        "/proc",
        "--tmpfs",
        "/tmp",
        "--bind",
        str(real_ws),
        str(real_ws),
    ]
    for pin in _pins(real_ws):
        argv += ["--ro-bind", str(pin), str(pin)]
    for path in hidden_paths(section, home, extra_hidden):
        if path.is_dir():
            argv += ["--perms", "0000", "--tmpfs", str(path)]
        else:
            argv += ["--ro-bind", "/dev/null", str(path)]
    for name in _UNSET_ENV:
        argv += ["--unsetenv", name]
    argv += ["--chdir", workspace, "--", "/bin/sh", "-c", command]
    return SandboxPlan(argv, seccomp=filter_path)
