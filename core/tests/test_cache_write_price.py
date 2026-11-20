"""A cache write is charged at its own price (TD-5280).

Some providers charge more to store prompt tokens in their cache than for
plain input (1.25 times, on Anthropic models). The meter used to charge
them as plain input, so it under-reported. These pin the arithmetic, that
nothing changes for a tier that names no write price, and that a prompt
cache can be asked for through a tier's ``extra_body``.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from tests.test_cost import BRAIN
from tests.test_request_extras import _tier
from tstd.config import TierConfig
from tstd.cost import compute_call_cost, compute_call_details, prompt_bill
from tstd.provider import ChatCompletionRequest, Usage

# $3.00 in, $15.00 out, $0.30 read, $3.75 write: a 1.25 times write.
PRICED = BRAIN.model_copy(
    update={
        "input_price": 3.00,
        "output_price": 15.00,
        "cache_read_price": 0.30,
        "cache_write_price": 3.75,
    }
)


def _usage(**details: Any) -> Usage:
    return Usage.from_api_dict(
        {
            "prompt_tokens": 10_000,
            "completion_tokens": 1_000,
            "total_tokens": 11_000,
            "prompt_tokens_details": details,
        }
    )


def test_the_write_count_is_read_from_the_usage_block() -> None:
    assert _usage(cached_tokens=0, cache_write_tokens=8_000).cache_write_tokens == 8_000
    assert _usage(cached_tokens=6_000).cache_write_tokens is None
    assert Usage.from_api_dict({"prompt_tokens": 5}).cache_write_tokens is None
    assert Usage.from_api_dict(None).cache_write_tokens is None


@pytest.mark.parametrize("bad", [-1, "8000", 8000.5, True, None, [8000]])
def test_a_write_count_that_is_not_a_plain_count_is_ignored(bad: object) -> None:
    assert _usage(cached_tokens=0, cache_write_tokens=bad).cache_write_tokens is None


def test_a_first_call_that_writes_the_cache() -> None:
    usage = _usage(cached_tokens=0, cache_write_tokens=8_000)
    bill = prompt_bill(usage, PRICED)
    assert (bill.cached_tokens, bill.uncached_tokens, bill.written_tokens) == (0, 10_000, 8_000)
    # 2,000 plain at $3.00 + 8,000 written at $3.75 + 1,000 out at $15.00
    expected = (2_000 * 3.00 + 8_000 * 3.75 + 1_000 * 15.00) / 1_000_000
    assert compute_call_cost(usage, PRICED) == pytest.approx(expected)
    assert compute_call_cost(usage, PRICED) == pytest.approx(0.051)


def test_a_later_call_that_reads_it() -> None:
    usage = _usage(cached_tokens=8_000, cache_write_tokens=500)
    # 8,000 read at $0.30 + 500 written at $3.75 + 1,500 plain at $3.00 + output
    expected = (8_000 * 0.30 + 500 * 3.75 + 1_500 * 3.00 + 1_000 * 15.00) / 1_000_000
    assert compute_call_cost(usage, PRICED) == pytest.approx(expected)


def test_the_breakdown_still_adds_up() -> None:
    usage = _usage(cached_tokens=8_000, cache_write_tokens=500)
    parts = compute_call_details(usage, PRICED)
    assert parts["uncached_prompt_tokens"] == 2_000
    assert parts["prompt_cost"] + parts["cached_cost"] + parts["completion_cost"] == (
        pytest.approx(parts["total_cost"])
    )
    assert parts["total_cost"] == compute_call_cost(usage, PRICED)


def test_no_write_price_charges_a_write_as_plain_input() -> None:
    """A tier written before the field existed costs what it always did."""
    assert BRAIN.cache_write_price is None
    with_writes = _usage(cached_tokens=2_000, cache_write_tokens=6_000)
    without = _usage(cached_tokens=2_000)
    assert compute_call_cost(with_writes, BRAIN) == compute_call_cost(without, BRAIN)
    old = (8_000 * BRAIN.input_price + 2_000 * BRAIN.cache_read_price) / 1_000_000 + (
        1_000 * BRAIN.output_price / 1_000_000
    )
    assert compute_call_cost(with_writes, BRAIN) == pytest.approx(old)


def test_no_reported_writes_changes_nothing_on_a_priced_tier() -> None:
    usage = _usage(cached_tokens=2_000)
    expected = (8_000 * 3.00 + 2_000 * 0.30 + 1_000 * 15.00) / 1_000_000
    assert compute_call_cost(usage, PRICED) == pytest.approx(expected)


def test_a_write_count_larger_than_the_prompt_is_capped() -> None:
    """A provider's bad figure cannot bill more tokens than the prompt had."""
    usage = _usage(cached_tokens=4_000, cache_write_tokens=50_000)
    bill = prompt_bill(usage, PRICED)
    assert bill.written_tokens == 6_000
    expected = (4_000 * 0.30 + 6_000 * 3.75 + 1_000 * 15.00) / 1_000_000
    assert compute_call_cost(usage, PRICED) == pytest.approx(expected)


def test_a_free_write_can_be_said() -> None:
    free = PRICED.model_copy(update={"cache_write_price": 0.0})
    usage = _usage(cached_tokens=0, cache_write_tokens=10_000)
    assert compute_call_cost(usage, free) == pytest.approx(1_000 * 15.00 / 1_000_000)


def test_a_negative_write_price_is_refused() -> None:
    fields = BRAIN.model_dump()
    fields["cache_write_price"] = -1.0
    with pytest.raises(ValidationError):
        TierConfig(**fields)


def test_a_prompt_cache_can_be_asked_for_through_extra_body() -> None:
    """``cache_control`` goes out as a top-level field of the request."""
    tier = _tier("anthropic/some-model", 1_000, {"cache_control": {"type": "ephemeral"}})
    body = ChatCompletionRequest(
        model=tier.require_slug(), messages=[], max_tokens=64, extra_body=tier.extra_body
    ).to_dict()
    assert body["cache_control"] == {"type": "ephemeral"}
    assert body["model"] == "anthropic/some-model" and body["max_tokens"] == 64
