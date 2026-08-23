"""The signed charter (TD-4001) — spec §12.4 as a start gate.

An autonomous run may begin only against a charter the human wrote and
signed by committing it: ``<workspace>/.tst/autonomy/CHARTER.md``, a
markdown file whose YAML frontmatter carries the §12.4 shape
(``objective``, ``definition_of_done``, ``source_of_truth``,
``boundary``, ``caps``, ``stop_conditions``).  Prose below the closing
fence is free-form human context and is ignored here.  ``boundary`` and
``caps`` reuse the ``.tst/config.yaml`` models exactly — one wall
vocabulary, not two — and are required: a charter that omitted them
would otherwise default to the loosest wall on the shelf.

Three gates, all fail-closed:

- the file must exist and parse to a valid :class:`Charter`; every
  refusal names the offending field(s);
- it must be git-tracked with no uncommitted change to that path — a
  dirty edit and a staged-but-uncommitted one both refuse;
- the agent cannot write it: the classifier treats the path as steering
  (Class C), while the sibling ``DECISIONS.md`` ledger stays writable.

The start flow itself arrives with TD-4003; this module only answers
"may a run start here?" via :func:`charter_start_error`.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from ..boundary_config import BoundarySection, CapsSection
from ..config import ConfigError

_GIT_TIMEOUT = 30.0

# A charter is a page or three of YAML; anything past this is not a
# charter but something else wearing one, and parsing megabytes on the
# event loop is how a start check becomes an outage.
MAX_CHARTER_BYTES = 1024 * 1024

CHARTER_RELATIVE_PARTS = (".tst", "autonomy", "CHARTER.md")
CHARTER_COMMIT_SUBJECT = "tst: sign charter"


def charter_path(workspace: str | Path) -> Path:
    """Path of *workspace*'s signed charter."""
    return Path(workspace).joinpath(*CHARTER_RELATIVE_PARTS)


class CharterError(ConfigError):
    """Raised when the charter is absent, unparseable, or invalid."""


class Charter(BaseModel):
    """Spec §12.4 charter shape (TD-4001).

    Attributes:
        objective: What the run is for; non-empty.
        definition_of_done: At least one done condition — a run with no
            definition of done can never stop.
        source_of_truth: Where the run's facts come from; may be empty.
        boundary: The declared wall; required.  Reuses ``BoundarySection``
            from ``.tst/config.yaml``.  A signed charter states its wall
            explicitly — defaulting to the loosest wall on omission would
            let an omitted section silently widen what the human wrote in
            config.  The charter narrows the workspace wall, never widens
            it; enforcement of that lands with the runner (TD-4003).
        caps: Spend/wall-clock/iteration limits; required, same rule.
        stop_conditions: Additional reasons to stop early; may be empty.

    Unknown keys are refused at every level (``extra="forbid"``, and the
    shared sections forbid them too): a typo like ``definiton_of_done``
    fails loudly with the misspelled name rather than silently dropping
    a constraint.
    """

    model_config = ConfigDict(extra="forbid")

    objective: str
    definition_of_done: list[str]
    source_of_truth: list[str] = Field(default_factory=list)
    boundary: BoundarySection
    caps: CapsSection
    stop_conditions: list[str] = Field(default_factory=list)

    @field_validator("objective")
    @classmethod
    def _objective_non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("objective must be a non-empty string")
        return v.strip()

    @field_validator("definition_of_done")
    @classmethod
    def _dod_non_empty(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError(
                "definition_of_done must list at least one done condition "
                "(a run with no definition of done can never stop)"
            )
        if any(not isinstance(entry, str) or not entry.strip() for entry in v):
            raise ValueError("definition_of_done entries must be non-empty strings")
        return v

    @field_validator("source_of_truth", "stop_conditions")
    @classmethod
    def _entries_non_empty(cls, v: list[str]) -> list[str]:
        if any(not isinstance(entry, str) or not entry.strip() for entry in v):
            raise ValueError("entries must be non-empty strings")
        return v

    @property
    def slug(self) -> str:
        """Branch-safe slug of the objective (``tst/auto/<slug>``, TD-4102)."""
        return charter_slug(self.objective)


def charter_slug(objective: str) -> str:
    """Turn a charter objective into a git-ref-safe slug.

    Always a non-empty ``[a-z][a-z0-9-]*`` that is never ``main`` or
    ``master``, so :func:`tstd.autonomy.checkpoint.auto_branch` cannot
    name a primary branch.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", objective.strip().lower()).strip("-")[:48]
    if not slug or not slug[0].isalpha():
        slug = ("run-" + slug).strip("-")[:48]
    if not slug or not slug[0].isalpha():
        slug = "run"
    if slug in {"main", "master"}:
        slug = f"run-{slug}"
    return slug


def _frontmatter(text: str) -> str:
    """The YAML to parse: the fenced block, or the whole text as one doc.

    A markdown charter opens with ``---`` at column 0 and closes the
    fence the same way before its free-form body.  Only column-0 fences
    count: an indented ``---`` inside a block scalar is content, and
    treating it as a closer would silently truncate the signed contract.
    An opening fence with no closer leaves nothing to strip — and a
    leading ``---`` is also a legal YAML document marker, so the whole
    text parses as-is either way.
    """
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        return text
    for i in range(1, len(lines)):
        if lines[i] == "---":
            return "\n".join(lines[1:i])
    return text


def charter_notes(text: str) -> str:
    """Free-form prose after a column-0 closing fence, or empty.

    Whole-file YAML has no notes.  An opening fence with no closer is
    also YAML, not a truncated body — same rule as :func:`_frontmatter`.
    """
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        return ""
    for i in range(1, len(lines)):
        if lines[i] == "---":
            return "\n".join(lines[i + 1 :]).strip()
    return ""


class _UniqueKeyLoader(yaml.SafeLoader):
    """A SafeLoader that refuses duplicate mapping keys.

    A signed contract may not last-win silently: two ``objective:`` keys
    mean the charter is malformed, not that the second one won.
    """

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
        mapping: dict[Any, Any] = {}
        for key_node, value_node in node.value:
            key: Any = self.construct_object(key_node, deep=deep)
            try:
                duplicate = key in mapping
            except TypeError as e:
                raise yaml.constructor.ConstructorError(
                    "while constructing a mapping",
                    node.start_mark,
                    f"found unhashable key {key!r}",
                    key_node.start_mark,
                ) from e
            if duplicate:
                raise yaml.constructor.ConstructorError(
                    "while constructing a mapping",
                    node.start_mark,
                    f"found duplicate key {key!r}",
                    key_node.start_mark,
                )
            mapping[key] = self.construct_object(value_node, deep=deep)
        return mapping


def parse_charter(text: str, *, source: Path | str = "<charter>") -> Charter:
    """Parse *text* into a :class:`Charter`.

    Accepts the markdown frontmatter form (``---\\nyaml\\n---\\nbody``)
    or a whole-file YAML mapping; prose after the closing fence is
    ignored.

    Raises:
        CharterError: On invalid YAML, a non-mapping document, or empty
            content — the empty case names the required fields.  A
            validation failure is re-raised the way
            ``load_workspace_boundary`` does it: ``{loc}: {msg}``, so the
            message names the offending field.
    """
    try:
        data: Any = yaml.load(_frontmatter(text), Loader=_UniqueKeyLoader)
    except yaml.YAMLError as e:
        raise CharterError(f"Invalid YAML in {source}: {e}") from e

    if data is None or data == {}:
        raise CharterError(
            f"{source} defines no charter fields — 'objective' and "
            "'definition_of_done' are required"
        )
    if not isinstance(data, dict):
        raise CharterError(f"{source} must contain a YAML mapping at the top level")

    try:
        return Charter.model_validate(data)
    except ValidationError as e:
        first = e.errors()[0]
        loc = ".".join(str(p) for p in first["loc"])
        raise CharterError(f"Invalid charter in {source}: {loc}: {first['msg']}") from e


async def load_charter(workspace: str | Path) -> Charter:
    """Read and validate ``CHARTER.md`` from *workspace*.

    Raises:
        CharterError: When the file is absent — it must exist and be
            signed before an autonomous run starts — unreadable, oversized
            (``MAX_CHARTER_BYTES``), or invalid.
    """
    path = charter_path(workspace)
    if not path.is_file():
        raise CharterError(
            f"No charter found at {path} — .tst/autonomy/CHARTER.md must exist "
            "and be signed before an autonomous run starts"
        )
    try:
        if path.stat().st_size > MAX_CHARTER_BYTES:
            raise CharterError(
                f"{path} is larger than {MAX_CHARTER_BYTES} bytes — "
                "that is not a charter; refusing to parse it"
            )
        # utf-8-sig: a BOM (Windows-authored files) must not turn valid
        # YAML into a cryptic parse failure.  PyYAML only strips one at
        # the very start of a stream, and frontmatter parsing feeds it a
        # slice that may begin mid-stream.
        text = path.read_text(encoding="utf-8-sig")
    except OSError as e:
        raise CharterError(f"Failed to read {path}: {e}") from e
    # Parse off the event loop: yaml on an unexpected multi-hundred-KB
    # file is blocking work.
    return await asyncio.to_thread(parse_charter, text, source=path)


async def _git(
    workspace: Path, *args: str, env: dict[str, str] | None = None
) -> tuple[int, str, str]:
    """Run ``git -C <workspace> <args>``; returns (rc, stdout, stderr).

    Mirrors ``Checkpointer._git`` (TD-705), module-level because the
    charter gate runs before any session exists to own a checkpointer.
    """
    proc = await asyncio.create_subprocess_exec(
        "git",
        "-C",
        str(workspace),
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    try:
        out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=_GIT_TIMEOUT)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise
    rc = proc.returncode if proc.returncode is not None else -1
    return rc, out_b.decode("utf-8", errors="replace"), err_b.decode("utf-8", errors="replace")


def _git_env() -> dict[str, str]:
    """Child environment for the gate's git calls.

    Secrets are stripped (the shell tool's filter), and the variables
    that redirect git to a *different* repository are popped: an ambient
    ``GIT_DIR`` pointing at a repo where some identical-looking charter
    happens to be committed must not make this workspace's untracked
    charter look clean.  Repo discovery here is purely cwd-based.

    Function-local import: keeps ``tstd.tools`` out of this module's
    import graph, the same way ``classifier.py`` defers its tool imports.
    """
    from ..tools.shell import sanitized_env  # local import: no cycle

    env = sanitized_env()
    for var in (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_COMMON_DIR",
    ):
        env.pop(var, None)
    return env


async def sign_charter(workspace: str | Path) -> str | None:
    """Commit ``CHARTER.md`` if it is dirty. ``None`` when it is signed.

    This is the human sign (TD-4003). It uses the repo's own git
    identity — not the agent identity memory commits use — because the
    start button is the human. Already-clean is success, not an error.
    """
    ws = Path(workspace)
    rel = "/".join(CHARTER_RELATIVE_PARTS)
    if not charter_path(ws).is_file():
        return f"{rel} is missing — write a charter before an autonomous run starts"
    env = _git_env()
    try:
        rc, out, _ = await _git(ws, "rev-parse", "--is-inside-work-tree", env=env)
        if rc != 0 or out.strip() != "true":
            return (
                f"{rel} must be committed before an autonomous run starts, "
                f"but {ws} is not inside a git repository"
            )
        rc, _, err = await _git(ws, "add", "--", rel, env=env)
        if rc != 0:
            return err.strip() or f"git add failed for {rel}"
        rc, _, _ = await _git(ws, "diff", "--cached", "--quiet", "--", rel, env=env)
        if rc == 0:
            return None
        rc, out, err = await _git(ws, "commit", "-m", CHARTER_COMMIT_SUBJECT, "--", rel, env=env)
        if rc != 0:
            return (err or out).strip() or f"git commit failed for {rel}"
    except (OSError, TimeoutError) as e:
        return f"git unavailable ({e}); cannot sign {rel}"
    return None


async def charter_start_error(workspace: str | Path) -> str | None:
    """Why an autonomous run may not start here, or ``None`` when it may.

    In order: the charter must exist and validate (the refusal names the
    offending fields), the workspace must sit inside a git repository,
    the charter must be tracked, and its committed bytes must be the
    bytes on disk.  The last check hashes the working file against
    ``HEAD:<path>`` rather than trusting ``git status``, so
    skip-worktree/assume-unchanged bits cannot hide a tampered charter;
    a staged-but-uncommitted change refuses too.  Tracking accepts any
    case (folded compare): on case-insensitive filesystems the index may
    hold ``charter.md`` while the disk shows ``CHARTER.md``, and the
    subsequent pathspecs use the name the index actually tracks.  A git
    failure refuses too: a state that cannot be verified is not thereby
    clean.
    """
    ws = Path(workspace)
    rel = "/".join(CHARTER_RELATIVE_PARTS)
    try:
        await load_charter(ws)
    except CharterError as e:
        return str(e)

    env = _git_env()
    try:
        rc, out, _ = await _git(ws, "rev-parse", "--is-inside-work-tree", env=env)
        if rc != 0 or out.strip() != "true":
            return (
                f"{rel} must be committed before an autonomous run starts, "
                f"but {ws} is not inside a git repository"
            )
        rc, out, _ = await _git(ws, "ls-files", "--", rel, env=env)
        if rc != 0:
            return f"git ls-files failed for {rel} — refusing to start"
        tracked = [name for name in out.splitlines() if name.casefold() == rel.casefold()]
        if not tracked:
            return (
                f"{rel} is not git-tracked — commit the signed charter before "
                "an autonomous run starts"
            )
        tracked_rel = tracked[0]

        def _uncommitted() -> str:
            return f"{rel} has uncommitted changes — commit them before an autonomous run starts"

        rc, head_blob, _ = await _git(ws, "rev-parse", f"HEAD:{tracked_rel}", env=env)
        if rc != 0:
            # Tracked in the index but not in any commit: staged-new, or a
            # repository with zero commits.
            return _uncommitted()
        rc2, disk_blob, err = await _git(ws, "hash-object", "--", tracked_rel, env=env)
        if rc != 0 or rc2 != 0:
            return f"git hash check failed for {rel}: {err.strip()} — refusing to start"
        if disk_blob.strip() != head_blob.strip():
            return _uncommitted()
    except (OSError, TimeoutError) as e:
        return f"git unavailable ({e}); cannot verify that {rel} is committed"
    return None
