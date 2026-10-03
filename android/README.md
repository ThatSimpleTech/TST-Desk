# TST Desk Android shell

Sideload viewer for a TST Desk daemon. It is the same UI, in a WebView. There is no account and no relay.

The phone and the computer both need Tailscale. On the computer, turn on **Allow remote attach** in Settings. The app asks for the `ws://` address and the contents of `remote-token` (`~/.local/share/tst-desk/remote-token` on Linux). That file changes every time the daemon starts. The port-file token does not work. Closing the app drops the pasted token; open it again and paste the current file.

The shell refuses any host that is not a Tailscale address (`100.64.0.0/10`, `fd7a:115c:a1e0::/48`) or a `*.ts.net` name. It does not listen on the network. The UI is served to the WebView on `127.0.0.1` only.

## Build

Needs JDK 17+, the Android SDK (platform 35, build-tools 35), and the UI's `npm` dependencies.

```sh
echo "sdk.dir=$ANDROID_HOME" > android/local.properties
cd android && ./gradlew :app:testDebugUnitTest :app:assembleRelease
```

The sideload APK is `android/app/build/outputs/apk/release/app-release.apk`. It is signed with the debug keystore so it can be installed without Play Console. It is not a Play upload.

Install with `adb install -r` that file, or copy it to the phone and open it. Unknown-sources install has to be allowed for this app.
