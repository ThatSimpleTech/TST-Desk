"""The daemon's CA bundle no longer trusts the distrusted emSign root (SUPPLY-01).

certifi 2026.7.22 still carried 'emSign ECC Root CA - G3' (PYSEC-2026-4320 /
GHSA-6q48-6c7w-9gw2, fixed in 2026.10.18); httpx uses it for every model,
connector and web_fetch call.
"""

from __future__ import annotations

import re
from pathlib import Path

import certifi


def _labels() -> list[str]:
    text = Path(certifi.where()).read_text(encoding="utf-8")
    return re.findall(r'# Label: "([^"]+)"', text)


def test_the_distrusted_root_is_gone() -> None:
    assert "emSign ECC Root CA - G3" not in _labels()


def test_certifi_is_at_least_the_fixed_release() -> None:
    year, month, day = (int(part) for part in certifi.__version__.split(".")[:3])
    assert (year, month, day) >= (2026, 10, 18)
