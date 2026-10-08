# TST Desk Android shell

Sideload viewer for a TST Desk daemon. It is the same UI, in a WebView. There is no account and no relay.

The phone and the computer both need Tailscale. On the computer, turn on **Allow remote attach** in Settings. Settings shows the `ws://` address. The token is the contents of `remote-token` (`~/.local/share/tst-desk/remote-token` on Linux), not the port-file token. Paste both once: the app saves them, encrypted with an Android Keystore key, and reconnects every time it opens (TD-5005). The address and token stay the same when TST Desk restarts on the computer.

If the computer refuses the saved token (Settings → **New token**, or remote attach turned off and on), the app forgets it and asks again with the address filled in. **Forget this computer** in the phone's Settings clears both.

The shell refuses any host that is not a Tailscale address (`100.64.0.0/10`, `fd7a:115c:a1e0::/48`) or a `*.ts.net` name. It does not listen on the network. The UI is served to the WebView on `127.0.0.1` only.

## Build

Needs JDK 17+, the Android SDK (platform 35, build-tools 35), and the UI's `npm` dependencies.

```sh
echo "sdk.dir=$ANDROID_HOME" > android/local.properties
cd android && ./gradlew :app:testDebugUnitTest :app:assembleRelease
```

The sideload APK is `android/app/build/outputs/apk/release/app-release.apk`. It is signed with the debug keystore so it can be installed without Play Console. It is not a Play upload.

Install with `adb install -r` that file, or copy it to the phone and open it. Unknown-sources install has to be allowed for this app.
