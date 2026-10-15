package com.thatsimpletech.tstdesk

import org.json.JSONObject
import org.junit.After
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
        assertEquals("size_mismatch", files.finish(ID_A))
        files.begin(beginJson(size = body.size, sha = sha256Of("other".toByteArray())))
        files.append(ID_A, 0, body)
        assertEquals("hash_mismatch", files.finish(ID_A))
        assertEquals("bad bytes are not resumed", 0, files.status(ID_A).have)
        assertNull(files.resolveVerified(ID_A))
        assertEquals("io", files.finish(ID_B))
    }

    @Test
    fun finishRenamesToDataWithExt() {
        beginBody(name = "App.APK")
        files.append(ID_A, 0, body)
        assertNull(files.finish(ID_A))
        val dir = File(root, ID_A)
        assertTrue(File(dir, "data.apk").isFile)
        assertFalse(File(dir, "data.part").exists())
        assertEquals("data.apk", files.meta(ID_A)?.file)
        assertEquals(Begin.Ready(4, STATE_VERIFIED), beginBody(name = "App.APK"))
        assertNull("finishing twice is still verified", files.finish(ID_A))
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
                assertNull(files.finish(id))
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
        assertNull(files.finish(ID_A))
        val file = files.resolveVerified(ID_A)
        assertNotNull(file)
        assertEquals(File(root, "$ID_A/data.apk"), file)
        assertNull(files.resolveVerified("../$ID_A"))

        val metaFile = File(root, "$ID_A/meta.json")
        metaFile.writeText(JSONObject(metaFile.readText()).put("file", "../../escape").toString())
        assertNull("meta.json can only name data[.ext]", files.resolveVerified(ID_A))
    }

    @Test
    fun recordApkCachesInfoAndAFailedWriteKeepsTheFile() {
        beginBody()
        files.append(ID_A, 0, body)
        assertNull(files.finish(ID_A))
        val apk = ApkInfo("com.example.app", 3, "1.2", listOf("a".repeat(64)))
        assertTrue(files.recordApk(ID_A, apk))
        assertEquals(apk, files.status(ID_A).meta?.apk)

        val dir = File(root, ID_A)
        assertTrue(dir.setWritable(false))
        try {
            assertFalse("a write error is a result, not a crash", files.recordApk(ID_A, apk.copy(versionCode = 4)))
            assertEquals("still verified", STATE_VERIFIED, files.status(ID_A).state)
            assertEquals(apk, files.status(ID_A).meta?.apk)
        } finally {
            dir.setWritable(true)
        }
        assertFalse(files.recordApk(ID_B, apk))
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
        )
        for ((raw, want) in vectors) {
            assertEquals(raw, want, safeFileName(raw))
            assertEquals("idempotent: $raw", want, safeFileName(want))
        }
        beginBody(name = "C:\\out\\CON.apk")
        assertEquals("begin stores the safe name", "_CON.apk", files.meta(ID_A)?.name)
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
