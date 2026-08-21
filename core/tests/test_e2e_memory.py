"""M4 exit test: the memory harness must pass (TD-2701).

The harness lives in ``tstd.e2e_memory``; this wrapper pins accept and
the reject sister into CI. Embeddings are disabled on the plan's config
so the pass never needs a sidecar.
"""

from __future__ import annotations

from pathlib import Path

from tstd.e2e_memory import memory_config, run_memory


def test_memory_harness_disables_embeddings() -> None:
    config = memory_config()
    assert config.embeddings.base_url == ""
    assert config.embeddings.model == ""


async def test_memory_harness_accept(tmp_path: Path) -> None:
    result = await run_memory(tmp_path / "workspace", tmp_path / "data", resolution="accept")
    assert result.ok, f"memory harness accept failed:\n{result.report()}"
    assert result.elapsed < 60.0


async def test_memory_harness_reject(tmp_path: Path) -> None:
    result = await run_memory(tmp_path / "workspace", tmp_path / "data", resolution="reject")
    assert result.ok, f"memory harness reject failed:\n{result.report()}"
    assert result.elapsed < 60.0
