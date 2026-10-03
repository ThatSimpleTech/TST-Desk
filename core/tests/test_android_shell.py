"""TD-5001 — the Android shell stays a loopback viewer.

These read the shell sources. They do not need an Android SDK. The
behavioral tests live in android/app/src/test.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ANDROID = ROOT / "android"
PKG = ANDROID / "app" / "src" / "main" / "kotlin" / "com" / "thatsimpletech" / "tstdesk"


def test_asset_server_binds_loopback_only() -> None:
    source = (PKG / "AssetServer.kt").read_text(encoding="utf-8")
    assert 'const val LOOPBACK_HOST = "127.0.0.1"' in source
    assert "check(bindHost == LOOPBACK_HOST)" in source
    assert "ServerSocket(0, 50, address)" in source


def test_manifest_does_not_back_up_or_phone_home() -> None:
    manifest = (ANDROID / "app" / "src" / "main" / "AndroidManifest.xml").read_text(encoding="utf-8")
    assert 'android:allowBackup="false"' in manifest
    assert "android.webkit.WebView.EnableSafeBrowsing" in manifest
    assert 'android:value="false"' in manifest
    assert "0.0.0.0" not in manifest


def test_webview_allows_the_daemon_socket_from_loopback() -> None:
    """127.0.0.1 is a secure context, so a never-allow mixed-content mode
    would block the daemon's cleartext ws:// before the attach parser runs.
    """
    source = (PKG / "MainActivity.kt").read_text(encoding="utf-8")
    assert "MIXED_CONTENT_ALWAYS_ALLOW" in source
    assert "MIXED_CONTENT_NEVER_ALLOW" not in source


def test_gradle_does_not_pull_play_services() -> None:
    gradle = (ANDROID / "app" / "build.gradle.kts").read_text(encoding="utf-8")
    lowered = gradle.lower()
    assert "play-services" not in lowered
    assert "firebase" not in lowered
    assert "com.google.gms" not in lowered
    assert 'signingConfig = signingConfigs.getByName("debug")' in gradle
    # aapt drops directories named `_…` unless this pattern is replaced.
    # SvelteKit's client bundle is `_app`.
    assert "ignoreAssetsPattern" in gradle
    assert "<dir>_*" not in gradle
