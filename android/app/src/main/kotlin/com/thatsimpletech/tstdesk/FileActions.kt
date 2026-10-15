package com.thatsimpletech.tstdesk

import android.app.Activity
import android.content.ActivityNotFoundException
import android.content.ContentResolver
import android.content.ContentValues
import android.content.Context
import android.content.Intent
import android.content.pm.PackageInfo
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.StatFs
import android.provider.MediaStore
import android.view.WindowManager
import java.io.File
import java.io.IOException
import java.security.MessageDigest
import java.util.concurrent.ExecutorService

const val FILES_AUTHORITY = "com.thatsimpletech.tstdesk.files"
private const val OCTET_STREAM = "application/octet-stream"
private const val DOWNLOADS_FOLDER = "Download/TST Desk"
private const val COPY_BUFFER = 1 shl 20

/** The one place both the activity and the provider find the received files. */
fun receivedFilesFor(context: Context): ReceivedFiles {
    val cache = context.cacheDir
    return ReceivedFiles(File(cache, "received"), freeBytes = { StatFs(cache.path).availableBytes })
}

/** `content://…files/r/<id>/<name>`. The name is for the viewer's title; the provider ignores it. */
fun receivedFileUri(id: String, name: String): Uri = Uri.Builder()
    .scheme(ContentResolver.SCHEME_CONTENT)
    .authority(FILES_AUTHORITY)
    .appendPath("r")
    .appendPath(id)
    .appendPath(name)
    .build()

/**
 * The Android half of `TstDeskFiles` (TD-5145). Hashing and copying run on
 * [executor], one at a time; intents and window flags run on the UI thread.
 * Every result reaches the page through [callPage] as a `__tstDeskOnFile` event.
 */
class FileActions(
    private val activity: Activity,
    private val files: ReceivedFiles,
    private val executor: ExecutorService,
    private val callPage: (String) -> Unit,
) : FileHost {
    override fun finish(id: String) {
        executor.execute {
            val code = files.finish(id, ::readApkInfo)
            emit(if (code == null) fileEventJson(EVENT_VERIFIED, id) else fileEventJson(EVENT_FAILED, id, code))
        }
    }

    override fun status(id: String): String {
        val entry = files.status(id)
        val archive = entry.meta?.apk?.takeIf { entry.state == STATE_VERIFIED }
        val apk = archive?.let {
            verdict(it, installedInfo(it.packageName), it.packageName == activity.packageName)
        }
        return statusJson(entry, activity.packageManager.canRequestPackageInstalls(), apk)
    }

    override fun open(id: String) {
        activity.runOnUiThread {
            val meta = files.meta(id)
            if (meta == null || files.resolveVerified(id) == null) {
                emit(fileEventJson(EVENT_OPEN_FAILED, id, "not_verified"))
                return@runOnUiThread
            }
            val view = Intent(Intent.ACTION_VIEW)
                .setDataAndType(receivedFileUri(id, meta.name), meta.mime)
                .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
            // An untyped file would open in whatever app claimed `*/*`; let the user pick.
            val intent = if (meta.mime == OCTET_STREAM) Intent.createChooser(view, null) else view
            val event = try {
                activity.startActivity(intent)
                fileEventJson(EVENT_OPENED, id)
            } catch (_: ActivityNotFoundException) {
                fileEventJson(EVENT_OPEN_FAILED, id, "no_app")
            } catch (_: SecurityException) {
                fileEventJson(EVENT_OPEN_FAILED, id, "io")
            }
            emit(event)
        }
    }

    override fun saveToDownloads(id: String) {
        executor.execute { emit(saveCopy(id)) }
    }

    override fun keepAwake(on: Boolean) {
        activity.runOnUiThread {
            val flag = WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON
            if (on) activity.window.addFlags(flag) else activity.window.clearFlags(flag)
        }
    }

    private fun emit(json: String) {
        activity.runOnUiThread { if (!activity.isDestroyed) callPage(json) }
    }

    /**
     * Called by finish once the hash matched, before the file counts as verified.
     * Null only drops the card's APK line: Android's installer checks the package itself.
     * It runs on [executor], where an uncaught exception would end the app.
     */
    private fun readApkInfo(file: File): ApkInfo? = apkInfoOrNull {
        @Suppress("DEPRECATION")
        activity.packageManager.getPackageArchiveInfo(file.path, signerFlags())?.let(::apkInfoOf)
    }

    /** Null when the package is not installed or, on API 30+, not visible to this app. */
    private fun installedInfo(pkg: String): ApkInfo? = try {
        @Suppress("DEPRECATION")
        apkInfoOf(activity.packageManager.getPackageInfo(pkg, signerFlags()))
    } catch (_: PackageManager.NameNotFoundException) {
        null
    }

    /** API 29+ MediaStore, which needs no storage permission and renames on a clash. */
    private fun saveCopy(id: String): String {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.Q) {
            return fileEventJson(EVENT_SAVE_FAILED, id, "needs_android_10")
        }
        val failed = fileEventJson(EVENT_SAVE_FAILED, id, "io")
        val meta = files.meta(id) ?: return failed
        val file = files.resolveVerified(id) ?: return failed
        val resolver = activity.contentResolver
        val values = ContentValues().apply {
            put(MediaStore.MediaColumns.DISPLAY_NAME, meta.name)
            put(MediaStore.MediaColumns.MIME_TYPE, meta.mime)
            put(MediaStore.MediaColumns.RELATIVE_PATH, DOWNLOADS_FOLDER)
            put(MediaStore.MediaColumns.IS_PENDING, 1)
        }
        val uri = storeCall { resolver.insert(MediaStore.Downloads.EXTERNAL_CONTENT_URI, values) } ?: return failed
        val copied = storeCall {
            val out = resolver.openOutputStream(uri) ?: throw IOException("no output stream")
            out.use { sink -> file.inputStream().use { it.copyTo(sink, COPY_BUFFER) } }
            resolver.update(uri, ContentValues().apply { put(MediaStore.MediaColumns.IS_PENDING, 0) }, null, null)
        }
        if (copied == null) {
            storeCall { resolver.delete(uri, null, null) }
            return failed
        }
        val saved = storeCall { displayName(resolver, uri) } ?: meta.name
        return fileEventJson(EVENT_SAVED, id, where = "$DOWNLOADS_FOLDER/$saved")
    }
}

/** MediaStore and the copy report refusals as these; anything else is a bug and should surface. */
private inline fun <T> storeCall(block: () -> T?): T? = try {
    block()
} catch (_: IOException) {
    null
} catch (_: SecurityException) {
    null
} catch (_: IllegalArgumentException) {
    null
} catch (_: IllegalStateException) {
    null
}

private fun displayName(resolver: ContentResolver, uri: Uri): String? =
    resolver.query(uri, arrayOf(MediaStore.MediaColumns.DISPLAY_NAME), null, null, null)?.use {
        if (it.moveToFirst()) it.getString(0) else null
    }

@Suppress("DEPRECATION")
private fun signerFlags(): Int =
    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) PackageManager.GET_SIGNING_CERTIFICATES else PackageManager.GET_SIGNATURES

@Suppress("DEPRECATION")
private fun apkInfoOf(info: PackageInfo): ApkInfo? {
    val pkg = info.packageName ?: return null
    val modern = Build.VERSION.SDK_INT >= Build.VERSION_CODES.P
    val code = if (modern) info.longVersionCode else info.versionCode.toLong()
    val signatures = if (modern) info.signingInfo?.apkContentsSigners else info.signatures
    val signers = signatures
        ?.map { hex(MessageDigest.getInstance("SHA-256").digest(it.toByteArray())) }
        ?.takeIf { it.isNotEmpty() }
    return ApkInfo(pkg, code, info.versionName, signers)
}
