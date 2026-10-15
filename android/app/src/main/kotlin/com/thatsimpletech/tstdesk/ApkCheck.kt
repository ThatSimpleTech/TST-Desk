package com.thatsimpletech.tstdesk

import org.json.JSONArray
import org.json.JSONObject

const val APK_MIME = "application/vnd.android.package-archive"

/**
 * What an APK says about itself, or what the installed copy says (TD-5145).
 * [signers] are the SHA-256s of the signing certificates, lowercase hex;
 * null when Android would not give them.
 */
data class ApkInfo(
    val packageName: String,
    val versionCode: Long,
    val versionName: String?,
    val signers: List<String>?,
)

/** The card's APK line: the archive, and what is known about the installed app. */
data class ApkVerdict(
    val archive: ApkInfo,
    val isSelf: Boolean,
    val sameSigner: Boolean?,
    val downgrade: Boolean,
    val installedVersionCode: Long?,
    val installedVersionName: String?,
)

/**
 * Compares a received APK with the installed app of the same package.
 * A null [installed] means unknown, not "not installed": on API 30+ package
 * visibility hides other apps, and this shell declares no `<queries>`.
 * A missing signer list gives no warning, since the installer is the judge.
 */
fun verdict(archive: ApkInfo, installed: ApkInfo?, isSelf: Boolean): ApkVerdict {
    if (installed == null) {
        return ApkVerdict(archive, isSelf, null, false, null, null)
    }
    val ours = archive.signers
    val theirs = installed.signers
    val sameSigner = if (ours == null || theirs == null) null else ours.toSet() == theirs.toSet()
    return ApkVerdict(
        archive = archive,
        isSelf = isSelf,
        sameSigner = sameSigner,
        downgrade = archive.versionCode < installed.versionCode,
        installedVersionCode = installed.versionCode,
        installedVersionName = installed.versionName,
    )
}

/** The `apk` object of `fileStatus`, exactly as the bridge contract (§1.7) names it. */
fun ApkVerdict.toStatusJson(): JSONObject = JSONObject()
    .put("package", archive.packageName)
    .put("versionCode", archive.versionCode)
    .put("versionName", archive.versionName ?: JSONObject.NULL)
    .put("isSelf", isSelf)
    .put("installedVersionCode", installedVersionCode ?: JSONObject.NULL)
    .put("installedVersionName", installedVersionName ?: JSONObject.NULL)
    .put("sameSigner", sameSigner ?: JSONObject.NULL)

/** How the archive's info is cached in `meta.json`, so status never re-parses the APK. */
fun ApkInfo.toMetaJson(): JSONObject = JSONObject()
    .put("package", packageName)
    .put("versionCode", versionCode)
    .put("versionName", versionName ?: JSONObject.NULL)
    .put("signers", signers?.let { JSONArray(it) } ?: JSONObject.NULL)

/** The cached info back, or null for anything that is not the shape written above. */
fun apkInfoFromMeta(raw: Any?): ApkInfo? {
    val json = raw as? JSONObject ?: return null
    val pkg = json.opt("package") as? String ?: return null
    val code = (json.opt("versionCode") as? Number)?.toLong() ?: return null
    val name = json.opt("versionName") as? String
    val signers = (json.opt("signers") as? JSONArray)?.let { list ->
        (0 until list.length()).map { list.opt(it) as? String ?: return null }
    }
    return ApkInfo(pkg, code, name, signers)
}
