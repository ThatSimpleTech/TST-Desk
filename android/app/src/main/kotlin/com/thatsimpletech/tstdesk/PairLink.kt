package com.thatsimpletech.tstdesk

import org.json.JSONObject
import java.net.URI
import java.net.URISyntaxException
import java.net.URLDecoder
import java.nio.charset.StandardCharsets
import java.util.concurrent.atomic.AtomicReference

/** What the bridge asks of the activity for pairing (TD-5014). */
interface PairingHost {
    fun deviceName(): String

    /** True when exactly one `__tstDeskOnScan` callback will follow. */
    fun startScan(): Boolean

    fun takePairLink(): String?

    companion object {
        /** No activity behind the bridge: nothing to scan, no link. */
        val NONE: PairingHost = object : PairingHost {
            override fun deviceName() = ""
            override fun startScan() = false
            override fun takePairLink(): String? = null
        }
    }
}

const val MAX_PAIR_LINK_CHARS = 2048
const val MAX_DEVICE_NAME_CHARS = 64
const val MAX_SCAN_TEXT_CHARS = 4096

private val PAIR_PARAMS = setOf("h", "p", "c", "n")

/**
 * Shape check for a `tstdesk://pair?h=&p=&c=&n=` link the app was opened
 * with. The UI's parsePairLink is the real validator (Tailscale host, code
 * alphabet); this only keeps anything that is not a pairing link out of the page.
 */
fun pairLinkOrNull(raw: String?): String? {
    val text = raw?.trim() ?: return null
    if (text.isEmpty() || text.length > MAX_PAIR_LINK_CHARS) return null
    if (text.any { it.isWhitespace() || it.isISOControl() }) return null
    val uri = try {
        URI(text)
    } catch (_: URISyntaxException) {
        return null
    }
    if (!uri.scheme.equals("tstdesk", ignoreCase = true)) return null
    // The whole authority, so no user info or port rides along with "pair".
    if (!uri.rawAuthority.equals("pair", ignoreCase = true)) return null
    val path = uri.rawPath ?: ""
    if (path != "" && path != "/") return null
    if (uri.rawFragment != null) return null
    val params = queryParams(uri.rawQuery ?: return null) ?: return null
    if (params["h"].isNullOrEmpty() || params["c"].isNullOrEmpty()) return null
    val port = params["p"]?.takeIf { it.length in 1..5 && it.all { c -> c in '0'..'9' } }?.toInt()
    if (port == null || port !in 1..65535) return null
    return text
}

/** Decoded pairing params, or null for a repeated or undecodable one. */
private fun queryParams(raw: String): Map<String, String>? {
    val out = HashMap<String, String>()
    for (pair in raw.split('&')) {
        if (pair.isEmpty()) continue
        val name = pair.substringBefore('=')
        if (name !in PAIR_PARAMS) continue
        val value = try {
            URLDecoder.decode(pair.substringAfter('=', ""), StandardCharsets.UTF_8)
        } catch (_: IllegalArgumentException) {
            return null
        }
        if (out.put(name, value) != null) return null
    }
    return out
}

/**
 * The link the app was opened with, until the page takes it. A link the
 * page has already received by callback is cleared, so it is not redeemed twice.
 */
class PendingPairLink {
    private val link = AtomicReference<String?>(null)

    /** Keep a valid link, replacing an older one. Returns it, or null if refused. */
    fun offer(raw: String?): String? {
        val checked = pairLinkOrNull(raw) ?: return null
        link.set(checked)
        return checked
    }

    fun take(): String? = link.getAndSet(null)

    fun clearIf(delivered: String) {
        link.compareAndSet(delivered, null)
    }

    /**
     * A scanned pairing link the page did not receive. An activity recreated
     * while the scanner was open is still loading its page, so the link
     * waits here for that page's boot, the same as an opened link.
     */
    fun keepUndeliveredScan(text: String?, delivered: Boolean): String? =
        if (delivered) null else offer(text)
}

/**
 * The name the phone pairs as: the user's own device name when set, else
 * "<Manufacturer> <Model>" (once, when the model already starts with it).
 */
fun deviceLabel(globalName: String?, manufacturer: String?, model: String?): String {
    val chosen = cleanName(globalName)
    if (chosen.isNotEmpty()) return chosen
    val maker = cleanName(manufacturer)
    val kind = cleanName(model)
    val joined = when {
        maker.isEmpty() -> kind
        kind.isEmpty() -> maker
        kind.startsWith(maker, ignoreCase = true) -> kind
        else -> "$maker $kind"
    }
    return clip(joined, MAX_DEVICE_NAME_CHARS)
}

private fun cleanName(raw: String?): String =
    clip(raw.orEmpty().filterNot { it.isISOControl() }.trim(), MAX_DEVICE_NAME_CHARS)

/** Cut to [max] chars without leaving half of a surrogate pair. */
private fun clip(text: String, max: Int): String {
    if (text.length <= max) return text
    val end = if (Character.isHighSurrogate(text[max - 1])) max - 1 else max
    return text.substring(0, end).trim()
}

object ScanReason {
    const val CANCELED = "canceled"
    const val PERMISSION = "permission"
    const val NO_CAMERA = "no_camera"
    const val ERROR = "error"

    val ALL = setOf(CANCELED, PERMISSION, NO_CAMERA, ERROR)
}

/** The object `__tstDeskOnScan` receives, as JSON text. */
fun scanResultJson(text: String?, reason: String?): String {
    if (text != null) {
        if (text.isEmpty() || text.length > MAX_SCAN_TEXT_CHARS) return scanFailureJson(ScanReason.ERROR)
        return JSONObject().put("ok", true).put("text", text).toString()
    }
    return scanFailureJson(if (reason in ScanReason.ALL) reason!! else ScanReason.CANCELED)
}

private fun scanFailureJson(reason: String): String =
    JSONObject().put("ok", false).put("reason", reason).toString()

const val SCAN_CALLBACK = "__tstDeskOnScan"
const val PAIR_LINK_CALLBACK = "__tstDeskOnPairLink"
private val PAGE_CALLBACKS = setOf(SCAN_CALLBACK, PAIR_LINK_CALLBACK)

/**
 * Script that calls one fixed page callback with a JSON literal and says
 * whether the page had it. A throwing handler still counts as delivered;
 * its error is rethrown on its own turn so it shows in the console.
 */
fun pageCallScript(function: String, jsonArgument: String): String {
    require(function in PAGE_CALLBACKS) { "unknown page callback" }
    return "(function(){var f=window.$function;if(typeof f!=='function')return false;" +
        "try{f($jsonArgument);}catch(e){setTimeout(function(){throw e;},0);}return true;})()"
}
