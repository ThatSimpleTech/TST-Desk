package com.thatsimpletech.tstdesk

import org.json.JSONException
import org.json.JSONObject
import java.io.File
import java.io.FileInputStream
import java.io.FileOutputStream
import java.io.IOException
import java.security.MessageDigest
import java.security.SecureRandom
import java.util.Locale
import java.util.concurrent.ConcurrentHashMap

private val FILE_ID = Regex("^[0-9a-f]{32}$")
private val SHA256_HEX = Regex("^[0-9a-f]{64}$")
private val MIME_SHAPE = Regex("^[a-z0-9][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*$")
private val DATA_EXT = Regex("^[a-z0-9]{1,8}$")

/** The largest file the phone takes, and the most the cache holds in all. */
const val MAX_RECEIVED_BYTES = 2L shl 30

/** Kept free on top of what a download still needs, so the phone never fills up. */
const val FREE_MARGIN_BYTES = 64L shl 20

private const val PART_MAX_AGE_MS = 24L * 60 * 60 * 1000
private const val VERIFIED_MAX_AGE_MS = 7 * PART_MAX_AGE_MS
private const val META = "meta.json"
private const val META_TMP = "meta.json.tmp"
private const val PART = "data.part"
private const val HASH_BUFFER = 1 shl 20

const val STATE_NONE = "none"
const val STATE_PARTIAL = "partial"
const val STATE_VERIFIED = "verified"

fun isFileId(raw: String?): Boolean = raw != null && FILE_ID.matches(raw)

/** One received file's `meta.json` (TD-5143 design §1.3). [file] is set once verified. */
data class FileMeta(
    val id: String,
    val sessionId: String,
    val name: String,
    val size: Long,
    val mime: String,
    val sha256: String,
    val createdMs: Long,
    val file: String?,
    val apk: ApkInfo?,
)

sealed interface Begin {
    data class Ready(val have: Long, val state: String) : Begin

    data class Refused(val code: String) : Begin
}

private sealed interface Parsed {
    data class Ok(val meta: FileMeta) : Parsed

    data class Bad(val code: String) : Parsed
}

data class Append(val ok: Boolean, val have: Long, val code: String?)

data class EntryStatus(val state: String, val have: Long, val size: Long, val meta: FileMeta?)

/**
 * Files the agent sent, downloaded over the page's socket into
 * `cacheDir/received/<id>/` (TD-5145). Cache, so Android may reclaim it and
 * nothing outlives an uninstall; the daemon keeps the durable copy.
 */
class ReceivedFiles(
    private val root: File,
    private val freeBytes: () -> Long,
    private val now: () -> Long = System::currentTimeMillis,
) {
    /** Starts or resumes a download from the page's `{id, sessionId, name, size, mime, sha256}`. */
    fun begin(metaJson: String?): Begin {
        val meta = when (val parsed = parseBegin(metaJson)) {
            is Parsed.Bad -> return Begin.Refused(parsed.code)
            is Parsed.Ok -> parsed.meta
        }
        val ready = try {
            synchronized(lockFor(meta.id)) { prepare(meta) }
        } catch (_: IOException) {
            // A write the app's own cache refuses is, in practice, a full disk.
            return Begin.Refused("no_space")
        }
        if (ready.state == STATE_VERIFIED) return ready
        evictFor(meta.id, meta.size)
        if (freeBytes() < meta.size - ready.have + FREE_MARGIN_BYTES) return Begin.Refused("no_space")
        return ready
    }

    /** Appends one chunk, only at the end of what is already there. */
    fun append(id: String, offset: Long, bytes: ByteArray): Append {
        if (!isFileId(id)) return Append(false, 0, "bad_data")
        synchronized(lockFor(id)) {
            val meta = readMeta(id) ?: return Append(false, 0, "io")
            if (verifiedFile(meta) != null) return Append(false, meta.size, "bad_offset")
            val part = File(dirOf(id), PART)
            if (!part.isFile) return Append(false, 0, "io")
            val have = part.length()
            if (offset != have) return Append(false, have, "bad_offset")
            if (have + bytes.size > meta.size) return Append(false, have, "bad_data")
            try {
                FileOutputStream(part, true).use { it.write(bytes) }
            } catch (_: IOException) {
                return Append(false, part.length(), "io")
            }
            return Append(true, part.length(), null)
        }
    }

    /**
     * Checks the length and SHA-256, then names the file `data[.ext]`. Null means verified.
     * An APK's checked bytes go through [readApk] first, so status never shows a
     * verified APK without its info (the page installs straight away when it has none).
     */
    fun finish(id: String, readApk: (File) -> ApkInfo?): String? {
        if (!isFileId(id)) return "io"
        synchronized(lockFor(id)) {
            val meta = readMeta(id) ?: return "io"
            if (verifiedFile(meta) != null) return null
            val dir = dirOf(id)
            val part = File(dir, PART)
            if (!part.isFile) return "io"
            if (part.length() != meta.size) return "size_mismatch"
            return try {
                if (sha256Hex(part) != meta.sha256) {
                    // Truncated, so a retry starts clean instead of resuming bad bytes.
                    FileOutputStream(part).close()
                    "hash_mismatch"
                } else {
                    val name = dataFileName(meta.name)
                    val apk = if (meta.mime == APK_MIME) readApk(part) else null
                    // Meta first: it counts as verified only once data[.ext] exists too,
                    // so a failed write leaves data.part to finish again, never an orphan.
                    writeMeta(meta.copy(file = name, apk = apk))
                    if (!part.renameTo(File(dir, name))) throw IOException("rename failed")
                    null
                }
            } catch (_: IOException) {
                "io"
            }
        }
    }

    /**
     * Unlocked on purpose: hashing a large file holds the lock for seconds,
     * and the page's bridge calls are synchronous, so a lock here would freeze it.
     */
    fun status(id: String): EntryStatus {
        val meta = if (isFileId(id)) readMeta(id) else null
        if (meta == null) return EntryStatus(STATE_NONE, 0, 0, null)
        if (verifiedFile(meta) != null) return EntryStatus(STATE_VERIFIED, meta.size, meta.size, meta)
        val part = File(dirOf(id), PART)
        val have = if (part.isFile) part.length() else 0
        return EntryStatus(if (have > 0) STATE_PARTIAL else STATE_NONE, have, meta.size, meta)
    }

    /** The verified file, or null. Never a partial one: only this is shared with other apps. */
    fun resolveVerified(id: String): File? {
        if (!isFileId(id)) return null
        val meta = readMeta(id) ?: return null
        val file = verifiedFile(meta) ?: return null
        File(dirOf(id), META).setLastModified(now())
        return file
    }

    fun meta(id: String): FileMeta? = if (isFileId(id)) readMeta(id) else null

    fun discard(id: String) {
        if (!isFileId(id)) return
        synchronized(lockFor(id)) { dirOf(id).deleteRecursively() }
    }

    /** Drops downloads idle for a day, verified files unused for a week, and anything unknown. */
    fun sweep(nowMs: Long) {
        for (child in root.listFiles() ?: return) {
            if (!child.isDirectory || !isFileId(child.name)) {
                child.deleteRecursively()
                continue
            }
            synchronized(lockFor(child.name)) {
                val meta = readMeta(child.name)
                val age = nowMs - lastUsed(child)
                val stale = when {
                    meta == null -> true
                    verifiedFile(meta) != null -> age > VERIFIED_MAX_AGE_MS
                    else -> age > PART_MAX_AGE_MS
                }
                if (stale) child.deleteRecursively()
            }
        }
    }

    private fun prepare(meta: FileMeta): Begin.Ready {
        val dir = dirOf(meta.id)
        val old = readMeta(meta.id)
        if (old != null && old.size == meta.size && old.sha256 == meta.sha256) {
            File(dir, META).setLastModified(now())
            if (verifiedFile(old) != null) return Begin.Ready(old.size, STATE_VERIFIED)
            val part = File(dir, PART)
            if (part.isFile && part.length() <= old.size) {
                val have = part.length()
                return Begin.Ready(have, if (have > 0) STATE_PARTIAL else STATE_NONE)
            }
            FileOutputStream(part).close()
            return Begin.Ready(0, STATE_NONE)
        }
        dir.deleteRecursively()
        if (!dir.mkdirs()) throw IOException("could not create the folder")
        writeMeta(meta)
        FileOutputStream(File(dir, PART)).close()
        return Begin.Ready(0, STATE_NONE)
    }

    /** Least recently used first, never [activeId], until the cache fits [MAX_RECEIVED_BYTES]. */
    private fun evictFor(activeId: String, size: Long) {
        val others = (root.listFiles() ?: return)
            .filter { it.isDirectory && isFileId(it.name) && it.name != activeId }
            .map { it to bytesIn(it) }
        var total = others.sumOf { it.second } + size
        for ((dir, bytes) in others.sortedBy { lastUsed(it.first) }) {
            if (total <= MAX_RECEIVED_BYTES) return
            // One lock at a time, never inside the active id's, so two begins cannot deadlock.
            synchronized(lockFor(dir.name)) { dir.deleteRecursively() }
            total -= bytes
        }
    }

    private fun parseBegin(raw: String?): Parsed {
        val bad = Parsed.Bad("bad_meta")
        val json = try {
            JSONObject(raw ?: return bad)
        } catch (_: JSONException) {
            return bad
        }
        val size = when (val n = json.opt("size")) {
            is Int, is Long -> (n as Number).toLong()
            // Android's org.json reads a number past Long as a Double.
            is Number -> return if (n.toDouble() > MAX_RECEIVED_BYTES) Parsed.Bad("too_large") else bad
            else -> return bad
        }
        if (size < 0) return bad
        if (size > MAX_RECEIVED_BYTES) return Parsed.Bad("too_large")
        val id = (json.opt("id") as? String)?.takeIf(::isFileId) ?: return bad
        val sha = (json.opt("sha256") as? String)?.takeIf { SHA256_HEX.matches(it) } ?: return bad
        val sessionId = (json.opt("sessionId") as? String)?.takeIf { it.length in 1..256 } ?: return bad
        val name = (json.opt("name") as? String)?.takeIf { it.length <= 4096 } ?: return bad
        val mime = (json.opt("mime") as? String)?.lowercase(Locale.ROOT)
            ?.takeIf { it.length <= 127 && MIME_SHAPE.matches(it) } ?: return bad
        return Parsed.Ok(FileMeta(id, sessionId, safeFileName(name), size, mime, sha, now(), null, null))
    }

    private fun verifiedFile(meta: FileMeta): File? {
        val name = meta.file ?: return null
        val file = File(dirOf(meta.id), name)
        return if (file.isFile && file.length() == meta.size) file else null
    }

    private fun dirOf(id: String) = File(root, id)

    private fun readMeta(id: String): FileMeta? {
        val text = try {
            File(dirOf(id), META).readText()
        } catch (_: IOException) {
            return null
        }
        return try {
            metaFromJson(JSONObject(text), id)
        } catch (_: JSONException) {
            null
        }
    }

    private fun writeMeta(meta: FileMeta) {
        val dir = dirOf(meta.id)
        val tmp = File(dir, META_TMP)
        tmp.writeText(metaToJson(meta).toString())
        if (!tmp.renameTo(File(dir, META))) throw IOException("could not save meta.json")
    }

    private companion object {
        /** Process-wide, so the activity and a recreated one share them. */
        val locks = ConcurrentHashMap<String, Any>()

        fun lockFor(id: String): Any = locks.computeIfAbsent(id) { Any() }
    }
}

/** `data` plus the name's extension when it is short and plain, so viewers see the type. */
fun dataFileName(name: String): String {
    val dot = name.lastIndexOf('.')
    val ext = if (dot > 0) name.substring(dot + 1).lowercase(Locale.ROOT) else ""
    return if (DATA_EXT.matches(ext)) "data.$ext" else "data"
}

private fun metaToJson(meta: FileMeta): JSONObject = JSONObject()
    .put("id", meta.id)
    .put("sessionId", meta.sessionId)
    .put("name", meta.name)
    .put("size", meta.size)
    .put("mime", meta.mime)
    .put("sha256", meta.sha256)
    .put("createdMs", meta.createdMs)
    .put("file", meta.file ?: JSONObject.NULL)
    .put("apk", meta.apk?.toMetaJson() ?: JSONObject.NULL)

private fun metaFromJson(json: JSONObject, id: String): FileMeta? {
    if (json.opt("id") != id) return null
    val sessionId = json.opt("sessionId") as? String ?: return null
    val name = json.opt("name") as? String ?: return null
    val size = (json.opt("size") as? Number)?.toLong() ?: return null
    val mime = json.opt("mime") as? String ?: return null
    val sha = json.opt("sha256") as? String ?: return null
    val created = (json.opt("createdMs") as? Number)?.toLong() ?: return null
    val file = (json.opt("file") as? String)?.takeIf { it == dataFileName(name) }
    return FileMeta(id, sessionId, name, size, mime, sha, created, file, apkInfoFromMeta(json.opt("apk")))
}

private fun bytesIn(dir: File): Long = dir.listFiles()?.sumOf { it.length() } ?: 0

private fun lastUsed(dir: File): Long =
    dir.listFiles()?.maxOfOrNull { it.lastModified() } ?: dir.lastModified()

private fun sha256Hex(file: File): String {
    val digest = MessageDigest.getInstance("SHA-256")
    FileInputStream(file).use { input ->
        val buffer = ByteArray(HASH_BUFFER)
        while (true) {
            val read = input.read(buffer)
            if (read < 0) break
            digest.update(buffer, 0, read)
        }
    }
    return hex(digest.digest())
}

fun hex(bytes: ByteArray): String =
    bytes.joinToString("") { (it.toInt() and 0xff).toString(16).padStart(2, '0') }

/**
 * The key that keeps `TstDeskFiles` to the top frame (TD-5145). WebView
 * injects bridge objects into every frame, iframes included; only the top
 * frame is handed this key, through `evaluateJavascript` on the main frame.
 */
class FilesKey(private val random: SecureRandom = SecureRandom()) {
    @Volatile private var current: String? = null

    /** A fresh 128-bit key. The old one stops matching at once. */
    fun rotate(): String {
        val bytes = ByteArray(16)
        random.nextBytes(bytes)
        return hex(bytes).also { current = it }
    }

    /** Constant time over the key's length, so timing does not leak a prefix. */
    fun matches(candidate: String?): Boolean {
        val want = current ?: return false
        if (candidate == null || candidate.length != want.length) return false
        var diff = 0
        for (i in want.indices) diff = diff or (want[i].code xor candidate[i].code)
        return diff == 0
    }
}
