"""Line comments from the diff view, sent back to the agent (TD-5132).

The person comments on a line or a range of a file's latest diff in the
Files pane, then sends them all at once. This module turns them into the
one user turn the model reads: each comment named by its file and lines,
with the code around it.

The window's word is not taken for the code. The daemon reads the file as
it is now and checks that the commented lines are still what the diff
showed; only then does the model get the lines around them from disk. When
the file has changed since (a hand edit, a later write) or cannot be read,
the model gets the lines as the person reviewed them, marked as such,
rather than code the comment was not about.

Every path must be inside the session's workspace: the wall ``fs_read``
keeps, checked the same way. The code around a comment is read only where
a built-in read would run without asking, so a policy rule that makes a
file ask (TD-5141) keeps the rest of it out of the message too.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from .autonomy.classifier import Boundary, DecisionClass, canonical_path
from .policy import PolicyConfig, resolve
from .protocol import LineComment, LineCommentAnchor
from .tools.boundary import PathGuard, RefusalError
from .tools.diff import snapshot_text
from .tools.registry import Tool

#: Lines of code shown above and below each comment: a unified diff's own
#: context, so the model sees around a comment what the person saw.
CONTEXT_LINES = 3

#: The most text one submission may put in front of the model. Fifty
#: comments of a few lines each fit many times over; a paste of whole files
#: into ranges does not.
MAX_TEXT_CHARS = 200_000

# Stands in for fs_read when asking the policy whether a file reads without
# asking. resolve() reads only its name and path field.
_READ = Tool(name="fs_read", path_fields=("path",))

_CHANGED = "the file has changed since this diff; these are the lines as you reviewed them"
_UNREADABLE = "the file could not be read now; these are the lines as you reviewed them"
_GUARDED = "a policy rule keeps the rest of this file out of the message"


class LineCommentError(Exception):
    """A submission refused as a whole; ``code`` is the wire error code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class CommentsTurn:
    """The user turn a submission becomes: what the model reads, and the
    comments the ``user_turn`` carries for the window."""

    text: str
    anchors: list[LineCommentAnchor]


def build_comments_turn(
    comments: Sequence[LineComment], workspace: Path, policy: PolicyConfig
) -> CommentsTurn:
    """Check, read and render *comments* as one user turn.

    Blocking (it reads files): the daemon runs it off the event loop.

    Raises:
        LineCommentError: ``line_comment_outside_workspace`` when a path is
            outside the wall, ``line_comments_too_large`` when the text
            would pass :data:`MAX_TEXT_CHARS`. Nothing is queued either way.
    """
    root = canonical_path(workspace)
    guard = PathGuard(Boundary(workspace_root=root))
    paths: list[Path] = []
    for comment in comments:
        try:
            paths.append(guard.check_read(comment.path))
        except RefusalError as exc:
            raise LineCommentError(
                "line_comment_outside_workspace",
                f"A comment names a file outside this session's workspace ({exc.path}), "
                "so none of the comments were sent.",
            ) from None

    blocks = [
        _render_one(n, comment, path, root, policy)
        for n, (comment, path) in enumerate(zip(comments, paths, strict=True), start=1)
    ]
    count = len(comments)
    noun = "comment" if count == 1 else "comments"
    header = (
        f"Review comments on your changes ({count} {noun}). Each names a file and its "
        "line numbers; lines marked > are the ones the comment is about."
    )
    text = "\n\n".join([header, *blocks])
    if len(text) > MAX_TEXT_CHARS:
        raise LineCommentError(
            "line_comments_too_large",
            f"These comments quote too much code to send at once ({len(text):,} characters; "
            f"the most is {MAX_TEXT_CHARS:,}). Send fewer comments, or shorter ranges.",
        )
    anchors = [
        LineCommentAnchor(
            id=comment.id,
            path=str(path),
            start_line=comment.start_line,
            end_line=comment.end_line,
            body=comment.body,
            base_seq=comment.base_seq,
        )
        for comment, path in zip(comments, paths, strict=True)
    ]
    return CommentsTurn(text=text, anchors=anchors)


def _render_one(
    number: int, comment: LineComment, path: Path, root: Path, policy: PolicyConfig
) -> str:
    start, end = comment.start_line, comment.end_line
    span = f"line {start}" if start == end else f"lines {start}-{end}"
    rows, note = _excerpt(comment, path, root, policy)
    title = f"{number}. {_display_path(path, root)}, {span}"
    if note:
        title = f"{title} ({note})"
    code = _fenced(rows)
    return f"{title}:\n{code}\nComment: {comment.body.strip()}"


def _excerpt(
    comment: LineComment, path: Path, root: Path, policy: PolicyConfig
) -> tuple[list[tuple[int, str, bool]], str | None]:
    """Numbered rows to show, each marked when commented, and a note on
    where they came from when it is not the file as it is now."""
    start, end = comment.start_line, comment.end_line
    reviewed = [(start + i, line, True) for i, line in enumerate(comment.lines)]
    if not _reads_without_asking(path, root, policy):
        return reviewed, _GUARDED
    text = snapshot_text(path)
    if text is None:
        return reviewed, _UNREADABLE
    lines = text.splitlines()
    if lines[start - 1 : end] != list(comment.lines):
        return reviewed, _CHANGED
    first = max(1, start - CONTEXT_LINES)
    last = min(len(lines), end + CONTEXT_LINES)
    return [(n, lines[n - 1], start <= n <= end) for n in range(first, last + 1)], None


def _reads_without_asking(path: Path, root: Path, policy: PolicyConfig) -> bool:
    effect = resolve(policy, _READ, {"path": str(path)}, DecisionClass.A, root)
    return effect == "auto"


def _display_path(path: Path, root: Path) -> str:
    """Workspace-relative, as the model's own tools take it."""
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def _fenced(rows: list[tuple[int, str, bool]]) -> str:
    """Rows in a code fence longer than any backtick run inside them."""
    width = len(str(rows[-1][0])) if rows else 1
    body = "\n".join(f"{'>' if marked else ' '} {n:>{width}} | {line}" for n, line, marked in rows)
    longest = 0
    run = 0
    for ch in body:
        run = run + 1 if ch == "`" else 0
        longest = max(longest, run)
    fence = "`" * max(3, longest + 1)
    return f"{fence}\n{body}\n{fence}"
