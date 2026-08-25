"""The CLI's turn deadline tracks the daemon's retry budget.

A fixed client deadline is a silent trap once ``provider_retry.max_retries``
is raised: the daemon keeps retrying, the CLI stops listening, and the user is
told the turn "timed out" while it was still legitimately in backoff.
"""

from __future__ import annotations

from typing import Any

import pytest

from tstd.cli import _TURN_TIMEOUT_SECS, _turn_timeout_secs
from tstd.config import ProviderRetryConfig
from tstd.provider import RetryConfig, worst_case_retry_seconds


class _Cfg:
    def __init__(self, retry: ProviderRetryConfig) -> None:
        self.provider_retry = retry


def _patch_config(monkeypatch: pytest.MonkeyPatch, retry: ProviderRetryConfig) -> None:
    monkeypatch.setattr("tstd.config.load_config", lambda *a, **k: _Cfg(retry))


class TestTurnTimeoutTracksRetryBudget:
    def test_default_budget_still_leaves_the_base_allowance(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_config(monkeypatch, ProviderRetryConfig())
        assert _turn_timeout_secs() >= _TURN_TIMEOUT_SECS

    def test_raising_max_retries_extends_the_deadline(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_config(monkeypatch, ProviderRetryConfig(max_retries=3))
        low = _turn_timeout_secs()
        _patch_config(monkeypatch, ProviderRetryConfig(max_retries=8))
        high = _turn_timeout_secs()
        assert high > low
        # And it must actually cover the daemon's worst case, not merely grow.
        budget = worst_case_retry_seconds(RetryConfig(max_retries=8))
        assert high >= _TURN_TIMEOUT_SECS + budget

    def test_the_old_fixed_deadline_would_have_been_too_short(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression guard for the bug this fixes."""
        _patch_config(monkeypatch, ProviderRetryConfig(max_retries=8))
        assert worst_case_retry_seconds(RetryConfig(max_retries=8)) > _TURN_TIMEOUT_SECS
        assert _turn_timeout_secs() > _TURN_TIMEOUT_SECS

    def test_unreadable_config_falls_back_rather_than_raising(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(*a: Any, **k: Any) -> Any:
            raise OSError("config unreadable")

        monkeypatch.setattr("tstd.config.load_config", _boom)
        assert _turn_timeout_secs() == _TURN_TIMEOUT_SECS
