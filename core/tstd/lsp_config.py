"""Language-server configuration (TD-5139).

The ``lsp:`` block of the user's ``config.yaml`` lists language servers
by file pattern. TST Desk starts one as a child process speaking LSP over
its stdin and stdout, never a socket, so nothing listens on any address.
Only pydantic here, so ``config.py`` can import it.

A workspace's ``.tst/config.yaml`` cannot add a server: a repo's command
would start as soon as the agent wrote a file, so only the user's own
file can name one.
"""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

MAX_LSP_SERVERS = 10

# Arguments that make a server listen on a socket or pipe instead of
# talking over stdio. TST Desk only speaks stdio, and a listening server
# would be reachable by anything else on the machine (prime directive 1).
_LISTEN_FLAGS = ("--port", "--socket", "--tcp", "--listen", "--host", "--pipe", "--node-ipc")


class LspServerConfig(BaseModel):
    """One language server, picked for the files its ``match`` names."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(default="", max_length=60)
    command: list[str] = Field(min_length=1, max_length=64)
    match: list[str] = Field(min_length=1, max_length=32)
    language_id: str = Field(min_length=1, max_length=40)
    init_options: dict[str, Any] = Field(default_factory=dict)
    start_timeout_seconds: float = Field(default=30.0, gt=0, le=300)
    request_timeout_seconds: float = Field(default=10.0, gt=0, le=120)
    diagnostics_wait_seconds: float = Field(default=3.0, ge=0, le=60)

    @field_validator("command", mode="before")
    @classmethod
    def _split_command(cls, value: Any) -> Any:
        if isinstance(value, str):
            try:
                return shlex.split(value)
            except ValueError as e:
                raise ValueError(f"command cannot be split into words: {e}") from e
        return value

    @field_validator("command")
    @classmethod
    def _stdio_only(cls, value: list[str]) -> list[str]:
        if not value or not value[0].strip():
            raise ValueError("command must start with a program")
        for word in value[1:]:
            flag = word.split("=", 1)[0].casefold()
            if flag in _LISTEN_FLAGS:
                raise ValueError(
                    f"{word!r}: language servers run over stdio only; "
                    "drop the socket or port argument"
                )
        return value

    @property
    def label(self) -> str:
        """``name``, or the program's file name."""
        return self.name.strip() or Path(self.command[0]).name


class LspConfig(BaseModel):
    """The ``lsp:`` block. No servers is the default: nothing starts."""

    model_config = ConfigDict(extra="forbid")

    servers: list[LspServerConfig] = Field(default_factory=list, max_length=MAX_LSP_SERVERS)
    idle_seconds: float = Field(default=600.0, gt=0, le=86400)
    max_diagnostics: int = Field(default=20, ge=1, le=200)

    def server_for(self, rel: str) -> LspServerConfig | None:
        """The first server whose patterns match workspace-relative *rel*."""
        from .after_write import patterns_match

        for server in self.servers:
            if patterns_match(server.match, rel):
                return server
        return None
