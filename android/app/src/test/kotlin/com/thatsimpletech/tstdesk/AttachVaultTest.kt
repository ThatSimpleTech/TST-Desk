package com.thatsimpletech.tstdesk

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

/** A plain-JVM AES-GCM key stands in for the Keystore one. */
private class JvmSealer : Sealer {
    private val key: SecretKey = KeyGenerator.getInstance("AES").apply { init(256) }.generateKey()

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
    }

    @Test
    fun clearForgetsIt() {
        val vault = AttachVault(JvmSealer(), MemoryStore())
        vault.save(target)
        vault.clear()
        assertNull(vault.load())
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
    fun aBlobFromAnotherKeyReadsAsNothingAndIsDropped() {
        val store = MemoryStore()
        AttachVault(JvmSealer(), store).save(target)
        val other = AttachVault(JvmSealer(), store)
        assertNull(other.load())
        assertNull("undecryptable blob is removed", store.value)
    }

    @Test
    fun aDamagedBlobReadsAsNothing() {
        val store = MemoryStore()
        store.value = "no-separator"
        assertNull(AttachVault(JvmSealer(), store).load())
        store.value = "!!:!!"
        assertNull(AttachVault(JvmSealer(), store).load())
    }

    @Test
    fun theBridgeIsTheVault() {
        val bridge = AttachBridge(AttachVault(JvmSealer(), MemoryStore()))
        assertNull(bridge.loadAttach())
        assertTrue(bridge.saveAttach(target))
        assertEquals(target, bridge.loadAttach())
        bridge.clearAttach()
        assertNull(bridge.loadAttach())
    }
}
