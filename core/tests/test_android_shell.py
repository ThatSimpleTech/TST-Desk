"""TD-5001 — the Android shell stays a loopback viewer.

These read the shell sources. They do not need an Android SDK. The
behavioral tests live in android/app/src/test.
"""

from __future__ import annotations

import re
from pathlib import Path
from xml.etree import ElementTree

ROOT = Path(__file__).resolve().parents[2]
ANDROID = ROOT / "android"
PKG = ANDROID / "app" / "src" / "main" / "kotlin" / "com" / "thatsimpletech" / "tstdesk"


def test_asset_server_binds_loopback_only() -> None:
    source = (PKG / "AssetServer.kt").read_text(encoding="utf-8")
    assert 'const val LOOPBACK_HOST = "127.0.0.1"' in source
    assert "check(bindHost == LOOPBACK_HOST)" in source
    # TD-5014: the remembered port (or a random one) on that same address.
    assert "val address = InetAddress.getByName(LOOPBACK_HOST)" in source
    assert "bindLoopback(address, remembered)" in source
    assert "server.bind(InetSocketAddress(address, port), 50)" in source
    assert source.count("InetSocketAddress(") == 1
    assert "ServerSocket(0" not in source and "ServerSocket(port" not in source


def test_manifest_does_not_back_up_or_phone_home() -> None:
    manifest_path = ANDROID / "app" / "src" / "main" / "AndroidManifest.xml"
    manifest = manifest_path.read_text(encoding="utf-8")
    assert 'android:allowBackup="false"' in manifest
    assert "android.webkit.WebView.EnableSafeBrowsing" in manifest
    assert 'android:value="false"' in manifest
    assert "0.0.0.0" not in manifest


def test_webview_clears_the_status_bar() -> None:
    """Target 35 draws edge-to-edge. The page has to sit below the status bar.

    WebView paints through its own padding, so the inset belongs on a parent.
    """
    source = (PKG / "MainActivity.kt").read_text(encoding="utf-8")
    assert "setOnApplyWindowInsetsListener" in source
    assert "WindowInsets.Type.systemBars()" in source
    assert "FrameLayout" in source


def test_webview_opens_the_app_root() -> None:
    """`/index.html` is not a client route. That pathname is the 404 page."""
    source = (PKG / "MainActivity.kt").read_text(encoding="utf-8")
    assert 'loadUrl("http://${AssetServer.LOOPBACK_HOST}:$port/")' in source


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


# TD-5145: files the agent sent, downloaded, opened and installed on the phone.

_FILE_BRIDGE_METHODS = {
    "filesVersion",
    "filesKeyRequest",
    "fileBegin",
    "fileAppend",
    "fileFinish",
    "fileStatus",
    "fileOpen",
    "fileSaveToDownloads",
    "fileDiscard",
    "fileKeepAwake",
}
_ANDROID_NS = "{http://schemas.android.com/apk/res/android}"


def test_manifest_install_permission_and_private_readonly_provider() -> None:
    """The installer permission, one private provider, and nothing that widens storage."""
    manifest_path = ANDROID / "app" / "src" / "main" / "AndroidManifest.xml"
    tree = ElementTree.parse(manifest_path)
    permissions = {p.get(f"{_ANDROID_NS}name") for p in tree.iter("uses-permission")}
    assert "android.permission.REQUEST_INSTALL_PACKAGES" in permissions
    assert not [p for p in permissions if p and p.endswith("_EXTERNAL_STORAGE")]
    assert list(tree.iter("receiver")) == []
    providers = list(tree.iter("provider"))
    assert len(providers) == 1
    provider = providers[0]
    assert provider.get(f"{_ANDROID_NS}name") == ".ReceivedFilesProvider"
    assert provider.get(f"{_ANDROID_NS}authorities") == "com.thatsimpletech.tstdesk.files"
    assert provider.get(f"{_ANDROID_NS}exported") == "false"
    assert provider.get(f"{_ANDROID_NS}grantUriPermissions") == "true"


def test_page_callbacks_include_file_and_key_callbacks() -> None:
    source = (PKG / "PairLink.kt").read_text(encoding="utf-8")
    assert 'const val FILE_CALLBACK = "__tstDeskOnFile"' in source
    assert 'const val FILES_KEY_CALLBACK = "__tstDeskOnFilesKey"' in source
    callbacks = re.search(r"PAGE_CALLBACKS = setOf\(([^)]*)\)", source)
    assert callbacks is not None
    names = {name.strip() for name in callbacks.group(1).split(",")}
    assert names == {"SCAN_CALLBACK", "PAIR_LINK_CALLBACK", "FILE_CALLBACK", "FILES_KEY_CALLBACK"}


def test_version_0_2_5_code_16() -> None:
    gradle = (ANDROID / "app" / "build.gradle.kts").read_text(encoding="utf-8")
    assert "versionCode = 20\n" in gradle
    assert 'versionName = "0.4.2"' in gradle


def test_provider_opens_read_only() -> None:
    """Other apps get a read grant per intent; the provider never hands out a writable fd."""
    source = (PKG / "ReceivedFilesProvider.kt").read_text(encoding="utf-8")
    open_file = source.split("override fun openFile(", 1)[1].split("override fun ", 1)[0]
    assert 'if (mode != "r") throw SecurityException(' in open_file
    assert "ParcelFileDescriptor.MODE_READ_ONLY" in open_file
    assert "resolveVerified(id)" in open_file
    assert "MODE_WRITE" not in source and "MODE_READ_WRITE" not in source
    for verb in ("insert", "update", "delete"):
        body = source.split(f"override fun {verb}(", 1)[1].split("override fun ", 1)[0]
        assert "throw UnsupportedOperationException(" in body


def test_files_bridge_checks_key() -> None:
    """WebView injects TstDeskFiles into iframes too.

    So every file method checks the top-frame key before anything else.
    """
    source = (PKG / "FileBridge.kt").read_text(encoding="utf-8")
    chunks = source.split("@JavascriptInterface")[1:]
    methods: dict[str, str] = {}
    for chunk in chunks:
        found = re.match(r"\s*fun (\w+)\(", chunk)
        assert found is not None
        methods[found.group(1)] = chunk
    assert set(methods) == _FILE_BRIDGE_METHODS
    for name, body in methods.items():
        if name in {"filesVersion", "filesKeyRequest"}:
            assert "key.matches" not in body
            continue
        first_statement = body.split("{", 1)[1].strip().splitlines()[0]
        assert first_statement == "if (!key.matches(k)) return BAD_KEY", name


# U+200E, U+200F, U+202A-U+202E, U+2066-U+2069, built from code points so
# this file holds none of them either.
_BIDI_CONTROLS = frozenset(
    chr(cp) for cp in (0x200E, 0x200F, *range(0x202A, 0x202F), *range(0x2066, 0x206A))
)
_BINARY_SUFFIXES = frozenset({".png", ".webp", ".jpg", ".jpeg", ".gif", ".jar", ".so"})


def test_android_sources_hold_no_raw_bidi_controls() -> None:
    """TD-5145: a raw bidi control reorders how source reads (Trojan Source).

    Code and tests name them as escapes ("\\u202E") instead.
    """
    src = ANDROID / "app" / "src"
    found = []
    for path in sorted(src.rglob("*")):
        if not path.is_file() or path.suffix.lower() in _BINARY_SUFFIXES:
            continue
        text = path.read_bytes().decode("utf-8", errors="replace")
        for number, line in enumerate(text.splitlines(), start=1):
            hits = sorted({f"U+{ord(c):04X}" for c in line if c in _BIDI_CONTROLS})
            if hits:
                found.append(f"{path.relative_to(ROOT)}:{number} {' '.join(hits)}")
    assert len(_BIDI_CONTROLS) == 11
    assert found == []
