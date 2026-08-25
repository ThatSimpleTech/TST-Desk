"""Shared rust-ring geometry. Painters must not invent a second look.

Matches ``GlowLayer.svelte`` and the macOS helper: ``--color-accent``
``#b4532a``, a 1.6s pulse, click-through, every display.
"""

from __future__ import annotations

INSET_PX = 4
CORE_WIDTH_PX = 6
HALO_WIDTH_PX = 16
PULSE_PERIOD_SECONDS = 1.6
PULSE_MIN = 0.75
PULSE_MAX = 1.0
TICK_SECONDS = 1.0 / 30.0

# ``--color-accent`` light / dark (ui/src/lib/tokens.css).
LIGHT_RUST_RGB = (180, 83, 42)
DARK_RUST_RGB = (208, 121, 79)
