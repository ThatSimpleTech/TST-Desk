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
    "instructions below, use the provided tools, and report concisely. "
    "When you need the public web, fire several web_search queries in one "
    "turn (different angles), web_fetch the two or three best URLs, then "
    "answer from those pages. Do not stop at snippets."
)

#: Block [3] placeholder until the memory story lands (spec §5).  Keeps
#: the memory slot positionally stable in the prompt so a real memory
#: block can slot in without reordering the cache prefix.
MEMORY_PLACEHOLDER = "<!-- memory: none loaded for this session -->"

#: Opening label of block [1b].  Exposed so callers can find the one line
#: that *states* the root: the path itself also occurs in the block's worked
#: example, and as the head of a longer path in every steering provenance
#: comment, so counting occurrences of the path answers a different
#: question.  Label plus root is the statement; the label alone is not.
WORKSPACE_ROOT_LABEL = "Workspace root:"

#: Block [1b] — the workspace root (TD-1810).  ``fs_*`` tools accept a
#: workspace-relative or absolute path; the guard joins the relative form
#: to this root.  The worked example is spelled out rather than implied.
#:
#: The resolution rule is stated as a rule, not as a claim about what the
#: rest of the prompt contains.  Only the brain tier is given the
#: workspace manifest (``tier.py``), so a block asserting that workspace
#: files *are listed* relative to the root would be false on the worker
#: and the validator, which get no listing at all.  The same bytes go to
#: every tier — that shared head is the point of the position — so the
#: sentence has to be true without knowing which blocks follow it.
_WORKSPACE_ROOT_TEMPLATE = (
    "{label} {root}\n"
    "That is an absolute path on this machine. A path written relative to "
    'the workspace is joined to it: "src/app.py" means "{root}/src/app.py". '
    "Pass either form to tools. A path that walks above this root is refused."
)

#: Characters that may not appear in a stated root.  ``\n`` ends the label
#: line early and silently re-reads the tail of the path as prose, which
#: breaks the feature without breaking anything visibly; ``\r`` does the
#: same on the consumer's side.  The rest are non-printing characters that
#: a model, a log line, or the inspector may render, strip, or normalise
#: differently, so a root containing one could not be shown to equal the
#: root the path guard enforces.  C0 (tab included), DEL, C1 \u2014 which carries
#: U+0085 NEL, a line break to anything that follows Unicode UAX-14 \u2014 and the
#: Unicode line and paragraph separators.
_FORBIDDEN_ROOT_CHARS = (
    frozenset(chr(code) for code in range(0x20))
    | frozenset(chr(code) for code in range(0x7F, 0xA0))
    | {"\u2028", "\u2029"}
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

    Raises:
        ValueError: If the resolved root contains a control character.
            Refusing is deliberate.  Escaping would put a string in the
            prompt that is not the path, and emitting anyway is the one
            outcome with no symptom: the model would read a truncated
            root, join it with a listed entry, and hand the path guard an
            absolute path that points somewhere else.  A workspace root
            that cannot be stated verbatim cannot be stated at all, and
            §6 forbids the silent failure.
    """
    root = Path(workspace_path).resolve().as_posix()
    offenders = sorted(_FORBIDDEN_ROOT_CHARS.intersection(root))
    if offenders:
        codepoints = ", ".join(f"U+{ord(ch):04X}" for ch in offenders)
        raise ValueError(
            f"workspace root contains control characters ({codepoints}) and "
            "cannot be stated in the system prompt; rename or relocate the "
            "workspace directory"
        )
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
        prefix_tokens: Heuristic token count of the whole *prefix* — the
            base prompt and block [1b] included.  This is the cache
            figure, not the cost of the user's steering files; read
            *steering_tokens* for that.
        steering_tokens: Heuristic token count of the steering block
            alone, or 0 for a tier that received no steering.  Split out
            because the two numbers answer different questions and the
            prefix figure answers the steering one wrongly: block [1b] is
            session-constant machinery, not something the user wrote.
        steering: The underlying :class:`AssembledSteering` from the
            context assembler.  Carried so the loop can emit the
            instruction stack and detect steering changes (TD-509).
    """

    tier: TierName
    text: str
    prefix: str
    prefix_hash: str
    prefix_tokens: int
    steering_tokens: int
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

        # Brain always occupies the memory slot. Empty load keeps the
        # placeholder (TD-2201); a selected set replaces it.
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

        steering_block = context.blocks.get("steering")
        prefix_parts = [BASE_SYSTEM_PROMPT, root_block]
        if steering_block is not None:
            prefix_parts.append(steering_block)
        prefix = "\n\n".join(prefix_parts)
        prefix_hash = hashlib.sha256(prefix.encode("utf-8")).hexdigest()

        # Counted separately, not derived by subtraction: the prefix is
        # joined with separators, so prefix minus base minus root is off by
        # the joins, and a figure that is nearly right is the failure mode
        # this split exists to remove.
        steering_tokens = heuristic_count(steering_block).count if steering_block else 0

        assembled = AssembledPrompt(
            tier=tier,
            text=text,
            prefix=prefix,
            prefix_hash=prefix_hash,
            prefix_tokens=heuristic_count(prefix).count,
            steering_tokens=steering_tokens,
            steering=context.steering,
        )
        self.last_assembled = assembled
        return assembled
