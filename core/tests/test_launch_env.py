"""The daemon's children get the user's environment, not the AppImage's (PKG-02).

In the released AppImage every python3 the agent ran died with "No module
named encodings": the launcher's PYTHONHOME pointed into the image, and git
and curl loaded the image's libraries. The daemon now cleans its own
environment once, before it starts anything.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tstd.launch_env import restore_user_environment

APPDIR = "/tmp/.mount_TSTDesk1234"


def _appimage_env() -> dict[str, str]:
    sep = os.pathsep
    return {
        "APPDIR": APPDIR,
        "APPIMAGE": "/home/me/TST.Desk.AppImage",
        "HOME": "/home/me",
        "PYTHONHOME": f"{APPDIR}/usr/",
        "PYTHONPATH": f"{APPDIR}/usr/share/pyshared/",
        "PATH": sep.join([f"{APPDIR}/usr/bin", "/usr/local/bin", "/usr/bin"]),
        "LD_LIBRARY_PATH": sep.join([f"{APPDIR}/usr/lib/", "/opt/mine/lib"]),
        "XDG_DATA_DIRS": sep.join([f"{APPDIR}/usr/share/", "/usr/share"]),
        "GTK_PATH": f"{APPDIR}/usr/lib/gtk-3.0",
        "GIO_EXTRA_MODULES": f"{APPDIR}/usr/lib/gio/modules",
        "GSETTINGS_SCHEMA_DIR": f"{APPDIR}/usr/share/glib-2.0/schemas",
        "GST_PLUGIN_SYSTEM_PATH_1_0": f"{APPDIR}/usr/lib/gstreamer-1.0",
        "GDK_BACKEND": "x11",
        "GTK_THEME": "Adwaita:light",
        "LANG": "en_US.UTF-8",
    }


def test_the_appimages_entries_go_and_the_users_stay() -> None:
    env = _appimage_env()
    changed = restore_user_environment(env)
    sep = os.pathsep
    assert env["PATH"] == sep.join(["/usr/local/bin", "/usr/bin"])
    assert env["LD_LIBRARY_PATH"] == "/opt/mine/lib"
    assert env["XDG_DATA_DIRS"] == "/usr/share"
    for gone in (
        "PYTHONHOME",
        "PYTHONPATH",
        "GTK_PATH",
        "GIO_EXTRA_MODULES",
        "GSETTINGS_SCHEMA_DIR",
        "GST_PLUGIN_SYSTEM_PATH_1_0",
        "GDK_BACKEND",
        "GTK_THEME",
    ):
        assert gone not in env, gone
    assert env["LANG"] == "en_US.UTF-8" and env["HOME"] == "/home/me"
    assert set(changed) >= {"PATH", "PYTHONHOME", "LD_LIBRARY_PATH", "GDK_BACKEND"}


def test_pyinstallers_saved_library_path_comes_back() -> None:
    env = {"LD_LIBRARY_PATH": "/tmp/_MEI123:/opt/mine/lib", "LD_LIBRARY_PATH_ORIG": "/opt/mine/lib"}
    restore_user_environment(env)
    assert env == {"LD_LIBRARY_PATH": "/opt/mine/lib"}


def test_outside_a_bundle_nothing_changes() -> None:
    env = {
        "PATH": "/usr/bin",
        "PYTHONPATH": "/home/me/lib",
        "GTK_THEME": "Dark",
        "GDK_BACKEND": "wayland",
    }
    before = dict(env)
    assert restore_user_environment(env) == []
    assert env == before


def test_a_users_own_theme_is_kept_outside_an_appimage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "_MEIPASS", "/tmp/_MEIabc", raising=False)
    env = {"GTK_THEME": "Dark", "PATH": "/tmp/_MEIabc:/usr/bin"}
    restore_user_environment(env)
    assert env == {"GTK_THEME": "Dark", "PATH": "/usr/bin"}


@pytest.mark.skipif(shutil.which("python3") is None, reason="needs a system python3")
def test_a_system_python_starts_again(tmp_path: Path) -> None:
    """The report's failure, end to end: PYTHONHOME into a folder with no stdlib."""
    appdir = tmp_path / "squashfs-root"
    (appdir / "usr").mkdir(parents=True)
    env = {**os.environ, "APPDIR": str(appdir), "PYTHONHOME": f"{appdir}/usr/"}
    python3 = shutil.which("python3")
    assert python3 is not None
    broken = subprocess.run([python3, "-c", "pass"], env=env, capture_output=True, check=False)
    assert broken.returncode != 0 and b"encodings" in broken.stderr
    restore_user_environment(env)
    fixed = subprocess.run(
        [python3, "-c", "print(40 + 2)"], env=env, capture_output=True, check=False
    )
    assert fixed.returncode == 0 and fixed.stdout.strip() == b"42"
