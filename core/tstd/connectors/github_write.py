"""GitHub writes: branches, files, pull requests, issues, merge, delete."""

from __future__ import annotations

import base64
from typing import Any

from ..config import GithubConnectorConfig
from .github_api import _as_dict, _repo, github_call
from .github_models import (
    GithubError,
    quote_branch,
    quote_path,
    require_branch,
    require_number,
    require_path,
    require_ref,
)
from .github_tokens import TokenStore

_MAX_PUT_BYTES = 1_000_000
_MAX_COMMENT = 65_536


async def _default_branch(
    cfg: GithubConnectorConfig, store: TokenStore, owner: str, repo: str, *, transport: Any
) -> str:
    payload = await github_call(cfg, store, "GET", _repo(owner, repo), transport=transport)
    if not isinstance(payload, dict) or not isinstance(payload.get("default_branch"), str):
        raise GithubError("GitHub request failed.")
    return require_branch(payload["default_branch"])


async def create_branch(
    cfg: GithubConnectorConfig,
    store: TokenStore,
    owner: str,
    repo: str,
    branch: str,
    from_ref: str = "",
    *,
    transport: Any = None,
) -> str:
    name = require_branch(branch)
    source = (
        require_ref(from_ref)
        if from_ref
        else await _default_branch(cfg, store, owner, repo, transport=transport)
    )
    ref_payload = await github_call(
        cfg,
        store,
        "GET",
        f"{_repo(owner, repo)}/git/ref/heads/{quote_branch(source)}",
        transport=transport,
    )
    if not isinstance(ref_payload, dict):
        raise GithubError("GitHub request failed.")
    obj = _as_dict(ref_payload.get("object"))
    sha = obj.get("sha")
    if not isinstance(sha, str) or not sha:
        raise GithubError("GitHub request failed.")
    await github_call(
        cfg,
        store,
        "POST",
        f"{_repo(owner, repo)}/git/refs",
        body={"ref": f"refs/heads/{name}", "sha": sha},
        transport=transport,
    )
    return f"created refs/heads/{name} at {sha}"


async def put_file(
    cfg: GithubConnectorConfig,
    store: TokenStore,
    owner: str,
    repo: str,
    path: str,
    content: str,
    message: str,
    branch: str,
    *,
    transport: Any = None,
) -> str:
    if not isinstance(content, str) or not isinstance(message, str) or not message.strip():
        raise GithubError("Commit message is required.")
    raw = content.encode("utf-8")
    if len(raw) > _MAX_PUT_BYTES:
        raise GithubError("That file is too large to commit.")
    quoted = quote_path(path)
    ref = require_branch(branch)
    existing: Any = None
    try:
        existing = await github_call(
            cfg,
            store,
            "GET",
            f"{_repo(owner, repo)}/contents/{quoted}?ref={ref}",
            transport=transport,
        )
    except GithubError as exc:
        if exc.message != "GitHub could not find that.":
            raise
    body: dict[str, Any] = {
        "message": message,
        "content": base64.b64encode(raw).decode("ascii"),
        "branch": ref,
    }
    if isinstance(existing, dict) and isinstance(existing.get("sha"), str):
        body["sha"] = existing["sha"]
    await github_call(
        cfg,
        store,
        "PUT",
        f"{_repo(owner, repo)}/contents/{quoted}",
        body=body,
        transport=transport,
    )
    verb = "updated" if isinstance(existing, dict) else "created"
    return f"{verb} {require_path(path)} on {ref}"


async def open_pull(
    cfg: GithubConnectorConfig,
    store: TokenStore,
    owner: str,
    repo: str,
    title: str,
    head: str,
    base: str,
    body: str = "",
    *,
    transport: Any = None,
) -> str:
    if not isinstance(title, str) or not title.strip():
        raise GithubError("A pull request title is required.")
    payload = await github_call(
        cfg,
        store,
        "POST",
        f"{_repo(owner, repo)}/pulls",
        body={
            "title": title,
            "head": require_branch(head),
            "base": require_branch(base),
            "body": body if isinstance(body, str) else "",
        },
        transport=transport,
    )
    number = payload.get("number") if isinstance(payload, dict) else None
    if not isinstance(number, int):
        raise GithubError("GitHub request failed.")
    return f"opened #{number} {title}"


async def comment(
    cfg: GithubConnectorConfig,
    store: TokenStore,
    owner: str,
    repo: str,
    number: int,
    body: str,
    *,
    transport: Any = None,
) -> str:
    if not isinstance(body, str) or not body.strip() or len(body) > _MAX_COMMENT:
        raise GithubError("That comment is empty or too long.")
    num = require_number(number)
    await github_call(
        cfg,
        store,
        "POST",
        f"{_repo(owner, repo)}/issues/{num}/comments",
        body={"body": body},
        transport=transport,
    )
    return f"commented on #{num}"


async def create_issue(
    cfg: GithubConnectorConfig,
    store: TokenStore,
    owner: str,
    repo: str,
    title: str,
    body: str = "",
    *,
    transport: Any = None,
) -> str:
    if not isinstance(title, str) or not title.strip():
        raise GithubError("An issue title is required.")
    payload = await github_call(
        cfg,
        store,
        "POST",
        f"{_repo(owner, repo)}/issues",
        body={"title": title, "body": body if isinstance(body, str) else ""},
        transport=transport,
    )
    number = payload.get("number") if isinstance(payload, dict) else None
    if not isinstance(number, int):
        raise GithubError("GitHub request failed.")
    return f"opened issue #{number} {title}"


async def merge_pull(
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
        cfg,
        store,
        "PUT",
        f"{_repo(owner, repo)}/pulls/{num}/merge",
        body={"merge_method": "merge"},
        transport=transport,
    )
    sha = payload.get("sha") if isinstance(payload, dict) else ""
    if not isinstance(sha, str):
        sha = ""
    return f"merged #{num} at {sha}".rstrip()


async def delete_branch(
    cfg: GithubConnectorConfig,
    store: TokenStore,
    owner: str,
    repo: str,
    branch: str,
    *,
    transport: Any = None,
) -> str:
    name = require_branch(branch)
    await github_call(
        cfg,
        store,
        "DELETE",
        f"{_repo(owner, repo)}/git/refs/heads/{quote_branch(name)}",
        transport=transport,
    )
    return f"deleted refs/heads/{name}"
