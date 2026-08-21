"""Tailscale bind resolution (TD-3601).

Live Tailscale is not required. Every classification path uses a fake
interface table except the getifaddrs smoke test.
"""

from __future__ import annotations

import sys

import pytest

from tstd.tailscale_bind import (
    interface_looks_like_tailscale,
    is_tailscale_address,
    list_interfaces,
    resolve_remote_bind,
)

FAKE_TABLE = {
    "lo0": ("127.0.0.1", "::1"),
    "en0": ("192.168.1.10",),
    "eth0": ("10.0.0.8",),
    "tailscale0": ("100.64.1.5", "fd7a:115c:a1e0::5"),
    "utun3": ("100.64.9.2", "fe80::1"),
    "utun4": ("192.168.90.1",),
    "tailscale1": ("192.168.50.2",),
}


def _table() -> dict[str, tuple[str, ...]]:
    return FAKE_TABLE


def test_empty_bind_is_off() -> None:
    def boom() -> dict[str, tuple[str, ...]]:
        raise AssertionError("enumerated")

    assert resolve_remote_bind("") is None
    assert resolve_remote_bind("   ") is None
    # Off must not consult the enumerator.
    assert resolve_remote_bind("", interfaces=boom) is None


def test_cgnat_ipv4_resolves_without_a_table() -> None:
    assert resolve_remote_bind("100.64.1.5") == "100.64.1.5"


def test_tailscale_ula_resolves_without_a_table() -> None:
    assert resolve_remote_bind("fd7a:115c:a1e0::5") == "fd7a:115c:a1e0::5"


def test_interface_name_resolves_from_the_table() -> None:
    assert resolve_remote_bind("tailscale0", interfaces=_table) == "100.64.1.5"


def test_utun_with_cgnat_resolves() -> None:
    assert resolve_remote_bind("utun3", interfaces=_table) == "100.64.9.2"


def test_non_cgnat_on_tailscale_named_iface_is_allowed() -> None:
    assert resolve_remote_bind("192.168.50.2", interfaces=_table) == "192.168.50.2"


def test_lan_address_is_refused() -> None:
    with pytest.raises(ValueError, match="not a Tailscale"):
        resolve_remote_bind("192.168.1.10", interfaces=_table)
    with pytest.raises(ValueError, match="not a Tailscale"):
        resolve_remote_bind("10.0.0.8", interfaces=_table)
    with pytest.raises(ValueError, match="not a Tailscale"):
        resolve_remote_bind("172.16.0.1", interfaces=_table)


def test_unspecified_is_refused() -> None:
    with pytest.raises(ValueError, match=r"never 0\.0\.0\.0"):
        resolve_remote_bind("0.0.0.0")
    with pytest.raises(ValueError, match=r"never 0\.0\.0\.0"):
        resolve_remote_bind("::")
    with pytest.raises(ValueError, match=r"never 0\.0\.0\.0"):
        resolve_remote_bind("*")


def test_loopback_is_not_a_remote_bind() -> None:
    with pytest.raises(ValueError, match="not a Tailscale"):
        resolve_remote_bind("127.0.0.1", interfaces=_table)


def test_non_tailscale_interface_name_is_refused() -> None:
    with pytest.raises(ValueError, match="not a Tailscale interface"):
        resolve_remote_bind("eth0", interfaces=_table)
    with pytest.raises(ValueError, match="not a Tailscale interface"):
        resolve_remote_bind("en0", interfaces=_table)
    with pytest.raises(ValueError, match="not a Tailscale interface"):
        resolve_remote_bind("utun4", interfaces=_table)


def test_missing_interface_is_refused() -> None:
    with pytest.raises(ValueError, match="not found"):
        resolve_remote_bind("tailscale0", interfaces=lambda: {"en0": ("192.168.1.10",)})


def test_is_tailscale_address() -> None:
    assert is_tailscale_address("100.64.0.1")
    assert is_tailscale_address("100.127.255.254")
    assert is_tailscale_address("fd7a:115c:a1e0:1::2")
    assert not is_tailscale_address("100.63.255.255")
    assert not is_tailscale_address("100.128.0.1")
    assert not is_tailscale_address("192.168.1.1")
    assert not is_tailscale_address("0.0.0.0")
    assert not is_tailscale_address("127.0.0.1")
    assert not is_tailscale_address("tailscale0")


def test_interface_name_matching() -> None:
    assert interface_looks_like_tailscale("tailscale0", ())
    assert interface_looks_like_tailscale("Tailscale", ("192.168.1.1",))
    assert interface_looks_like_tailscale("utun3", ("100.64.1.1",))
    assert not interface_looks_like_tailscale("utun3", ("192.168.1.1",))
    assert not interface_looks_like_tailscale("eth0", ("100.64.1.1",))


def test_list_interfaces_smoke() -> None:
    """Default enumerator reads ifaddrs; it must not call tailscale status."""
    if sys.platform == "win32":
        assert list_interfaces() == {}
        return
    table = list_interfaces()
    assert isinstance(table, dict)
    addrs = [addr for found in table.values() for addr in found]
    assert any(addr in {"127.0.0.1", "::1"} for addr in addrs)
