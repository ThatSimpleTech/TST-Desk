"""YAML frontmatter parsing for steering files.

Steering files (especially ``.tst/rules/*.md``) may carry YAML frontmatter
delimited by ``---`` lines.  This module parses it and returns the
metadata dict and the body content.

TD-503: path-scoped rules use frontmatter ``appliesTo`` to declare which
paths a rule applies to.
"""

from __future__ import annotations

import re
from typing import Any

import yaml

# Matches YAML frontmatter delimited by --- or ... lines.
# Group 1 captures the YAML body.  The frontmatter must be the very first
# thing in the file.
_FRONTMATTER_RE = re.compile(
    r"^---\s*\n(.*?)\n(?:---|\.\.\.)\s*\n",
    re.DOTALL,
)


def parse_frontmatter(content: str) -> tuple[dict[str, Any], str]:
    """Parse YAML frontmatter from markdown *content*.

    Returns ``(metadata, body)`` where *metadata* is the parsed frontmatter
    dict and *body* is the content after the frontmatter delimiter.

    If no frontmatter is present, returns ``({}, content)``.
    Invalid YAML, empty frontmatter, and non-dict frontmatter all degrade
    gracefully to ``({}, content)`` — the caller never needs to handle a
    parse failure.
    """
    match = _FRONTMATTER_RE.match(content)
    if not match:
        return {}, content

    raw = match.group(1).strip()
    if not raw:
        return {}, content[match.end() :]

    try:
        metadata = yaml.safe_load(raw)
    except yaml.YAMLError:
        return {}, content[match.end() :]

    if not isinstance(metadata, dict):
        return {}, content[match.end() :]

    return metadata, content[match.end() :]
