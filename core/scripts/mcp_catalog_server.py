"""A stdio MCP server that lists one of six typical tool catalogs (TD-5127).

Used by ``measure_mcp_tool_search.py`` to load five or more servers whose
tools look like the ones people connect: an issue tracker, a wiki, a
calendar, mail, a SQL database and cloud storage. The names, descriptions
and schemas are written for this measurement, sized like the public
servers of each kind; no call does anything but echo its arguments.

    python mcp_catalog_server.py tracker
"""

from __future__ import annotations

import json
import sys
from typing import Any

# (name, description, [(param, type, description, required)]). A type of
# "enum:a|b" is a string enum; "array" is a list of strings.
Param = tuple[str, str, str, bool]
Spec = tuple[str, str, list[Param]]

_LIMIT: Param = ("limit", "integer", "Most results to return (1-100, default 25)", False)

CATALOGS: dict[str, list[Spec]] = {
    "tracker": [
        (
            "create_issue",
            "Create a new issue in a project. Returns the issue key and its URL.",
            [
                ("project", "string", "Project key, e.g. ENG", True),
                ("title", "string", "One-line summary of the issue", True),
                (
                    "body",
                    "string",
                    "Markdown description: steps, expected and actual result",
                    False,
                ),
                ("labels", "array", "Label names to apply; unknown labels are created", False),
                ("assignee", "string", "Username of the person to assign", False),
                ("priority", "enum:low|medium|high|urgent", "Priority, default medium", False),
            ],
        ),
        (
            "get_issue",
            "Get one issue with its fields, state, assignee and labels.",
            [
                ("key", "string", "Issue key, e.g. ENG-123", True),
            ],
        ),
        (
            "update_issue",
            "Change an issue's title, description, labels or assignee. "
            "Only the fields you pass change.",
            [
                ("key", "string", "Issue key, e.g. ENG-123", True),
                ("title", "string", "New summary", False),
                ("body", "string", "New Markdown description; replaces the old one", False),
                ("labels", "array", "The full new label list", False),
                ("assignee", "string", "Username, or empty to unassign", False),
            ],
        ),
        (
            "list_issues",
            "List a project's issues, newest first, with filters.",
            [
                ("project", "string", "Project key", True),
                ("state", "enum:open|closed|all", "Which issues, default open", False),
                ("assignee", "string", "Only issues assigned to this username", False),
                ("label", "string", "Only issues with this label", False),
                ("sort", "enum:created|updated|priority", "Sort order, default created", False),
                _LIMIT,
            ],
        ),
        (
            "search_issues",
            "Full-text search over issue titles, descriptions and comments "
            "across every project you can see.",
            [
                ("query", "string", "Search words; supports quoted phrases and -exclusions", True),
                ("project", "string", "Limit the search to one project key", False),
                _LIMIT,
            ],
        ),
        (
            "add_comment",
            "Add a Markdown comment to an issue.",
            [
                ("key", "string", "Issue key", True),
                ("body", "string", "Comment text in Markdown", True),
            ],
        ),
        (
            "list_comments",
            "List the comments on an issue, oldest first.",
            [
                ("key", "string", "Issue key", True),
                _LIMIT,
            ],
        ),
        (
            "assign_issue",
            "Assign an issue to a person.",
            [
                ("key", "string", "Issue key", True),
                ("assignee", "string", "Username", True),
            ],
        ),
        (
            "transition_issue",
            "Move an issue to another workflow state, e.g. In Review.",
            [
                ("key", "string", "Issue key", True),
                (
                    "to_state",
                    "string",
                    "Target state name as the project's workflow names it",
                    True,
                ),
                ("comment", "string", "Optional comment recorded with the transition", False),
            ],
        ),
        (
            "link_issues",
            "Link two issues, e.g. one blocks or duplicates the other.",
            [
                ("from_key", "string", "Issue key the link starts from", True),
                ("to_key", "string", "Issue key the link points to", True),
                ("relation", "enum:blocks|duplicates|relates_to", "Kind of link", True),
            ],
        ),
        ("list_projects", "List the projects you can see, with their keys and leads.", [_LIMIT]),
        (
            "list_labels",
            "List the labels defined in a project.",
            [
                ("project", "string", "Project key", True),
            ],
        ),
        (
            "create_label",
            "Create a label in a project.",
            [
                ("project", "string", "Project key", True),
                ("name", "string", "Label name", True),
                ("color", "string", "Hex color without #, e.g. d73a4a", False),
            ],
        ),
        (
            "list_sprints",
            "List a project's sprints with start and end dates.",
            [
                ("project", "string", "Project key", True),
                ("state", "enum:active|future|closed", "Which sprints, default active", False),
            ],
        ),
    ],
    "wiki": [
        (
            "search_pages",
            "Search wiki pages by title and body text.",
            [
                ("query", "string", "Search words", True),
                ("space", "string", "Limit to one space key", False),
                _LIMIT,
            ],
        ),
        (
            "get_page",
            "Get a page's body and metadata.",
            [
                ("page_id", "string", "Page id", True),
                ("format", "enum:markdown|storage|text", "Body format, default markdown", False),
            ],
        ),
        (
            "create_page",
            "Create a page in a space, optionally under a parent page.",
            [
                ("space", "string", "Space key", True),
                ("title", "string", "Page title; must be unique in the space", True),
                ("body", "string", "Page body in Markdown", True),
                ("parent_id", "string", "Parent page id; omit for a top-level page", False),
            ],
        ),
        (
            "update_page",
            "Replace a page's body. Pass the version you read to avoid "
            "overwriting someone else's edit.",
            [
                ("page_id", "string", "Page id", True),
                ("body", "string", "New body in Markdown", True),
                ("title", "string", "New title", False),
                ("version", "integer", "Version number the edit is based on", True),
            ],
        ),
        (
            "delete_page",
            "Move a page to the space's trash.",
            [
                ("page_id", "string", "Page id", True),
            ],
        ),
        ("list_spaces", "List the spaces you can read.", [_LIMIT]),
        (
            "get_page_children",
            "List a page's child pages.",
            [
                ("page_id", "string", "Page id", True),
                _LIMIT,
            ],
        ),
        (
            "add_page_comment",
            "Comment on a page.",
            [
                ("page_id", "string", "Page id", True),
                ("body", "string", "Comment in Markdown", True),
            ],
        ),
        (
            "list_page_comments",
            "List a page's comments.",
            [
                ("page_id", "string", "Page id", True),
                _LIMIT,
            ],
        ),
        (
            "move_page",
            "Move a page under another parent page.",
            [
                ("page_id", "string", "Page id", True),
                ("new_parent_id", "string", "Id of the new parent page", True),
            ],
        ),
        (
            "get_page_history",
            "List a page's versions with author and date.",
            [
                ("page_id", "string", "Page id", True),
                _LIMIT,
            ],
        ),
        (
            "attach_file",
            "Attach a local file to a page.",
            [
                ("page_id", "string", "Page id", True),
                ("path", "string", "Path of the local file to upload", True),
                ("comment", "string", "Attachment comment", False),
            ],
        ),
    ],
    "calendar": [
        ("list_calendars", "List the calendars you can see, with ids and access roles.", []),
        (
            "list_events",
            "List events in a time range, expanded from recurring series.",
            [
                ("calendar_id", "string", "Calendar id; primary for your own", True),
                ("time_min", "string", "Start of the range, RFC 3339", True),
                ("time_max", "string", "End of the range, RFC 3339", True),
                ("query", "string", "Only events whose text matches", False),
                _LIMIT,
            ],
        ),
        (
            "get_event",
            "Get one event with attendees and conferencing details.",
            [
                ("calendar_id", "string", "Calendar id", True),
                ("event_id", "string", "Event id", True),
            ],
        ),
        (
            "create_event",
            "Create an event and invite attendees.",
            [
                ("calendar_id", "string", "Calendar id", True),
                ("summary", "string", "Event title", True),
                ("start", "string", "Start time, RFC 3339, or a date for all-day", True),
                ("end", "string", "End time, RFC 3339, or a date for all-day", True),
                ("attendees", "array", "Attendee email addresses", False),
                ("location", "string", "Place or meeting link", False),
                ("description", "string", "Event notes", False),
                ("reminder_minutes", "integer", "Popup reminder before the start", False),
            ],
        ),
        (
            "update_event",
            "Change an event. Only the fields you pass change; attendees are notified.",
            [
                ("calendar_id", "string", "Calendar id", True),
                ("event_id", "string", "Event id", True),
                ("summary", "string", "New title", False),
                ("start", "string", "New start, RFC 3339", False),
                ("end", "string", "New end, RFC 3339", False),
                ("attendees", "array", "The full new attendee list", False),
                ("location", "string", "New place or link", False),
            ],
        ),
        (
            "delete_event",
            "Delete an event and notify its attendees.",
            [
                ("calendar_id", "string", "Calendar id", True),
                ("event_id", "string", "Event id", True),
            ],
        ),
        (
            "respond_to_event",
            "Accept, decline or tentatively accept an invitation.",
            [
                ("event_id", "string", "Event id", True),
                ("response", "enum:accepted|declined|tentative", "Your answer", True),
                ("comment", "string", "Note to the organizer", False),
            ],
        ),
        (
            "find_free_time",
            "Find slots when every attendee is free.",
            [
                ("attendees", "array", "Email addresses to check", True),
                ("duration_minutes", "integer", "Length of the meeting", True),
                ("time_min", "string", "Earliest start, RFC 3339", True),
                ("time_max", "string", "Latest end, RFC 3339", True),
            ],
        ),
        (
            "quick_add",
            "Create an event from a sentence like 'Lunch with Ana Friday 1pm'.",
            [
                ("calendar_id", "string", "Calendar id", True),
                ("text", "string", "The sentence", True),
            ],
        ),
        (
            "move_event",
            "Move an event to another calendar.",
            [
                ("event_id", "string", "Event id", True),
                ("destination_calendar_id", "string", "Calendar id to move it to", True),
            ],
        ),
    ],
    "mail": [
        (
            "search_messages",
            "Search mail with the provider's query syntax, newest first.",
            [
                ("query", "string", "Search query, e.g. from:ana is:unread", True),
                ("label", "string", "Only messages with this label", False),
                _LIMIT,
            ],
        ),
        (
            "get_message",
            "Get one message with headers and body.",
            [
                ("message_id", "string", "Message id", True),
                ("format", "enum:full|metadata|text", "How much to return, default full", False),
            ],
        ),
        (
            "get_thread",
            "Get every message in a thread, oldest first.",
            [
                ("thread_id", "string", "Thread id", True),
            ],
        ),
        (
            "send_message",
            "Send an email.",
            [
                ("to", "array", "Recipient addresses", True),
                ("subject", "string", "Subject line", True),
                ("body", "string", "Plain-text or HTML body", True),
                ("cc", "array", "Cc addresses", False),
                ("bcc", "array", "Bcc addresses", False),
                ("attachments", "array", "Local file paths to attach", False),
                (
                    "reply_to_message_id",
                    "string",
                    "Message this replies to, to keep the thread",
                    False,
                ),
            ],
        ),
        (
            "create_draft",
            "Save a draft without sending it.",
            [
                ("to", "array", "Recipient addresses", True),
                ("subject", "string", "Subject line", True),
                ("body", "string", "Body", True),
                ("cc", "array", "Cc addresses", False),
            ],
        ),
        (
            "update_draft",
            "Replace a draft's recipients, subject or body.",
            [
                ("draft_id", "string", "Draft id", True),
                ("to", "array", "Recipient addresses", False),
                ("subject", "string", "Subject line", False),
                ("body", "string", "Body", False),
            ],
        ),
        ("send_draft", "Send a saved draft.", [("draft_id", "string", "Draft id", True)]),
        ("list_labels", "List the mailbox's labels and folders.", []),
        (
            "add_label",
            "Add a label to a message.",
            [
                ("message_id", "string", "Message id", True),
                ("label", "string", "Label name or id", True),
            ],
        ),
        (
            "remove_label",
            "Remove a label from a message.",
            [
                ("message_id", "string", "Message id", True),
                ("label", "string", "Label name or id", True),
            ],
        ),
        (
            "archive_message",
            "Archive a message (remove it from the inbox).",
            [
                ("message_id", "string", "Message id", True),
            ],
        ),
        (
            "mark_read",
            "Mark a message read or unread.",
            [
                ("message_id", "string", "Message id", True),
                ("read", "boolean", "True for read, false for unread", True),
            ],
        ),
    ],
    "sql": [
        ("list_schemas", "List the database's schemas.", []),
        (
            "list_tables",
            "List tables with row estimates.",
            [
                ("schema", "string", "Schema name, default public", False),
            ],
        ),
        (
            "describe_table",
            "Columns, types, nullability, defaults and keys of a table.",
            [
                ("table", "string", "Table name", True),
                ("schema", "string", "Schema name, default public", False),
            ],
        ),
        (
            "run_query",
            "Run a read-only SQL query and return rows as JSON. Writes are "
            "refused; the query runs in a read-only transaction.",
            [
                ("sql", "string", "One SQL statement; use $1, $2 for parameters", True),
                ("params", "array", "Values for $1, $2, ...", False),
                ("limit", "integer", "Most rows to return, default 200", False),
                ("timeout_seconds", "integer", "Statement timeout, default 30", False),
            ],
        ),
        (
            "explain_query",
            "Show the query plan with estimated costs.",
            [
                ("sql", "string", "The SQL statement", True),
                ("analyze", "boolean", "Run it and show real timings (read-only queries)", False),
            ],
        ),
        (
            "list_indexes",
            "List a table's indexes and their definitions.",
            [
                ("table", "string", "Table name", True),
            ],
        ),
        (
            "get_table_stats",
            "Size, row count, dead tuples and last vacuum of a table.",
            [
                ("table", "string", "Table name", True),
            ],
        ),
        (
            "list_views",
            "List views in a schema.",
            [
                ("schema", "string", "Schema name, default public", False),
            ],
        ),
        (
            "get_view_definition",
            "Get the SQL that defines a view.",
            [
                ("view", "string", "View name", True),
            ],
        ),
        (
            "list_functions",
            "List stored functions with their signatures.",
            [
                ("schema", "string", "Schema name, default public", False),
            ],
        ),
    ],
    "storage": [
        (
            "list_files",
            "List files in a folder, newest first.",
            [
                ("folder_id", "string", "Folder id; omit for the root", False),
                ("order_by", "enum:modified|name|size", "Sort order, default modified", False),
                _LIMIT,
            ],
        ),
        (
            "search_files",
            "Search file names and contents.",
            [
                ("query", "string", "Search words", True),
                ("mime_type", "string", "Only files of this type, e.g. application/pdf", False),
                ("modified_after", "string", "Only files changed after this RFC 3339 time", False),
                _LIMIT,
            ],
        ),
        (
            "get_file_metadata",
            "Name, size, owner, type and sharing of a file.",
            [
                ("file_id", "string", "File id", True),
            ],
        ),
        (
            "download_file",
            "Download a file to a local path.",
            [
                ("file_id", "string", "File id", True),
                ("destination", "string", "Local path to write", True),
            ],
        ),
        (
            "upload_file",
            "Upload a local file.",
            [
                ("path", "string", "Local path of the file", True),
                ("folder_id", "string", "Folder to put it in; omit for the root", False),
                ("name", "string", "Name to give it; default the local name", False),
            ],
        ),
        (
            "create_folder",
            "Create a folder.",
            [
                ("name", "string", "Folder name", True),
                ("parent_id", "string", "Parent folder id; omit for the root", False),
            ],
        ),
        (
            "move_file",
            "Move a file to another folder.",
            [
                ("file_id", "string", "File id", True),
                ("folder_id", "string", "Destination folder id", True),
            ],
        ),
        (
            "copy_file",
            "Copy a file.",
            [
                ("file_id", "string", "File id", True),
                ("name", "string", "Name of the copy", False),
            ],
        ),
        (
            "rename_file",
            "Rename a file.",
            [
                ("file_id", "string", "File id", True),
                ("name", "string", "New name", True),
            ],
        ),
        ("delete_file", "Move a file to the trash.", [("file_id", "string", "File id", True)]),
        (
            "share_file",
            "Share a file with a person.",
            [
                ("file_id", "string", "File id", True),
                ("email", "string", "Their email address", True),
                ("role", "enum:reader|commenter|writer", "Access to grant", True),
            ],
        ),
        (
            "list_permissions",
            "List who a file is shared with.",
            [
                ("file_id", "string", "File id", True),
            ],
        ),
    ],
}


def _schema(params: list[Param]) -> dict[str, Any]:
    properties: dict[str, Any] = {}
    for name, kind, description, _required in params:
        if kind.startswith("enum:"):
            prop: dict[str, Any] = {"type": "string", "enum": kind[5:].split("|")}
        elif kind == "array":
            prop = {"type": "array", "items": {"type": "string"}}
        else:
            prop = {"type": kind}
        prop["description"] = description
        properties[name] = prop
    required = [name for name, _kind, _description, req in params if req]
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


def tools_for(catalog: str) -> list[dict[str, Any]]:
    return [
        {"name": name, "description": description, "inputSchema": _schema(params)}
        for name, description, params in CATALOGS[catalog]
    ]


def main(catalog: str) -> None:
    tools = tools_for(catalog)
    for line in sys.stdin:
        msg = json.loads(line)
        if "id" not in msg:
            continue
        if msg["method"] == "initialize":
            result: dict[str, Any] = {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": catalog, "version": "1"},
            }
        elif msg["method"] == "tools/list":
            result = {"tools": tools}
        else:
            text = json.dumps(msg.get("params", {}))
            result = {"content": [{"type": "text", "text": text}]}
        reply = {"jsonrpc": "2.0", "id": msg["id"], "result": result}
        sys.stdout.write(json.dumps(reply) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main(sys.argv[1])
