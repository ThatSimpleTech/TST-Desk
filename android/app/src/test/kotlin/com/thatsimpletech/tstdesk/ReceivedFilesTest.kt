package com.thatsimpletech.tstdesk

import org.json.JSONObject
import org.junit.After
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File
import java.io.RandomAccessFile
import java.nio.file.Files
import java.security.MessageDigest

private const val ID_A = "9f1c0e6b2a7d4c11b0e5a3f4d2c1b0a9"
private const val ID_B = "0123456789abcdef0123456789abcdef"
private const val ID_C = "fedcba9876543210fedcba9876543210"
private const val DAY_MS = 24L * 60 * 60 * 1000
private const val GIB = 1L shl 30

private val noApk: (File) -> ApkInfo? = { null }

fun sha256Of(bytes: ByteArray): String = hex(MessageDigest.getInstance("SHA-256").digest(bytes))

fun beginJson(
    id: String = ID_A,
    size: Any = 4,
    sha: String = "0".repeat(64),
    name: String = "app-release.apk",
    mime: String = APK_MIME,
): String = JSONObject()
    .put("id", id).put("sessionId", "s1").put("name", name)
    .put("size", size).put("mime", mime).put("sha256", sha)
    .toString()

class ReceivedFilesTest {
    private val root: File = Files.createTempDirectory("received").toFile()
    private var free = Long.MAX_VALUE
    private var clock = 1_000_000_000_000L
    private val files = ReceivedFiles(root, freeBytes = { free }, now = { clock })
    private val body = "PK\u0003\u0004".toByteArray()

    @After
    fun cleanUp() {
        root.deleteRecursively()
    }

    private fun beginBody(id: String = ID_A, name: String = "app-release.apk"): Begin =
        files.begin(beginJson(id = id, size = body.size, sha = sha256Of(body), name = name))

    @Test
    fun beginStartsAtZero() {
        assertEquals(Begin.Ready(0, STATE_NONE), beginBody())
        val meta = JSONObject(File(root, "$ID_A/meta.json").readText())
        assertEquals("app-release.apk", meta.getString("name"))
        assertEquals(clock, meta.getLong("createdMs"))
        assertTrue("file stays null until verified", meta.isNull("file"))
        assertTrue(meta.isNull("apk"))
        assertEquals(0, File(root, "$ID_A/data.part").length())
    }

    @Test
    fun resumeReturnsPartLength() {
        beginBody()
        assertTrue(files.append(ID_A, 0, body.copyOfRange(0, 3)).ok)
        assertEquals(Begin.Ready(3, STATE_PARTIAL), beginBody())
        assertEquals(EntryStatus(STATE_PARTIAL, 3, 4, files.meta(ID_A)), files.status(ID_A))
    }

    @Test
    fun metaChangeResets() {
        beginBody()
        files.append(ID_A, 0, body.copyOfRange(0, 3))
        val changed = files.begin(beginJson(size = 4, sha = "1".repeat(64)))
        assertEquals(Begin.Ready(0, STATE_NONE), changed)
        assertEquals("1".repeat(64), files.meta(ID_A)?.sha256)
        assertEquals(0, files.status(ID_A).have)
        files.discard(ID_A)
        assertFalse(File(root, ID_A).exists())
        assertEquals(EntryStatus(STATE_NONE, 0, 0, null), files.status(ID_A))
    }

    @Test
    fun appendRefusesWrongOffset() {
        assertEquals("nothing begun", Append(false, 0, "io"), files.append(ID_A, 0, body))
        beginBody()
        assertEquals(Append(false, 0, "bad_offset"), files.append(ID_A, 2, body))
        assertEquals(Append(true, 2, null), files.append(ID_A, 0, body.copyOfRange(0, 2)))
        assertEquals("a resent chunk", Append(false, 2, "bad_offset"), files.append(ID_A, 0, body.copyOfRange(0, 2)))
        assertEquals("past the declared size", Append(false, 2, "bad_data"), files.append(ID_A, 2, body))
        assertEquals(Append(false, 0, "bad_data"), files.append("../$ID_A", 0, body))
    }

    @Test
    fun finishChecksSizeAndHash() {
        beginBody()
        files.append(ID_A, 0, body.copyOfRange(0, 3))
        assertEquals("size_mismatch", files.finish(ID_A, noApk))
        files.begin(beginJson(size = body.size, sha = sha256Of("other".toByteArray())))
        files.append(ID_A, 0, body)
        assertEquals("hash_mismatch", files.finish(ID_A) { error("parsed unchecked bytes") })
        assertEquals("bad bytes are not resumed", 0, files.status(ID_A).have)
        assertNull(files.resolveVerified(ID_A))
        assertEquals("io", files.finish(ID_B, noApk))
    }

    @Test
    fun finishRenamesToDataWithExt() {
        beginBody(name = "App.APK")
        files.append(ID_A, 0, body)
        assertNull(files.finish(ID_A, noApk))
        val dir = File(root, ID_A)
        assertTrue(File(dir, "data.apk").isFile)
        assertFalse(File(dir, "data.part").exists())
        assertEquals("data.apk", files.meta(ID_A)?.file)
        assertEquals(Begin.Ready(4, STATE_VERIFIED), beginBody(name = "App.APK"))
        assertNull("finishing twice is still verified", files.finish(ID_A, noApk))
        assertEquals(Append(false, 4, "bad_offset"), files.append(ID_A, 4, body))

        assertEquals("data.apk", dataFileName("app-release.apk"))
        assertEquals("data.gz", dataFileName("con.tar.gz"))
        assertEquals("data", dataFileName(".env"))
        assertEquals("data", dataFileName("notes"))
        assertEquals("data", dataFileName("x.verylongext"))
        assertEquals("data", dataFileName("x.tar_gz"))
    }

    @Test
    fun badIdAndOversizeRefused() {
        val bad = Begin.Refused("bad_meta")
        assertEquals(bad, files.begin(beginJson(id = ID_A.uppercase())))
        assertEquals(bad, files.begin(beginJson(id = "../" + ID_A.drop(3))))
        assertEquals(bad, files.begin(beginJson(size = -1)))
        assertEquals(bad, files.begin(beginJson(size = 1.5)))
        assertEquals(bad, files.begin(beginJson(size = "4")))
        assertEquals(bad, files.begin(beginJson(sha = "A".repeat(64))))
        assertEquals(bad, files.begin(beginJson(mime = "not a mime")))
        assertEquals(bad, files.begin("not json"))
        assertEquals(bad, files.begin(null))
        assertEquals(bad, files.begin(JSONObject(beginJson()).apply { remove("sessionId") }.toString()))
        assertEquals(Begin.Refused("too_large"), files.begin(beginJson(size = MAX_RECEIVED_BYTES + 1)))
        assertEquals(Begin.Refused("too_large"), files.begin(beginJson(size = 1e30)))
        assertFalse("nothing was written", root.exists() && root.list()!!.isNotEmpty())
        assertEquals(Begin.Ready(0, STATE_NONE), files.begin(beginJson(size = MAX_RECEIVED_BYTES)))
    }

    @Test
    fun noSpaceRefused() {
        val size = 10L shl 20
        free = size + FREE_MARGIN_BYTES - 1
        assertEquals(Begin.Refused("no_space"), files.begin(beginJson(size = size)))
        free = size + FREE_MARGIN_BYTES
        assertEquals(Begin.Ready(0, STATE_NONE), files.begin(beginJson(size = size)))
        files.append(ID_A, 0, ByteArray(1 shl 20))
        free = size - (1 shl 20) + FREE_MARGIN_BYTES
        assertEquals("a resume needs only the rest", Begin.Ready(1L shl 20, STATE_PARTIAL), files.begin(beginJson(size = size)))
    }

    @Test
    fun lruNeverEvictsActive() {
        val entries = listOf(Triple(ID_A, GIB, 3 * DAY_MS), Triple(ID_B, 3 * GIB / 4, 2 * DAY_MS), Triple(ID_C, 3 * GIB / 4, DAY_MS))
        for ((id, size, _) in entries) files.begin(beginJson(id = id, size = size))
        // Sparse parts stand in for real downloads; only lengths are counted.
        for ((id, size, ageMs) in entries) {
            RandomAccessFile(File(root, "$id/data.part"), "rw").use { it.setLength(size) }
            File(root, id).listFiles()!!.forEach { it.setLastModified(clock - ageMs) }
        }
        assertEquals(Begin.Ready(GIB, STATE_PARTIAL), files.begin(beginJson(id = ID_A, size = GIB)))
        assertTrue("the oldest entry is the active one, and stays", File(root, "$ID_A/data.part").isFile)
        assertFalse("the least recent other entry goes", File(root, ID_B).exists())
        assertTrue("one eviction was enough", File(root, ID_C).exists())
    }

    @Test
    fun sweepDropsOldPartsAndOldFiles() {
        fun aged(id: String, verified: Boolean, ageMs: Long) {
            beginBody(id = id)
            if (verified) {
                files.append(id, 0, body)
                assertNull(files.finish(id, noApk))
            }
            File(root, id).listFiles()!!.forEach { it.setLastModified(clock - ageMs) }
        }
        aged(ID_A, verified = false, ageMs = 2 * DAY_MS)
        aged(ID_B, verified = true, ageMs = 2 * DAY_MS)
        aged(ID_C, verified = true, ageMs = 8 * DAY_MS)
        val fresh = "11111111111111111111111111111111"
        aged(fresh, verified = false, ageMs = DAY_MS / 24)
        File(root, "junk").writeText("x")
        File(root, "not-an-id").mkdirs()
        File(root, "22222222222222222222222222222222").mkdirs()

        files.sweep(clock)

        assertEquals(setOf(ID_B, fresh), root.list()!!.toSet())
    }

    @Test
    fun resolveVerifiedOnly() {
        assertNull(files.resolveVerified(ID_A))
        beginBody()
        files.append(ID_A, 0, body)
        assertNull("complete but not checked", files.resolveVerified(ID_A))
        assertNull(files.finish(ID_A, noApk))
        val file = files.resolveVerified(ID_A)
        assertNotNull(file)
        assertEquals(File(root, "$ID_A/data.apk"), file)
        assertNull(files.resolveVerified("../$ID_A"))

        val metaFile = File(root, "$ID_A/meta.json")
        metaFile.writeText(JSONObject(metaFile.readText()).put("file", "../../escape").toString())
        assertNull("meta.json can only name data[.ext]", files.resolveVerified(ID_A))
    }

    @Test
    fun failedMetaWriteKeepsThePartForTheRetry() {
        beginBody()
        files.append(ID_A, 0, body)
        val dir = File(root, ID_A)
        // A folder where meta.json.tmp goes fails the meta write and nothing else.
        val blocker = File(dir, "meta.json.tmp")
        assertTrue(blocker.mkdir())
        assertEquals("io", files.finish(ID_A, noApk))
        assertEquals("the checked bytes stay as data.part", body.size.toLong(), File(dir, "data.part").length())
        assertFalse(File(dir, "data.apk").exists())
        assertEquals(EntryStatus(STATE_PARTIAL, 4, 4, files.meta(ID_A)), files.status(ID_A))

        assertTrue(blocker.delete())
        free = FREE_MARGIN_BYTES
        assertEquals("the retry needs no room for a second copy", Begin.Ready(4, STATE_PARTIAL), beginBody())
        assertNull(files.finish(ID_A, noApk))
        assertEquals(STATE_VERIFIED, files.status(ID_A).state)
    }

    @Test
    fun failedRenameAfterTheMetaIsNotVerified() {
        beginBody()
        files.append(ID_A, 0, body)
        val dir = File(root, ID_A)
        // A non-empty folder named data.apk makes the rename fail after the meta is saved.
        val blocker = File(dir, "data.apk")
        assertTrue(File(blocker, "x").mkdirs())
        assertEquals("io", files.finish(ID_A, noApk))
        assertEquals(STATE_PARTIAL, files.status(ID_A).state)
        assertNull(files.resolveVerified(ID_A))
        assertEquals(Begin.Ready(4, STATE_PARTIAL), beginBody())

        assertTrue(blocker.deleteRecursively())
        assertNull("finishing again re-checks the part", files.finish(ID_A, noApk))
        assertEquals(File(dir, "data.apk"), files.resolveVerified(ID_A))
    }

    @Test
    fun apkInfoIsInTheMetaBeforeItShowsVerified() {
        beginBody()
        files.append(ID_A, 0, body)
        val apk = ApkInfo("com.example.app", 3, "1.2", listOf("a".repeat(64)))
        var stateDuringParse: String? = null
        var parsedBytes: ByteArray? = null
        val code = files.finish(ID_A) { file ->
            stateDuringParse = files.status(ID_A).state
            parsedBytes = file.readBytes()
            apk
        }
        assertNull(code)
        assertEquals("the page's poll cannot see verified yet", STATE_PARTIAL, stateDuringParse)
        assertArrayEquals("it parses the checked bytes", body, parsedBytes)
        val after = files.status(ID_A)
        assertEquals(STATE_VERIFIED, after.state)
        assertEquals(apk, after.meta?.apk)
        assertNull("finishing twice does not parse again", files.finish(ID_A) { error("parsed twice") })
        assertEquals(apk, files.meta(ID_A)?.apk)
    }

    @Test
    fun onlyApksAreParsedAndAnUnreadableOneStillVerifies() {
        files.begin(beginJson(size = body.size, sha = sha256Of(body), name = "notes.pdf", mime = "application/pdf"))
        files.append(ID_A, 0, body)
        assertNull(files.finish(ID_A) { error("not an APK") })
        assertNull(files.status(ID_A).meta?.apk)

        beginBody(id = ID_B)
        files.append(ID_B, 0, body)
        assertNull("Android's installer reports a bad archive itself", files.finish(ID_B) { null })
        assertEquals(STATE_VERIFIED, files.status(ID_B).state)
        assertNull(files.status(ID_B).meta?.apk)
    }

    @Test
    fun safeNameVectors() {
        val vectors = listOf(
            "app-release.apk" to "app-release.apk",
            "android/app/build/outputs/apk/release/app-release.apk" to "app-release.apk",
            "C:\\out\\App.apk" to "App.apk",
            "a:b*c?d\"e<f>g|h.txt" to "a_b_c_d_e_f_g_h.txt",
            "report.pdf. . " to "report.pdf",
            "CON" to "_CON",
            "con.tar.gz" to "_con.tar.gz",
            "COM1 .log" to "_COM1 .log",
            "CONSOLE.txt" to "CONSOLE.txt",
            ".." to "file",
            "" to "file",
            "\u0007bell\u007f.txt" to "bell.txt",
            ".env" to ".env",
            "x".repeat(200) + ".apk" to "x".repeat(116) + ".apk",
            "é".repeat(100) to "é".repeat(60),
            "a".repeat(130) + ".verylongextension12" to "a".repeat(120),
            "name with spaces.zip" to "name with spaces.zip",
            "a‮fdp.exe" to "afdp.exe",
            "CON" + " ".repeat(150) + "y.log" to "_CON" + " ".repeat(112) + ".log",
        )
        for ((raw, want) in vectors) {
            assertEquals(raw, want, safeFileName(raw))
            assertEquals("idempotent: $raw", want, safeFileName(want))
        }
        beginBody(name = "C:\\out\\CON.apk")
        assertEquals("begin stores the safe name", "_CON.apk", files.meta(ID_A)?.name)
    }

    @Test
    fun safeNameDropsExactlyTheBidiControls() {
        val dropped = listOf(0x200E, 0x200F) + (0x202A..0x202E) + (0x2066..0x2069)
        for (cp in dropped) {
            assertEquals("U+%04X".format(cp), "ab.txt", safeFileName("a${cp.toChar()}b.txt"))
        }
        // The neighbours stay, so the three implementations drop the same set.
        for (cp in listOf(0x200D, 0x2029, 0x202F, 0x2065, 0x206A)) {
            val raw = "a${cp.toChar()}b.txt"
            assertEquals("U+%04X".format(cp), raw, safeFileName(raw))
        }
        beginBody(name = "invoice\u202Efdp.exe")
        assertEquals("begin stores the safe name", "invoicefdp.exe", files.meta(ID_A)?.name)
    }

    @Test
    fun filesKeyRotatesAndMatchesConstantTime() {
        val key = FilesKey()
        assertFalse("no key before the first request", key.matches(""))
        assertFalse(key.matches(null))
        val first = key.rotate()
        assertTrue(Regex("^[0-9a-f]{32}$").matches(first))
        assertTrue(key.matches(first))
        assertFalse(key.matches(first.dropLast(1)))
        assertFalse(key.matches(first + "0"))
        assertFalse(key.matches(first.dropLast(1) + if (first.last() == '0') '1' else '0'))
        val second = key.rotate()
        assertNotEquals(first, second)
        assertFalse("the old key stops working", key.matches(first))
        assertTrue(key.matches(second))
    }
}
