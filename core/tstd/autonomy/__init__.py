"""Autonomy hooks — decision classifier, ledger, and checkpoints (Epic E7).

Shipped in v0.1 so Class A decisions stop interrupting the user
immediately — the classifier makes interactive mode better right away
(spec §12.9).
"""

from .checkpoint import Checkpointer, CheckpointOutcome, Notice
from .classifier import (
    Boundary,
    Classification,
    DecisionClass,
    DecisionClassifier,
    DecisionRequest,
    Rule,
)
from .ledger import DecisionLedger, LedgerEntry, format_entry, parse_ledger
from .worker import AmbiguousClassifier, build_classifier_prompt, parse_decision

__all__ = [
    "AmbiguousClassifier",
    "Boundary",
    "CheckpointOutcome",
    "Checkpointer",
    "Classification",
    "DecisionClass",
    "DecisionClassifier",
    "DecisionLedger",
    "DecisionRequest",
    "LedgerEntry",
    "Notice",
    "Rule",
    "build_classifier_prompt",
    "format_entry",
    "parse_decision",
    "parse_ledger",
]
