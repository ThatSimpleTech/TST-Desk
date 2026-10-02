"""GitHub REST calls behind the repo tools.

A user-to-server token cannot call ``/installation/repositories``. The
app's visible repos are ``/user/installations`` and then each
installation's repository list. Responses are shortened before they
reach the model. Error text never includes a body (TD-4852).
"""

from __future__ import annotations

import base64
from collections.abc import Callable
from typing import Any

from ..config import GithubConnectorConfig
from .github_http import api_json
from .github_models import (
    GithubError,
    quote_path,
    require_login,
    require_number,
    require_owner,
    require_ref,
)
from .github_tokens import TokenStore, ensure_access

_MAX_TEXT = 100_000
_PAGE = 100
_MAX_REPOS = 100
_MAX_TREE = 200
_MAX_INSTALLATIONS = 20


async def github_call(
    cfg: GithubConnectorConfig,
    store: TokenStore,
    method: str,
    path: str,
    *,
    body: dict[str, Any] | None = None,
    transport: Any = None,
    now: Callable[[], float] | None = None,
) -> Any:
    """One API call. A 401 refreshes once and retries. The body is never echoed."""
    token = await ensure_access(cfg, store, now=now, transport=transport)
    status, payload = await api_json(
        cfg.api_host,
        method,
        path,
        token,
        timeout_seconds=cfg.timeout_seconds,
        body=body,
        transport=transport,
    )
    if status == 401:
        token = await ensure_access(cfg, store, now=now, force=True, transport=transport)
        status, payload = await api_json(
            cfg.api_host,
            method,
            path,
            token,
            timeout_seconds=cfg.timeout_seconds,
            body=body,
            transport=transport,
        )
        if status == 401:
            raise GithubError("GitHub rejected the stored token. Connect again in Settings.")
    if status == 404:
        raise GithubError("GitHub could not find that.")
    if status >= 300:
        raise GithubError("GitHub request failed.")
    return payload


def _as_dict(value: object) -> dict[Any, Any]:
    if isinstance(value, dict):
        return value
    return {}


def _repo(owner: str, repo: str) -> str:
    return f"/repos/{require_owner(owner)}/{require_owner(repo)}"


def _clip(text: str) -> str:
    if len(text) <= _MAX_TEXT:
        return text
    return text[:_MAX_TEXT] + "\n(truncated)"


def _lines(rows: list[str], cap: int) -> str:
    shown = rows[:cap]
    body = "\n".join(shown) if shown else "(none)"
    if len(rows) > cap:
        body += f"\n(truncated to {cap})"
    return body


async def list_repos(
    cfg: GithubConnectorConfig, store: TokenStore, *, transport: Any = None
) -> str:
    payload = await github_call(
        cfg, store, "GET", f"/user/installations?per_page={_PAGE}", transport=transport
    )
    installations = payload.get("installations") if isinstance(payload, dict) else None
    if not isinstance(installations, list):
        raise GithubError("GitHub request failed.")
    rows: list[str] = []
    truncated = len(installations) >= _PAGE
    fetched = 0
    for item in installations:
        if len(rows) >= _MAX_REPOS or fetched >= _MAX_INSTALLATIONS:
            truncated = True
            break
        if not isinstance(item, dict) or not isinstance(item.get("id"), int):
            continue
        fetched += 1
        page = await github_call(
            cfg,
            store,
            "GET",
            f"/user/installations/{item['id']}/repositories?per_page={_PAGE}",
            transport=transport,
        )
        repos = page.get("repositories") if isinstance(page, dict) else None
        if not isinstance(repos, list):
            continue
        if len(repos) >= _PAGE:
            truncated = True
        for repo in repos:
            if len(rows) >= _MAX_REPOS:
                truncated = True
                break
            if not isinstance(repo, dict):
                continue
            name = repo.get("full_name")
            if not isinstance(name, str):
                continue
            mark = " private" if repo.get("private") is True else ""
            rows.append(f"{name}{mark}")
    body = "\n".join(rows) if rows else "(none)"
    if truncated:
        body += f"\n(truncated to {len(rows)})"
    return body


async def get_file(
    cfg: GithubConnectorConfig,
    store: TokenStore,
    owner: str,
    repo: str,
    path: str,
    ref: str = "",
    *,
    transport: Any = None,
) -> str:
    quoted = quote_path(path)
    query = f"?ref={require_ref(ref)}" if ref else ""
    payload = await github_call(
        cfg, store, "GET", f"{_repo(owner, repo)}/contents/{quoted}{query}", transport=transport
    )
    if isinstance(payload, list):
        return "That path is a directory. Use github_get_tree."
    if not isinstance(payload, dict):
        raise GithubError("GitHub request failed.")
    if payload.get("type") == "dir":
        return "That path is a directory. Use github_get_tree."
    encoded = payload.get("content")
    if not isinstance(encoded, str) or not encoded.strip():
        return "That file has no text content to return."
    try:
        raw = base64.b64decode(encoded, validate=False)
    except ValueError:
        return "That file has no text content to return."
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return f"Binary file, {len(raw)} bytes."
    return _clip(text)


async def get_tree(
    cfg: GithubConnectorConfig,
    store: TokenStore,
    owner: str,
    repo: str,
    ref: str,
    *,
    transport: Any = None,
) -> str:
    quoted = require_ref(ref)
    payload = await github_call(
        cfg,
        store,
        "GET",
        f"{_repo(owner, repo)}/git/trees/{quoted}?recursive=1",
        transport=transport,
    )
    entries = payload.get("tree") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        raise GithubError("GitHub request failed.")
    rows: list[str] = []
    for item in entries:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        path = item.get("path")
        if isinstance(kind, str) and isinstance(path, str):
            rows.append(f"{kind} {path}")
    text = _lines(rows, _MAX_TREE)
    if payload.get("truncated") is True and "(truncated" not in text:
        text += "\n(truncated)"
    return text


def _issue_line(item: dict[str, Any]) -> str | None:
    if "pull_request" in item:
        return None
    number = item.get("number")
    title = item.get("title")
    state = item.get("state")
    if not isinstance(number, int) or not isinstance(title, str):
        return None
    return f"#{number} {title} ({state})"


async def list_issues(
    cfg: GithubConnectorConfig,
    store: TokenStore,
    owner: str,
    repo: str,
    state: str = "open",
    *,
    transport: Any = None,
) -> str:
    if state not in {"open", "closed", "all"}:
        raise GithubError("Issue state is not valid.")
    payload = await github_call(
        cfg,
        store,
        "GET",
        f"{_repo(owner, repo)}/issues?per_page={_PAGE}&state={state}",
        transport=transport,
    )
    if not isinstance(payload, list):
        raise GithubError("GitHub request failed.")
    rows = [line for item in payload if isinstance(item, dict) and (line := _issue_line(item))]
    text = _lines(rows, _PAGE)
    if len(payload) >= _PAGE:
        text += "\n(truncated to one page)"
    return text


async def get_issue(
    cfg: GithubConnectorConfig,
    store: TokenStore,
    owner: str,
    repo: str,
    number: int,
    *,
    transport: Any = None,
) -> str:
    num = require_number(number)
    payload = await github_call(
        cfg, store, "GET", f"{_repo(owner, repo)}/issues/{num}", transport=transport
    )
    if not isinstance(payload, dict):
        raise GithubError("GitHub request failed.")
    if "pull_request" in payload:
        return "That number is a pull request. Use github_get_pull."
    title = payload.get("title") if isinstance(payload.get("title"), str) else ""
    state = payload.get("state") if isinstance(payload.get("state"), str) else ""
    body = payload.get("body") if isinstance(payload.get("body"), str) else ""
    return _clip(f"#{num} {title}\nstate: {state}\n\n{body}")


async def list_pulls(
    cfg: GithubConnectorConfig,
    store: TokenStore,
    owner: str,
    repo: str,
    state: str = "open",
    *,
    transport: Any = None,
) -> str:
    if state not in {"open", "closed", "all"}:
        raise GithubError("Pull state is not valid.")
    payload = await github_call(
        cfg,
        store,
        "GET",
        f"{_repo(owner, repo)}/pulls?per_page={_PAGE}&state={state}",
        transport=transport,
    )
    if not isinstance(payload, list):
        raise GithubError("GitHub request failed.")
    rows: list[str] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        number = item.get("number")
        title = item.get("title")
        pr_state = item.get("state")
        if isinstance(number, int) and isinstance(title, str):
            rows.append(f"#{number} {title} ({pr_state})")
    text = _lines(rows, _PAGE)
    if len(payload) >= _PAGE:
        text += "\n(truncated to one page)"
    return text


async def get_pull(
    cfg: GithubConnectorConfig,
    store: TokenStore,
    owner: str,
    repo: str,
    number: int,
    *,
    transport: Any = None,
) -> str:
    num = require_number(number)
    payload = await github_call(
        cfg, store, "GET", f"{_repo(owner, repo)}/pulls/{num}", transport=transport
    )
    if not isinstance(payload, dict):
        raise GithubError("GitHub request failed.")
    title = payload.get("title") if isinstance(payload.get("title"), str) else ""
    state = payload.get("state") if isinstance(payload.get("state"), str) else ""
    head = _as_dict(payload.get("head"))
    base = _as_dict(payload.get("base"))
    head_ref = head.get("ref") if isinstance(head.get("ref"), str) else ""
    base_ref = base.get("ref") if isinstance(base.get("ref"), str) else ""
    body = payload.get("body") if isinstance(payload.get("body"), str) else ""
    return _clip(f"#{num} {title}\nstate: {state}\n{head_ref} -> {base_ref}\n\n{body}")


async def pull_checks(
    cfg: GithubConnectorConfig,
    store: TokenStore,
    owner: str,
    repo: str,
    number: int,
    *,
    transport: Any = None,
) -> str:
    num = require_number(number)
    pull = await github_call(
        cfg, store, "GET", f"{_repo(owner, repo)}/pulls/{num}", transport=transport
    )
    if not isinstance(pull, dict):
        raise GithubError("GitHub request failed.")
    head = _as_dict(pull.get("head"))
    sha = head.get("sha")
    if not isinstance(sha, str) or not sha:
        raise GithubError("GitHub request failed.")
    root = _repo(owner, repo)
    checks = await github_call(
        cfg, store, "GET", f"{root}/commits/{sha}/check-runs?per_page={_PAGE}", transport=transport
    )
    status = await github_call(
        cfg, store, "GET", f"{root}/commits/{sha}/status", transport=transport
    )
    rows: list[str] = []
    runs = checks.get("check_runs") if isinstance(checks, dict) else None
    if isinstance(runs, list):
        for run in runs:
            if not isinstance(run, dict):
                continue
            name = run.get("name") if isinstance(run.get("name"), str) else "check"
            conclusion = run.get("conclusion") or run.get("status") or ""
            rows.append(f"{name}: {conclusion}")
    if isinstance(status, dict):
        combined = status.get("state")
        if isinstance(combined, str):
            rows.append(f"combined: {combined}")
    return _lines(rows, _PAGE)


async def fetch_login(
    cfg: GithubConnectorConfig,
    access_token: str,
    *,
    transport: Any = None,
) -> str | None:
    """Best-effort login after connect. A failure leaves the login unknown."""
    try:
        status, payload = await api_json(
            cfg.api_host,
            "GET",
            "/user",
            access_token,
            timeout_seconds=cfg.timeout_seconds,
            transport=transport,
        )
    except GithubError:
        return None
    if status != 200 or not isinstance(payload, dict):
        return None
    return require_login(payload.get("login"))
