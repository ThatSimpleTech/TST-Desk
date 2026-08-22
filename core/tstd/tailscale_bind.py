"""Resolve an opt-in Tailscale bind target (TD-3601).

A host is Tailscale when it is CGNAT ``100.64.0.0/10`` or Tailscale ULA
``fd7a:115c:a1e0::/48``, or when it sits on an interface whose name looks
like Tailscale (``tailscale*``, or ``utun*`` that already has a Tailscale
address). The interface table is injectable so tests never need a live
Tailscale node. The default enumerator reads ``getifaddrs`` — it does not
call ``tailscale status``.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import sys
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from ipaddress import IPv4Address, IPv6Address, ip_address, ip_network

InterfaceTable = Mapping[str, Sequence[str]]
InterfaceEnumerator = Callable[[], InterfaceTable]

TAILSCALE_CGNAT = ip_network("100.64.0.0/10")
TAILSCALE_ULA = ip_network("fd7a:115c:a1e0::/48")

_UNSPECIFIED = frozenset({"0.0.0.0", "::", "*"})
_AF_INET = 2
_INET_ADDRSTRLEN = 16
_INET6_ADDRSTRLEN = 46


def _af_inet6() -> int:
    # sockaddr.sa_family values: Darwin 30, Linux 10.
    return 30 if sys.platform == "darwin" else 10


def _strip_zone(host: str) -> str:
    return host.split("%", 1)[0]


def _as_ip(host: str) -> IPv4Address | IPv6Address | None:
    try:
        addr = ip_address(_strip_zone(host))
    except ValueError:
        return None
    if addr.version == 6 and addr.ipv4_mapped is not None:
        return addr.ipv4_mapped
    return addr


def is_tailscale_address(host: str) -> bool:
    """True when *host* is in Tailscale's CGNAT or ULA range."""
    addr = _as_ip(host)
    if addr is None or addr.is_unspecified or addr.is_loopback:
        return False
    return addr in TAILSCALE_CGNAT or addr in TAILSCALE_ULA


def interface_looks_like_tailscale(name: str, addresses: Sequence[str]) -> bool:
    """True when *name* is a Tailscale iface, or a ``utun`` carrying one."""
    lowered = name.lower()
    if lowered.startswith("tailscale"):
        return True
    if lowered.startswith("utun"):
        return any(is_tailscale_address(addr) for addr in addresses)
    return False


def _usable_bind_address(addresses: Sequence[str]) -> str | None:
    parsed: list[IPv4Address | IPv6Address] = []
    for raw in addresses:
        addr = _as_ip(raw)
        if addr is not None:
            parsed.append(addr)
    for ip in parsed:
        if ip in TAILSCALE_CGNAT:
            return str(ip)
    for ip in parsed:
        if ip in TAILSCALE_ULA:
            return str(ip)
    for ip in parsed:
        if not ip.is_unspecified and not ip.is_link_local:
            return str(ip)
    return None


def _ip_on_tailscale_interface(host: str, table: InterfaceTable) -> bool:
    target = _as_ip(host)
    if target is None:
        return False
    for name, addresses in table.items():
        if not interface_looks_like_tailscale(name, addresses):
            continue
        if any(_as_ip(addr) == target for addr in addresses):
            return True
    return False


def resolve_remote_bind(
    spec: str,
    interfaces: InterfaceEnumerator | None = None,
) -> str | None:
    """Return the Tailscale address to bind, or ``None`` when bind is off.

    Empty *spec* is off and does not enumerate interfaces. A CGNAT / ULA
    IP resolves without a table. Interface names and non-CGNAT IPs consult
    *interfaces* (or ``list_interfaces``).

    Raises:
        ValueError: *spec* is unspecified, not Tailscale, or not found.
    """
    spec = spec.strip()
    if not spec:
        return None
    if spec in _UNSPECIFIED:
        raise ValueError(
            f"Refusing to bind to {spec!r}: never 0.0.0.0 / :: "
            f"(prime directive §2.1; Tailscale is the only non-loopback bind)"
        )

    addr = _as_ip(spec)
    if addr is not None:
        if addr.is_unspecified:
            raise ValueError(
                f"Refusing to bind to {spec!r}: never 0.0.0.0 / :: "
                f"(prime directive §2.1; Tailscale is the only non-loopback bind)"
            )
        if is_tailscale_address(spec):
            return str(addr)
        table = (interfaces or list_interfaces)()
        if _ip_on_tailscale_interface(spec, table):
            return str(addr)
        raise ValueError(
            f"Refusing to bind to {spec!r}: not a Tailscale address "
            f"(need 100.64.0.0/10, fd7a:115c:a1e0::/48, or an address "
            f"on a Tailscale interface)"
        )

    table = (interfaces or list_interfaces)()
    match = next((name for name in table if name.lower() == spec.lower()), None)
    if match is None:
        raise ValueError(f"Refusing to bind to interface {spec!r}: not found")
    addresses = table[match]
    if not interface_looks_like_tailscale(match, addresses):
        raise ValueError(f"Refusing to bind to interface {spec!r}: not a Tailscale interface")
    chosen = _usable_bind_address(addresses)
    if chosen is None:
        raise ValueError(f"Refusing to bind to interface {spec!r}: no usable address")
    return chosen


class _Ifaddrs(ctypes.Structure):
    pass


_Ifaddrs._fields_ = [
    ("ifa_next", ctypes.POINTER(_Ifaddrs)),
    ("ifa_name", ctypes.c_char_p),
    ("ifa_flags", ctypes.c_uint),
    ("ifa_addr", ctypes.c_void_p),
    ("ifa_netmask", ctypes.c_void_p),
    ("ifa_ifu", ctypes.c_void_p),
    ("ifa_data", ctypes.c_void_p),
]


_LIBC: ctypes.CDLL | None = None


def _load_libc() -> ctypes.CDLL:
    global _LIBC
    if _LIBC is not None:
        return _LIBC
    name = ctypes.util.find_library("c")
    if name is None:
        raise OSError("libc not found")
    libc = ctypes.CDLL(name, use_errno=True)
    libc.inet_ntop.argtypes = [
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    libc.inet_ntop.restype = ctypes.c_char_p
    libc.getifaddrs.argtypes = [ctypes.POINTER(ctypes.POINTER(_Ifaddrs))]
    libc.getifaddrs.restype = ctypes.c_int
    libc.freeifaddrs.argtypes = [ctypes.POINTER(_Ifaddrs)]
    libc.freeifaddrs.restype = None
    _LIBC = libc
    return libc


def _sockaddr_ip(libc: ctypes.CDLL, addr: int) -> str | None:
    """Read an IPv4/IPv6 string from a ``sockaddr`` pointer."""
    family_bytes = ctypes.string_at(addr, 2)
    if sys.platform == "darwin":
        family = family_bytes[1]
    else:
        family = int.from_bytes(family_bytes, sys.byteorder)
    af6 = _af_inet6()
    if family == _AF_INET:
        src, size, af = addr + 4, _INET_ADDRSTRLEN, _AF_INET
    elif family == af6:
        src, size, af = addr + 8, _INET6_ADDRSTRLEN, af6
    else:
        return None
    buf = ctypes.create_string_buffer(size)
    if not libc.inet_ntop(af, src, buf, size):
        return None
    return buf.value.decode("ascii")


def list_interfaces() -> dict[str, tuple[str, ...]]:
    """Local ``{ifname: addrs}`` from getifaddrs.

    Windows has no getifaddrs; the table is empty. A CGNAT IPv4 in
    ``remote.bind`` still resolves without enumeration.
    """
    if sys.platform == "win32":
        return {}
    return _list_interfaces_posix()


def _list_interfaces_posix() -> dict[str, tuple[str, ...]]:
    libc = _load_libc()
    head = ctypes.POINTER(_Ifaddrs)()
    if libc.getifaddrs(ctypes.byref(head)) != 0:
        raise OSError(ctypes.get_errno(), "getifaddrs failed")
    grouped: dict[str, list[str]] = defaultdict(list)
    try:
        node = head
        while node:
            current = node.contents
            raw_name = current.ifa_name
            addr_ptr = current.ifa_addr
            if raw_name and addr_ptr:
                name = raw_name.decode() if isinstance(raw_name, bytes) else raw_name
                ip = _sockaddr_ip(libc, int(addr_ptr))
                if ip is not None and ip not in grouped[name]:
                    grouped[name].append(ip)
            node = current.ifa_next
    finally:
        libc.freeifaddrs(head)
    return {name: tuple(addrs) for name, addrs in grouped.items()}
