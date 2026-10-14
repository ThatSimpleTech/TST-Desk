package com.thatsimpletech.tstdesk

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.security.InvalidKeyException
import java.security.KeyStoreException
import java.security.ProviderException
import javax.crypto.AEADBadTagException
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

/** A plain-JVM AES-GCM key stands in for the Keystore one. */
private class JvmSealer : Sealer {
    private val key: SecretKey = KeyGenerator.getInstance("AES").apply { init(256) }.generateKey()
    var discarded = 0

    override fun seal(plain: ByteArray): Pair<ByteArray, ByteArray> {
        val c = Cipher.getInstance("AES/GCM/NoPadding")
        c.init(Cipher.ENCRYPT_MODE, key)
        return c.iv to c.doFinal(plain)
    }

    override fun open(iv: ByteArray, sealed: ByteArray): ByteArray {
        val c = Cipher.getInstance("AES/GCM/NoPadding")
        c.init(Cipher.DECRYPT_MODE, key, GCMParameterSpec(128, iv))
        return c.doFinal(sealed)
    }

    override fun discardKey() {
        discarded++
    }
}

/** A Keystore that will not open anything right now, the way a busy one fails at launch. */
private class FailingSealer(private val error: Exception) : Sealer {
    override fun seal(plain: ByteArray): Pair<ByteArray, ByteArray> = throw error
    override fun open(iv: ByteArray, sealed: ByteArray): ByteArray = throw error
}

/** A key that will not seal until it is dropped, the way a key that no longer loads fails. */
private class StuckKeySealer(private var failuresLeft: Int) : Sealer {
    private val working = JvmSealer()
    var seals = 0
    var discarded = 0

    override fun seal(plain: ByteArray): Pair<ByteArray, ByteArray> {
        seals++
        if (failuresLeft > 0) {
            failuresLeft--
            throw KeyStoreException("the saved attach key did not load")
        }
        return working.seal(plain)
    }

    override fun open(iv: ByteArray, sealed: ByteArray): ByteArray = working.open(iv, sealed)

    override fun discardKey() {
        discarded++
    }
}

private class MemoryStore : BlobStore {
    var value: String? = null
    override fun read() = value
    override fun write(value: String) {
        this.value = value
    }
    override fun remove() {
        value = null
    }
}

private class FakePairing : PairingHost {
    var scans = 0
    var link: String? = "tstdesk://pair?h=100.64.7.7&p=39079&c=ABCD2345&n=ezer"
    override fun deviceName() = "Pixel 11 Pro Fold"
    override fun startScan(): Boolean {
        scans++
        return true
    }
    override fun takePairLink(): String? = link.also { link = null }
}

class AttachVaultTest {
    private val target = """{"host":"100.64.7.7","port":39079,"token":"${"ab".repeat(32)}"}"""

    @Test
    fun aSavedTargetComesBackAndIsNotStoredInTheClear() {
        val store = MemoryStore()
        val vault = AttachVault(JvmSealer(), store)
        assertTrue(vault.save(target))
        assertFalse("token must not sit in plain text", store.value!!.contains("abab"))
        assertFalse(store.value!!.contains("100.64.7.7"))
        assertEquals(target, vault.load())
        assertEquals(AttachVault.STATUS_OK, vault.status)
    }

    @Test
    fun nothingSavedReportsNone() {
        val vault = AttachVault(JvmSealer(), MemoryStore())
        assertEquals(AttachVault.STATUS_NONE, vault.status)
        assertNull(vault.load())
        assertEquals(AttachVault.STATUS_NONE, vault.status)
    }

    @Test
    fun clearForgetsItAndDropsTheKey() {
        val sealer = JvmSealer()
        val vault = AttachVault(sealer, MemoryStore())
        vault.save(target)
        vault.clear()
        assertNull(vault.load())
        assertEquals(AttachVault.STATUS_NONE, vault.status)
        assertEquals(1, sealer.discarded)
    }

    @Test
    fun clearStillForgetsWhenTheKeyCannotBeDropped() {
        val store = MemoryStore()
        store.value = "x:y"
        val vault = AttachVault(
            object : Sealer by FailingSealer(KeyStoreException("busy")) {
                override fun discardKey() {
                    throw KeyStoreException("busy")
                }
            },
            store,
        )
        vault.clear()
        assertNull(store.value)
    }

    @Test
    fun somethingThatIsNotATargetIsRefused() {
        val store = MemoryStore()
        val vault = AttachVault(JvmSealer(), store)
        assertFalse(vault.save("not json"))
        assertFalse(vault.save("""{"host":"x"}"""))
        assertFalse(vault.save("{" + "x".repeat(AttachVault.MAX_BYTES) + "}"))
        assertNull(store.value)
    }

    @Test
    fun pairedAndKeptAddressShapesSave() {
        val vault = AttachVault(JvmSealer(), MemoryStore())
        val paired = """{"host":"100.64.7.7","port":39079,"token":"${"cd".repeat(32)}",""" +
            """"deviceId":"dev_0123456789ab","desktopName":"ezer"}"""
        assertTrue(vault.save(paired))
        assertEquals(paired, vault.load())
        // A refused key keeps the address and the desktop's name, not the token.
        val kept = """{"host":"100.64.7.7","port":39079,"desktopName":"ezer"}"""
        assertTrue(vault.save(kept))
        assertEquals(kept, vault.load())
    }

    @Test
    fun aBlobFromAnotherKeyIsUnreadableAndDropped() {
        val store = MemoryStore()
        AttachVault(JvmSealer(), store).save(target)
        val other = AttachVault(JvmSealer(), store)
        assertNull(other.load())
        assertEquals(AttachVault.STATUS_UNREADABLE, other.status)
        assertNull("undecryptable blob is removed", store.value)
    }

    @Test
    fun aWrappedBadTagIsStillUnreadable() {
        val store = MemoryStore()
        AttachVault(JvmSealer(), store).save(target)
        val vault = AttachVault(FailingSealer(ProviderException("finish", AEADBadTagException("tag"))), store)
        assertNull(vault.load())
        assertEquals(AttachVault.STATUS_UNREADABLE, vault.status)
        assertNull(store.value)
    }

    @Test
    fun aBusyKeystoreKeepsTheBlobAndReportsUnavailable() {
        val store = MemoryStore()
        val sealer = JvmSealer()
        AttachVault(sealer, store).save(target)
        val saved = store.value
        for (error in listOf(
            KeyStoreException("busy"),
            ProviderException("keystore2 not ready"),
            InvalidKeyException("key not loaded"),
            IllegalStateException("odd provider"),
        )) {
            val vault = AttachVault(FailingSealer(error), store)
            assertNull(vault.load())
            assertEquals(error.toString(), AttachVault.STATUS_UNAVAILABLE, vault.status)
            assertEquals("blob kept for the next launch", saved, store.value)
        }
        // The next launch, with a working Keystore, still has the computer.
        val later = AttachVault(sealer, store)
        assertEquals(target, later.load())
        assertEquals(AttachVault.STATUS_OK, later.status)
    }

    @Test
    fun aKeyThatWillNotSealIsReplacedAndTheSaveGoesThrough() {
        val store = MemoryStore()
        store.value = "b2xk:b2xkLWJsb2Itc2VhbGVkLXdpdGgtdGhlLXN0dWNrLWtleQ=="
        val sealer = StuckKeySealer(failuresLeft = 1)
        val vault = AttachVault(sealer, store)
        assertTrue(vault.save(target))
        assertEquals(2, sealer.seals)
        assertEquals("the stuck key is dropped once", 1, sealer.discarded)
        assertEquals(target, vault.load())
        assertEquals(AttachVault.STATUS_OK, vault.status)
    }

    @Test
    fun aSaveFailsOnlyWhenTheFreshKeyFailsToo() {
        val store = MemoryStore()
        val sealer = StuckKeySealer(failuresLeft = 2)
        assertFalse(AttachVault(sealer, store).save(target))
        assertEquals("one retry, not a loop", 2, sealer.seals)
        assertEquals(1, sealer.discarded)
        assertNull(store.value)
    }

    @Test
    fun aKeyThatCannotBeDroppedStillGetsTheSecondSeal() {
        val store = MemoryStore()
        val sealer = StuckKeySealer(failuresLeft = 1)
        val vault = AttachVault(
            object : Sealer by sealer {
                override fun discardKey() {
                    throw KeyStoreException("busy")
                }
            },
            store,
        )
        assertTrue(vault.save(target))
        assertEquals(2, sealer.seals)
        assertEquals(target, vault.load())
    }

    @Test
    fun aWorkingKeyIsNeverDropped() {
        val sealer = StuckKeySealer(failuresLeft = 0)
        assertTrue(AttachVault(sealer, MemoryStore()).save(target))
        assertEquals(1, sealer.seals)
        assertEquals(0, sealer.discarded)
    }

    @Test
    fun aDamagedBlobIsUnreadable() {
        val store = MemoryStore()
        for (damaged in listOf("no-separator", "!!:!!", "a:b:c", "QUJD:QUJD", ":")) {
            store.value = damaged
            val vault = AttachVault(JvmSealer(), store)
            assertNull(vault.load())
            assertEquals(damaged, AttachVault.STATUS_UNREADABLE, vault.status)
            assertNull(damaged, store.value)
        }
    }

    @Test
    fun theBridgeIsTheVault() {
        val bridge = AttachBridge(AttachVault(JvmSealer(), MemoryStore()))
        assertNull(bridge.loadAttach())
        assertEquals("none", bridge.attachStatus())
        assertTrue(bridge.saveAttach(target))
        assertEquals(target, bridge.loadAttach())
        assertEquals("ok", bridge.attachStatus())
        bridge.clearAttach()
        assertNull(bridge.loadAttach())
        assertFalse("no activity, no scanner", bridge.scanPairing())
        assertNull(bridge.takePairLink())
    }

    @Test
    fun theBridgeAsksTheActivityForPairing() {
        val pairing = FakePairing()
        val bridge = AttachBridge(AttachVault(JvmSealer(), MemoryStore()), pairing)
        assertEquals("Pixel 11 Pro Fold", bridge.deviceName())
        assertTrue(bridge.scanPairing())
        assertEquals(1, pairing.scans)
        assertEquals("tstdesk://pair?h=100.64.7.7&p=39079&c=ABCD2345&n=ezer", bridge.takePairLink())
        assertNull("taking the link clears it", bridge.takePairLink())
    }
}
