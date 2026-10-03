package com.thatsimpletech.tstdesk

import android.annotation.SuppressLint
import android.app.Activity
import android.os.Build
import android.os.Bundle
import android.view.View
import android.view.WindowInsets
import android.webkit.WebResourceRequest
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.FrameLayout
import java.io.IOException

/** Viewer for one TST Desk daemon. The page is the bundled UI. The socket is not. */
class MainActivity : Activity() {
    private var server: AssetServer? = null
    private var webView: WebView? = null

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val started = AssetServer { path -> readAsset(path) }
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
        val port = started.port
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

    override fun onDestroy() {
        webView?.destroy()
        webView = null
        server?.stop()
        server = null
        super.onDestroy()
    }

    private fun applySystemBarInsets(root: View) {
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

    private fun readAsset(path: String): ByteArray? {
        return try {
            assets.open(path).use { it.readBytes() }
        } catch (_: IOException) {
            null
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
