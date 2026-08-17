"""The shipped paths a `local`-preset user actually runs (TD-1805).

TD-1805 made a loopback tier's ``slug`` optional and resolved it from
``/v1/models``.  Two shipped paths kept calling ``require_slug()`` on the
brain tier *before* anything resolved it, so on the ``local`` preset — the
one preset that leaves the slug unset — they raised ``ModelDiscoveryError``
before doing any work at all:

* ``tstd.e2e_harness.mock_plan`` — the offline harness (TD-1401)
* ``tstd.benchmarks.measure_first_token_latency`` — the perf baseline (TD-1404)

The whole suite ran under the default *remote* preset, where every slug is
pinned in config, so nothing ever exercised the branch.  These tests run
both paths with ``active_preset: local`` against a real loopback
``/v1/models`` server, which is the only configuration that would have
caught it.

They also pin the two things that must not be traded away for that fix:
an unresolvable endpoint still fails loudly rather than substituting a
model, and the live leg refuses an off-box endpoint *before* it sends
anything to it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest

from tests.test_model_discovery import ModelsEndpoint, closed_endpoint
from tstd.benchmarks import measure_first_token_latency
from tstd.config import ModelDiscoveryError, cached_config, default_config_yaml
from tstd.config_write import save_active_preset
from tstd.discovery import discover_model
from tstd.e2e_harness import main, mock_plan, run
from tstd.e2e_live import off_box_refusal
from tstd.logging import user_data_dir
from tstd.provider import ChatCompletionRequest, ChatCompletionResponse, ChatMessage

SERVED_MODEL = "served-by-the-endpoint:1b"


def _install_config(
    monkeypatch: pytest.MonkeyPatch, home: Path, preset: str, base_url: str
) -> None:
    """Make *preset* active in a throwaway user config aimed at *base_url*.

    ``HOME`` is redirected first, so the developer's own config is never
    read and never rewritten — and the run is reproducible from the shipped
    defaults rather than from whatever that machine happens to have.
    """
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.delenv("APPDATA", raising=False)
    cached_config.cache_clear()
    config_path = user_data_dir() / "config.yaml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        default_config_yaml().replace("http://127.0.0.1:11434/v1", base_url),
        encoding="utf-8",
    )
    save_active_preset(preset)
    cached_config.cache_clear()


@pytest.fixture
async def local_preset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[ModelsEndpoint]:
    """``active_preset: local``, aimed at a loopback endpoint serving one model."""
    server = ModelsEndpoint([SERVED_MODEL])
    await server.start()
    _install_config(monkeypatch, tmp_path / "home", "local", server.base_url)
    try:
        yield server
    finally:
        await server.stop()


@pytest.fixture
def local_preset_no_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """``active_preset: local``, aimed at a port with nothing listening."""
    url = closed_endpoint()
    _install_config(monkeypatch, tmp_path / "home", "local", url)
    return url


@pytest.fixture
def remote_preset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The shipped default: every tier pinned and priced."""
    _install_config(monkeypatch, tmp_path / "home", "tst-default", "http://127.0.0.1:11434/v1")


@pytest.fixture(autouse=True)
def _leave_no_cached_config() -> Iterator[None]:
    """A mutated config must never outlive its test — resolution writes the
    discovered slug onto the shared object every other test also reads."""
    cached_config.cache_clear()
    yield
    cached_config.cache_clear()


# ── The offline harness ────────────────────────────────────────────────


class TestHarnessOnTheLocalPreset:
    async def test_the_whole_pass_goes_green(
        self, local_preset: ModelsEndpoint, tmp_path: Path
    ) -> None:
        """The regression, end to end: before the fix this raised
        ``ModelDiscoveryError`` out of plan construction and no pass ran."""
        result = await run(tmp_path / "workspace", tmp_path / "data")
        assert result.ok, f"harness failed:\n{result.report()}"

    async def test_the_script_is_keyed_on_the_discovered_model(
        self, local_preset: ModelsEndpoint, tmp_path: Path
    ) -> None:
        """The slug came from the endpoint, not from config — so the scripted
        tool call answers the model the loop will actually ask for, and one
        round-trip covered all three tiers."""
        plan = await mock_plan(tmp_path / "workspace")
        assert cached_config().tier("brain").slug == SERVED_MODEL
        assert local_preset.request_count == 1

        response = await plan.provider.chat_completion(
            ChatCompletionRequest(
                model=SERVED_MODEL,
                messages=[ChatMessage(role="user", content="write the greeting")],
            )
        )
        assert isinstance(response, ChatCompletionResponse)
        assert response.message.tool_calls, "the scripted fs_write must be keyed on this model"

    async def test_a_zero_price_preset_is_not_expected_to_bill(
        self, local_preset: ModelsEndpoint, tmp_path: Path
    ) -> None:
        """The local preset prices every tier at zero.  Demanding a non-zero
        cost there would fail the pass for behaving correctly; the ledger
        check is what proves free work is still tracked."""
        plan = await mock_plan(tmp_path / "workspace")
        assert plan.expect_spend is False

    @pytest.mark.usefixtures("remote_preset")
    async def test_a_priced_preset_still_demands_a_bill(self, tmp_path: Path) -> None:
        """TD-1401's assertion is unchanged where there is a price to bill."""
        plan = await mock_plan(tmp_path / "workspace")
        assert cached_config().active_preset == "tst-default"
        assert plan.expect_spend is True

    async def test_a_dead_endpoint_fails_loudly(
        self, local_preset_no_server: str, tmp_path: Path
    ) -> None:
        """Never a substituted model: the typed error names the endpoint and
        the fix, the same one the daemon would report."""
        with pytest.raises(ModelDiscoveryError) as excinfo:
            await mock_plan(tmp_path / "workspace")
        assert excinfo.value.endpoint == local_preset_no_server
        assert excinfo.value.fix

    def test_the_cli_reports_a_dead_endpoint_as_not_run(
        self,
        local_preset_no_server: str,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Exit 2, like every other absent-server reason — an unreadable
        traceback would read as a broken harness rather than a machine that
        has no model server up."""
        code = main(["--workspace", str(tmp_path / "ws"), "--data-dir", str(tmp_path / "data")])
        assert code == 2
        assert local_preset_no_server in capsys.readouterr().out


# ── The perf baseline ──────────────────────────────────────────────────


class TestBenchmarkOnTheLocalPreset:
    async def test_first_token_latency_measures(
        self, local_preset: ModelsEndpoint, tmp_path: Path
    ) -> None:
        """Before the fix this raised out of the mock's construction, so the
        metric could not be measured at all on a local preset."""
        elapsed = await measure_first_token_latency(tmp_path / "bench")
        assert elapsed > 0

    async def test_a_dead_endpoint_fails_loudly(
        self, local_preset_no_server: str, tmp_path: Path
    ) -> None:
        with pytest.raises(ModelDiscoveryError):
            await measure_first_token_latency(tmp_path / "bench")


# ── Nothing leaves the box before the box is checked ───────────────────


class TestOffBoxEndpointIsRefusedFirst:
    def test_a_remote_endpoint_is_refused(self) -> None:
        assert off_box_refusal("https://api.example.com/v1") is not None

    def test_a_loopback_endpoint_is_allowed(self) -> None:
        assert off_box_refusal("http://127.0.0.1:11434/v1") is None

    def test_the_live_cli_sends_nothing_to_an_off_box_endpoint(
        self,
        local_preset_no_server: str,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Discovery used to run before the loopback check, so pointing
        ``--live-endpoint`` at a remote host contacted it and only then
        refused the run.  A refusal after the packet has left is not one."""
        contacted: list[str] = []

        async def _spy(base_url: str, **kwargs: object) -> str:
            contacted.append(base_url)
            raise AssertionError("discovery must not run against an off-box endpoint")

        monkeypatch.setattr("tstd.e2e_harness.discover_model", _spy)
        code = main(
            [
                "--workspace",
                str(tmp_path / "ws"),
                "--data-dir",
                str(tmp_path / "data"),
                "--live-endpoint",
                "https://api.example.com/v1",
            ]
        )
        assert code == 2
        assert contacted == []
        assert "loopback" in capsys.readouterr().out

    async def test_discovery_itself_refuses_an_off_box_url(self) -> None:
        """Belt and braces, at the root.  Loopback-only used to be a property
        of every caller checking first, and the harness's CLI stopped
        checking; a keyless GET is the wrong thing to send a stranger no
        matter who asked for it (§2.2, §2.3)."""
        with pytest.raises(ModelDiscoveryError) as excinfo:
            await discover_model("https://api.example.com/v1", tier="brain")
        assert "not on this machine" in str(excinfo.value)
        assert "brain" in excinfo.value.fix

    async def test_discovery_itself_still_reaches_a_loopback_endpoint(
        self, local_preset: ModelsEndpoint
    ) -> None:
        """The guard must not have closed the door on the supported case."""
        assert await discover_model(local_preset.base_url) == SERVED_MODEL


# ── The fix text matches the failure ───────────────────────────────────


class TestTheFixMatchesTheFailure:
    async def test_an_unreachable_endpoint_says_start_a_server(self) -> None:
        with pytest.raises(ModelDiscoveryError) as excinfo:
            await discover_model(closed_endpoint())
        assert "Start a local OpenAI-compatible model server" in excinfo.value.fix

    @pytest.mark.parametrize("status", [401, 403, 404])
    async def test_a_4xx_answer_says_check_the_base_url(self, status: int) -> None:
        """Something answered, so "start a server" would send the user to
        check a process that is already running."""
        server = ModelsEndpoint([], status=status, body='{"error": "nope"}')
        await server.start()
        try:
            with pytest.raises(ModelDiscoveryError) as excinfo:
                await discover_model(server.base_url)
            assert "base_url" in excinfo.value.fix
            assert "Start a local" not in excinfo.value.fix
        finally:
            await server.stop()

    async def test_a_5xx_answer_says_the_server_is_unwell(self) -> None:
        server = ModelsEndpoint([], status=503, body='{"error": "loading"}')
        await server.start()
        try:
            with pytest.raises(ModelDiscoveryError) as excinfo:
                await discover_model(server.base_url)
            assert "logs" in excinfo.value.fix
            assert "base_url" not in excinfo.value.fix
        finally:
            await server.stop()

    async def test_a_non_model_list_says_check_the_base_url(self) -> None:
        """Valid JSON, wrong service — not "load a model into that server"."""
        server = ModelsEndpoint([], body='{"status": "ok"}')
        await server.start()
        try:
            with pytest.raises(ModelDiscoveryError) as excinfo:
                await discover_model(server.base_url)
            assert "base_url" in excinfo.value.fix
            assert "Load a model" not in excinfo.value.fix
        finally:
            await server.stop()

    async def test_an_empty_model_list_says_load_a_model(self) -> None:
        """The one case where the server really is the right one and idle."""
        server = ModelsEndpoint([])
        await server.start()
        try:
            with pytest.raises(ModelDiscoveryError) as excinfo:
                await discover_model(server.base_url)
            assert "Load a model" in excinfo.value.fix
        finally:
            await server.stop()
