"""Build and smoke-test the bundled tstd sidecar binary (TD-1301).

    uv run python scripts/build_sidecar.py

Produces ``shell/binaries/tstd-<host triple>`` via PyInstaller onefile —
the Tauri ``externalBin`` convention — then launches it against a scratch
data dir to prove the bundle serves without a system Python, and reports
binary size and cold-start time (acceptance: startup under 3 s).

Run from anywhere; paths resolve from this file's location.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

CORE = Path(__file__).resolve().parent.parent
REPO = CORE.parent
ENTRY = CORE / "scripts" / "tstd_sidecar_entry.py"
BINARIES = REPO / "shell" / "binaries"
STARTUP_BUDGET_S = 3.0

# Data files importlib.resources must find inside the frozen bundle.
DATAS = [(CORE / "tstd" / "config.yaml", "tstd")]


def host_triple() -> str:
    out = subprocess.run(["rustc", "-vV"], check=True, capture_output=True, text=True).stdout
    for line in out.splitlines():
        if line.startswith("host:"):
            return line.split(":", 1)[1].strip()
    raise RuntimeError("could not parse host triple from rustc -vV")


def build(triple: str) -> Path:
    with tempfile.TemporaryDirectory(prefix="tstd-sidecar-") as work:
        cmd = [
            "uv",
            "run",
            "pyinstaller",
            "--onefile",
            "--name",
            "tstd",
            "--distpath",
            work,
            "--workpath",
            str(Path(work) / "build"),
            "--specpath",
            str(Path(work) / "spec"),
            "--paths",
            str(CORE),
            "--clean",
            "--noconfirm",
        ]
        for src, dest in DATAS:
            cmd += ["--add-data", f"{src}:{dest}"]
        cmd.append(str(ENTRY))
        subprocess.run(cmd, check=True, cwd=CORE)
        # PyInstaller appends .exe on Windows, and Tauri's externalBin
        # lookup expects the suffix on the triplet file name too.
        suffix = ".exe" if sys.platform == "win32" else ""
        BINARIES.mkdir(parents=True, exist_ok=True)
        target = BINARIES / f"tstd-{triple}{suffix}"
        shutil.move(str(Path(work) / f"tstd{suffix}"), target)
        target.chmod(0o755)
        return target


def _parent_pid(pid: int) -> int | None:
    """Best-effort ppid via `ps`. None if the process is gone."""
    proc = subprocess.run(
        ["ps", "-o", "ppid=", "-p", str(pid)],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return None
    text = proc.stdout.strip()
    if not text:
        return None
    try:
        parsed = int(text)
    except ValueError:
        return None
    return parsed if parsed > 0 else None


def _is_descendant(ancestor: int, pid: int) -> bool:
    """True when `pid` is `ancestor` or a descendant of it."""
    if ancestor <= 0 or pid <= 0:
        return False
    seen: set[int] = set()
    cur = pid
    while cur not in seen:
        if cur == ancestor:
            return True
        seen.add(cur)
        parent = _parent_pid(cur)
        if parent is None or parent == cur:
            return False
        cur = parent
    return False


def _reap_group(proc: subprocess.Popen[bytes]) -> None:
    """Kill the sidecar's process group so a onefile grandchild cannot linger."""
    if proc.pid:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            proc.kill()
    try:
        proc.wait(timeout=10.0)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5.0)


def smoke(binary: Path) -> float:
    """Launch the bundle like the shell does; return seconds to port.json."""
    with tempfile.TemporaryDirectory(prefix="tstd-smoke-") as data_dir:
        started = time.monotonic()
        proc = subprocess.Popen(
            [str(binary), "--data-dir", data_dir, "--log-level", "INFO"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            port_file = Path(data_dir) / "port.json"
            deadline = started + 30.0
            while not port_file.exists():
                if proc.poll() is not None:
                    raise RuntimeError(f"sidecar exited early with {proc.returncode}")
                if time.monotonic() > deadline:
                    raise TimeoutError("sidecar did not write port.json within 30 s")
                time.sleep(0.05)
            elapsed = time.monotonic() - started
            info = json.loads(port_file.read_text(encoding="utf-8"))
            if not info.get("port"):
                raise RuntimeError(f"port.json missing port: {info!r}")
            written = int(info.get("pid") or 0)
            if written != proc.pid and not _is_descendant(proc.pid, written):
                raise RuntimeError(
                    f"port.json pid {written} is neither the sidecar ({proc.pid}) "
                    "nor a descendant — the host would refuse to attach (TD-1304)"
                )
            return elapsed
        finally:
            _reap_group(proc)


def main() -> int:
    triple = host_triple()
    print(f"building tstd-{triple} ...")
    binary = build(triple)
    size_mb = binary.stat().st_size / (1024 * 1024)
    startup = smoke(binary)
    print(f"binary:  {binary}")
    print(f"size:    {size_mb:.1f} MB")
    print(f"startup: {startup:.2f} s (budget {STARTUP_BUDGET_S:.0f} s)")
    if startup >= STARTUP_BUDGET_S:
        print("startup budget exceeded", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
