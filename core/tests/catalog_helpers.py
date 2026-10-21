"""The test model catalog (TD-5172).

No presets ship, so tests that need a model routed somewhere use this: the
shipped config with ``fixtures/test_presets.yaml`` spliced over its empty
``presets: {}`` block. Those are the four presets the package shipped before,
so tests written against ``tst-default``, ``budget``, ``local`` and ``vllm``
keep meaning what they meant.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from tstd.config import default_config_yaml

TEST_PRESETS = Path(__file__).parent / "fixtures" / "test_presets.yaml"
_EMPTY_CATALOG = "\npresets: {}\n"


@lru_cache(maxsize=1)
def catalog_config_yaml() -> str:
    """The shipped config.yaml text with the test catalog in place."""
    shipped = default_config_yaml()
    if _EMPTY_CATALOG not in shipped:
        raise AssertionError("shipped config.yaml no longer has an empty presets block")
    block = TEST_PRESETS.read_text(encoding="utf-8")
    return shipped.replace(_EMPTY_CATALOG, "\n" + block + "\n", 1)
