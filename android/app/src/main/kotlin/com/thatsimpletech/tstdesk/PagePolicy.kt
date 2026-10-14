package com.thatsimpletech.tstdesk

import java.net.URI

/** The WebView may stay on the loopback shell. Every other URL is refused. */
fun isShellPage(raw: String, port: Int): Boolean {
    if (port !in 1..65535) return false
    val uri = try {
        URI(raw.trim())
    } catch (_: Exception) {
        return false
    }
    if (!uri.scheme.equals("http", ignoreCase = true)) return false
    if (uri.userInfo != null) return false
    if (uri.host != AssetServer.LOOPBACK_HOST) return false
    val uriPort = if (uri.port == -1) 80 else uri.port
    return uriPort == port
}
