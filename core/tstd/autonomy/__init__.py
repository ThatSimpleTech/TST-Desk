"""Autonomy hooks — decision classifier, ledger, and checkpoints (Epic E7).

Shipped in v0.1 so Class A decisions stop interrupting the user
immediately — the classifier makes interactive mode better right away
(spec §12.9).
"""

from .charter import Charter, CharterError, charter_path, charter_slug
from .checkpoint import Checkpointer, CheckpointOutcome, Notice, auto_branch, session_branch
from .classifier import (
    Boundary,
    Classification,
    DecisionClass,
    DecisionClassifier,
    DecisionRequest,
    Rule,
)
from .judgment import (
    Judgment,
    JudgmentBackend,
    JudgmentKind,
    JudgmentQuestion,
    ScriptedJudgmentBackend,
    WorkerChatJudgmentBackend,
    ideal_judgment,
    parse_judgment,
    render_question_prompt,
)
from .ledger import DecisionLedger, LedgerEntry, format_entry, parse_ledger
from .worker import (
    AmbiguousClassifier,
    build_classifier_prompt,
    classifier_question,
    parse_decision,
)

__all__ = [
    "AmbiguousClassifier",
    "Boundary",
    "Charter",
    "CharterError",
    "CheckpointOutcome",
    "Checkpointer",
    "Classification",
    "DecisionClass",
    "DecisionClassifier",
    "DecisionLedger",
    "DecisionRequest",
    "Judgment",
    "JudgmentBackend",
    "JudgmentKind",
    "JudgmentQuestion",
    "LedgerEntry",
    "Notice",
    "Rule",
    "ScriptedJudgmentBackend",
    "WorkerChatJudgmentBackend",
    "auto_branch",
    "build_classifier_prompt",
    "charter_path",
    "charter_slug",
    "classifier_question",
    "format_entry",
    "ideal_judgment",
    "parse_decision",
    "parse_judgment",
    "parse_ledger",
    "render_question_prompt",
    "session_branch",
]
