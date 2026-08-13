"""Autonomy hooks — decision classifier, ledger, and checkpoints (Epic E7).

Shipped in v0.1 so Class A decisions stop interrupting the user
immediately — the classifier makes interactive mode better right away
(spec §12.9).
"""

from .classifier import (
    Boundary,
    Classification,
    DecisionClass,
    DecisionClassifier,
    DecisionRequest,
    Rule,
)
from .worker import AmbiguousClassifier, build_classifier_prompt, parse_decision

__all__ = [
    "AmbiguousClassifier",
    "Boundary",
    "Classification",
    "DecisionClass",
    "DecisionClassifier",
    "DecisionRequest",
    "Rule",
    "build_classifier_prompt",
    "parse_decision",
]
