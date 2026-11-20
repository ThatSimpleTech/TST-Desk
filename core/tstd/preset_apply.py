"""The config a chat on one catalog entry runs with (TD-5234).

One function, used wherever a session takes its snapshot of the catalog
(TD-1721): the daemon's config, pointed at the entry, with that entry's
``overrides`` laid over the global sections. Everything a turn reads from
the snapshot (stale-result clearing, compaction, consult, the image cap,
workflow limits, the effort a chat starts on) follows the entry with no
change at the call sites.
"""

from __future__ import annotations

from .config import ModelConfig
from .preset_overrides import PresetOverrides, merged_sections


def preset_overrides(config: ModelConfig, name: str) -> PresetOverrides | None:
    """The ``overrides`` of entry *name*, or None when it sets none."""
    preset = config.presets.get(name)
    return preset.overrides if preset is not None else None


def effective_config(config: ModelConfig, name: str) -> ModelConfig:
    """*config* as a chat on entry *name* sees it. Pure: *config* is not changed.

    With no ``overrides`` this is ``config`` with ``active_preset`` set,
    what a session's snapshot was before TD-5234. An unknown *name* has no
    overrides either; choosing a real one is the caller's job.
    """
    update: dict[str, object] = {"active_preset": name}
    update.update(merged_sections(config, preset_overrides(config, name)))
    return config.model_copy(update=update)
