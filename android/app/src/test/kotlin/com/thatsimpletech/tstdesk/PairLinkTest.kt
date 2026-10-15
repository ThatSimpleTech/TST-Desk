package com.thatsimpletech.tstdesk

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Test

class PairLinkTest {
    private val good = "tstdesk://pair?h=100.64.7.7&p=39079&c=ABCD2345&n=ezer"

    @Test
    fun aPairingLinkIsKeptAsGiven() {
        assertEquals(good, pairLinkOrNull(good))
        assertEquals(good, pairLinkOrNull("  $good\n"))
        val spaced = "tstdesk://pair?h=desk.tailnet.ts.net&p=39079&c=abcd-2345&n=Adam%27s%20desk"
        assertEquals(spaced, pairLinkOrNull(spaced))
        assertEquals(
            "a trailing slash and any param order",
            "tstdesk://pair/?n=ezer&c=ABCD2345&p=1&h=fd7a:115c:a1e0::1",
            pairLinkOrNull("tstdesk://pair/?n=ezer&c=ABCD2345&p=1&h=fd7a:115c:a1e0::1"),
        )
        assertEquals("the name is optional", good.substringBefore("&n="), pairLinkOrNull(good.substringBefore("&n=")))
    }

    @Test
    fun theWrongSchemeOrHostIsRefused() {
        assertNull(pairLinkOrNull(good.replace("tstdesk://", "https://")))
        assertNull(pairLinkOrNull(good.replace("tstdesk://", "tstdesk2://")))
        assertNull(pairLinkOrNull(good.replace("//pair", "//attach")))
        assertNull(pairLinkOrNull(good.replace("//pair", "//evil@pair")))
        assertNull(pairLinkOrNull(good.replace("//pair", "//pair:80")))
        assertNull(pairLinkOrNull(good.replace("//pair?", "//pair/extra?")))
        assertNull(pairLinkOrNull("tstdesk:pair?h=100.64.7.7&p=39079&c=ABCD2345"))
        assertNull(pairLinkOrNull("$good#frag"))
        assertNull(pairLinkOrNull("javascript:alert(1)"))
    }

    @Test
    fun missingOrBadParamsAreRefused() {
        assertNull(pairLinkOrNull(null))
        assertNull(pairLinkOrNull(""))
        assertNull(pairLinkOrNull("tstdesk://pair"))
        assertNull(pairLinkOrNull("tstdesk://pair?p=39079&c=ABCD2345"))
        assertNull(pairLinkOrNull("tstdesk://pair?h=100.64.7.7&c=ABCD2345"))
        assertNull(pairLinkOrNull("tstdesk://pair?h=100.64.7.7&p=39079"))
        assertNull(pairLinkOrNull("tstdesk://pair?h=&p=39079&c=ABCD2345"))
        assertNull(pairLinkOrNull("tstdesk://pair?h=100.64.7.7&p=0&c=ABCD2345"))
        assertNull(pairLinkOrNull("tstdesk://pair?h=100.64.7.7&p=65536&c=ABCD2345"))
        assertNull(pairLinkOrNull("tstdesk://pair?h=100.64.7.7&p=-1&c=ABCD2345"))
        assertNull(pairLinkOrNull("tstdesk://pair?h=100.64.7.7&p=39079&c=ABCD2345&h=10.0.0.1"))
        assertNull(pairLinkOrNull("tstdesk://pair?h=100.64.7.7&p=39079&c=%zz"))
        assertNull(pairLinkOrNull("tstdesk://pair?h=100.64.7.7&p=39079&c=AB CD"))
        assertNull(pairLinkOrNull(good + "&x=" + "a".repeat(MAX_PAIR_LINK_CHARS)))
    }

    @Test
    fun theTailscaleRuleIsTheUisJob() {
        // One parser decides the host (parsePairLink in the UI); the shell
        // only refuses what is not a pairing link at all.
        assertEquals(
            "tstdesk://pair?h=10.0.0.5&p=39079&c=ABCD2345",
            pairLinkOrNull("tstdesk://pair?h=10.0.0.5&p=39079&c=ABCD2345"),
        )
    }

    @Test
    fun aPendingLinkIsTakenOnce() {
        val pending = PendingPairLink()
        assertNull(pending.offer("https://example.com/"))
        assertNull(pending.take())
        val kept = pending.offer(good)
        assertEquals(good, kept)
        assertEquals(good, pending.take())
        assertNull(pending.take())
    }

    @Test
    fun aDeliveredLinkIsClearedButANewerOneStays() {
        val pending = PendingPairLink()
        val first = pending.offer(good)!!
        pending.clearIf(first)
        assertNull(pending.take())
        val older = pending.offer(good)!!
        val newer = pending.offer(good.replace("ABCD2345", "WXYZ6789"))!!
        pending.clearIf(older)
        assertEquals(newer, pending.take())
    }

    @Test
    fun aScanThePageMissedWaitsForItsBoot() {
        val pending = PendingPairLink()
        assertEquals(good, pending.keepUndeliveredScan(good, delivered = false))
        assertEquals("the reloaded page takes it once", good, pending.take())
        assertNull(pending.take())
    }

    @Test
    fun aDeliveredOrNonLinkScanIsNotKept() {
        val pending = PendingPairLink()
        assertNull(pending.keepUndeliveredScan(good, delivered = true))
        assertNull("the page already has it", pending.take())
        assertNull(pending.keepUndeliveredScan("ABCD-EFGH", delivered = false))
        assertNull(pending.keepUndeliveredScan("https://example.com/", delivered = false))
        assertNull("a failed scan has no text", pending.keepUndeliveredScan(null, delivered = false))
        assertNull(pending.take())
    }

    @Test
    fun anUntakenLinkMovesToARecreatedActivityAndATakenOneDoesNot() {
        // What onRetainNonConfigurationInstance / onCreate do across a recreate.
        val before = PendingPairLink()
        before.offer(good)
        val after = PendingPairLink()
        after.offer(before.take())
        assertNull("the old page can no longer take it", before.take())
        assertEquals(good, after.take())

        val spent = PendingPairLink()
        spent.offer(good)
        assertEquals(good, spent.take())
        val next = PendingPairLink()
        next.offer(spent.take())
        assertNull("a spent link is not handed on", next.take())
    }

    @Test
    fun theDeviceNameIsTheUsersOwnFirst() {
        assertEquals("Adam's Fold", deviceLabel("Adam's Fold", "Google", "Pixel 11 Pro Fold"))
        assertEquals("Google Pixel 11 Pro Fold", deviceLabel(null, "Google", "Pixel 11 Pro Fold"))
        assertEquals("Google Pixel 11 Pro Fold", deviceLabel("  ", "Google", "Pixel 11 Pro Fold"))
        assertEquals("OnePlus 12", deviceLabel(null, "OnePlus", "OnePlus 12"))
        assertEquals("samsung SM-F956B", deviceLabel(null, "samsung", "SM-F956B"))
        assertEquals("Pixel", deviceLabel(null, null, "Pixel"))
        assertEquals("", deviceLabel(null, null, null))
        assertEquals("Fold", deviceLabel("Fo\u0000ld\n", "Google", "Pixel"))
    }

    @Test
    fun theDeviceNameIsCappedWithoutSplittingACharacter() {
        assertEquals(MAX_DEVICE_NAME_CHARS, deviceLabel("x".repeat(100), null, null).length)
        val emoji = "x".repeat(MAX_DEVICE_NAME_CHARS - 1) + "📱"
        val label = deviceLabel(emoji, null, null)
        assertEquals("x".repeat(MAX_DEVICE_NAME_CHARS - 1), label)
        assertTrue(deviceLabel(null, "M".repeat(40), "N".repeat(40)).length <= MAX_DEVICE_NAME_CHARS)
    }

    @Test
    fun aScanResultIsJson() {
        val ok = JSONObject(scanResultJson("tstdesk://pair?h=1\"</script>", null))
        assertTrue(ok.getBoolean("ok"))
        assertEquals("tstdesk://pair?h=1\"</script>", ok.getString("text"))
        for (reason in ScanReason.ALL) {
            val failed = JSONObject(scanResultJson(null, reason))
            assertFalse(failed.getBoolean("ok"))
            assertEquals(reason, failed.getString("reason"))
        }
        assertEquals("canceled", JSONObject(scanResultJson(null, null)).getString("reason"))
        assertEquals("canceled", JSONObject(scanResultJson(null, "made-up")).getString("reason"))
        assertEquals("error", JSONObject(scanResultJson("", null)).getString("reason"))
        assertEquals(
            "error",
            JSONObject(scanResultJson("x".repeat(MAX_SCAN_TEXT_CHARS + 1), null)).getString("reason"),
        )
    }

    @Test
    fun aPageCallOnlyNamesAKnownCallback() {
        val script = pageCallScript(PAIR_LINK_CALLBACK, JSONObject.quote("tstdesk://pair?c='); alert(1); ('"))
        assertTrue(script.contains("window.__tstDeskOnPairLink"))
        assertTrue("the link stays inside one JSON string", script.contains("\"tstdesk://pair?c='); alert(1); ('\""))
        assertTrue(pageCallScript(SCAN_CALLBACK, "{}").contains("window.__tstDeskOnScan"))
    }

    @Test(expected = IllegalArgumentException::class)
    fun anUnknownCallbackIsRefused() {
        pageCallScript("alert", "1")
    }

    @Test
    fun pageCallScriptAcceptsFileAndKeyCallbacks() {
        assertEquals("__tstDeskOnFile", FILE_CALLBACK)
        assertEquals("__tstDeskOnFilesKey", FILES_KEY_CALLBACK)
        val event = pageCallScript(FILE_CALLBACK, fileEventJson(EVENT_VERIFIED, "9f1c0e6b2a7d4c11b0e5a3f4d2c1b0a9"))
        assertTrue(event.contains("window.__tstDeskOnFile;"))
        assertTrue(event.contains("\"event\":\"verified\""))
        assertTrue(pageCallScript(FILES_KEY_CALLBACK, JSONObject.quote("ab12")).contains("window.__tstDeskOnFilesKey;"))
    }

    @Test
    fun stillRefusesUnknownNames() {
        for (name in listOf("__tstDeskOnFiles", "__tstdeskonfile", "__tstDeskOnFile;alert(1)", "")) {
            assertThrows(name, IllegalArgumentException::class.java) { pageCallScript(name, "{}") }
        }
    }
}
