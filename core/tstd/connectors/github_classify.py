"""Tool-name sets the classifier and the registry share.

This module imports nothing else on purpose. The classifier loads it at
import time, and the HTTP client must not come along for that ride.
"""

from __future__ import annotations

# Read-only. An explicit Class A grant — see the github-read rule.
GITHUB_READ_TOOLS = frozenset(
    {
        "github_list_repos",
        "github_get_file",
        "github_get_tree",
        "github_list_issues",
        "github_get_issue",
        "github_list_pulls",
        "github_get_pull",
        "github_pull_checks",
    }
)

# Approval unless a policy rule allows them. Not path tools: an in-workspace
# path grant would classify a push as Class A.
GITHUB_ASK_TOOLS = frozenset(
    {
        "github_create_branch",
        "github_put_file",
        "github_open_pull",
        "github_comment",
        "github_create_issue",
        "github_git",
    }
)

# Class C via side_effect_class "never". Settings still refuses in the handler.
GITHUB_NEVER_TOOLS = frozenset(
    {
        "github_merge",
        "github_delete_branch",
        "github_update_settings",
    }
)
