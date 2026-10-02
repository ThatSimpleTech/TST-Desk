"""GitHub tools. Every call is classified; this module does not bypass that.

Read tools declare ``auto`` and match the github-read rule. Writes declare
``ask`` and do not set path or host metadata: a path would become Class A
inside the workspace, and a host would become Class C under network deny.
The destination is ``connectors.github`` and the model cannot choose it."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, cast

from ..tools.registry import SideEffectClass, Tool
from ..tools.results import HandlerRefusal
from .github_api import (
    get_file,
    get_issue,
    get_pull,
    get_tree,
    list_issues,
    list_pulls,
    list_repos,
    pull_checks,
)
from .github_classify import GITHUB_ASK_TOOLS, GITHUB_NEVER_TOOLS, GITHUB_READ_TOOLS
from .github_git import run_git
from .github_models import OFF_MESSAGE, GithubError
from .github_tokens import KeychainTokenStore, TokenStore
from .github_write import (
    comment,
    create_branch,
    create_issue,
    delete_branch,
    merge_pull,
    open_pull,
    put_file,
)

_Handler = Callable[..., Awaitable[str]]


def _schema(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def _str(description: str) -> dict[str, str]:
    return {"type": "string", "description": description}


def _int(description: str) -> dict[str, str]:
    return {"type": "integer", "description": description}


def _tool(
    name: str,
    description: str,
    side: SideEffectClass,
    properties: dict[str, Any],
    required: list[str],
    *,
    parallel: bool,
) -> Tool:
    return Tool(
        name=name,
        description=description,
        parameters=_schema(properties, required),
        side_effect_class=side,
        parallel_safe=parallel,
    )


def github_tools() -> list[Tool]:
    """The GitHub tool surface. Settings exists so the refusal is classified."""
    owner = _str("Repository owner")
    repo = _str("Repository name")
    pair = {"owner": owner, "repo": repo}
    reads = [
        _tool(
            "github_list_repos",
            "List repositories this GitHub App installation can see.",
            "auto",
            {},
            [],
            parallel=True,
        ),
        _tool(
            "github_get_file",
            "Read a file from a repository. Directories belong to github_get_tree.",
            "auto",
            {**pair, "path": _str("File path"), "ref": _str("Branch, tag, or commit")},
            ["owner", "repo", "path"],
            parallel=True,
        ),
        _tool(
            "github_get_tree",
            "List a repository tree at a branch or commit.",
            "auto",
            {**pair, "ref": _str("Branch, tag, or commit")},
            ["owner", "repo", "ref"],
            parallel=True,
        ),
        _tool(
            "github_list_issues",
            "List issues. Pull requests are omitted.",
            "auto",
            {**pair, "state": _str("open, closed, or all")},
            ["owner", "repo"],
            parallel=True,
        ),
        _tool(
            "github_get_issue",
            "Read one issue.",
            "auto",
            {**pair, "number": _int("Issue number")},
            ["owner", "repo", "number"],
            parallel=True,
        ),
        _tool(
            "github_list_pulls",
            "List pull requests.",
            "auto",
            {**pair, "state": _str("open, closed, or all")},
            ["owner", "repo"],
            parallel=True,
        ),
        _tool(
            "github_get_pull",
            "Read one pull request.",
            "auto",
            {**pair, "number": _int("Pull request number")},
            ["owner", "repo", "number"],
            parallel=True,
        ),
        _tool(
            "github_pull_checks",
            "Read check runs and combined status for a pull request.",
            "auto",
            {**pair, "number": _int("Pull request number")},
            ["owner", "repo", "number"],
            parallel=True,
        ),
    ]
    asks = [
        _tool(
            "github_create_branch",
            "Create a branch from another branch or the default branch.",
            "ask",
            {**pair, "branch": _str("New branch"), "from_ref": _str("Source branch or commit")},
            ["owner", "repo", "branch"],
            parallel=False,
        ),
        _tool(
            "github_put_file",
            "Create or update a file through the contents API.",
            "ask",
            {
                **pair,
                "path": _str("File path"),
                "content": _str("New file text"),
                "message": _str("Commit message"),
                "branch": _str("Branch to commit on"),
            },
            ["owner", "repo", "path", "content", "message", "branch"],
            parallel=False,
        ),
        _tool(
            "github_open_pull",
            "Open a pull request.",
            "ask",
            {
                **pair,
                "title": _str("Title"),
                "head": _str("Head branch"),
                "base": _str("Base branch"),
                "body": _str("Description"),
            },
            ["owner", "repo", "title", "head", "base"],
            parallel=False,
        ),
        _tool(
            "github_comment",
            "Comment on an issue or pull request.",
            "ask",
            {**pair, "number": _int("Issue or pull number"), "body": _str("Comment text")},
            ["owner", "repo", "number", "body"],
            parallel=False,
        ),
        _tool(
            "github_create_issue",
            "Open an issue.",
            "ask",
            {**pair, "title": _str("Title"), "body": _str("Body")},
            ["owner", "repo", "title"],
            parallel=False,
        ),
        _tool(
            "github_git",
            "Clone, fetch, or push over HTTPS. The token is not written to git config.",
            "ask",
            {
                **pair,
                "action": {"type": "string", "enum": ["clone", "fetch", "push"]},
                "dest": _str("Directory inside the workspace"),
            },
            ["owner", "repo", "action", "dest"],
            parallel=False,
        ),
    ]
    nevers = [
        _tool(
            "github_merge",
            "Merge a pull request. Requires approval.",
            "never",
            {**pair, "number": _int("Pull request number")},
            ["owner", "repo", "number"],
            parallel=False,
        ),
        _tool(
            "github_delete_branch",
            "Delete a branch. Requires approval.",
            "never",
            {**pair, "branch": _str("Branch to delete")},
            ["owner", "repo", "branch"],
            parallel=False,
        ),
        _tool(
            "github_update_settings",
            "GitHub settings and secrets are not available.",
            "never",
            {},
            [],
            parallel=False,
        ),
    ]
    return [*reads, *asks, *nevers]


def register_github_tools(registry: Any) -> None:
    for tool in github_tools():
        registry.register(tool)


def _config(session: object) -> Any:
    from ..config import GithubConnectorConfig, cached_config

    config = getattr(session, "config", None)
    connectors = getattr(config, "connectors", None)
    github = getattr(connectors, "github", None)
    if isinstance(github, GithubConnectorConfig):
        return github
    return cached_config().connectors.github


def _store(session: object) -> TokenStore:
    custom = getattr(session, "github_tokens", None)
    if custom is not None:
        return cast(TokenStore, custom)
    return KeychainTokenStore()


async def _enabled(session: object) -> Any:
    cfg = _config(session)
    if not cfg.enabled():
        raise GithubError(OFF_MESSAGE, phase="off")
    return cfg


async def _run(name: str, session: object, arguments: dict[str, Any]) -> str:
    if "force" in arguments:
        raise GithubError("Force is not available.")
    if name == "github_update_settings":
        raise GithubError("GitHub settings and secrets are not available.")
    cfg = await _enabled(session)
    store = _store(session)
    transport = getattr(session, "github_transport", None)
    owner = arguments.get("owner", "")
    repo = arguments.get("repo", "")
    if name == "github_list_repos":
        return await list_repos(cfg, store, transport=transport)
    if name == "github_get_file":
        return await get_file(
            cfg,
            store,
            owner,
            repo,
            arguments["path"],
            arguments.get("ref", ""),
            transport=transport,
        )
    if name == "github_get_tree":
        return await get_tree(cfg, store, owner, repo, arguments["ref"], transport=transport)
    if name == "github_list_issues":
        return await list_issues(
            cfg, store, owner, repo, arguments.get("state", "open"), transport=transport
        )
    if name == "github_get_issue":
        return await get_issue(cfg, store, owner, repo, arguments["number"], transport=transport)
    if name == "github_list_pulls":
        return await list_pulls(
            cfg, store, owner, repo, arguments.get("state", "open"), transport=transport
        )
    if name == "github_get_pull":
        return await get_pull(cfg, store, owner, repo, arguments["number"], transport=transport)
    if name == "github_pull_checks":
        return await pull_checks(cfg, store, owner, repo, arguments["number"], transport=transport)
    if name == "github_create_branch":
        return await create_branch(
            cfg,
            store,
            owner,
            repo,
            arguments["branch"],
            arguments.get("from_ref", ""),
            transport=transport,
        )
    if name == "github_put_file":
        return await put_file(
            cfg,
            store,
            owner,
            repo,
            arguments["path"],
            arguments["content"],
            arguments["message"],
            arguments["branch"],
            transport=transport,
        )
    if name == "github_open_pull":
        return await open_pull(
            cfg,
            store,
            owner,
            repo,
            arguments["title"],
            arguments["head"],
            arguments["base"],
            arguments.get("body", ""),
            transport=transport,
        )
    if name == "github_comment":
        return await comment(
            cfg, store, owner, repo, arguments["number"], arguments["body"], transport=transport
        )
    if name == "github_create_issue":
        return await create_issue(
            cfg,
            store,
            owner,
            repo,
            arguments["title"],
            arguments.get("body", ""),
            transport=transport,
        )
    if name == "github_git":
        from pathlib import Path

        workspace = getattr(session, "workspace_path", None)
        if not isinstance(workspace, Path):
            raise GithubError("No workspace is open.")
        runner = getattr(session, "github_runner", None)
        return await run_git(
            workspace=workspace,
            web_host=cfg.web_host,
            api_host=cfg.api_host,
            client_id=cfg.client_id,
            timeout_seconds=cfg.timeout_seconds,
            action=str(arguments.get("action", "")),
            dest=str(arguments.get("dest", "")),
            owner=str(owner),
            repo=str(repo),
            runner=runner,
        )
    if name == "github_merge":
        return await merge_pull(cfg, store, owner, repo, arguments["number"], transport=transport)
    if name == "github_delete_branch":
        return await delete_branch(
            cfg, store, owner, repo, arguments["branch"], transport=transport
        )
    raise GithubError("GitHub request failed.")


def _bind(name: str) -> _Handler:
    async def handler(session: object, tool_call_id: str = "", **arguments: Any) -> str:
        del tool_call_id
        try:
            return await _run(name, session, arguments)
        except GithubError as exc:
            raise HandlerRefusal("github_failed", exc.message) from None

    return handler


def register_github_handlers(dispatcher: Any) -> None:
    for name in (*GITHUB_READ_TOOLS, *GITHUB_ASK_TOOLS, *GITHUB_NEVER_TOOLS):
        dispatcher.register_handler(name, _bind(name))
