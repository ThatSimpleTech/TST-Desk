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


def no_model_refusal(config: ModelConfig, name: str, engine: str) -> str | None:
    """Why TST Desk's own loop cannot run on entry *name*, or None (TD-5239).

    The loop needs the entry's models, and an ``engine: grok`` entry may
    have none. The Grok engine needs nothing from the entry.
    """
    preset = config.presets.get(name)
    if preset is None or preset.has_tiers or engine != "native":
        return None
    return (
        f"{preset.label or name} runs on the Grok CLI and has no model of its own, so a "
        "chat on TST Desk's own loop cannot use it. Start a new chat to use it."
    )


def engine_refusal(config: ModelConfig, name: str, engine: str) -> str | None:
    """Why an open chat on *engine* cannot switch to entry *name*, or None (TD-5239).

    A chat keeps the engine it started on, so an entry that names another
    engine is refused, and so is one with no model for TST Desk's own loop.
    An entry that names no engine suits either, as before.
    """
    missing = no_model_refusal(config, name, engine)
    if missing is not None:
        return missing
    preset = config.presets.get(name)
    if preset is None or preset.engine is None or preset.engine == engine:
        return None
    ours = "the Grok CLI" if engine == "grok" else "TST Desk's own loop"
    theirs = "the Grok CLI" if preset.engine == "grok" else "TST Desk's own loop"
    return (
        f"This chat runs on {ours}, and {preset.label or name} runs on {theirs}. A chat "
        "keeps the engine it started on. Start a new chat to use it."
    )
