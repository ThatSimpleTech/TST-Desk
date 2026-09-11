"""Local worker remap for CU-heavy sessions (TD-3903).

Non-CU turns keep the active preset's remote worker. After a desktop_ or
browser_ tool, the worker *client* uses the named local-worker preset's
worker tier. Brain is unchanged. Mock provider only — no live model.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from tests.test_dispatch import (
    attach_auto_approver,
    make_classifier,
    wait_for_turn,
)
from tstd.config import ComputerUseConfig, ModelConfig, Preset, TierConfig, default_config_yaml
from tstd.desktop.mock import MockDesktopDriver
from tstd.local_worker import (
    effective_tier,
    is_cu_tool,
    last_cu_surface,
    local_worker_tier,
    session_is_cu_heavy,
    titlebar_slugs,
)
from tstd.loop import agent_loop
from tstd.mock import MockProvider, Script
from tstd.protocol import TierState, ToolCall
from tstd.router import TierRouter
from tstd.session import Session, SessionRunner
from tstd.tools import create_registry, register_builtin_handlers
from tstd.tools.dispatch import ToolDispatcher


def _vllm_worker_url() -> str:
    """The shipped vllm worker URL — slugs/URLs stay in yaml (§2.7)."""
    data = yaml.safe_load(default_config_yaml())
    assert isinstance(data, dict)
    presets = data["presets"]
    assert isinstance(presets, dict)
    return str(presets["vllm"]["worker"]["base_url"])


def _tier(slug: str, base_url: str) -> TierConfig:
    return TierConfig(
        slug=slug,
        base_url=base_url,
        input_price=1.0,
        output_price=2.0,
        cache_read_price=0.5,
        context_window=100_000,
        max_output_tokens=1_000,
    )


def _loopback_tier(slug: str | None, base_url: str) -> TierConfig:
    return TierConfig(
        slug=slug,
        base_url=base_url,
        input_price=0.0,
        output_price=0.0,
        cache_read_price=0.0,
        context_window=32_768,
        max_output_tokens=4_096,
    )


def _cu_config(
    *,
    local_preset: str = "vllm",
    local_slug: str | None = "local-vllm-worker",
) -> ModelConfig:
    remote = "https://remote.example/v1"
    local_url = _vllm_worker_url()
    return ModelConfig(
        presets={
            "remote": Preset(
                brain=_tier("test-brain", remote),
                worker=_tier("test-worker", remote),
                validator=_tier("test-validator", remote),
            ),
            "vllm": Preset(
                brain=_loopback_tier(None, local_url),
                worker=_loopback_tier(local_slug, local_url),
                validator=_loopback_tier(None, local_url),
            ),
        },
        active_preset="remote",
        computer_use=ComputerUseConfig(local_worker_preset=local_preset),
    )


class RecordingFactory:
    """Mock factory that records which tier URL/slug it was asked to serve."""

    def __init__(self, mock: MockProvider) -> None:
        self.mock = mock
        self.urls: list[str] = []
        self.slugs: list[str | None] = []

    async def __call__(self, tier_cfg: TierConfig) -> MockProvider:
        self.urls.append(tier_cfg.base_url)
        self.slugs.append(tier_cfg.slug)
        return self.mock


async def _start(
    session: Session,
    router: TierRouter,
    factory: RecordingFactory,
    config: ModelConfig,
    dispatcher: ToolDispatcher,
) -> SessionRunner:
    registry = dispatcher.registry
    runner = SessionRunner(
        session,
        loop_factory=lambda s: agent_loop(
            s,
            router,
            factory,
            config,
            tool_registry=registry,
            tool_dispatcher=dispatcher,
        ),
    )
    await runner.start()
    return runner


def _cu_dispatcher(workspace: Path) -> ToolDispatcher:
    registry = create_registry()
    dispatcher = attach_auto_approver(
        ToolDispatcher(registry, classifier=make_classifier(str(workspace)))
    )
    register_builtin_handlers(dispatcher, desktop_driver=MockDesktopDriver())
    return dispatcher


class TestLocalWorkerHelpers:
    def test_cu_tool_signal_matches_screen_tab(self) -> None:
        assert is_cu_tool("desktop_screenshot")
        assert is_cu_tool("browser_navigate")
        assert not is_cu_tool("fs_read")
        assert not is_cu_tool("echo")
        # Grok names the same MCP tools computer-use__<tool>; diagnostics
        # are not an episode.
        assert is_cu_tool("computer-use__click")
        assert is_cu_tool("computer-use__screenshot")
        assert not is_cu_tool("computer-use__check_permissions")
        assert not is_cu_tool("computer-use__health")
        assert not is_cu_tool("computer-use__overlay_session")

    def test_non_cu_uses_active_worker(self) -> None:
        config = _cu_config()
        assert effective_tier(config, "worker", cu_heavy=False) is config.tier("worker")
        assert effective_tier(config, "brain", cu_heavy=True) is config.tier("brain")
        assert local_worker_tier(config) is config.presets["vllm"].worker

    def test_cu_heavy_remaps_worker_only(self) -> None:
        config = _cu_config()
        remapped = effective_tier(config, "worker", cu_heavy=True)
        assert remapped.base_url == _vllm_worker_url()
        assert remapped.slug == "local-vllm-worker"
        assert effective_tier(config, "brain", cu_heavy=True).slug == "test-brain"
        slugs = titlebar_slugs(config, cu_heavy=True)
        assert slugs["worker"] == "local-vllm-worker"
        assert slugs["brain"] == "test-brain"

    def test_empty_preset_never_remaps(self) -> None:
        config = _cu_config(local_preset="")
        assert local_worker_tier(config) is None
        assert effective_tier(config, "worker", cu_heavy=True) is config.tier("worker")
        assert titlebar_slugs(config, cu_heavy=True)["worker"] == "test-worker"

    def test_missing_preset_never_remaps(self) -> None:
        config = _cu_config(local_preset="no-such-preset")
        assert local_worker_tier(config) is None
        assert effective_tier(config, "worker", cu_heavy=True).slug == "test-worker"

    def test_unresolved_local_slug_is_omitted(self) -> None:
        config = _cu_config(local_slug=None)
        slugs = titlebar_slugs(config, cu_heavy=True)
        assert "worker" not in slugs
        assert slugs["brain"] == "test-brain"

    async def test_session_scans_event_log(self, tmp_path: Path) -> None:
        session = Session(str(tmp_path))
        assert session_is_cu_heavy(session) is False
        await session.event_log.add(
            ToolCall(
                session_id=session.id,
                tool_call_id="t1",
                name="desktop_click",
                arguments={},
                seq=1,
            )
        )
        session.used_cu = False
        assert session_is_cu_heavy(session) is True
        assert session.used_cu is True

    async def test_last_cu_surface_follows_the_log(self, tmp_path: Path) -> None:
        session = Session(str(tmp_path))
        assert last_cu_surface(session) is None
        await session.event_log.add(
            ToolCall(
                session_id=session.id,
                tool_call_id="t1",
                name="browser_screenshot",
                arguments={},
                seq=1,
            )
        )
        assert last_cu_surface(session) == "browser"
        await session.event_log.add(
            ToolCall(
                session_id=session.id,
                tool_call_id="t2",
                name="desktop_screenshot",
                arguments={},
                seq=2,
            )
        )
        assert last_cu_surface(session) == "desktop"


class TestLocalWorkerLoop:
    async def test_non_cu_worker_stays_on_remote(self, tmp_path: Path) -> None:
        config = _cu_config()
        session = Session(str(tmp_path))
        router = TierRouter(lead_turns=1)
        router.set_tier("worker")
        mock = MockProvider(sequences={"test-worker": [Script(kind="stream", content="ok")]})
        factory = RecordingFactory(mock)
        dispatcher = _cu_dispatcher(tmp_path)
        runner = await _start(session, router, factory, config, dispatcher)

        await session.add_user_message("hello")
        await wait_for_turn(session, 1)

        assert factory.urls == [config.tier("worker").base_url]
        assert factory.slugs == ["test-worker"]
        assert mock.calls[-1].model == "test-worker"
        states = [e for e in session.event_log.all_events if isinstance(e, TierState)]
        assert states[-1].model_slugs["worker"] == "test-worker"
        assert states[-1].model_slugs["brain"] == "test-brain"

        await runner.cancel()

    async def test_after_cu_tool_worker_hits_local_loopback(self, tmp_path: Path) -> None:
        config = _cu_config()
        session = Session(str(tmp_path))
        session.persist_dir = tmp_path / "persist"
        session.persist_dir.mkdir()
        router = TierRouter(lead_turns=1)
        router.set_tier("worker")
        mock = MockProvider(
            sequences={
                "test-worker": [
                    Script(
                        kind="tool_call",
                        tool_name="desktop_screenshot",
                        tool_arguments="{}",
                    ),
                ],
                "local-vllm-worker": [Script(kind="stream", content="done")],
            }
        )
        factory = RecordingFactory(mock)
        dispatcher = _cu_dispatcher(tmp_path)
        runner = await _start(session, router, factory, config, dispatcher)

        await session.add_user_message("look at the screen")
        await wait_for_turn(session, 1)

        local_url = _vllm_worker_url()
        remote_url = config.tier("worker").base_url
        assert factory.urls == [remote_url, local_url]
        assert factory.slugs == ["test-worker", "local-vllm-worker"]
        assert [c.model for c in mock.calls] == ["test-worker", "local-vllm-worker"]
        states = [e for e in session.event_log.all_events if isinstance(e, TierState)]
        assert states[-1].model_slugs["worker"] == "local-vllm-worker"
        assert states[-1].model_slugs["brain"] == "test-brain"
        assert session.used_cu is True

        await runner.cancel()

    async def test_brain_url_unchanged_after_cu(self, tmp_path: Path) -> None:
        config = _cu_config()
        session = Session(str(tmp_path))
        session.persist_dir = tmp_path / "persist"
        session.persist_dir.mkdir()
        router = TierRouter(lead_turns=2)
        mock = MockProvider(
            sequences={
                "test-brain": [
                    Script(
                        kind="tool_call",
                        tool_name="desktop_screenshot",
                        tool_arguments="{}",
                    ),
                    Script(kind="stream", content="saw it"),
                ],
            }
        )
        factory = RecordingFactory(mock)
        dispatcher = _cu_dispatcher(tmp_path)
        runner = await _start(session, router, factory, config, dispatcher)

        await session.add_user_message("screenshot then stay on brain")
        await wait_for_turn(session, 1)

        brain_url = config.tier("brain").base_url
        # One client per URL — the follow-up reuses the brain client.
        assert factory.urls == [brain_url]
        assert factory.slugs == ["test-brain"]
        assert [c.model for c in mock.calls] == ["test-brain", "test-brain"]
        states = [e for e in session.event_log.all_events if isinstance(e, TierState)]
        assert states[-1].model_slugs["brain"] == "test-brain"
        assert states[-1].model_slugs["worker"] == "local-vllm-worker"

        await runner.cancel()

    async def test_empty_preset_stays_remote_after_cu(self, tmp_path: Path) -> None:
        config = _cu_config(local_preset="")
        session = Session(str(tmp_path))
        session.persist_dir = tmp_path / "persist"
        session.persist_dir.mkdir()
        router = TierRouter(lead_turns=1)
        router.set_tier("worker")
        mock = MockProvider(
            sequences={
                "test-worker": [
                    Script(
                        kind="tool_call",
                        tool_name="desktop_screenshot",
                        tool_arguments="{}",
                    ),
                    Script(kind="stream", content="done"),
                ],
            }
        )
        factory = RecordingFactory(mock)
        dispatcher = _cu_dispatcher(tmp_path)
        runner = await _start(session, router, factory, config, dispatcher)

        await session.add_user_message("look")
        await wait_for_turn(session, 1)

        remote = config.tier("worker").base_url
        assert factory.urls == [remote]
        assert factory.slugs == ["test-worker"]
        assert [c.model for c in mock.calls] == ["test-worker", "test-worker"]
        states = [e for e in session.event_log.all_events if isinstance(e, TierState)]
        assert states[-1].model_slugs["worker"] == "test-worker"

        await runner.cancel()
