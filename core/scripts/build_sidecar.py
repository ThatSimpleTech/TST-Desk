"""Build and smoke-test the bundled tstd sidecar binary (TD-1301).

    uv run python scripts/build_sidecar.py [--if-missing]

Produces ``shell/binaries/tstd-<host triple>`` via PyInstaller onefile —
the Tauri ``externalBin`` convention — then launches it against a scratch
data dir to prove the bundle serves without a system Python, and reports
binary size and cold-start time (acceptance: startup under 3 s).

``--if-missing`` skips the build when this host's sidecar already exists;
tauri.conf.json's beforeDevCommand uses it so `tauri dev` pays the build
once instead of on every start.

Run from anywhere; paths resolve from this file's location.
"""

from __future__ import annotations

import argparse
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


def target_path(triple: str) -> Path:
    """Where this host's sidecar lands; Tauri's externalBin convention."""
    # PyInstaller appends .exe on Windows, and Tauri's externalBin
    # lookup expects the suffix on the triplet file name too.
    suffix = ".exe" if sys.platform == "win32" else ""
    return BINARIES / f"tstd-{triple}{suffix}"


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
            cmd += ["--add-data", f"{src}{os.pathsep}{dest}"]
        cmd.append(str(ENTRY))
        subprocess.run(cmd, check=True, cwd=CORE)
        # PyInstaller appends .exe on Windows, and Tauri's externalBin
        # lookup expects the suffix on the triplet file name too.
        built = Path(work) / f"tstd{'.exe' if sys.platform == 'win32' else ''}"
        BINARIES.mkdir(parents=True, exist_ok=True)
        target = target_path(triple)
        shutil.move(str(built), target)
        target.chmod(0o755)
        return target


def _parent_pid(pid: int) -> int | None:
    """Best-effort ppid. None if the process is gone."""
    if sys.platform == "win32":
        return _win_parent_pid(pid)
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


def _win_parent_pid(pid: int) -> int | None:
    """Same lookup the host uses (`wmic`), with a CIM fallback."""
    proc = subprocess.run(
        ["wmic", "process", f"where processid={pid}", "get", "parentprocessid"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode == 0:
        for line in proc.stdout.splitlines():
            line = line.strip()
            if line.isdigit():
                parsed = int(line)
                return parsed if parsed > 0 else None
    proc = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-Command",
            f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}').ParentProcessId",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    text = proc.stdout.strip()
    if proc.returncode != 0 or not text.isdigit():
        return None
    parsed = int(text)
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
    if proc.pid and sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
            capture_output=True,
            check=False,
        )
    elif proc.pid:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError, AttributeError):
            proc.kill()
    try:
        proc.wait(timeout=10.0)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5.0)


def smoke(binary: Path) -> float:
    """Launch the bundle like the shell does; return seconds to port.json."""
    with tempfile.TemporaryDirectory(prefix="tstd-smoke-", ignore_cleanup_errors=True) as data_dir:
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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--if-missing",
        action="store_true",
        help="exit early when this host's sidecar is already built",
    )
    args = parser.parse_args()
    triple = host_triple()
    if args.if_missing and target_path(triple).is_file():
        print(f"sidecar up to date: {target_path(triple)}")
        return 0
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
