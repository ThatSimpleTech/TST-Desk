package com.thatsimpletech.tstdesk

import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.webkit.JavascriptInterface
import java.security.KeyStore
import java.util.Base64
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

/** Encrypts and decrypts one small blob. Android uses the Keystore; tests use a fake. */
interface Sealer {
    fun seal(plain: ByteArray): Pair<ByteArray, ByteArray>
    fun open(iv: ByteArray, sealed: ByteArray): ByteArray
}

/** Where the sealed text lives. Android uses private SharedPreferences. */
interface BlobStore {
    fun read(): String?
    fun write(value: String)
    fun remove()
}

/**
 * The phone's saved attach target (TD-5005): `{"host","port","token"}` from
 * the UI, sealed with a non-exportable Keystore key. Anything that does not
 * look like that object is refused, and anything that will not decrypt is
 * dropped, so a damaged or restored file reads as "nothing saved".
 */
class AttachVault(private val sealer: Sealer, private val store: BlobStore) {
    fun load(): String? {
        val stored = store.read() ?: return null
        val parts = stored.split(':')
        if (parts.size != 2) {
            store.remove()
            return null
        }
        return try {
            val decoder = Base64.getDecoder()
            String(sealer.open(decoder.decode(parts[0]), decoder.decode(parts[1])), Charsets.UTF_8)
        } catch (_: Exception) {
            store.remove()
            null
        }
    }

    fun save(raw: String): Boolean {
        if (!looksLikeTarget(raw)) return false
        return try {
            val (iv, sealed) = sealer.seal(raw.toByteArray(Charsets.UTF_8))
            val encoder = Base64.getEncoder()
            store.write("${encoder.encodeToString(iv)}:${encoder.encodeToString(sealed)}")
            true
        } catch (_: Exception) {
            false
        }
    }

    fun clear() = store.remove()

    companion object {
        const val MAX_BYTES = 4096

        /** Cheap shape check. The UI's parser is the real validator on load. */
        fun looksLikeTarget(raw: String): Boolean {
            val text = raw.trim()
            return text.length in 2..MAX_BYTES &&
                text.startsWith("{") && text.endsWith("}") &&
                "\"host\"" in text && "\"port\"" in text && "\"token\"" in text
        }
    }
}

/** The `TstDeskNative` object the UI calls. Only the bundled shell page can reach it. */
class AttachBridge(private val vault: AttachVault) {
    @JavascriptInterface fun loadAttach(): String? = vault.load()

    @JavascriptInterface fun saveAttach(raw: String): Boolean = vault.save(raw)

    @JavascriptInterface fun clearAttach() = vault.clear()
}

/** AES-256-GCM with a key that never leaves the Android Keystore. */
class KeystoreSealer(private val alias: String = "tstdesk.attach") : Sealer {
    private fun key(): SecretKey {
        val ks = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
        (ks.getKey(alias, null) as? SecretKey)?.let { return it }
        val gen = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore")
        gen.init(
            KeyGenParameterSpec.Builder(
                alias,
                KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT,
            )
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .setKeySize(256)
                .build(),
        )
        return gen.generateKey()
    }

    override fun seal(plain: ByteArray): Pair<ByteArray, ByteArray> {
        val cipher = Cipher.getInstance("AES/GCM/NoPadding")
        cipher.init(Cipher.ENCRYPT_MODE, key())
        return cipher.iv to cipher.doFinal(plain)
    }

    override fun open(iv: ByteArray, sealed: ByteArray): ByteArray {
        val cipher = Cipher.getInstance("AES/GCM/NoPadding")
        cipher.init(Cipher.DECRYPT_MODE, key(), GCMParameterSpec(128, iv))
        return cipher.doFinal(sealed)
    }
}

/** Private app storage. `allowBackup` is off, so it never leaves the phone. */
class PrefsBlobStore(context: Context) : BlobStore {
    private val prefs = context.getSharedPreferences("attach", Context.MODE_PRIVATE)

    override fun read(): String? = prefs.getString(KEY, null)

    override fun write(value: String) {
        prefs.edit().putString(KEY, value).apply()
    }

    override fun remove() {
        prefs.edit().remove(KEY).apply()
    }

    private companion object {
        const val KEY = "target"
    }
}
