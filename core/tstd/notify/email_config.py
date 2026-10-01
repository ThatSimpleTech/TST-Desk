"""Surgical ``notify.email`` writer (TD-3820).

The password is not a yaml key. Callers that have one send it to the
keychain instead. A dump of the whole config would drop the teaching
comments in the shipped file, so this replaces only the email block.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..config import EmailNotifyConfig, ensure_user_config
from ..config_write import _atomic_write, _block_bounds, _find_key


def save_email_notify(email: EmailNotifyConfig, path: Path | None = None) -> Path:
    """Replace or append ``notify.email``. Never writes a password key."""
    config_path = ensure_user_config(path)
    lines = config_path.read_text(encoding="utf-8").split("\n")
    block = _email_block(email)
    notify_at = _find_key(lines, 0, len(lines), "notify", 0)
    if notify_at < 0:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append("notify:")
        lines.extend(block)
    else:
        notify_end = _block_bounds(lines, notify_at, 0)
        email_at = _find_key(lines, notify_at + 1, notify_end, "email", 2)
        if email_at < 0:
            lines[notify_end:notify_end] = block
        else:
            email_end = _block_bounds(lines, email_at, 2)
            lines[email_at:email_end] = block
    _atomic_write(config_path, "\n".join(lines))
    return config_path


def _email_block(email: EmailNotifyConfig) -> list[str]:
    return [
        "  email:",
        f"    enabled: {'true' if email.enabled else 'false'}",
        f"    host: {json.dumps(email.host)}",
        f"    port: {email.port}",
        f"    security: {email.security}",
        f"    username: {json.dumps(email.username)}",
        f"    from_address: {json.dumps(email.from_address)}",
        f"    timeout_seconds: {email.timeout_seconds}",
    ]
