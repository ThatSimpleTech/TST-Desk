"""Memory ranking sends nothing unless it is turned on (ENT2-03).

Every install's config.yaml was copied with `embeddings.base_url` filled
in, so each turn posted the message and every memory note to
127.0.0.1:8080, a port the person never chose.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from tstd.config import default_config_yaml, load_config
from tstd.context.embeddings import EmbeddingsClient


def _load(tmp_path: Path, embeddings: dict[str, object]) -> EmbeddingsClient:
    data = yaml.safe_load(default_config_yaml())
    data["embeddings"] = embeddings
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return EmbeddingsClient.from_config(load_config(path))


def test_the_shipped_config_ranks_nothing(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(default_config_yaml(), encoding="utf-8")
    assert EmbeddingsClient.from_config(load_config(path)).enabled is False


def test_an_installs_copied_url_alone_sends_nothing(tmp_path: Path) -> None:
    client = _load(tmp_path, {"base_url": "http://127.0.0.1:8080/v1", "model": "nomic-embed-text"})
    assert client.enabled is False


async def test_off_never_posts(tmp_path: Path) -> None:
    client = _load(tmp_path, {"base_url": "http://127.0.0.1:9/v1", "model": "m"})
    assert await client.embed_or_none(["a message", "a memory note"]) is None


def test_turned_on_it_ranks(tmp_path: Path) -> None:
    client = _load(
        tmp_path, {"enabled": True, "base_url": "http://127.0.0.1:8080/v1", "model": "m"}
    )
    assert client.enabled is True
