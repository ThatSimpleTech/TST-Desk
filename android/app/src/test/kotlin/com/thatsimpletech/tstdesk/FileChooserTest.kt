package com.thatsimpletech.tstdesk

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * TD-5026. The file chooser callback itself needs a device; these cover the
 * pure parts — the MIME list, the multi flag, and which returned URIs are kept.
 */
class FileChooserTest {

    @Test
    fun aBlankAcceptListAsksForEverything() {
        // The composer's `<input type=file multiple>` sends no accept filter on
        // purpose, so the chooser must not hide the files the daemon can take.
        assertTrue(chooserMimeTypes(emptyArray()).contentEquals(arrayOf("*/*")))
        assertTrue(chooserMimeTypes(null).contentEquals(arrayOf("*/*")))
        assertTrue(chooserMimeTypes(arrayOf("", "  ")).contentEquals(arrayOf("*/*")))
    }

    @Test
    fun aRealAcceptListPassesThroughTrimmed() {
        assertEquals(
            listOf("image/*", "text/plain"),
            chooserMimeTypes(arrayOf(" image/* ", "text/plain")).toList(),
        )
    }

    @Test
    fun aNonMimeTokenIsDropped() {
        // A bare extension with no slash is not a MIME type; a list of only those
        // falls back to */* rather than an empty filter that shows nothing.
        assertTrue(chooserMimeTypes(arrayOf(".txt")).contentEquals(arrayOf("*/*")))
        assertEquals(
            listOf("image/png"),
            chooserMimeTypes(arrayOf(".txt", "image/png")).toList(),
        )
    }

    @Test
    fun multiOnlyPassesWhenTheInputIsMultiple() {
        assertTrue(chooserAllowsMultiple(true))
        assertFalse(chooserAllowsMultiple(false))
    }

    @Test
    fun onlyContentAndFileUrisAreKept() {
        val kept = acceptedChooserUriStrings(
            listOf(
                "content://com.example/document/abc",
                "file:///sdcard/note.txt",
                "CONTENT://upper/still-ok",
                "https://evil.example/x",
                "javascript:alert(1)",
                null,
                "",
            ),
        )
        assertEquals(
            listOf(
                "content://com.example/document/abc",
                "file:///sdcard/note.txt",
                "CONTENT://upper/still-ok",
            ),
            kept,
        )
    }

    @Test
    fun aJunkOnlyResultIsEmpty() {
        assertTrue(acceptedChooserUriStrings(listOf("https://evil.example/x", null)).isEmpty())
    }
}
