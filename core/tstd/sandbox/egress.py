"""A sandboxed command's one way out: the bridge to the egress proxy (TD-5306).

With ``sandbox.network: allowlist`` a sandboxed command still has every
namespace unshared, so it has no route off the computer. What it gets is
a bridge: the sandbox's first process listens on the namespace's own
``127.0.0.1:3128``, forks off a relay, and execs the command with the
proxy variables pointing there. For each connection the relay asks the
daemon, over a socket pair the daemon handed in before the start, for a
fresh connected socket (``SCM_RIGHTS``), and pipes the two together. The
daemon serves the other end with ``EgressProxy`` (TD-5305), which checks
the token and the host.

Nothing on the host listens, and the seccomp filter that refuses
``socket(AF_UNIX)`` stays as it is: a socket pair and descriptor passing
do not use that call, and inside the namespace ``127.0.0.1`` is the only
address there is. A command that ignores the proxy variables has no route
at all.

The grant ends when the sandbox does: the daemon closes its copy of the
command's end once the command has started, so the serve loop sees end of
file when the last process inside exits, and revokes the token. A
background job keeps its grant for as long as it runs.
"""

from __future__ import annotations

import array
import asyncio
import contextlib
import shutil
import socket
import sys
from collections.abc import Awaitable, Callable, Collection
from dataclasses import dataclass

from ..logging import get_logger
from .proxy import EgressDecision, EgressProxy

log = get_logger("tstd.sandbox.egress")

#: Where the bridge listens inside the namespace. The namespace is the
#: command's own, so nothing else can be on it.
BRIDGE_PORT = 3128
PROXY_USER = "tst"

# The bridge, run as the sandbox's first process by a stock Python with no
# site packages (``-I -S``), so it works from the packaged app as well as a
# source checkout. Standard library only. argv: fd, port, then the command.
BRIDGE_SOURCE = r"""
import array, os, socket, sys, threading
fd, port, argv = int(sys.argv[1]), int(sys.argv[2]), sys.argv[3:]
listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
listener.bind(("127.0.0.1", port))
listener.listen(64)
if os.fork() != 0:
    listener.close()
    os.close(fd)
    os.execvp(argv[0], argv)
os.setsid()
null = os.open(os.devnull, os.O_RDWR)
for n in (0, 1, 2):
    os.dup2(null, n)
daemon = socket.socket(fileno=fd)
lock = threading.Lock()

def pump(src, dst):
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        try:
            dst.shutdown(socket.SHUT_WR)
        except OSError:
            pass

def handle(client):
    try:
        with lock:
            daemon.sendall(b"c")
            _, anc, _, _ = daemon.recvmsg(1, socket.CMSG_LEN(4))
        if not anc:
            os._exit(0)
        fds = array.array("i")
        fds.frombytes(anc[0][2][: fds.itemsize])
        upstream = socket.socket(fileno=fds[0])
    except OSError:
        os._exit(0)
    a = threading.Thread(target=pump, args=(client, upstream), daemon=True)
    b = threading.Thread(target=pump, args=(upstream, client), daemon=True)
    a.start(); b.start(); a.join(); b.join()
    client.close(); upstream.close()

while True:
    client, _ = listener.accept()
    threading.Thread(target=handle, args=(client,), daemon=True).start()
"""

OnDecision = Callable[[EgressDecision], Awaitable[None]]


async def _log_only(decision: EgressDecision) -> None:
    log.info(
        "sandbox egress",
        extra={
            "extra_fields": {
                "verdict": decision.verdict,
                "host": decision.host,
                "port": decision.port,
                "reason": decision.reason,
            }
        },
    )


_recorder: OnDecision = _log_only
_proxy: EgressProxy | None = None


def set_recorder(on_decision: OnDecision) -> None:
    """Where every allow and deny goes: the daemon's audit writer."""
    global _recorder
    _recorder = on_decision


def set_proxy(proxy: EgressProxy | None) -> None:
    """Replace the proxy (tests point its resolver and dialer at local servers)."""
    global _proxy
    _proxy = proxy


def proxy() -> EgressProxy:
    global _proxy
    if _proxy is None:

        async def record(decision: EgressDecision) -> None:
            await _recorder(decision)

        _proxy = EgressProxy(record)
    return _proxy


def bridge_python() -> str | None:
    """A Python the bridge can run on, or None.

    The daemon's own interpreter from a source checkout; the packaged app
    is one frozen binary with no interpreter to hand, so a system
    ``python3`` there.
    """
    if not getattr(sys, "frozen", False):
        return sys.executable
    return shutil.which("python3")


@dataclass
class Channel:
    """One sandboxed command's way to the proxy."""

    child_fd: int
    env: dict[str, str]
    bridge: list[str]
    _parent: socket.socket
    _child: socket.socket
    _token: str
    _task: asyncio.Task[None] | None = None

    def started(self) -> None:
        """The command has started: drop the daemon's copy of its end."""
        with contextlib.suppress(OSError):
            self._child.close()

    def close(self) -> None:
        """End the grant now (the command could not start)."""
        self.started()
        proxy().revoke(self._token)
        if self._task is not None:
            self._task.cancel()
        with contextlib.suppress(OSError):
            self._parent.close()


def open_channel(session_id: str, tool_call_id: str, hosts: Collection[str]) -> Channel | None:
    """A grant, a socket pair and the bridge argv for one command, or None.

    None when there is nothing to reach (no hosts listed) or no Python for
    the bridge; the command then runs with no network, as it would with
    ``network: deny``.
    """
    if not hosts:
        return None
    python = bridge_python()
    if python is None:
        log.warning("sandbox network allowlist needs python3 on this computer; network stays off")
        return None
    grant = proxy().grant(session_id, tool_call_id, hosts)
    parent, child = socket.socketpair()
    child.set_inheritable(True)
    url = f"http://{PROXY_USER}:{grant.token}@127.0.0.1:{BRIDGE_PORT}"
    env = {name: url for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")}
    env.update({name.lower(): url for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")})
    env.update({"NO_PROXY": "", "no_proxy": ""})
    bridge = [python, "-I", "-S", "-c", BRIDGE_SOURCE, str(child.fileno()), str(BRIDGE_PORT)]
    channel = Channel(child.fileno(), env, bridge, parent, child, grant.token)
    channel._task = asyncio.get_running_loop().create_task(_serve(parent, grant.token))
    return channel


async def _serve(parent: socket.socket, token: str) -> None:
    """Hand the bridge a fresh connection per request until the sandbox is gone."""
    loop = asyncio.get_running_loop()
    parent.setblocking(False)
    served: set[asyncio.Task[None]] = set()
    try:
        while await loop.sock_recv(parent, 1):
            ours, theirs = socket.socketpair()
            try:
                await _send_fd(parent, theirs.fileno())
            finally:
                theirs.close()
            reader, writer = await asyncio.open_connection(sock=ours)
            task = loop.create_task(proxy().serve(reader, writer))
            served.add(task)
            task.add_done_callback(served.discard)
    except (OSError, asyncio.CancelledError):
        pass
    finally:
        proxy().revoke(token)
        with contextlib.suppress(OSError):
            parent.close()


async def _send_fd(sock: socket.socket, fd: int) -> None:
    rights = [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", [fd]))]
    while True:
        try:
            sock.sendmsg([b"f"], rights)
            return
        except BlockingIOError:
            await asyncio.sleep(0.01)
