"""Pinned context rides the brain after memory (TD-2805)."""

from __future__ import annotations

from pathlib import Path

from tstd.context.prompt import PromptAssembler
from tstd.context_pins import add_pin, load_project_context
from tstd.memory_store import memory_dir


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_lifo_drops_newest(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    old = ws / "old.md"
    new = ws / "new.md"
    _write(old, "old pin\n" + ("x\n" * 80))
    _write(new, "new pin\n" + ("y\n" * 80))
    add_pin(ws, str(old))
    add_pin(ws, str(new))
    loaded = load_project_context(ws, token_budget=40)
    names = [p.path for p in loaded.files]
    dropped = [p.path for p in loaded.dropped]
    assert "new.md" in dropped
    assert "old.md" in names or loaded.block is None


def test_brain_has_pins_after_memory(tmp_path: Path) -> None:
    home, ws = tmp_path / "home", tmp_path / "ws"
    _write(ws / "AGENTS.md", "steering\n")
    _write(memory_dir(ws) / "MEMORY.md", "memory-bytes\n")
    pinned = ws / "notes.md"
    _write(pinned, "pin-bytes\n")
    add_pin(ws, str(pinned))
    ctx = load_project_context(ws, 2000)
    assembler = PromptAssembler(ws, home_dir=home)
    brain = assembler.assemble_sync("brain", memory="memory-bytes\n", project_context=ctx.block)
    worker = assembler.assemble_sync("worker", task="go", project_context=ctx.block)
    assert "pin-bytes" in brain.text
    assert brain.text.index("memory-bytes") < brain.text.index("pin-bytes")
    assert "pin-bytes" not in worker.text
    empty = assembler.assemble_sync("brain", memory="memory-bytes\n")
    assert brain.prefix_hash == empty.prefix_hash
