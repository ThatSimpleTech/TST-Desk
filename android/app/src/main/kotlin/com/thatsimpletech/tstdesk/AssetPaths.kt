package com.thatsimpletech.tstdesk

import java.net.URLDecoder
import java.nio.charset.StandardCharsets
import java.nio.file.Path

/**
 * Map an HTTP path onto a file inside the bundled UI.
 * A path that escapes the bundle is null. A route with no file extension
 * is the shell document, so client-side routes still open.
 */
fun bundledAssetPath(rawPath: String): String? {
    val decoded = try {
        URLDecoder.decode(rawPath, StandardCharsets.UTF_8)
    } catch (_: IllegalArgumentException) {
        return null
    }
    if (decoded.contains('\\') || decoded.contains('\u0000')) return null
    val trimmed = decoded.trim().trimStart('/')
    if (trimmed.split('/').any { it == ".." || it == "." }) return null
    val relative = when {
        trimmed.isEmpty() -> "index.html"
        trimmed.substringAfterLast('/').contains('.') -> trimmed
        else -> "index.html"
    }
    val root = Path.of("ui")
    val resolved = root.resolve(relative).normalize()
    if (!resolved.startsWith(root)) return null
    return root.relativize(resolved).toString().replace('\\', '/')
}

fun assetMime(path: String): String {
    return when (path.substringAfterLast('.', "").lowercase()) {
        "html" -> "text/html; charset=utf-8"
        "js" -> "text/javascript; charset=utf-8"
        "css" -> "text/css; charset=utf-8"
        "json" -> "application/json"
        "svg" -> "image/svg+xml"
        "png" -> "image/png"
        "woff2" -> "font/woff2"
        "woff" -> "font/woff"
        "ttf" -> "font/ttf"
        "txt" -> "text/plain; charset=utf-8"
        "map" -> "application/json"
        "ico" -> "image/x-icon"
        else -> "application/octet-stream"
    }
}
