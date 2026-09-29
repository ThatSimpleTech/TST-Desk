"""Call a config loader with a daemon's file (TD-4843).

``cached_config()`` with no argument stays the library cache. A daemon
always passes its own ``config.yaml``. Tests replace that loader with
``lambda: cfg``, which has no parameter. Passing a path would TypeError,
and catching TypeError would also hide a real failure in the loader.
The signature decides; the call itself is not wrapped.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

T = TypeVar("T")

_POSITIONAL = frozenset(
    {
        inspect.Parameter.POSITIONAL_ONLY,
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
        inspect.Parameter.VAR_POSITIONAL,
        inspect.Parameter.VAR_KEYWORD,
    }
)


def accepts_config_path(loader: Callable[..., T]) -> bool:
    """True when *loader* can take the config path as one positional."""
    try:
        signature = inspect.signature(loader)
    except (TypeError, ValueError):
        return False
    return any(param.kind in _POSITIONAL for param in signature.parameters.values())


def call_config_loader(loader: Callable[..., T], path: Path) -> T:
    """Call *loader* on *path*, or with no arguments when it takes none."""
    if accepts_config_path(loader):
        return loader(path)
    return loader()
