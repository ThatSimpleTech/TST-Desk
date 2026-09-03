"""Suite-wide isolation from the developer's machine.

``load_config()`` with no path reads ``user_data_dir()/config.yaml``. On CI
that is the shipped default, created on first use; on a developer's Mac it
is whatever the app last saved — an ``engine.kind`` of ``grok`` sends every
daemon test to the real Grok CLI instead of the mock provider. Redirect
``HOME`` to a throwaway per-test directory, the idiom
``test_local_preset_paths._install_config`` already uses, so a local run
sees what CI sees and never rewrites the developer's own files. A module
that redirects ``HOME`` itself still wins: its fixtures run after this one.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from tstd.config import cached_config


@pytest.fixture(autouse=True)
def _throwaway_home(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    # A sibling of tmp_path, not inside it: tests assert tmp_path ends up empty.
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.delenv("APPDATA", raising=False)
    cached_config.cache_clear()
    yield
    cached_config.cache_clear()
