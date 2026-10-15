package com.thatsimpletech.tstdesk

import java.util.Locale

private const val NAME_MAX_BYTES = 120
private const val EXT_MAX_CHARS = 16
private const val FORBIDDEN = ":*?\"<>|"
private val DEVICE_NAMES: Set<String> =
    setOf("CON", "PRN", "AUX", "NUL") + (1..9).flatMap { listOf("COM$it", "LPT$it") }

/**
 * The file name rule shared with the daemon and the desktop shell (TD-5143
 * design §1.4). The steps run in the same order in all three, so a name the
 * daemon stored is unchanged here and the same card shows the same name.
 */
fun safeFileName(raw: String): String {
    var name = raw.split('/', '\\').last()
    name = name.filterNot { it.code <= 0x1F || it.code == 0x7F }
    name = name.map { if (it in FORBIDDEN) '_' else it }.joinToString("")
    name = devicePrefixed(name.trimEnd(' ', '.'))
    name = devicePrefixed(truncated(name).trimEnd(' ', '.'))
    return name.ifEmpty { "file" }
}

/** `CON.txt` and friends cannot be created on Windows, so they get a leading `_`. */
private fun devicePrefixed(name: String): String {
    val head = name.substringBefore('.').trimEnd(' ')
    return if (head.uppercase(Locale.ROOT) in DEVICE_NAMES) "_$name" else name
}

/** At most [NAME_MAX_BYTES] of UTF-8, keeping a short extension whole. */
private fun truncated(name: String): String {
    if (utf8Length(name) <= NAME_MAX_BYTES) return name
    val dot = name.lastIndexOf('.')
    val ext = if (dot > 0) name.substring(dot) else ""
    if (ext.isEmpty() || ext.codePointCount(0, ext.length) > EXT_MAX_CHARS) {
        return utf8Prefix(name, NAME_MAX_BYTES)
    }
    return utf8Prefix(name.substring(0, dot), NAME_MAX_BYTES - utf8Length(ext)) + ext
}

private fun utf8Length(text: String): Int {
    var bytes = 0
    var i = 0
    while (i < text.length) {
        val cp = text.codePointAt(i)
        bytes += utf8Width(cp)
        i += Character.charCount(cp)
    }
    return bytes
}

/** The longest prefix of whole code points that fits in [budget] UTF-8 bytes. */
private fun utf8Prefix(text: String, budget: Int): String {
    var used = 0
    var i = 0
    while (i < text.length) {
        val cp = text.codePointAt(i)
        val width = utf8Width(cp)
        if (used + width > budget) break
        used += width
        i += Character.charCount(cp)
    }
    return text.substring(0, i)
}

private fun utf8Width(cp: Int): Int = when {
    cp < 0x80 -> 1
    cp < 0x800 -> 2
    cp < 0x10000 -> 3
    else -> 4
}
