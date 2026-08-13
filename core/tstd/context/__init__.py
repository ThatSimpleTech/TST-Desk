"""Context assembler — steering file resolution and prompt assembly.

Epic E5: correct, inspectable, affordable instruction loading.
"""

from .assembler import AssembledSteering, ContextAssembler, ResolvedSource
from .discover import Precedence, SteeringFileResolver, SteeringSource
from .manifest import ManifestConfig, ManifestResult, WorkspaceManifest
from .prompt import (
    BASE_SYSTEM_PROMPT,
    MEMORY_PLACEHOLDER,
    AssembledPrompt,
    PromptAssembler,
)
from .stack import build_instruction_stack
from .tier import (
    DEFAULT_VALIDATOR_SUBSET,
    TierContext,
    TierContextConfig,
    assemble_for_tier,
    assemble_for_tier_sync,
    default_config_for_tier,
)
from .tokens import (
    HeuristicTokenCounter,
    TiktokenTokenCounter,
    TokenCount,
    TokenCounter,
    make_token_counter,
)

__all__ = [
    "BASE_SYSTEM_PROMPT",
    "DEFAULT_VALIDATOR_SUBSET",
    "MEMORY_PLACEHOLDER",
    "AssembledPrompt",
    "AssembledSteering",
    "ContextAssembler",
    "HeuristicTokenCounter",
    "ManifestConfig",
    "ManifestResult",
    "Precedence",
    "PromptAssembler",
    "ResolvedSource",
    "SteeringFileResolver",
    "SteeringSource",
    "TierContext",
    "TierContextConfig",
    "TiktokenTokenCounter",
    "TokenCount",
    "TokenCounter",
    "WorkspaceManifest",
    "assemble_for_tier",
    "assemble_for_tier_sync",
    "build_instruction_stack",
    "default_config_for_tier",
    "make_token_counter",
]
