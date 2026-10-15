package com.thatsimpletech.tstdesk

import android.webkit.JavascriptInterface
import org.json.JSONObject
import java.util.Base64

const val EVENT_VERIFIED = "verified"
const val EVENT_FAILED = "failed"
const val EVENT_OPENED = "opened"
const val EVENT_OPEN_FAILED = "open_failed"
const val EVENT_SAVED = "saved"
const val EVENT_SAVE_FAILED = "save_failed"

private const val BAD_KEY = """{"ok":false,"code":"bad_key"}"""
private const val OK = """{"ok":true}"""
private const val STARTED = """{"ok":true,"started":true}"""

/** The work behind the bridge that needs Android: hashing, intents, MediaStore, the window. */
interface FileHost {
    /** Async: one `verified` or `failed` event follows. */
    fun finish(id: String)

    /** The `fileStatus` JSON. */
    fun status(id: String): String

    /** Async: one `opened` or `open_failed` event follows. */
    fun open(id: String)

    /** Async: one `saved` or `save_failed` event follows. */
    fun saveToDownloads(id: String)

    fun keepAwake(on: Boolean)
}

/**
 * The `TstDeskFiles` object (TD-5145, design §1.7). Separate from
 * `TstDeskNative` so that bridge stays as it was. Every method that touches
 * a file takes the top-frame key first, because WebView injects this object
 * into iframes too. Calls arrive on WebView's bridge thread.
 */
class FileBridge(
    private val key: FilesKey,
    private val files: ReceivedFiles,
    private val host: FileHost,
    private val onKeyRequest: () -> Unit,
) {
    @JavascriptInterface
    fun filesVersion(): Int = 1

    /** The key arrives on `__tstDeskOnFilesKey`, in the top frame only. */
    @JavascriptInterface
    fun filesKeyRequest(): String {
        onKeyRequest()
        return OK
    }

    @JavascriptInterface
    fun fileBegin(k: String?, metaJson: String?): String {
        if (!key.matches(k)) return BAD_KEY
        return when (val begun = files.begin(metaJson)) {
            is Begin.Ready -> JSONObject().put("ok", true).put("have", begun.have).put("state", begun.state)
            is Begin.Refused -> JSONObject().put("ok", false).put("code", begun.code)
        }.toString()
    }

    @JavascriptInterface
    fun fileAppend(k: String?, id: String?, offset: String?, b64: String?): String {
        if (!key.matches(k)) return BAD_KEY
        val fileId = id.orEmpty()
        val at = offset?.takeIf { it.length in 1..18 && it.all { c -> c in '0'..'9' } }?.toLong()
            ?: return appendJson(Append(false, files.status(fileId).have, "bad_offset"))
        val bytes = try {
            Base64.getDecoder().decode(b64 ?: "")
        } catch (_: IllegalArgumentException) {
            return appendJson(Append(false, files.status(fileId).have, "bad_data"))
        }
        return appendJson(files.append(fileId, at, bytes))
    }

    @JavascriptInterface
    fun fileFinish(k: String?, id: String?): String {
        if (!key.matches(k)) return BAD_KEY
        host.finish(id.orEmpty())
        return STARTED
    }

    @JavascriptInterface
    fun fileStatus(k: String?, id: String?): String {
        if (!key.matches(k)) return BAD_KEY
        return host.status(id.orEmpty())
    }

    @JavascriptInterface
    fun fileOpen(k: String?, id: String?): String {
        if (!key.matches(k)) return BAD_KEY
        host.open(id.orEmpty())
        return STARTED
    }

    @JavascriptInterface
    fun fileSaveToDownloads(k: String?, id: String?): String {
        if (!key.matches(k)) return BAD_KEY
        host.saveToDownloads(id.orEmpty())
        return STARTED
    }

    @JavascriptInterface
    fun fileDiscard(k: String?, id: String?): String {
        if (!key.matches(k)) return BAD_KEY
        files.discard(id.orEmpty())
        return OK
    }

    @JavascriptInterface
    fun fileKeepAwake(k: String?, on: String?): String {
        if (!key.matches(k)) return BAD_KEY
        host.keepAwake(on == "1")
        return OK
    }
}

private fun appendJson(result: Append): String {
    val json = JSONObject().put("ok", result.ok).put("have", result.have)
    if (result.code != null) json.put("code", result.code)
    return json.toString()
}

/** The `fileStatus` reply. `apk` is only known once the file is verified. */
fun statusJson(entry: EntryStatus, canInstall: Boolean, apk: ApkVerdict?): String = JSONObject()
    .put("state", entry.state)
    .put("have", entry.have)
    .put("size", entry.size)
    .put("canInstall", canInstall)
    .put("apk", apk?.toStatusJson() ?: JSONObject.NULL)
    .toString()

/** The object `__tstDeskOnFile` receives: `{event, id, code?, where?}`. */
fun fileEventJson(event: String, id: String, code: String? = null, where: String? = null): String {
    val json = JSONObject().put("event", event).put("id", id)
    if (code != null) json.put("code", code)
    if (where != null) json.put("where", where)
    return json.toString()
}
