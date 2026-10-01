"""The CLI's turn deadline tracks the daemon's retry budget.

The base is ``scheduler.max_run_seconds`` (or ``--timeout``). On top of
it goes the provider retry allowance when the config can be read. A
fixed 120 second client deadline used to report "timed out" while the
daemon was still in backoff.
"""

from __future__ import annotations

from typing import Any

import pytest

from tstd.cli import _turn_timeout_secs
from tstd.config import DEFAULT_MAX_RUN_SECONDS, ProviderRetryConfig, SchedulerConfig
from tstd.provider import RetryConfig, worst_case_retry_seconds


class _Cfg:
    def __init__(
        self,
        retry: ProviderRetryConfig,
        scheduler: SchedulerConfig | None = None,
    ) -> None:
        self.provider_retry = retry
        self.scheduler = scheduler if scheduler is not None else SchedulerConfig()


def _patch_config(monkeypatch: pytest.MonkeyPatch, retry: ProviderRetryConfig) -> None:
    monkeypatch.setattr("tstd.config.load_config", lambda *a, **k: _Cfg(retry))


class TestTurnTimeoutTracksRetryBudget:
    def test_default_budget_is_the_config_plus_the_retry_allowance(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_config(monkeypatch, ProviderRetryConfig())
        base = float(DEFAULT_MAX_RUN_SECONDS)
        allowance = worst_case_retry_seconds(
            RetryConfig(
                max_retries=ProviderRetryConfig().max_retries,
                initial_delay=ProviderRetryConfig().initial_delay,
                max_delay=ProviderRetryConfig().max_delay,
            )
        )
        assert _turn_timeout_secs() == base + allowance

    def test_raising_max_retries_extends_the_deadline(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_config(monkeypatch, ProviderRetryConfig(max_retries=3))
        low = _turn_timeout_secs()
        _patch_config(monkeypatch, ProviderRetryConfig(max_retries=8))
        high = _turn_timeout_secs()
        assert high > low
        budget = worst_case_retry_seconds(RetryConfig(max_retries=8))
        assert high == float(DEFAULT_MAX_RUN_SECONDS) + budget

    def test_the_deadline_is_the_base_plus_retries_not_a_fixed_120(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The base moved from 120s to scheduler.max_run_seconds.

        The retry add-on stayed. A raised retry count still lengthens
        the wait, and the total is that base plus the budget.
        """
        _patch_config(monkeypatch, ProviderRetryConfig(max_retries=8))
        budget = worst_case_retry_seconds(RetryConfig(max_retries=8))
        assert _turn_timeout_secs() == float(DEFAULT_MAX_RUN_SECONDS) + budget
        assert _turn_timeout_secs() > float(DEFAULT_MAX_RUN_SECONDS)

    def test_unreadable_config_falls_back_rather_than_raising(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(*a: Any, **k: Any) -> Any:
            raise OSError("config unreadable")

        monkeypatch.setattr("tstd.config.load_config", _boom)
        assert _turn_timeout_secs() == float(DEFAULT_MAX_RUN_SECONDS)

    def test_timeout_flag_replaces_the_base_and_keeps_the_retry_allowance(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_config(monkeypatch, ProviderRetryConfig())
        flagged = _turn_timeout_secs(budget=30)
        budget = worst_case_retry_seconds(
            RetryConfig(
                max_retries=ProviderRetryConfig().max_retries,
                initial_delay=ProviderRetryConfig().initial_delay,
                max_delay=ProviderRetryConfig().max_delay,
            )
        )
        assert flagged == 30 + budget
        assert flagged < _turn_timeout_secs()

    def test_timeout_flag_without_a_readable_config_is_exact(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(*a: Any, **k: Any) -> Any:
            raise OSError("config unreadable")

        monkeypatch.setattr("tstd.config.load_config", _boom)
        assert _turn_timeout_secs(budget=30) == 30
