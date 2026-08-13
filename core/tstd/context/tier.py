"""Per-tier context routing (TD-508).

Composes per-tier context blocks by selecting which steering sources,
manifest, and auxiliary blocks each tier receives (spec §4.6):

| Tier      | Gets                                                                 |
|-----------|----------------------------------------------------------------------|
| Brain     | Full steering + memory + workspace manifest                          |
| Worker    | Steering + current task + relevant files.  No memory/manifest.       |
| Validator | Standards/conventions steering subset + diff + test output. Only.    |

The validator's steering subset is configurable via ``TierContextConfig``
with a documented default -- see ``DEFAULT_VALIDATOR_SUBSET``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ..router import TierName
from .assembler import ContextAssembler, _path_matches_glob
from .discover import SteeringFileResolver, SteeringSource

# ── Default validator subset ────────────────────────────────────────────

#: Default glob patterns selecting the standards/conventions steering
#: sources for the validator tier.  Patterns are matched against the
#: steering file's absolute path using the same semantics as ``appliesTo``
#: (TD-503): patterns without a ``/`` match the basename at any depth.
#:
#: The default covers the workspace conventions doc (``AGENTS.md``) plus
#: rules whose names carry conventional standards/conventions/style
#: labels.  Override via ``TierContextConfig.validator_subset``.
DEFAULT_VALIDATOR_SUBSET: tuple[str, ...] = (
    "AGENTS.md",
    "standards*.md",
    "conventions*.md",
    "style*.md",
)


# ── Config ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class TierContextConfig:
    """Controls which blocks are included in each tier's context.

    Normally you would use ``default_config_for_tier()`` rather than
    constructing this directly.  The config is exposed so that the
    validator's *validator_subset* can be overridden.

    Attributes:
        validator_subset: Glob patterns selecting the steering sources
            included for the validator.  Replaces (not extends) the
            default.  Empty tuple means no steering at all.
    """

    validator_subset: tuple[str, ...] = DEFAULT_VALIDATOR_SUBSET


# ── Per-tier defaults ───────────────────────────────────────────────────


def default_config_for_tier(tier: TierName) -> TierContextConfig:
    """Return the default ``TierContextConfig`` for *tier*.

    Brain: steering + memory + manifest.
    Worker: steering + task + relevant files.
    Validator: steering subset + diff + test output.
    """
    return TierContextConfig()


# ── Result ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class TierContext:
    """The assembled context for one tier.

    Attributes:
        tier: The target tier.
        blocks: Ordered mapping of block name to content.  Only
            blocks that were requested *and* supplied appear here.
            Common keys: ``"steering"``, ``"memory"``, ``"manifest"``,
            ``"task"``, ``"relevant_files"``, ``"diff"``,
            ``"test_output"``.
    """

    tier: TierName
    blocks: dict[str, str]

    @property
    def text(self) -> str:
        """The full assembled prompt, blocks joined by blank lines.

        The block order is deterministic (insertion order of *blocks*).
        """
        return "\n\n".join(self.blocks.values())


# ── Subset matching ──────────────────────────────────────────────────────


def _source_matches_subset(
    source: SteeringSource,
    patterns: tuple[str, ...],
) -> bool:
    """Return True if *source* matches any of the *patterns*.

    Matches against the source's absolute path using the same glob
    semantics as ``appliesTo`` (TD-503): patterns without a ``/`` match
    the basename at any depth.
    """
    if not patterns:
        return False
    src_path = str(source.path)
    return any(_path_matches_glob(src_path, p) for p in patterns)


def _build_validator_filter(
    patterns: tuple[str, ...],
) -> Callable[[SteeringSource], bool]:
    """Build a source filter that selects only sources matching *patterns*."""
    if not patterns:
        return lambda _: False

    def _filter(source: SteeringSource) -> bool:
        return _source_matches_subset(source, patterns)

    return _filter


# ── Assembly ──────────────────────────────────────────────────────────────


def assemble_for_tier_sync(
    tier: TierName,
    *,
    workspace_path: str | Path,
    home_dir: str | Path | None = None,
    task: str | None = None,
    matched_paths: set[str] | None = None,
    manifest_text: str | None = None,
    diff: str | None = None,
    test_output: str | None = None,
    memory: str | None = None,
    config: TierContextConfig | None = None,
) -> TierContext:
    """Synchronous variant of :func:`assemble_for_tier` (tests, CLI).

    Args:
        tier: The target tier (``"brain"``, ``"worker"``, ``"validator"``).
        workspace_path: Path to the workspace root.
        home_dir: Override home directory (test seam).  Defaults to real
            user home.
        task: Current task description (worker).
        matched_paths: Workspace-relative paths the session has touched.
            Filters path-scoped rules (worker) and populates the
            ``relevant_files`` block (worker).
        manifest_text: Rendered workspace manifest text (brain).
        diff: Diff text (validator).
        test_output: Test output text (validator).
        memory: Memory block content (brain).
        config: Tier config override.  Defaults to
            ``default_config_for_tier(tier)``.

    Returns:
        A ``TierContext`` with the assembled blocks.
    """
    cfg = config if config is not None else default_config_for_tier(tier)

    # Build the assembler (with a test seam for home_dir).
    resolver = SteeringFileResolver(home_dir=home_dir) if home_dir is not None else None
    assembler = ContextAssembler(resolver=resolver)

    # Assemble steering, with a source filter for the validator.
    source_filter: Callable[[SteeringSource], bool] | None = None
    if tier == "validator":
        source_filter = _build_validator_filter(cfg.validator_subset)

    assembled = assembler.assemble_sync(
        workspace_path,
        matched_paths=matched_paths,
        source_filter=source_filter,
    )

    # Compose blocks per tier.  An empty steering block (e.g. a
    # validator subset matching nothing) is omitted entirely so tiers
    # that receive no steering don't carry an empty block.
    blocks: dict[str, str] = {}
    if assembled.block:
        blocks["steering"] = assembled.block

    if tier == "brain":
        if memory is not None:
            blocks["memory"] = memory
        if manifest_text is not None:
            blocks["manifest"] = manifest_text

    elif tier == "worker":
        if task is not None:
            blocks["task"] = task
        if matched_paths is not None and matched_paths:
            blocks["relevant_files"] = "\n".join(sorted(matched_paths))

    elif tier == "validator":
        if diff is not None:
            blocks["diff"] = diff
        if test_output is not None:
            blocks["test_output"] = test_output

    return TierContext(tier=tier, blocks=blocks)


async def assemble_for_tier(
    tier: TierName,
    *,
    workspace_path: str | Path,
    home_dir: str | Path | None = None,
    task: str | None = None,
    matched_paths: set[str] | None = None,
    manifest_text: str | None = None,
    diff: str | None = None,
    test_output: str | None = None,
    memory: str | None = None,
    config: TierContextConfig | None = None,
) -> TierContext:
    """Assemble the per-tier context for *tier*.

    Runs the filesystem work in a worker thread so the event loop
    is never blocked (AGENTS.md §6).  See :func:``assemble_for_tier_sync``
    for full parameter documentation.
    """
    return await asyncio.to_thread(
        assemble_for_tier_sync,
        tier,
        workspace_path=workspace_path,
        home_dir=home_dir,
        task=task,
        matched_paths=matched_paths,
        manifest_text=manifest_text,
        diff=diff,
        test_output=test_output,
        memory=memory,
        config=config,
    )
