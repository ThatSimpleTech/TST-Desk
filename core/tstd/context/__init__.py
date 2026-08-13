"""Context assembler — steering file resolution and prompt assembly.

Epic E5: correct, inspectable, affordable instruction loading.
"""

from .assembler import AssembledSteering, ContextAssembler, ResolvedSource
from .discover import Precedence, SteeringFileResolver, SteeringSource
from .manifest import ManifestConfig, ManifestResult, WorkspaceManifest
from .stack import build_instruction_stack
from .tokens import (
    HeuristicTokenCounter,
    TiktokenTokenCounter,
    TokenCount,
    TokenCounter,
    make_token_counter,
)

__all__ = [
    "AssembledSteering",
    "ContextAssembler",
    "HeuristicTokenCounter",
    "ManifestConfig",
    "ManifestResult",
    "Precedence",
    "ResolvedSource",
    "SteeringFileResolver",
    "SteeringSource",
    "TiktokenTokenCounter",
    "TokenCount",
    "TokenCounter",
    "WorkspaceManifest",
    "build_instruction_stack",
    "make_token_counter",
]
