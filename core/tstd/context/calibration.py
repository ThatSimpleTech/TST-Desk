"""Token estimates calibrated from provider usage (TD-5007, TD-5013).

The heuristic counter (4 chars/token) runs low against what a provider
reports: code and JSON tokenize denser than English prose. On the GPU server's
local model the estimate was 1.55-1.6x under ``usage.prompt_tokens``,
so compaction and in-turn elision aimed at a budget the real prompt had
already passed.

The fix is to learn the ratio instead of guessing it. Before each main
turn call the loop takes the base (uncalibrated) estimate of exactly the
messages and tools it sends. When the provider reports
``usage.prompt_tokens`` for that call, the ratio between the two is
folded into a per-endpoint store. Every later estimate for that endpoint
is scaled by it. The after-write review observes its own call the same
way (TD-5153), so a validator on an endpoint no turn calls learns too.

The store is process-wide on purpose: sessions in one daemon talk to
the same endpoints, and a ratio learned by one is true for all of them.
A delegate child does not estimate at all; it inherits only the
parent's tool-result cap.

Ratios survive a restart (TD-5013), but only when asked. A store built
with no path, which is what every test builds, never touches disk. The
daemon turns persistence on once at startup with
``token-calibration.json`` in its data folder: that loads the file, and
every later observation that moves a ratio rewrites it. So does one
whose entry was last written ``STAMP_REFRESH`` ago or more: a ratio that
has settled, or sits at a clamp bound, stops moving, and without that
its ``updated_at`` would freeze and an endpoint used every day would be
dropped as stale. A loaded ratio is a measurement, so the next
observation continues its EMA instead of replacing it. The file is::

    {"version": 1, "ratios": [{"base_url": "...", "model": "...",
                               "ratio": 1.17, "updated_at": "<ISO-8601 UTC>"}]}

Loading never fails startup. A missing file is the normal first run and
is silent. An unreadable or corrupt file, a different version, a
malformed entry, a ratio outside ``MIN_RATIO``..``MAX_RATIO`` and an
entry not updated for ``MAX_ENTRY_AGE`` are left out, with one log line
per load. An endpoint unused for a month may serve another model behind
the same slug, and a fresh first observation beats an EMA that starts
from an old number. With nothing loaded the first call is estimated at
ratio 1.0 (plus tool accounting), and the context-overflow retry still
backstops it.

Saving is a whole-file rewrite through a temp file in the same folder
and ``os.replace``, so a reader sees the old file or the new one, never
half of one. There is no fsync: the file is a cache, and one torn by a
power cut loads as corrupt and is relearned. A save never raises into
the loop, and a failing save is logged once per store, not per call.
Two daemons sharing a data folder each rewrite the whole file, and the
last writer wins. A ratio only the loser had learned drops out of the
file and is relearned on its endpoint's first call after a restart.

The workspace map's byte budget converts at the brain endpoint's
``4 / ratio`` too (``tier_chars_per_token``), fixed when a loop builds
its assembler.
"""

from __future__ import annotations

import contextlib
import json
import logging
import math
import os
import tempfile
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..logging import get_logger
from .tokens import CHARS_PER_TOKEN, TokenCount, TokenCounter

if TYPE_CHECKING:
    from ..config import TierConfig

log = get_logger("tstd.context.calibration")

# The first observation replaces the seed outright: the seed is a guess,
# the observation is a measurement. Later ones move it by this much.
EMA_ALPHA = 0.3
# A ratio outside this band is a provider reporting something other than
# this prompt's size (a cached template, a different tokenizer bug), not
# a tokenizer difference. Each sample is clamped before it is folded in.
MIN_RATIO = 0.5
MAX_RATIO = 3.0
# Below this the fixed per-request overhead (BOS, template preamble)
# dominates the ratio and would inflate every later estimate.
MIN_RAW_TOKENS = 256

# The persisted file (TD-5013): its name in the daemon's data folder, the
# one format version this code reads, the age past which an entry is left
# out at load, and how stale a saved stamp may get before an observation
# that did not move the ratio rewrites it anyway.
CALIBRATION_FILE = "token-calibration.json"
FILE_VERSION = 1
MAX_ENTRY_AGE = timedelta(days=30)
STAMP_REFRESH = timedelta(days=1)

CalibrationKey = tuple[str, str]
"""``(base_url, model slug)`` — one tokenizer behind one endpoint."""


def _clamp(ratio: float) -> float:
    return min(MAX_RATIO, max(MIN_RATIO, ratio))


def _now() -> datetime:
    return datetime.now(UTC)


def calibration_key(tier: TierConfig) -> CalibrationKey:
    """The store key for calls to *tier* (TD-5007, TD-5013).

    *tier* is the effective tier the call goes to, credential host
    applied. The turn's token counter and the workspace map's budget both
    key through here, so the two cannot name one endpoint two ways.
    Raises ``ModelDiscoveryError`` while the slug is unresolved.
    """
    return (tier.base_url, tier.require_slug())


def calibration_path(data_dir: str | Path) -> Path:
    """Where a daemon with *data_dir* keeps its learned ratios (TD-5013)."""
    return Path(data_dir) / CALIBRATION_FILE


class CalibrationStore:
    """Learned ``prompt_tokens / raw estimate`` ratios per endpoint.

    Memory only until :meth:`enable_persistence` names a file (TD-5013).
    """

    def __init__(self) -> None:
        self._ratios: dict[CalibrationKey, float] = {}
        self._updated: dict[CalibrationKey, datetime] = {}
        # Each key's ``updated_at`` as the file last held it (TD-5013).
        self._saved: dict[CalibrationKey, datetime] = {}
        self._path: Path | None = None
        self._save_failed = False

    @property
    def path(self) -> Path | None:
        """The file ratios are saved to; ``None`` while memory only."""
        return self._path

    def ratio(self, key: CalibrationKey) -> float | None:
        """The learned ratio for *key*, or ``None`` before any observation."""
        return self._ratios.get(key)

    def observe(
        self, key: CalibrationKey, raw_estimate: int, prompt_tokens: int | None
    ) -> float | None:
        """Fold one call's usage into *key*'s ratio; return the new ratio.

        ``raw_estimate`` must come from the base counter. Feeding it a
        calibrated estimate would compound the ratio on every call.
        Returns ``None`` (and changes nothing) when the observation is
        ignored: no usage figure, or an estimate too small to trust.
        With persistence on, a ratio that moved, or whose saved stamp is
        ``STAMP_REFRESH`` old, is saved before this returns (TD-5013).
        """
        if prompt_tokens is None or prompt_tokens <= 0 or raw_estimate < MIN_RAW_TOKENS:
            return None
        sample = _clamp(prompt_tokens / raw_estimate)
        previous = self._ratios.get(key)
        updated = sample if previous is None else previous + EMA_ALPHA * (sample - previous)
        now = _now()
        self._ratios[key] = updated
        self._updated[key] = now
        saved = self._saved.get(key)
        if updated != previous or saved is None or now - saved >= STAMP_REFRESH:
            self._save()
        return updated

    def clear(self) -> None:
        """Forget every ratio. Tests call this; the daemon never does.

        The file, if any, is left alone until the next save rewrites it.
        """
        self._ratios.clear()
        self._updated.clear()
        self._saved.clear()

    def enable_persistence(self, path: Path) -> None:
        """Load *path*, then save to it as ratios move (TD-5013).

        The daemon calls this once, at startup. A ratio this store already
        learned in memory is newer than the file's and is kept. Nothing
        here raises: a file that cannot be used is logged, and the next
        save replaces it.
        """
        self._path = path
        try:
            loaded = _load(path)
        except Exception as e:
            # ``_load`` handles every bad input it knows of. This is the
            # backstop for one it does not: the daemon calls this on
            # startup, and a cache must never keep it from starting.
            log.warning(
                "token calibration file could not be loaded; ratios start over",
                extra={"extra_fields": {"path": str(path), "error": repr(e)}},
            )
            loaded = {}
        for key, (ratio, updated_at) in loaded.items():
            if key not in self._ratios:
                self._ratios[key] = ratio
                self._updated[key] = updated_at
                self._saved[key] = updated_at

    def _save(self) -> None:
        """Rewrite the file from memory. Never raises; logs a failure once."""
        path = self._path
        if path is None:
            return
        try:
            entries = [
                {
                    "base_url": base_url,
                    "model": model,
                    "ratio": ratio,
                    "updated_at": self._updated[(base_url, model)].isoformat(timespec="seconds"),
                }
                for (base_url, model), ratio in sorted(self._ratios.items())
            ]
            text = json.dumps({"version": FILE_VERSION, "ratios": entries}, indent=2) + "\n"
            _atomic_write(path, text)
            self._saved = dict(self._updated)
        except Exception as e:
            # A cache that cannot be written must not fail a turn. One
            # line per store: a read-only folder would otherwise log on
            # every model call.
            if not self._save_failed:
                self._save_failed = True
                log.warning(
                    "token calibration not saved; it stays in memory, "
                    "and later save failures are not logged",
                    extra={"extra_fields": {"path": str(path), "error": repr(e)}},
                )


def _atomic_write(path: Path, text: str) -> None:
    """Same-folder temp file plus ``os.replace``; the temp never outlives a failure."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _parse_entry(entry: object, now: datetime) -> tuple[CalibrationKey, float, datetime] | str:
    """One file entry, or the reason it is left out."""
    if not isinstance(entry, dict):
        return "malformed"
    base_url, model = entry.get("base_url"), entry.get("model")
    ratio, stamp = entry.get("ratio"), entry.get("updated_at")
    if not (isinstance(base_url, str) and base_url and isinstance(model, str) and model):
        return "malformed"
    # bool is an int to isinstance; ``"ratio": true`` is not a ratio.
    if isinstance(ratio, bool) or not isinstance(ratio, int | float):
        return "malformed"
    if not isinstance(stamp, str):
        return "malformed"
    try:
        updated_at = datetime.fromisoformat(stamp)
    except ValueError:
        return "malformed"
    if updated_at.tzinfo is None:
        return "malformed"
    try:
        value = float(ratio)
    except (OverflowError, ValueError):  # an integer too large for a float
        return "malformed"
    # NaN fails both comparisons, so it lands here too.
    if not MIN_RATIO <= value <= MAX_RATIO:
        return "out_of_band"
    if now - updated_at > MAX_ENTRY_AGE:
        return "stale"
    return (base_url, model), value, updated_at


def _load(path: Path) -> dict[CalibrationKey, tuple[float, datetime]]:
    """The usable entries of *path*. Logs one line per load, except a missing file."""
    fields: dict[str, Any] = {"path": str(path)}
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError) as e:
        log.warning(
            "token calibration file unreadable; ratios start over",
            extra={"extra_fields": {**fields, "error": repr(e)}},
        )
        return {}
    try:
        data: Any = json.loads(text)
    except (ValueError, RecursionError) as e:  # RecursionError: absurd nesting
        log.warning(
            "token calibration file is not valid JSON; ratios start over",
            extra={"extra_fields": {**fields, "error": str(e)}},
        )
        return {}
    version = data.get("version") if isinstance(data, dict) else None
    if not (isinstance(version, int) and not isinstance(version, bool) and version == FILE_VERSION):
        log.warning(
            "token calibration file has an unknown version; ratios start over",
            extra={"extra_fields": {**fields, "version": repr(version), "expected": FILE_VERSION}},
        )
        return {}
    raw = data.get("ratios")
    if not isinstance(raw, list):
        log.warning(
            "token calibration file has no ratios list; ratios start over",
            extra={"extra_fields": fields},
        )
        return {}
    now = _now()
    loaded: dict[CalibrationKey, tuple[float, datetime]] = {}
    skipped: Counter[str] = Counter()
    for entry in raw:
        parsed = _parse_entry(entry, now)
        if isinstance(parsed, str):
            skipped[parsed] += 1
            continue
        key, ratio, updated_at = parsed
        # A key written twice (a hand edit) keeps its newest entry.
        if key not in loaded or updated_at > loaded[key][1]:
            loaded[key] = (ratio, updated_at)
    # Stale entries are routine; anything else means the file was edited
    # or written by something else.
    level = logging.WARNING if set(skipped) - {"stale"} else logging.INFO
    log.log(
        level,
        "token calibration loaded",
        extra={"extra_fields": {**fields, "loaded": len(loaded), **dict(skipped)}},
    )
    return loaded


_PROCESS_STORE = CalibrationStore()


def process_calibration_store() -> CalibrationStore:
    """The daemon-wide store every loop shares unless one is injected."""
    return _PROCESS_STORE


def enable_calibration_persistence(data_dir: str | Path) -> CalibrationStore:
    """Persist the daemon-wide store in *data_dir* and load it (TD-5013).

    The daemon's startup hook. Returns the store it enabled.
    """
    store = process_calibration_store()
    store.enable_persistence(calibration_path(data_dir))
    return store


def tier_chars_per_token(store: CalibrationStore, tier: TierConfig) -> float:
    """Characters one token of *tier*'s prompt stands for (TD-5013).

    ``4 / ratio`` once *store* has a ratio for the tier's endpoint, the
    flat 4 before. A tier whose slug discovery has not resolved yet has
    no key, so it is flat too.
    """
    if tier.slug is None:
        return CHARS_PER_TOKEN
    ratio = store.ratio(calibration_key(tier))
    return CHARS_PER_TOKEN if ratio is None else CHARS_PER_TOKEN / ratio


class CalibratedTokenCounter:
    """A :class:`TokenCounter` scaled by the endpoint's learned ratio.

    Uncalibrated, it counts exactly like its base and states the base's
    method. Calibrated, each count is ``ceil(base * ratio)`` and the
    method names the ratio, so a ContextCompacted log line says which
    numbers it believed.
    """

    def __init__(self, base: TokenCounter, store: CalibrationStore, key: CalibrationKey) -> None:
        self._base = base
        self._store = store
        self._key = key

    @property
    def base(self) -> TokenCounter:
        """The uncalibrated counter. Raw estimates for observation use it."""
        return self._base

    @property
    def store(self) -> CalibrationStore:
        return self._store

    @property
    def key(self) -> CalibrationKey:
        return self._key

    @property
    def ratio(self) -> float | None:
        return self._store.ratio(self._key)

    def count(self, text: str) -> TokenCount:
        raw = self._base.count(text)
        ratio = self.ratio
        if ratio is None:
            return raw
        return TokenCount(
            count=math.ceil(raw.count * ratio),
            method=f"{raw.method} x{ratio:.2f} calibrated from provider usage",
        )

    def observe(self, raw_estimate: int, prompt_tokens: int | None) -> float | None:
        """Record one call's usage against this counter's endpoint."""
        return self._store.observe(self._key, raw_estimate, prompt_tokens)


def calibration_ratio(counter: TokenCounter) -> float:
    """The scale *counter* applies on top of its base; 1.0 when none."""
    if isinstance(counter, CalibratedTokenCounter):
        ratio = counter.ratio
        if ratio is not None:
            return ratio
    return 1.0


def scale_tokens(counter: TokenCounter, tokens: int) -> int:
    """Scale a token figure that did not come from ``counter.count``.

    Per-message overhead is a token constant, not text, so it never
    passes through the counter. It is part of the raw estimate the
    ratio was learned against, so it has to be scaled the same way.
    """
    ratio = calibration_ratio(counter)
    if ratio == 1.0:
        return tokens
    return math.ceil(tokens * ratio)


def effective_chars_per_token(counter: TokenCounter) -> float:
    """Characters one estimated token stands for under *counter*.

    Elision and the tool-result cap turn a token budget back into
    characters. With a ratio of 1.5, one calibrated token is 4 / 1.5
    characters of text, so converting with a flat 4 would cut too little.
    """
    return CHARS_PER_TOKEN / calibration_ratio(counter)
