package com.thatsimpletech.tstdesk

import android.content.ContentProvider
import android.content.ContentValues
import android.database.Cursor
import android.database.MatrixCursor
import android.net.Uri
import android.os.ParcelFileDescriptor
import android.provider.OpenableColumns
import java.io.FileNotFoundException

/**
 * Serves verified received files, read-only, to the app an intent named
 * (TD-5145). Not exported: another app reads a URI only through the
 * per-intent grant, so nothing here can be listed or guessed into. A
 * platform provider, because androidx's FileProvider is not shipped.
 */
class ReceivedFilesProvider : ContentProvider() {
    override fun onCreate(): Boolean = true

    override fun openFile(uri: Uri, mode: String): ParcelFileDescriptor {
        if (mode != "r") throw SecurityException("received files are read-only")
        val id = fileIdOf(uri) ?: throw FileNotFoundException("not a received file")
        val file = files().resolveVerified(id) ?: throw FileNotFoundException("not verified")
        return ParcelFileDescriptor.open(file, ParcelFileDescriptor.MODE_READ_ONLY)
    }

    /** Name and size, which the installer and viewers show as the title. */
    override fun query(
        uri: Uri,
        projection: Array<out String>?,
        selection: String?,
        selectionArgs: Array<out String>?,
        sortOrder: String?,
    ): Cursor {
        val columns = projection?.filter { it in SHOWN_COLUMNS }?.toTypedArray() ?: SHOWN_COLUMNS
        val cursor = MatrixCursor(columns, 1)
        val id = fileIdOf(uri) ?: return cursor
        val meta = files().meta(id)?.takeIf { files().resolveVerified(id) != null } ?: return cursor
        cursor.addRow(columns.map { if (it == OpenableColumns.DISPLAY_NAME) meta.name else meta.size })
        return cursor
    }

    override fun getType(uri: Uri): String? = fileIdOf(uri)?.let { files().meta(it)?.mime }

    override fun insert(uri: Uri, values: ContentValues?): Uri? =
        throw UnsupportedOperationException("read-only")

    override fun update(uri: Uri, values: ContentValues?, selection: String?, selectionArgs: Array<out String>?): Int =
        throw UnsupportedOperationException("read-only")

    override fun delete(uri: Uri, selection: String?, selectionArgs: Array<out String>?): Int =
        throw UnsupportedOperationException("read-only")

    private fun files(): ReceivedFiles = receivedFilesFor(context ?: throw IllegalStateException("no context"))

    private companion object {
        val SHOWN_COLUMNS = arrayOf(OpenableColumns.DISPLAY_NAME, OpenableColumns.SIZE)

        fun fileIdOf(uri: Uri): String? = receivedFileId(uri.authority, uri.pathSegments)
    }
}

/** The id in `r/<id>/<name>` on this provider's authority, or null for any other shape. */
fun receivedFileId(authority: String?, segments: List<String>?): String? {
    if (authority != FILES_AUTHORITY || segments == null || segments.size != 3) return null
    if (segments[0] != "r") return null
    return segments[1].takeIf(::isFileId)
}
