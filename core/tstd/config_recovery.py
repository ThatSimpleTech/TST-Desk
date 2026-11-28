"""Start on a config.yaml that no longer loads, and get back to one that did (SETMODEL-04).

Every load at start and every write that lands keeps a copy of the file
as ``config.yaml.last-good``. When config.yaml no longer loads at start
(a typo, a half-finished hand edit, a zero-byte file), the daemon runs
on that copy, or on the shipped defaults when there is none, instead of
exiting. The broken file is left exactly as it is: the person may want
to fix it by hand. Settings says what is wrong and offers to restore the
last working copy, which keeps the broken one as
``config.yaml.damaged-<stamp>``.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .config import ConfigError, ModelConfig, default_config_yaml
from .config_bind import call_config_loader
from .data_version import read_only
from .logging import get_logger
from .store_recovery import set_aside

log = get_logger("tstd.config_recovery")

RunningOn = Literal["last_good", "defaults"]


@dataclass(frozen=True)
class ConfigTrouble:
    """config.yaml did not load at start; what the daemon runs on instead."""

    error: str
    running_on: RunningOn


def _fields(error: str) -> dict[str, dict[str, str]]:
    return {"extra_fields": {"error": error}}


def _warn(message: str, exc: Exception) -> None:
    log.warning(message, extra=_fields(str(exc)))


def last_good_path(config_path: Path) -> Path:
    return config_path.with_name(f"{config_path.name}.last-good")


def remember_good(config_path: Path) -> None:
    """Copy a config.yaml that just loaded to ``config.yaml.last-good``. Never raises."""
    if read_only() is not None:  # UPGRADE-02: a newer version's folder
        return
    if not config_path.is_file():
        return
    target = last_good_path(config_path)
    tmp = ""
    try:
        fd, tmp = tempfile.mkstemp(dir=config_path.parent, prefix=".last-good.", suffix=".tmp")
        os.close(fd)
        shutil.copyfile(config_path, tmp)
        os.chmod(tmp, 0o600)
        os.replace(tmp, target)
    except OSError as exc:
        if tmp:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
        _warn("could not keep a last-good config copy", exc)


def load_with_recovery(
    loader: Callable[..., ModelConfig], config_path: Path
) -> tuple[ModelConfig, ConfigTrouble | None]:
    """The config to run on, and the trouble that made it a fallback, if any."""
    from .config_write import check_config_text

    try:
        config = call_config_loader(loader, config_path)
    except ConfigError as exc:
        error = str(exc)
    else:
        remember_good(config_path)
        return config, None
    log.error("config.yaml does not load; starting on a fallback", extra=_fields(error))
    good = last_good_path(config_path)
    if good.is_file():
        try:
            text = good.read_text(encoding="utf-8")
            return check_config_text(text, config_path), ConfigTrouble(error, "last_good")
        except (OSError, ConfigError) as exc:
            _warn("the last-good copy does not load either", exc)
    return check_config_text(default_config_yaml(), config_path), ConfigTrouble(error, "defaults")


def restore_last_good(config_path: Path) -> str | None:
    """Put the last working copy back; the broken file's new name, or None if there is no copy.

    Raises ``ConfigError`` when the copy itself no longer loads.
    """
    from .config_write import check_config_text

    good = last_good_path(config_path)
    if not good.is_file():
        return None
    text = good.read_text(encoding="utf-8")
    check_config_text(text, config_path, "The last working copy does not load either")
    kept = set_aside(config_path) if config_path.exists() else None
    fd, tmp = tempfile.mkstemp(dir=config_path.parent, prefix=config_path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        os.replace(tmp, config_path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    return kept.name if kept is not None else ""
