"""Display ordering and negative-origin layouts — no X connection."""

from __future__ import annotations

from tst_cu_mcp.backends.linux import order_displays


def test_primary_is_index_zero_even_when_not_leftmost() -> None:
    # A laptop (primary) plus a monitor to its left, which has a negative origin.
    ordered = order_displays(
        [
            (2, -1920, 0, 1920, 1080, False, 1.0),
            (1, 0, 0, 1920, 1080, True, 1.0),
        ]
    )
    assert [d.display_id for d in ordered] == [1, 2]
    assert ordered[0].is_main is True
    assert ordered[1].x == -1920


def test_negative_origin_is_preserved() -> None:
    ordered = order_displays([(7, -1280, -200, 1280, 1024, True, 1.25)])
    assert ordered[0].x == -1280
    assert ordered[0].y == -200
    assert ordered[0].scale == 1.25


def test_top_then_left_tiebreak_among_secondaries() -> None:
    ordered = order_displays(
        [
            (1, 0, 0, 100, 100, True, 1.0),
            (3, 200, 50, 100, 100, False, 1.0),
            (2, 50, 50, 100, 100, False, 1.0),
        ]
    )
    assert [d.display_id for d in ordered] == [1, 2, 3]
