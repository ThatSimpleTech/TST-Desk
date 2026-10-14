package com.thatsimpletech.tstdesk

import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.webkit.JavascriptInterface
import java.security.KeyStore
import java.security.KeyStoreException
import java.util.Base64
import javax.crypto.AEADBadTagException
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

/** Encrypts and decrypts one small blob. Android uses the Keystore; tests use a fake. */
interface Sealer {
    fun seal(plain: ByteArray): Pair<ByteArray, ByteArray>
    fun open(iv: ByteArray, sealed: ByteArray): ByteArray

    /** Drop the key once nothing sealed with it is kept. */
    fun discardKey() = Unit
}

/** Where the sealed text lives. Android uses private SharedPreferences. */
interface BlobStore {
    fun read(): String?
    fun write(value: String)
    fun remove()
}

/**
 * The phone's saved attach target (TD-5005): `{"host","port","token"}` from
 * the UI, sealed with a non-exportable Keystore key. TD-5014 adds optional
 * `deviceId` and `desktopName`, and a kept address may have no token.
 *
 * A blob is dropped only when it is genuinely unreadable: a bad format, or
 * a GCM tag that does not authenticate. A Keystore that is busy at launch
 * keeps the blob and reports "unavailable", so the next launch can still
 * read it. Dropping on any error is how the phone forgot its computer.
 */
class AttachVault(private val sealer: Sealer, private val store: BlobStore) {
    /** What the last load() found, for `attachStatus()` (TD-5014). */
    @Volatile
    var status: String = STATUS_NONE
        private set

    fun load(): String? {
        val stored = store.read()
        if (stored == null) {
            status = STATUS_NONE
            return null
        }
        val parts = stored.split(':')
        if (parts.size != 2) return drop()
        val iv: ByteArray
        val sealed: ByteArray
        try {
            val decoder = Base64.getDecoder()
            iv = decoder.decode(parts[0])
            sealed = decoder.decode(parts[1])
        } catch (_: IllegalArgumentException) {
            return drop()
        }
        // Both sealers are AES-GCM. A blob without a 12-byte IV and a tag can
        // never open, and keeping it would report "unavailable" forever.
        if (iv.size != GCM_IV_BYTES || sealed.size < GCM_TAG_BYTES) return drop()
        return try {
            val plain = String(sealer.open(iv, sealed), Charsets.UTF_8)
            status = STATUS_OK
            plain
        } catch (e: Exception) {
            if (failedAuthentication(e)) {
                drop()
            } else {
                status = STATUS_UNAVAILABLE
                null
            }
        }
    }

    fun save(raw: String): Boolean {
        if (!looksLikeTarget(raw)) return false
        val plain = raw.toByteArray(Charsets.UTF_8)
        // A key that will not seal will not open the old blob either, and
        // this save replaces that blob anyway, so a fresh key costs nothing.
        // Without this a key stuck that way refuses every pairing for good.
        val (iv, sealed) = sealOrNull(plain) ?: run {
            try {
                sealer.discardKey()
            } catch (_: Exception) {
                // Still try: the second seal reports whether the Keystore is back.
            }
            sealOrNull(plain)
        } ?: return false
        return try {
            val encoder = Base64.getEncoder()
            store.write("${encoder.encodeToString(iv)}:${encoder.encodeToString(sealed)}")
            true
        } catch (_: Exception) {
            false
        }
    }

    private fun sealOrNull(plain: ByteArray): Pair<ByteArray, ByteArray>? =
        try {
            sealer.seal(plain)
        } catch (_: Exception) {
            null
        }

    /**
     * Forget the target. The key goes too, since it now guards nothing, so
     * "Forget this computer" also recovers from a key that no longer loads.
     */
    fun clear() {
        store.remove()
        try {
            sealer.discardKey()
        } catch (_: Exception) {
            // A busy Keystore keeps the old key. It still seals the next save.
        }
    }

    private fun drop(): String? {
        store.remove()
        status = STATUS_UNREADABLE
        return null
    }

    companion object {
        const val MAX_BYTES = 4096
        const val GCM_IV_BYTES = 12
        const val GCM_TAG_BYTES = 16

        const val STATUS_NONE = "none"
        const val STATUS_OK = "ok"
        const val STATUS_UNREADABLE = "unreadable"
        const val STATUS_UNAVAILABLE = "unavailable"

        /**
         * Cheap shape check. The UI's parser is the real validator on load.
         * No token is required (TD-5014): a refused key keeps the address.
         */
        fun looksLikeTarget(raw: String): Boolean {
            val text = raw.trim()
            return text.length in 2..MAX_BYTES &&
                text.startsWith("{") && text.endsWith("}") &&
                "\"host\"" in text && "\"port\"" in text
        }

        /** The Keystore reports a tag mismatch as AEADBadTagException, sometimes wrapped. */
        fun failedAuthentication(error: Throwable): Boolean {
            var cause: Throwable? = error
            var depth = 0
            while (cause != null && depth < 8) {
                if (cause is AEADBadTagException) return true
                cause = cause.cause
                depth++
            }
            return false
        }
    }
}

/** The `TstDeskNative` object the UI calls. Only the bundled shell page can reach it. */
class AttachBridge(
    private val vault: AttachVault,
    private val pairing: PairingHost = PairingHost.NONE,
) {
    @JavascriptInterface fun loadAttach(): String? = vault.load()

    @JavascriptInterface fun saveAttach(raw: String): Boolean = vault.save(raw)

    @JavascriptInterface fun clearAttach() = vault.clear()

    // TD-5014 pairing. A scan result or a later link arrives as a page callback.
    @JavascriptInterface fun deviceName(): String = pairing.deviceName()

    @JavascriptInterface fun scanPairing(): Boolean = pairing.startScan()

    @JavascriptInterface fun takePairLink(): String? = pairing.takePairLink()

    @JavascriptInterface fun attachStatus(): String = vault.status
}

/** AES-256-GCM with a key that never leaves the Android Keystore. */
class KeystoreSealer(private val alias: String = "tstdesk.attach") : Sealer {
    /**
     * A new key only when the alias is absent (TD-5014). An alias that exists
     * but will not load is usually a busy Keystore at launch. Replacing it
     * would make every saved blob undecryptable, so this throws instead.
     */
    @Synchronized
    private fun key(): SecretKey {
        val ks = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
        if (ks.containsAlias(alias)) {
            return ks.getKey(alias, null) as? SecretKey
                ?: throw KeyStoreException("the saved attach key did not load")
        }
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

    @Synchronized
    override fun discardKey() {
        val ks = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
        if (ks.containsAlias(alias)) ks.deleteEntry(alias)
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
