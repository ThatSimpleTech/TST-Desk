# TST Desk Android shell

Sideload viewer for a TST Desk daemon. It is the same UI, in a WebView. There is no account and no relay.

The phone and the computer both need Tailscale.

## Pair a phone (TD-5014)

1. On the computer, open **Settings → Pair a phone**. It turns on remote attach if it is off and shows a QR code and an 8-character code (`ABCD-EFGH`). The code works once and expires after 10 minutes.
2. On the phone, open TST Desk and tap **Scan**, then point the camera at the code. If the phone's own camera app offers to open the `tstdesk://pair` link, that works too: it opens this app and pairs. A pairing link opened from another app asks first before it replaces a computer the phone already has saved.
3. If the camera is not an option, type the computer's address and the code instead.

The phone gets its own key for that computer and saves it, encrypted with an Android Keystore key. It reconnects by itself every time it opens. Each paired phone shows up in the computer's Settings, and removing it there disconnects only that phone. If the computer stops accepting the saved key, the phone keeps the computer's address and asks you to scan again.

**Camera permission.** Android asks the first time you tap Scan. The scanner reads the code on the phone, so nothing is downloaded and nothing leaves it. If you said no, typing the code still works, or allow the camera for TST Desk in Android Settings → Apps.

### The older token way

Still works as a fallback. On the computer, turn on **Allow remote attach** in Settings. Settings shows the `ws://` address. The token is the contents of `remote-token` (`~/.local/share/tst-desk/remote-token` on Linux), not the port-file token. Paste both once and the phone saves them the same way (TD-5005). **New token** in the computer's Settings replaces only that token; paired phones keep their own keys.

**Forget this computer** in the phone's Settings clears what the phone saved.

## What it allows

The shell refuses any host that is not a Tailscale address (`100.64.0.0/10`, `fd7a:115c:a1e0::/48`) or a `*.ts.net` name. It does not listen on the network. The UI is served to the WebView on `127.0.0.1` only, on the same port each launch when it is free.

## Build

Needs JDK 17+, the Android SDK (platform 35, build-tools 35), and the UI's `npm` dependencies.

```sh
echo "sdk.dir=$ANDROID_HOME" > android/local.properties
cd android && ./gradlew :app:testDebugUnitTest :app:assembleRelease
```

The sideload APK is `android/app/build/outputs/apk/release/app-release.apk`. It is signed with the debug keystore so it can be installed without Play Console. It is not a Play upload.

Install with `adb install -r` that file, or copy it to the phone and open it. Unknown-sources install has to be allowed for this app.

## Upgrading

An APK bundles the UI, so it only understands the events its own UI knew. Against a newer daemon (TD-5150 or later), an older APK draws each in-flight trim of old tool results as an amber compaction row with a blank title, up to a dozen per tool-heavy turn. Nothing is wrong with the session. Build and install a new APK to get the "Trim" rows, which show how many results were trimmed and how many tokens that freed.

The same holds for a send the daemon could not save to disk (TD-5158, `message_not_saved`). A window or APK built before TD-5158 shows "Daemon error: message_not_saved" with the daemon's reason, leaves the message in the transcript as if sent and does not put the text back in the composer, so copy it before you send again. A newer APK puts the text back; attachments are never kept, so add them again.

0.1.10 (TD-5145) adds the "install unknown apps" permission and a private file provider, so a file the agent sends can be downloaded, opened, saved to Downloads, or (an APK) installed. It installs over 0.1.9 with the same debug keystore. The first 0.1.10 goes on with `adb install -r`, since 0.1.9 cannot receive files.

0.1.8 (TD-5016..5024, TD-3819..3821) changes no Kotlin: it bundles the UI that shows "Cut off by a restart" with a Continue button, keeps a draft per session, puts unsent text back in the composer when the computer is draining for an update, and adds the Email settings pane. It installs over 0.1.7 the same way.

0.1.7 (TD-5015) changes no Kotlin: it bundles the UI that also lists sessions on the other computers your paired computer has added (Settings → Computers there), and it installs over 0.1.6 the same way.

0.1.6 installs over 0.1.5 when both are built with the same debug keystore (`~/.android/debug.keystore` on the build machine). An over-the-top install keeps the app's data, so the saved computer stays. A build from a different keystore cannot install over it: Android asks to uninstall first, and uninstalling deletes the saved computer, so you pair again.
