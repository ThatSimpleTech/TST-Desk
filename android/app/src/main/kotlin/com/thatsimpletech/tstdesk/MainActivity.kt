package com.thatsimpletech.tstdesk

import android.annotation.SuppressLint
import android.app.Activity
import android.content.ActivityNotFoundException
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.provider.Settings
import android.view.View
import android.view.WindowInsets
import android.webkit.WebResourceRequest
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.FrameLayout
import org.json.JSONObject
import java.io.IOException
import java.util.concurrent.ExecutorService
import java.util.concurrent.Executors
import java.util.concurrent.atomic.AtomicBoolean

/** Viewer for one TST Desk daemon. The page is the bundled UI. The socket is not. */
class MainActivity : Activity(), PairingHost {
    private var server: AssetServer? = null
    private var webView: WebView? = null
    private var shellPort = -1
    private val pendingLink = PendingPairLink()
    private val scanning = AtomicBoolean(false)
    private val filesKey = FilesKey()
    private var fileWork: ExecutorService? = null

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        // TD-5014: a link the page had not taken yet when a configuration
        // change recreated the activity. Held in memory only, so a Recents
        // launch after the process died never brings one back.
        pendingLink.offer(lastNonConfigurationInstance as? String)
        // A fresh launch from a tstdesk://pair link. A restored or Recents
        // launch re-delivers the old intent, whose code is spent.
        if (savedInstanceState == null) acceptPairIntent(intent)
        val started = AssetServer(ports = PrefsPortMemory(this)) { path -> readAsset(path) }
        started.start()
        server = started
        val view = WebView(this)
        webView = view
        val settings = view.settings
        settings.javaScriptEnabled = true
        settings.domStorageEnabled = true
        settings.allowFileAccess = false
        settings.allowContentAccess = false
        settings.javaScriptCanOpenWindowsAutomatically = false
        settings.setSupportMultipleWindows(false)
        // 127.0.0.1 is a secure context. The daemon is cleartext ws:// on a
        // Tailscale address, which a secure page otherwise refuses. CSP, not
        // this switch, limits scripts and hosts. The attach parser limits
        // the socket to Tailscale.
        settings.mixedContentMode = WebSettings.MIXED_CONTENT_ALWAYS_ALLOW
        settings.mediaPlaybackRequiresUserGesture = true
        // TD-5005: the saved daemon address and token, sealed with a Keystore
        // key. Only the bundled shell page loads here, so only it can call this.
        view.addJavascriptInterface(
            AttachBridge(AttachVault(KeystoreSealer(), PrefsBlobStore(this)), this),
            "TstDeskNative",
        )
        // TD-5145: files the agent sent. Hashing and copying share one worker,
        // which first clears out stale downloads.
        val files = receivedFilesFor(this)
        val work = Executors.newSingleThreadExecutor()
        fileWork = work
        work.execute { files.sweep(System.currentTimeMillis()) }
        val actions = FileActions(this, files, work) { json -> callPage(FILE_CALLBACK, json) {} }
        view.addJavascriptInterface(
            FileBridge(filesKey, files, actions) { runOnUiThread { pushFilesKey() } },
            "TstDeskFiles",
        )
        val port = started.port
        shellPort = port
        view.webViewClient = object : WebViewClient() {
            override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
                return !isShellPage(request.url.toString(), port)
            }
        }
        val ink = getColor(R.color.ink)
        view.setBackgroundColor(ink)
        // WebView draws through its own padding, so the inset has to be a
        // parent. Target 35 lays the window out under the status bar.
        val root = FrameLayout(this)
        root.setBackgroundColor(ink)
        root.addView(
            view,
            FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.MATCH_PARENT,
                FrameLayout.LayoutParams.MATCH_PARENT,
            ),
        )
        applySystemBarInsets(root)
        setContentView(root)
        root.requestApplyInsets()
        // `/` is the app route. `/index.html` is only the file, and the
        // client router turns that pathname into its 404 page.
        view.loadUrl("http://${AssetServer.LOOPBACK_HOST}:$port/")
    }

    /** singleTask: a pairing link opened while the app runs lands here. */
    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        val link = acceptPairIntent(intent) ?: return
        callPage(PAIR_LINK_CALLBACK, JSONObject.quote(link)) { delivered ->
            // Not delivered: the page is still starting and takes it with takePairLink().
            if (delivered) pendingLink.clearIf(link)
        }
    }

    /** Moves an untaken link to the next instance; the old page can no longer take it. */
    override fun onRetainNonConfigurationInstance(): Any? = pendingLink.take()

    override fun onDestroy() {
        webView?.destroy()
        webView = null
        fileWork?.shutdown()
        fileWork = null
        server?.stop()
        server = null
        super.onDestroy()
    }

    override fun deviceName(): String {
        val chosen = try {
            Settings.Global.getString(contentResolver, Settings.Global.DEVICE_NAME)
        } catch (_: Exception) {
            null
        }
        return deviceLabel(chosen, Build.MANUFACTURER, Build.MODEL)
    }

    override fun takePairLink(): String? = pendingLink.take()

    /** Called on the bridge thread. One scan at a time; the result is a page callback. */
    override fun startScan(): Boolean {
        if (isFinishing || isDestroyed) return false
        if (!scanning.compareAndSet(false, true)) return false
        runOnUiThread {
            if (!packageManager.hasSystemFeature(PackageManager.FEATURE_CAMERA_ANY)) {
                finishScan(scanResultJson(null, ScanReason.NO_CAMERA))
                return@runOnUiThread
            }
            try {
                startActivityForResult(Intent(this, ScanActivity::class.java), SCAN_REQUEST)
            } catch (_: ActivityNotFoundException) {
                finishScan(scanResultJson(null, ScanReason.ERROR))
            }
        }
        return true
    }

    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)
        if (requestCode != SCAN_REQUEST) return
        val text = if (resultCode == RESULT_OK) data?.getStringExtra(ScanActivity.EXTRA_TEXT) else null
        finishScan(scanResultJson(text, data?.getStringExtra(ScanActivity.EXTRA_REASON))) { delivered ->
            // Recreated while the scanner was open: the reloading page takes
            // the scanned link through takePairLink() instead.
            pendingLink.keepUndeliveredScan(text, delivered)
        }
    }

    private fun finishScan(resultJson: String, done: (Boolean) -> Unit = {}) {
        scanning.set(false)
        callPage(SCAN_CALLBACK, resultJson, done)
    }

    private fun acceptPairIntent(intent: Intent?): String? {
        if (intent == null || intent.action != Intent.ACTION_VIEW) return null
        if (intent.flags and Intent.FLAG_ACTIVITY_LAUNCHED_FROM_HISTORY != 0) return null
        return pendingLink.offer(intent.dataString)
    }

    /**
     * Calls a fixed page callback with a JSON value, on the UI thread, and
     * only while the WebView shows the shell page (TD-5014).
     */
    private fun callPage(function: String, jsonArgument: String, done: (Boolean) -> Unit) {
        val view = webView
        if (view == null || !isShellPage(view.url ?: "", shellPort)) {
            done(false)
            return
        }
        view.evaluateJavascript(pageCallScript(function, jsonArgument)) { result ->
            done(result == "true")
        }
    }

    /**
     * A fresh TstDeskFiles key, sent by evaluateJavascript on the main frame,
     * so an iframe that asks for one rotates the key but never learns it.
     */
    private fun pushFilesKey() {
        callPage(FILES_KEY_CALLBACK, JSONObject.quote(filesKey.rotate())) {}
    }

    private fun readAsset(path: String): ByteArray? {
        return try {
            assets.open(path).use { it.readBytes() }
        } catch (_: IOException) {
            null
        }
    }

    private companion object {
        const val SCAN_REQUEST = 5014
    }
}

/** Pads [root] by the system bars, so content stays clear of them on target 35. */
internal fun applySystemBarInsets(root: View) {
    root.setOnApplyWindowInsetsListener { view, insets ->
        val pad = systemBarPadding(insets)
        view.setPadding(pad[0], pad[1], pad[2], pad[3])
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
            WindowInsets.CONSUMED
        } else {
            @Suppress("DEPRECATION")
            insets.consumeSystemWindowInsets()
        }
    }
}

/**
 * Status bar, navigation bar, and cutout. The keyboard is not included:
 * the window already resizes for that.
 */
internal fun systemBarPadding(insets: WindowInsets): IntArray {
    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
        val bars = insets.getInsets(
            WindowInsets.Type.systemBars() or WindowInsets.Type.displayCutout(),
        )
        return intArrayOf(bars.left, bars.top, bars.right, bars.bottom)
    }
    @Suppress("DEPRECATION")
    return intArrayOf(
        insets.systemWindowInsetLeft,
        insets.systemWindowInsetTop,
        insets.systemWindowInsetRight,
        insets.systemWindowInsetBottom,
    )
}
