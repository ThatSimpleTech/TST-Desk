"""Context assembler — steering file resolution and prompt assembly.

Epic E5: correct, inspectable, affordable instruction loading.
"""

from .assembler import AssembledSteering, ContextAssembler, ResolvedSource
from .discover import Precedence, SteeringFileResolver, SteeringSource

__all__ = [
    "AssembledSteering",
    "ContextAssembler",
    "Precedence",
    "ResolvedSource",
    "SteeringFileResolver",
    "SteeringSource",
]
