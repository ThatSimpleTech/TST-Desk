"""Cache-aware prompt assembly (TD-305).

Assembles the system prompt in the stable-prefix order from spec §4.5:

    [1]  TST Desk base system prompt  ← never changes
    [1b] Workspace root (absolute)    ← constant for the session (TD-1810)
    [2]  Steering block (resolved)    ← changes only when files change
    [3]  Memory block (relevant)      ← changes between sessions
    [4]  Workspace manifest           ← changes as files change
    [5]  Conversation                 ← appended by the loop, per turn

Blocks 1-2 are the provider prefix-cache target: on a long session they
are the difference between full-price and cache-read pricing on the
brain tier.  The assembler therefore returns, alongside the full text,
a SHA-256 of the
prefix and its token count, so tests and logs can assert that the
prefix stayed byte-identical across turns.

Per-tier composition (spec §4.6, TD-508) is delegated to
:func:`assemble_for_tier_sync`; this module adds the base prompt, the
memory placeholder, and the prefix hash on top.
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from pathlib import Path

from ..router import TierName
from .assembler import AssembledSteering
from .manifest import ManifestConfig, WorkspaceManifest
from .tier import TierContextConfig, assemble_for_tier_sync
from .tokens import heuristic_count

#: Block [1] — the TST Desk base system prompt. Never changes.
BASE_SYSTEM_PROMPT = (
    "You are TST Desk, a local agent workspace that plans, edits, and "
    "verifies work in the user's repository. Follow the steering "
    "instructions below, use the provided tools, and report concisely."
)

#: Block [3] placeholder until the memory story lands (spec §5).  Keeps
#: the memory slot positionally stable in the prompt so a real memory
#: block can slot in without reordering the cache prefix.
MEMORY_PLACEHOLDER = "<!-- memory: none loaded for this session -->"

#: Opening label of block [1b].  Exposed so callers can assert the root
#: is stated exactly once rather than counting occurrences of the path
#: itself, which also appears in steering provenance comments.
WORKSPACE_ROOT_LABEL = "Workspace root:"

#: Block [1b] — the workspace root (TD-1810).  Every ``fs_*`` tool
#: advertises its ``path`` argument as absolute while the manifest lists
#: entries workspace-relative, so without this block the model has to
#: guess the prefix that joins the two.  The worked example is spelled
#: out rather than implied: the failing behaviour is a small model
#: emitting the relative path verbatim, and showing the join once costs
#: less than a retry.
_WORKSPACE_ROOT_TEMPLATE = (
    "{label} {root}\n"
    "That is an absolute path on this machine. Workspace files are listed "
    "relative to this root, so the absolute path of a listed file is the root "
    'joined with its listed path: the entry "src/app.py" means '
    '"{root}/src/app.py". Tool arguments that ask for an absolute path must be '
    "written that way — never pass a relative path to a tool."
)


def workspace_root_block(workspace_path: str | Path) -> str:
    """Render block [1b]: the absolute workspace root, stated once.

    The root is rendered resolved and with POSIX separators for the same
    reason the manifest renders its entries that way (TD-1406): the model
    concatenates the two, and on Windows a backslash root would also have
    to survive JSON string escaping inside a tool-call argument.

    Args:
        workspace_path: Path to the workspace root.  Resolved, so a
            relative or symlinked path still yields the canonical root
            the path guard will accept.

    Returns:
        The rendered block text.
    """
    root = Path(workspace_path).resolve().as_posix()
    return _WORKSPACE_ROOT_TEMPLATE.format(label=WORKSPACE_ROOT_LABEL, root=root)


@dataclass(frozen=True)
class AssembledPrompt:
    """The assembled system prompt plus its cache-prefix fingerprint.

    Attributes:
        tier: The tier this prompt was assembled for.
        text: The full system prompt - blocks 1-4 in stable-prefix
            order, ready to be placed before the conversation.
        prefix: Blocks 1-2 (base + workspace root + steering) - the
            provider prefix-cache target.
        prefix_hash: SHA-256 of *prefix*.
        prefix_tokens: Heuristic token count of *prefix*.
        steering: The underlying :class:`AssembledSteering` from the
            context assembler.  Carried so the loop can emit the
            instruction stack and detect steering changes (TD-509).
    """

    tier: TierName
    text: str
    prefix: str
    prefix_hash: str
    prefix_tokens: int
    steering: AssembledSteering


class PromptAssembler:
    """Assembles the per-tier system prompt in stable-prefix order.

    Wraps :func:`assemble_for_tier_sync` (TD-508) with the base prompt,
    a memory placeholder for the brain tier, and the workspace manifest.
    The last assembled prompt is kept on ``last_assembled`` so tests and
    the loop can observe prefix stability across turns.
    """

    def __init__(
        self,
        workspace_path: str | Path,
        *,
        home_dir: str | Path | None = None,
        manifest_config: ManifestConfig | None = None,
        tier_config: TierContextConfig | None = None,
    ) -> None:
        """Create an assembler bound to *workspace_path*.

        Args:
            workspace_path: Path to the workspace root.
            home_dir: Override home directory (test seam).
            manifest_config: Optional manifest limits (depth/caps).
            tier_config: Optional per-tier context config override.
        """
        self._workspace = Path(workspace_path)
        self._home_dir = home_dir
        self._manifest = WorkspaceManifest(manifest_config or ManifestConfig())
        self._tier_config = tier_config
        self.last_assembled: AssembledPrompt | None = None

    async def assemble(
        self,
        tier: TierName,
        *,
        task: str | None = None,
        matched_paths: set[str] | None = None,
        memory: str | None = None,
        diff: str | None = None,
        test_output: str | None = None,
        approved_imports: frozenset[Path] = frozenset(),
        denied_imports: frozenset[Path] = frozenset(),
    ) -> AssembledPrompt:
        """Assemble the system prompt for *tier*.

        Runs the filesystem work in a worker thread so the event loop
        is never blocked (AGENTS.md §6).
        """
        return await asyncio.to_thread(
            self.assemble_sync,
            tier,
            task=task,
            matched_paths=matched_paths,
            memory=memory,
            diff=diff,
            test_output=test_output,
            approved_imports=approved_imports,
            denied_imports=denied_imports,
        )

    def assemble_sync(
        self,
        tier: TierName,
        *,
        task: str | None = None,
        matched_paths: set[str] | None = None,
        memory: str | None = None,
        diff: str | None = None,
        test_output: str | None = None,
        approved_imports: frozenset[Path] = frozenset(),
        denied_imports: frozenset[Path] = frozenset(),
    ) -> AssembledPrompt:
        """Synchronous variant of :meth:`assemble` (tests, CLI)."""
        # The brain tier carries the workspace manifest; others don't.
        manifest_text: str | None = None
        if tier == "brain":
            manifest_text = self._manifest.build(self._workspace).text

        # Brain always occupies the memory slot — a placeholder until
        # the memory module lands (spec §5).
        memory_block: str | None
        if tier == "brain":
            memory_block = memory if memory is not None else MEMORY_PLACEHOLDER
        else:
            memory_block = None

        context = assemble_for_tier_sync(
            tier,
            workspace_path=self._workspace,
            home_dir=self._home_dir,
            task=task,
            matched_paths=matched_paths,
            manifest_text=manifest_text,
            memory=memory_block,
            diff=diff,
            test_output=test_output,
            config=self._tier_config,
            approved_imports=approved_imports,
            denied_imports=denied_imports,
        )

        # Stable-prefix order: base, then the workspace root, then the
        # tier blocks in TD-508's insertion order (steering → memory →
        # manifest for brain; steering → task → relevant files for
        # worker; steering → diff → test output for validator).
        #
        # The root sits ahead of steering, not after it (TD-1810).  It is
        # constant for the session, and steering is the earliest block
        # that can change mid-session (a reload, TD-509) — anything after
        # a changed byte is re-tokenised, so the root would pay for every
        # steering edit if it followed.  Every tier gets it: the worker
        # calls the same fs_* tools, and a per-tier position would split
        # the base+root prefix the tiers currently share.
        root_block = workspace_root_block(self._workspace)
        parts = [BASE_SYSTEM_PROMPT, root_block] + [block for block in context.blocks.values()]
        text = "\n\n".join(parts)

        prefix_parts = [BASE_SYSTEM_PROMPT, root_block]
        if "steering" in context.blocks:
            prefix_parts.append(context.blocks["steering"])
        prefix = "\n\n".join(prefix_parts)
        prefix_hash = hashlib.sha256(prefix.encode("utf-8")).hexdigest()

        assembled = AssembledPrompt(
            tier=tier,
            text=text,
            prefix=prefix,
            prefix_hash=prefix_hash,
            prefix_tokens=heuristic_count(prefix).count,
            steering=context.steering,
        )
        self.last_assembled = assembled
        return assembled
