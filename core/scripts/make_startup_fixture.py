"""Write a data folder like a long-used install, for the start-up gate.

Many chats, each with a long event log and a saved conversation, each in its
own project folder with an AGENTS.md and a tree of files, the way a git
worktree per chat looks. A real install with 183 chats failed to start in
0.5.4 and 0.5.5: reviving every chat ran before the daemon listened, and the
host gave up after 30 seconds. The release gate starts the packaged app on
this folder and needs it connected within 10 seconds.

    uv run python scripts/make_startup_fixture.py <data-dir> [--sessions 300]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from pathlib import Path

from tstd.config import default_config_yaml
from tstd.protocol import AssistantDelta, TurnComplete, UserTurn
from tstd.provider import ChatMessage
from tstd.session_persist import SessionPersist
from tstd.session_store import SessionStore


def _project(root: Path, files: int) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "AGENTS.md").write_text("Use the project's own style.\n", encoding="utf-8")
    for i in range(files):
        sub = root / "src" / f"pkg{i % 40}" / f"mod{i % 7}"
        sub.mkdir(parents=True, exist_ok=True)
        (sub / f"file{i}.py").write_text(f"x = {i}\n", encoding="utf-8")


def _config(data_dir: Path) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "config.yaml").write_text(default_config_yaml(), encoding="utf-8")


async def build(data_dir: Path, sessions: int, turns: int, files: int) -> None:
    _config(data_dir)
    store = SessionStore(data_dir)
    persist = SessionPersist(data_dir, log_max_events=10_000)
    projects = data_dir.parent / f"{data_dir.name}-projects"
    for n in range(sessions):
        sid = str(uuid.uuid4())
        project = projects / f"chat-{n:03d}"
        _project(project, files)
        await store.upsert(sid, str(project), "idle")
        seq = 0
        conversation: list[ChatMessage] = []
        for t in range(turns):
            for event in (
                UserTurn(session_id=sid, turn_id=f"t{t}", content=f"question {t}", seq=1),
                AssistantDelta(session_id=sid, delta=f"answer {t} " * 20, seq=1),
                TurnComplete(
                    session_id=sid, tokens=10, cost=0.0, tier="brain", duration=0.1, seq=1
                ),
            ):
                seq += 1
                persist.append_event(sid, event.model_copy(update={"seq": seq}))
            conversation += [
                ChatMessage(role="user", content=f"question {t}"),
                ChatMessage(role="assistant", content=f"answer {t}"),
            ]
        persist.save_conversation(sid, conversation)
    print(json.dumps({"data_dir": str(data_dir), "sessions": sessions, "turns": turns}))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("data_dir", type=Path)
    parser.add_argument("--sessions", type=int, default=300)
    parser.add_argument("--turns", type=int, default=60)
    parser.add_argument("--files", type=int, default=400)
    args = parser.parse_args()
    asyncio.run(build(args.data_dir, args.sessions, args.turns, args.files))


if __name__ == "__main__":
    main()
