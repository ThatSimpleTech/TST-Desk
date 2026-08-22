"""Notification channels. Each channel is ``send(config, message)`` (spec §8)."""

from .slack import schedule, send

__all__ = ["schedule", "send"]
