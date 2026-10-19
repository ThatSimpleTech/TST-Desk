"""HTTPS git that never writes the token down.

Each command clears helpers, then sets one helper for that process:
``!python -m tstd.connectors.github_credential``. The remote URL has no
userinfo. ``GIT_CONFIG*`` and askpass variables are dropped so a parent
config cannot add another helper. The token stays in the keychain
(TD-4852, TD-4813).
"""

from __future__ import annotations

import shlex
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path

from ..git_safe import git_command
from ..logging import redact_secrets
from ..tools.shell import sanitized_env
from .github_models import GithubError, require_owner

GitRunner = Callable[[list[str], dict[str, str], Path, float], Awaitable[tuple[int, str, str]]]

_GIT_TIMEOUT_SECONDS = 120.0
_ACTIONS = frozenset({"clone", "fetch", "push"})


def git_child_env(web_host: str, api_host: str, client_id: str, timeout: float) -> dict[str, str]:
    """Env for one git command. Hosts and the public client id, never a token.

    The helper is a subprocess. It cannot see the daemon's ``--data-dir``,
    so the non-secret connector settings ride in ``TST_GITHUB_*``.
    """
    env = sanitized_env()
    for name in list(env):
        if name.startswith("GIT_CONFIG") or name in {"GIT_ASKPASS", "SSH_ASKPASS"}:
            del env[name]
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["TST_GITHUB_WEB_HOST"] = web_host
    env["TST_GITHUB_API_HOST"] = api_host
    env["TST_GITHUB_CLIENT_ID"] = client_id
    env["TST_GITHUB_TIMEOUT"] = str(timeout)
    return env


def git_argv(
    *,
    executable: str,
    web_host: str,
    action: str,
    dest: str,
    owner: str,
    repo: str,
) -> list[str]:
    """Argv for clone, fetch, or push. No token, no ``git config``, no ``--force``."""
    if action not in _ACTIONS:
        raise GithubError("That git action is not available.")
    if dest.startswith("-"):
        raise GithubError("That path is not valid.")
    require_owner(owner)
    require_owner(repo)
    helper = "!" + shlex.quote(executable) + " -m tstd.connectors.github_credential"
    url = f"https://{web_host}/{owner}/{repo}.git"
    prefix = git_command("-c", "credential.helper=", "-c", f"credential.helper={helper}")
    if action == "clone":
        return [*prefix, "clone", "--", url, dest]
    if action == "fetch":
        return [*prefix, "-C", dest, "fetch", "--", url]
    return [*prefix, "-C", dest, "push", "--", url, "HEAD"]


def contain_dest(workspace: Path, dest: str) -> Path:
    """Resolve *dest* and refuse anything outside *workspace*, including symlinks."""
    if not dest or dest.startswith("-"):
        raise GithubError("That path is not valid.")
    root = workspace.resolve()
    target = Path(dest)
    if not target.is_absolute():
        target = root / target
    resolved = target.resolve()
    try:
        resolved.relative_to(root)
    except ValueError:
        raise GithubError("That path is outside the workspace.") from None
    return resolved


async def default_git_runner(
    argv: list[str], env: dict[str, str], cwd: Path, timeout_seconds: float
) -> tuple[int, str, str]:
    import asyncio

    from ..proc_lifecycle import finish_subprocess

    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=cwd,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await finish_subprocess(proc, timeout=timeout_seconds)
    code = proc.returncode if proc.returncode is not None else 1
    return code, out.decode("utf-8", errors="replace"), err.decode("utf-8", errors="replace")


def _git_plan(
    *,
    workspace: Path,
    web_host: str,
    api_host: str,
    client_id: str,
    timeout_seconds: float,
    action: str,
    dest: str,
    owner: str,
    repo: str,
    executable: str,
) -> tuple[list[str], dict[str, str], Path]:
    """Argv, env, and cwd. Path resolution stays off the event loop."""
    root = workspace.resolve()
    contained = contain_dest(root, dest)
    argv = git_argv(
        executable=executable,
        web_host=web_host,
        action=action,
        dest=str(contained),
        owner=owner,
        repo=repo,
    )
    env = git_child_env(web_host, api_host, client_id, timeout_seconds)
    return argv, env, root


async def run_git(
    *,
    workspace: Path,
    web_host: str,
    api_host: str,
    client_id: str,
    timeout_seconds: float,
    action: str,
    dest: str,
    owner: str,
    repo: str,
    runner: GitRunner | None = None,
    executable: str | None = None,
) -> str:
    argv, env, root = _git_plan(
        workspace=workspace,
        web_host=web_host,
        api_host=api_host,
        client_id=client_id,
        timeout_seconds=timeout_seconds,
        action=action,
        dest=dest,
        owner=owner,
        repo=repo,
        executable=executable or sys.executable,
    )
    run = runner or default_git_runner
    code, out, err = await run(argv, env, root, _GIT_TIMEOUT_SECONDS)
    text = redact_secrets((out + ("\n" + err if err else "")).strip())
    if code != 0:
        detail = text or "git failed"
        raise GithubError(f"git {action} failed: {detail}")
    summary = f"git {action} {owner}/{repo}"
    return summary if not text else f"{summary}\n{text}"
