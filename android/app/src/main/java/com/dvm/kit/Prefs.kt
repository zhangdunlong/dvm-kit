package com.dvm.kit

import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import android.util.Log
import java.security.KeyStore
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

/**
 * 配置存储：AES-256-GCM 加密后落 SharedPreferences，密钥由 Android Keystore 托管，
 * 不随备份导出（allowBackup=false），也不会以明文出现在应用数据里。
 */
class Prefs(ctx: Context) {

    private val sp = ctx.getSharedPreferences("dvm_kit", Context.MODE_PRIVATE)

    private fun key(): SecretKey {
        val ks = KeyStore.getInstance(KEYSTORE).apply { load(null) }
        (ks.getEntry(ALIAS, null) as? KeyStore.SecretKeyEntry)?.let { return it.secretKey }
        val kg = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, KEYSTORE)
        kg.init(
            KeyGenParameterSpec.Builder(
                ALIAS,
                KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT
            )
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .setKeySize(256)
                .build()
        )
        return kg.generateKey()
    }

    fun save(json: String) {
        try {
            val cipher = Cipher.getInstance("AES/GCM/NoPadding")
            cipher.init(Cipher.ENCRYPT_MODE, key())
            val iv = cipher.iv
            val ct = cipher.doFinal(json.toByteArray(Charsets.UTF_8))
            val blob = ByteArray(iv.size + ct.size)
            System.arraycopy(iv, 0, blob, 0, iv.size)
            System.arraycopy(ct, 0, blob, iv.size, ct.size)
            sp.edit().putString("cfg", Base64.encodeToString(blob, Base64.NO_WRAP)).apply()
        } catch (e: Exception) {
            Log.w(TAG, "save failed", e)
        }
    }

    fun load(): String {
        val b64 = sp.getString("cfg", null) ?: return "{}"
        return try {
            val blob = Base64.decode(b64, Base64.NO_WRAP)
            if (blob.size <= IV_LEN) return "{}"
            val iv = blob.copyOfRange(0, IV_LEN)
            val ct = blob.copyOfRange(IV_LEN, blob.size)
            val cipher = Cipher.getInstance("AES/GCM/NoPadding")
            cipher.init(Cipher.DECRYPT_MODE, key(), GCMParameterSpec(128, iv))
            String(cipher.doFinal(ct), Charsets.UTF_8)
        } catch (e: Exception) {
            Log.w(TAG, "load failed", e)
            "{}"
        }
    }

    fun clear() {
        sp.edit().remove("cfg").apply()
    }

    companion object {
        private const val TAG = "DvmKit"
        private const val KEYSTORE = "AndroidKeyStore"
        private const val ALIAS = "dvm_kit_cfg"
        private const val IV_LEN = 12
    }
}
