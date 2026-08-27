"""Notification channels. Each channel is ``send(config, message)`` (spec §8).

Slack is the default export. Discord, Telegram, and ntfy are sibling
modules, not a 20-platform gateway.
"""

from .slack import schedule, send

__all__ = ["schedule", "send"]
