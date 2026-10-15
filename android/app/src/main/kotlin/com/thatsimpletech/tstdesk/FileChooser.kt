package com.thatsimpletech.tstdesk

/**
 * What the shell's file chooser asks for and accepts (TD-5026).
 *
 * A file input in the WebView is inert unless the host implements
 * `WebChromeClient.onShowFileChooser` and hands the chosen URIs back through
 * the callback. The composer sends a wildcard accept on purpose (a file refused
 * later names itself rather than being hidden by a filter), so the chooser
 * must show everything the daemon can take, not only images.
 *
 * These are the pure parts — the MIME list, the multi flag, which returned
 * URIs are usable — so they test without a device. Converting a string back
 * to an `android.net.Uri` happens in `MainActivity`, where the framework lives.
 */

/** What the page asked for, cleaned for `EXTRA_MIME_TYPES`. */
fun chooserMimeTypes(acceptTypes: Array<out String>?): Array<String> {
    val cleaned = acceptTypes
        ?.map { it.trim() }
        ?.filter { it.isNotEmpty() && it.contains('/') }
        ?.toTypedArray()
    return if (cleaned.isNullOrEmpty()) arrayOf(ANY_MIME) else cleaned
}

/** A file input may single-select even when the chooser would allow more. */
fun chooserAllowsMultiple(multiple: Boolean): Boolean = multiple

/**
 * Only `content://` and `file://` survive. A picker can hand back a URI from
 * an app that grants no readable stream; the WebView will not fetch other
 * schemes here, so a junk entry is dropped rather than passed on as a null.
 */
fun acceptedChooserUriStrings(strings: List<String?>): List<String> =
    strings.filterNotNull().filter { uri ->
        val scheme = uri.substringBefore(':', "").lowercase()
        scheme == "content" || scheme == "file"
    }

private const val ANY_MIME = "*/*"
