"""Cache-aware prompt assembly (TD-305).

Assembles the system prompt in the stable-prefix order from spec §4.5:

    [1] TST Desk base system prompt   ← never changes
    [2] Steering block (resolved)     ← changes only when files change
    [3] Memory block (relevant)       ← changes between sessions
    [4] Workspace manifest            ← changes as files change
    [5] Conversation                  ← appended by the loop, per turn

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


@dataclass(frozen=True)
class AssembledPrompt:
    """The assembled system prompt plus its cache-prefix fingerprint.

    Attributes:
        tier: The tier this prompt was assembled for.
        text: The full system prompt - blocks 1-4 in stable-prefix
            order, ready to be placed before the conversation.
        prefix: Blocks 1-2 (base + steering) - the provider
            prefix-cache target.
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
        )

        # Stable-prefix order: base first, then the tier blocks in
        # TD-508's insertion order (steering → memory → manifest for
        # brain; steering → task → relevant files for worker; steering
        # → diff → test output for validator).
        parts = [BASE_SYSTEM_PROMPT] + [block for block in context.blocks.values()]
        text = "\n\n".join(parts)

        prefix_parts = [BASE_SYSTEM_PROMPT]
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
