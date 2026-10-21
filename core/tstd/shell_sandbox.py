"""Bubblewrap sandbox for interactive shell commands (TD-5138).

Autonomy runs sit in a rootless container (``autonomy/sandbox.py``); an
interactive command used to run straight on the host. With ``sandbox:
enabled`` in ``.tst/config.yaml`` the shell tool runs each command under
``bwrap`` on Linux instead:

- the root is bound read-only, the workspace read-write and ``/tmp`` is a
  fresh private tmpfs, so writes land only in the workspace and temp;
- the steering files, ``.tst/`` and ``.git/config`` / ``.git/hooks`` are
  bound back read-only, so a command cannot edit policy, trust its own
  config or plant a hook that later runs outside the sandbox;
- every namespace is unshared, so there is no network at all;
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

import fnmatch
import os
import shlex
import shutil
import sys
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
# TST Desk's own workspace config and state, and git's code-running config.
_READ_ONLY_IN_WORKSPACE: tuple[str, ...] = (
    "AGENTS.md",
    "CLAUDE.md",
    ".tst",
    ".git/config",
    ".git/hooks",
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
    "ran in the shell sandbox: writes reach only the workspace and a private /tmp, no network"
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


@dataclass(frozen=True)
class SandboxPlan:
    """What one sandboxed command is wrapped in."""

    argv: list[str]
    unset_env: tuple[str, ...] = _UNSET_ENV

    def shell_text(self) -> str:
        """The line the host shell ``exec``s, so the spawned pid stays the
        process-group leader the timeout and cancel kills reach."""
        return "exec " + shlex.join(self.argv)


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
) -> SandboxPlan:
    """The ``bwrap`` argv that runs *command* in *workspace*.

    Order matters: bwrap applies mounts in sequence, so the workspace bind
    comes after the read-only root and the masks come last, which keeps
    ``~/.ssh`` covered even when the workspace is the home directory.
    """
    real_ws = str(Path(workspace).resolve())
    argv = [
        bwrap,
        "--die-with-parent",
        "--new-session",
        "--unshare-all",
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
        real_ws,
        real_ws,
    ]
    for rel in _READ_ONLY_IN_WORKSPACE:
        # A symlinked steering file is pinned where it really lives.
        target = (Path(real_ws) / rel).resolve()
        if target.exists():
            argv += ["--ro-bind", str(target), str(target)]
    for path in hidden_paths(section, home, extra_hidden):
        if path.is_dir():
            argv += ["--perms", "0000", "--tmpfs", str(path)]
        else:
            argv += ["--ro-bind", "/dev/null", str(path)]
    for name in _UNSET_ENV:
        argv += ["--unsetenv", name]
    argv += ["--chdir", workspace, "--", "/bin/sh", "-c", command]
    return SandboxPlan(argv)
