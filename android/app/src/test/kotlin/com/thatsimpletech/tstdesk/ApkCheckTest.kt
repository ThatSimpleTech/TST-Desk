package com.thatsimpletech.tstdesk

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class ApkCheckTest {
    private val ours = listOf("a".repeat(64))
    private val theirs = listOf("b".repeat(64))
    private val archive = ApkInfo("com.thatsimpletech.tstdesk", 10, "0.1.9", ours)

    private fun installed(code: Long, signers: List<String>?) =
        ApkInfo("com.thatsimpletech.tstdesk", code, "build $code", signers)

    @Test
    fun sameSignerUpgradeClean() {
        val v = verdict(archive, installed(9, ours), isSelf = true)
        assertEquals(true, v.sameSigner)
        assertFalse(v.downgrade)
        assertEquals(9L, v.installedVersionCode)
        assertEquals("build 9", v.installedVersionName)
        val json = v.toStatusJson()
        val want = JSONObject(
            """{"package":"com.thatsimpletech.tstdesk","versionCode":10,"versionName":"0.1.9","isSelf":true,
               "installedVersionCode":9,"installedVersionName":"build 9","sameSigner":true}""",
        )
        assertTrue(json.toString(), json.similar(want))
    }

    @Test
    fun otherSignerConflict() {
        val v = verdict(archive, installed(9, theirs), isSelf = true)
        assertEquals(false, v.sameSigner)
        assertEquals("order does not matter", true, verdict(archive.copy(signers = ours + theirs), installed(9, theirs + ours), true).sameSigner)
    }

    @Test
    fun downgradeFlagged() {
        assertTrue(verdict(archive, installed(11, ours), isSelf = true).downgrade)
        assertFalse("the same version reinstalls", verdict(archive, installed(10, ours), isSelf = true).downgrade)
    }

    @Test
    fun unknownInstalledGivesNulls() {
        val v = verdict(archive.copy(packageName = "com.example.other"), null, isSelf = false)
        assertNull(v.sameSigner)
        assertNull(v.installedVersionCode)
        assertNull(v.installedVersionName)
        assertFalse(v.downgrade)
        val json = v.toStatusJson()
        for (key in listOf("installedVersionCode", "installedVersionName", "sameSigner")) {
            assertTrue("$key is present and null", json.has(key) && json.isNull(key))
        }
        assertEquals(false, json.get("isSelf"))
    }

    @Test
    fun unknownSignerNoWarning() {
        assertNull(verdict(archive.copy(signers = null), installed(9, ours), isSelf = true).sameSigner)
        assertNull(verdict(archive, installed(9, null), isSelf = true).sameSigner)
    }

    @Test
    fun metaRoundTripKeepsEveryField() {
        assertEquals(archive, apkInfoFromMeta(archive.toMetaJson()))
        val bare = archive.copy(versionName = null, signers = null)
        assertEquals(bare, apkInfoFromMeta(bare.toMetaJson()))
        assertNull(apkInfoFromMeta(JSONObject.NULL))
        assertNull(apkInfoFromMeta(JSONObject().put("package", "x")))
    }
}
