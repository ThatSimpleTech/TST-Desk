package com.thatsimpletech.tstdesk

import org.json.JSONObject
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import java.nio.file.Files
import java.util.Base64

private const val ID = "9f1c0e6b2a7d4c11b0e5a3f4d2c1b0a9"

/** Records what the bridge handed to the Android side. */
private class FakeHost : FileHost {
    val calls = mutableListOf<String>()

    override fun finish(id: String) {
        calls += "finish $id"
    }

    override fun status(id: String): String {
        calls += "status $id"
        return """{"state":"none"}"""
    }

    override fun open(id: String) {
        calls += "open $id"
    }

    override fun saveToDownloads(id: String) {
        calls += "save $id"
    }

    override fun keepAwake(on: Boolean) {
        calls += "awake $on"
    }
}

/** The JSON the page's AndroidSink parses, checked against design §1.7. */
class FileBridgeTest {
    private val root = Files.createTempDirectory("bridge").toFile()
    private val files = ReceivedFiles(root, freeBytes = { Long.MAX_VALUE })
    private val key = FilesKey()
    private val host = FakeHost()
    private var keyRequests = 0
    private val bridge = FileBridge(key, files, host) { keyRequests++ }
    private val body = "hello".toByteArray()

    @After
    fun cleanUp() {
        root.deleteRecursively()
    }

    private fun assertJson(want: String, got: String) {
        assertTrue("want $want, got $got", JSONObject(want).similar(JSONObject(got)))
    }

    private fun b64(bytes: ByteArray) = Base64.getEncoder().encodeToString(bytes)

    @Test
    fun everyFileMethodNeedsTheTopFrameKey() {
        val badKey = """{"ok":false,"code":"bad_key"}"""
        for (k in listOf(null, "", "0".repeat(32))) {
            assertJson(badKey, bridge.fileBegin(k, beginJson(id = ID)))
            assertJson(badKey, bridge.fileAppend(k, ID, "0", b64(body)))
            assertJson(badKey, bridge.fileFinish(k, ID))
            assertJson(badKey, bridge.fileStatus(k, ID))
            assertJson(badKey, bridge.fileOpen(k, ID))
            assertJson(badKey, bridge.fileSaveToDownloads(k, ID))
            assertJson(badKey, bridge.fileDiscard(k, ID))
            assertJson(badKey, bridge.fileKeepAwake(k, "1"))
        }
        assertEquals("nothing reached Android", emptyList<String>(), host.calls)
        assertTrue("nothing was written", root.list()!!.isEmpty())
    }

    @Test
    fun theKeyIsOnlyHandedOutThroughTheHost() {
        assertEquals(1, bridge.filesVersion())
        assertJson("""{"ok":true}""", bridge.filesKeyRequest())
        assertEquals(1, keyRequests)
    }

    @Test
    fun beginAppendAndDiscardReplyWithTheContractShapes() {
        val k = key.rotate()
        val meta = beginJson(id = ID, size = body.size, sha = sha256Of(body), name = "notes.txt", mime = "text/plain")
        assertJson("""{"ok":true,"have":0,"state":"none"}""", bridge.fileBegin(k, meta))
        assertJson("""{"ok":false,"code":"bad_meta"}""", bridge.fileBegin(k, "{}"))
        assertJson("""{"ok":true,"have":2}""", bridge.fileAppend(k, ID, "0", b64(body.copyOfRange(0, 2))))
        assertJson("""{"ok":false,"code":"bad_offset","have":2}""", bridge.fileAppend(k, ID, "0", b64(body)))
        for (offset in listOf(null, "", "-2", "+2", "2.0", "0x2")) {
            assertJson("""{"ok":false,"code":"bad_offset","have":2}""", bridge.fileAppend(k, ID, offset, b64(body)))
        }
        assertJson("""{"ok":false,"code":"bad_data","have":2}""", bridge.fileAppend(k, ID, "2", "not base64!"))
        assertJson("""{"ok":true,"have":5}""", bridge.fileAppend(k, ID, "2", b64(body.copyOfRange(2, 5))))
        assertJson("""{"ok":true,"have":5,"state":"partial"}""", bridge.fileBegin(k, meta))
        assertJson("""{"ok":true}""", bridge.fileDiscard(k, ID))
        assertJson("""{"ok":true,"have":0,"state":"none"}""", bridge.fileBegin(k, meta))
    }

    @Test
    fun asyncMethodsStartAtOnceAndDelegate() {
        val k = key.rotate()
        val started = """{"ok":true,"started":true}"""
        assertJson(started, bridge.fileFinish(k, ID))
        assertJson(started, bridge.fileOpen(k, ID))
        assertJson(started, bridge.fileSaveToDownloads(k, ID))
        assertJson("""{"state":"none"}""", bridge.fileStatus(k, ID))
        assertJson("""{"ok":true}""", bridge.fileKeepAwake(k, "1"))
        assertJson("""{"ok":true}""", bridge.fileKeepAwake(k, "0"))
        assertEquals(
            listOf("finish $ID", "open $ID", "save $ID", "status $ID", "awake true", "awake false"),
            host.calls,
        )
    }

    @Test
    fun statusAndEventsUseTheContractShapes() {
        assertJson(
            """{"state":"none","have":0,"size":0,"canInstall":false,"apk":null}""",
            statusJson(files.status(ID), canInstall = false, apk = null),
        )
        val apk = verdict(ApkInfo("com.example.app", 3, "1.2", null), null, isSelf = false)
        assertJson(
            """{"state":"verified","have":5,"size":5,"canInstall":true,"apk":{"package":"com.example.app",
               "versionCode":3,"versionName":"1.2","isSelf":false,"installedVersionCode":null,
               "installedVersionName":null,"sameSigner":null}}""",
            statusJson(EntryStatus(STATE_VERIFIED, 5, 5, null), canInstall = true, apk = apk),
        )
        assertJson("""{"event":"verified","id":"$ID"}""", fileEventJson(EVENT_VERIFIED, ID))
        assertJson("""{"event":"failed","id":"$ID","code":"hash_mismatch"}""", fileEventJson(EVENT_FAILED, ID, "hash_mismatch"))
        assertJson(
            """{"event":"saved","id":"$ID","where":"Download/TST Desk/app.apk"}""",
            fileEventJson(EVENT_SAVED, ID, where = "Download/TST Desk/app.apk"),
        )
    }

    @Test
    fun aProviderUriNamesOnlyAnIdOnItsOwnAuthority() {
        assertEquals(ID, receivedFileId(FILES_AUTHORITY, listOf("r", ID, "app-release.apk")))
        assertEquals(null, receivedFileId("com.example.other", listOf("r", ID, "app-release.apk")))
        assertEquals(null, receivedFileId(FILES_AUTHORITY, listOf("r", "..", "app-release.apk")))
        assertEquals(null, receivedFileId(FILES_AUTHORITY, listOf("r", ID)))
        assertEquals(null, receivedFileId(FILES_AUTHORITY, listOf("x", ID, "a")))
        assertEquals(null, receivedFileId(FILES_AUTHORITY, null))
    }
}
