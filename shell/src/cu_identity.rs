//! Code identity, TCC prompt stamps, and stale-grant detection for the
//! computer-use host (TD-4823).
//!
//! macOS keys each Screen Recording / Accessibility grant on the bundle id
//! *and* a code-signing requirement. An ad-hoc signature's designated
//! requirement is `cdhash H"…"`, unique per build, so after a rebuild
//! System Settings still shows TST Desk ON while
//! `CGPreflightScreenCaptureAccess` and `AXIsProcessTrusted` return false.
//! Re-raising the dialog cannot repair that row; `tccutil reset` (or
//! toggling the row) can. This module lets the host tell a stale grant
//! from a missing one, remembers which build prompted, and runs the reset.
//!
//! Stamps live under `{data_dir}/cu-tcc/`, one file per service and kind,
//! each holding the cdhash that prompted or was last seen granted.

use std::path::{Path, PathBuf};
use std::sync::OnceLock;

/// The bundle identifier TCC rows are keyed on.
pub const BUNDLE_ID: &str = "com.thatsimpletech.tstdesk";
/// Directory under the data dir that holds the stamps.
pub const STAMP_DIR: &str = "cu-tcc";
pub const PROMPTED_SCREEN: &str = "prompted-screen";
pub const PROMPTED_AX: &str = "prompted-ax";
pub const GRANTED_SCREEN: &str = "granted-screen";
pub const GRANTED_AX: &str = "granted-ax";
const STAMP_NAMES: [&str; 4] = [PROMPTED_SCREEN, PROMPTED_AX, GRANTED_SCREEN, GRANTED_AX];

/// Where the pane's repair lives, for copy that has to name it.
pub const RESET_PATH: &str = "Reset grants in TST Desk → Settings → Computer use";

pub const HOST_CAVEAT: &str = "macOS grants Screen Recording and Accessibility to TST Desk — the \
    app, not a helper — and pins each grant to the build that was granted. Enable TST Desk in \
    System Settings, then fully Quit (Cmd+Q) and reopen it. If Settings already shows TST Desk \
    ON and it still fails, the grant belongs to an older build: use Reset grants in TST Desk → \
    Settings → Computer use, relaunch, and allow again. Re-prompting cannot repair a stale grant.";

/// How the running executable is signed, as TCC sees it.
#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize)]
pub struct CodeIdentity {
    /// `identity` (certificate-based, stable across rebuilds), `adhoc`
    /// (cdhash-pinned, changes every build), or `unsigned`.
    pub signing: &'static str,
    pub identifier: String,
    pub cdhash: String,
    pub team_id: String,
    pub designated_requirement: String,
    /// The `.app` this executable runs from, or empty for a loose binary.
    pub bundle_path: String,
    pub bundled: bool,
}

impl CodeIdentity {
    fn unknown() -> Self {
        Self {
            signing: "unsigned",
            identifier: String::new(),
            cdhash: String::new(),
            team_id: String::new(),
            designated_requirement: String::new(),
            bundle_path: String::new(),
            bundled: false,
        }
    }
}

/// The two TCC services computer use depends on.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Service {
    ScreenRecording,
    Accessibility,
}

impl Service {
    /// The System Settings pane, in current macOS wording.
    pub fn settings_pane(self) -> &'static str {
        match self {
            Service::ScreenRecording => "Screen & System Audio Recording",
            Service::Accessibility => "Accessibility",
        }
    }

    /// The `tccutil` service name.
    pub fn tcc_service(self) -> &'static str {
        match self {
            Service::ScreenRecording => "ScreenCapture",
            Service::Accessibility => "Accessibility",
        }
    }
}

/// The host's identity, read once: a running binary cannot change its signature.
pub fn identity() -> &'static CodeIdentity {
    static IDENTITY: OnceLock<CodeIdentity> = OnceLock::new();
    IDENTITY.get_or_init(|| {
        let mut ident = sec::read_signing_info();
        if let Ok(exe) = std::env::current_exe() {
            if let Some(app) = bundle_of(&exe) {
                ident.bundle_path = app.display().to_string();
                ident.bundled = true;
            }
        }
        ident
    })
}

/// `…/Foo.app/Contents/MacOS/foo` → `…/Foo.app`. `None` for a loose binary
/// (`tauri dev` in `shell/target/`), which TCC keys by path and lists as a
/// lowercase `tst-desk` row.
pub fn bundle_of(exe: &Path) -> Option<PathBuf> {
    let macos_dir = exe.parent()?;
    let contents = macos_dir.parent()?;
    let app = contents.parent()?;
    if macos_dir.file_name()? == "MacOS"
        && contents.file_name()? == "Contents"
        && app.extension()? == "app"
    {
        Some(app.to_path_buf())
    } else {
        None
    }
}

/// A grant is stale when this service was last seen granted under a
/// different code hash and the API says no now. Unknown identity (empty
/// `current`) never reads as stale — there is nothing to compare.
pub fn stale_grant(stamp: Option<&str>, current: &str, granted_now: bool) -> bool {
    if granted_now || current.is_empty() {
        return false;
    }
    matches!(stamp.map(str::trim), Some(seen) if !seen.is_empty() && seen != current)
}

/// The on-disk prompt / grant stamps for one data dir.
pub struct Stamps {
    dir: PathBuf,
}

impl Stamps {
    pub fn new(data_dir: &Path) -> Self {
        Self {
            dir: data_dir.join(STAMP_DIR),
        }
    }

    /// The cdhash recorded under `name`, or `None` when absent or empty.
    pub fn read(&self, name: &str) -> Option<String> {
        std::fs::read_to_string(self.dir.join(name))
            .ok()
            .map(|text| text.trim().to_string())
            .filter(|text| !text.is_empty())
    }

    /// Record `cdhash` under `name`. Best-effort; false when the dir is unwritable.
    pub fn write(&self, name: &str, cdhash: &str) -> bool {
        if std::fs::create_dir_all(&self.dir).is_err() {
            return false;
        }
        std::fs::write(self.dir.join(name), format!("{cdhash}\n")).is_ok()
    }

    /// Forget every stamp (after a reset, so the next request may prompt).
    pub fn clear(&self) {
        for name in STAMP_NAMES {
            let _ = std::fs::remove_file(self.dir.join(name));
        }
    }
}

/// `tccutil reset` both services for the bundle id, without a shell.
///
/// Returns a description when either invocation fails so the pane can
/// show the command for the user to run in Terminal. Only the window
/// reaches this — it is never a tool the model can call.
pub fn reset_tcc_grants() -> Option<String> {
    let mut errors = Vec::new();
    for service in [Service::ScreenRecording, Service::Accessibility] {
        let name = service.tcc_service();
        match std::process::Command::new("/usr/bin/tccutil")
            .args(["reset", name, BUNDLE_ID])
            .output()
        {
            Ok(out) if out.status.success() => {
                log::info!("tccutil reset {name} {BUNDLE_ID}: ok");
            }
            Ok(out) => {
                let stderr = String::from_utf8_lossy(&out.stderr).trim().to_string();
                log::warn!("tccutil reset {name} {BUNDLE_ID} failed ({}): {stderr}", out.status);
                errors.push(format!(
                    "tccutil reset {name} {BUNDLE_ID} failed ({}): {stderr}",
                    out.status
                ));
            }
            Err(e) => {
                log::warn!("could not run /usr/bin/tccutil reset {name}: {e}");
                errors.push(format!(
                    "could not run /usr/bin/tccutil reset {name} {BUNDLE_ID}: {e}"
                ));
            }
        }
    }
    if errors.is_empty() {
        None
    } else {
        Some(errors.join("; "))
    }
}

/// The fix line for one missing service, naming the only repair that
/// works for a stale row and what the signing state means for next time.
pub fn fix_copy(service: Service, stale: bool, ident: &CodeIdentity) -> String {
    let pane = service.settings_pane();
    let tcc = service.tcc_service();
    let mut text = if stale {
        format!(
            "System Settings may still show TST Desk ON under {pane}; that grant belongs to a \
             previous build of the app (its code signature changed). Use {RESET_PATH}, or run \
             `tccutil reset {tcc} {BUNDLE_ID}` in Terminal, then relaunch TST Desk and allow again."
        )
    } else {
        format!(
            "Open System Settings > Privacy & Security > {pane}, enable TST Desk, then fully \
             Quit (Cmd+Q) and reopen it. If it already shows ON, the grant belongs to an older \
             build: use {RESET_PATH}, relaunch, and allow again."
        )
    };
    if ident.signing == "adhoc" {
        text.push_str(
            " This build is ad-hoc signed, so every rebuild invalidates the grant; install with \
             shell/scripts/install-macos.sh to sign with a stable identity.",
        );
    }
    if !ident.bundled {
        text.push_str(
            " This host is running outside TST Desk.app, so macOS keys its grant by path and \
             lists it as a lowercase 'tst-desk' row.",
        );
    }
    text
}

#[cfg(target_os = "macos")]
mod sec {
    use super::CodeIdentity;
    use std::ffi::{c_char, c_void, CStr};
    use std::ptr;

    type CFTypeRef = *const c_void;

    const K_SEC_CS_SIGNING_INFORMATION: u32 = 1 << 1;
    const K_SEC_CODE_SIGNATURE_ADHOC: i64 = 0x2;
    const K_CF_STRING_ENCODING_UTF8: u32 = 0x0800_0100;
    const K_CF_NUMBER_SINT64_TYPE: i32 = 4;
    const TEXT_CAP: usize = 4096;

    #[allow(non_upper_case_globals)]
    #[link(name = "Security", kind = "framework")]
    extern "C" {
        fn SecCodeCopySelf(flags: u32, out: *mut CFTypeRef) -> i32;
        fn SecCodeCopySigningInformation(
            code: CFTypeRef,
            flags: u32,
            out: *mut CFTypeRef,
        ) -> i32;
        fn SecCodeCopyDesignatedRequirement(
            code: CFTypeRef,
            flags: u32,
            out: *mut CFTypeRef,
        ) -> i32;
        fn SecRequirementCopyString(req: CFTypeRef, flags: u32, out: *mut CFTypeRef) -> i32;
        static kSecCodeInfoIdentifier: CFTypeRef;
        static kSecCodeInfoUnique: CFTypeRef;
        static kSecCodeInfoTeamIdentifier: CFTypeRef;
        static kSecCodeInfoFlags: CFTypeRef;
    }

    #[link(name = "CoreFoundation", kind = "framework")]
    extern "C" {
        fn CFRelease(cf: CFTypeRef);
        fn CFGetTypeID(cf: CFTypeRef) -> usize;
        fn CFStringGetTypeID() -> usize;
        fn CFDataGetTypeID() -> usize;
        fn CFNumberGetTypeID() -> usize;
        fn CFDictionaryGetValue(dict: CFTypeRef, key: CFTypeRef) -> CFTypeRef;
        fn CFStringGetCString(s: CFTypeRef, buf: *mut c_char, size: isize, enc: u32) -> u8;
        fn CFDataGetLength(data: CFTypeRef) -> isize;
        fn CFDataGetBytePtr(data: CFTypeRef) -> *const u8;
        fn CFNumberGetValue(number: CFTypeRef, kind: i32, out: *mut c_void) -> u8;
    }

    /// `SecCodeCopySigningInformation` on this process. Fails soft to
    /// "unsigned" — the caller only loses the stale-grant diagnosis.
    pub(super) fn read_signing_info() -> CodeIdentity {
        let mut ident = CodeIdentity::unknown();
        // SAFETY: every ref is null-checked and released exactly once; the
        // dictionary values are read only while `info` is alive.
        unsafe {
            let mut code: CFTypeRef = ptr::null();
            if SecCodeCopySelf(0, &mut code) != 0 || code.is_null() {
                return ident;
            }
            let mut info: CFTypeRef = ptr::null();
            if SecCodeCopySigningInformation(code, K_SEC_CS_SIGNING_INFORMATION, &mut info) == 0
                && !info.is_null()
            {
                ident.identifier = cf_string(CFDictionaryGetValue(info, kSecCodeInfoIdentifier));
                ident.cdhash = cf_hex(CFDictionaryGetValue(info, kSecCodeInfoUnique));
                ident.team_id = cf_string(CFDictionaryGetValue(info, kSecCodeInfoTeamIdentifier));
                let flags = cf_i64(CFDictionaryGetValue(info, kSecCodeInfoFlags));
                ident.signing = if ident.identifier.is_empty() && ident.cdhash.is_empty() {
                    "unsigned"
                } else if flags & K_SEC_CODE_SIGNATURE_ADHOC != 0 {
                    "adhoc"
                } else {
                    "identity"
                };
                CFRelease(info);
            }
            let mut req: CFTypeRef = ptr::null();
            if SecCodeCopyDesignatedRequirement(code, 0, &mut req) == 0 && !req.is_null() {
                let mut text: CFTypeRef = ptr::null();
                if SecRequirementCopyString(req, 0, &mut text) == 0 && !text.is_null() {
                    ident.designated_requirement = cf_string(text);
                    CFRelease(text);
                }
                CFRelease(req);
            }
            CFRelease(code);
        }
        ident
    }

    unsafe fn cf_string(value: CFTypeRef) -> String {
        if value.is_null() || CFGetTypeID(value) != CFStringGetTypeID() {
            return String::new();
        }
        let mut buf = vec![0 as c_char; TEXT_CAP];
        let ok = CFStringGetCString(
            value,
            buf.as_mut_ptr(),
            TEXT_CAP as isize,
            K_CF_STRING_ENCODING_UTF8,
        );
        if ok == 0 {
            return String::new();
        }
        CStr::from_ptr(buf.as_ptr()).to_string_lossy().into_owned()
    }

    unsafe fn cf_hex(value: CFTypeRef) -> String {
        if value.is_null() || CFGetTypeID(value) != CFDataGetTypeID() {
            return String::new();
        }
        let len = CFDataGetLength(value);
        let bytes = CFDataGetBytePtr(value);
        if len <= 0 || bytes.is_null() {
            return String::new();
        }
        std::slice::from_raw_parts(bytes, len as usize)
            .iter()
            .map(|b| format!("{b:02x}"))
            .collect()
    }

    unsafe fn cf_i64(value: CFTypeRef) -> i64 {
        if value.is_null() || CFGetTypeID(value) != CFNumberGetTypeID() {
            return 0;
        }
        let mut out: i64 = 0;
        let slot: *mut i64 = &mut out;
        if CFNumberGetValue(value, K_CF_NUMBER_SINT64_TYPE, slot.cast::<c_void>()) == 0 {
            return 0;
        }
        out
    }
}

#[cfg(not(target_os = "macos"))]
mod sec {
    pub(super) fn read_signing_info() -> super::CodeIdentity {
        super::CodeIdentity::unknown()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn stale_grant_truth_table() {
        // Seen granted under another build, denied now: stale.
        assert!(stale_grant(Some("aaa"), "bbb", false));
        // Same build: just denied, not stale.
        assert!(!stale_grant(Some("aaa"), "aaa", false));
        // Granted now is never stale, whatever the stamp says.
        assert!(!stale_grant(Some("aaa"), "bbb", true));
        // Never seen granted: missing, not stale.
        assert!(!stale_grant(None, "bbb", false));
        assert!(!stale_grant(Some(""), "bbb", false));
        assert!(!stale_grant(Some("  \n"), "bbb", false));
        // Unknown identity: nothing to compare.
        assert!(!stale_grant(Some("aaa"), "", false));
    }

    #[test]
    fn bundle_of_finds_the_app() {
        assert_eq!(
            bundle_of(Path::new(
                "/Applications/TST Desk.app/Contents/MacOS/tst-desk"
            )),
            Some(PathBuf::from("/Applications/TST Desk.app"))
        );
        assert_eq!(
            bundle_of(Path::new("/Users/x/TST-Desk/shell/target/debug/tst-desk")),
            None
        );
        assert_eq!(
            bundle_of(Path::new("/Foo.app/Contents/Resources/tst-desk")),
            None
        );
    }

    #[test]
    fn stamps_round_trip_and_clear() {
        let dir = std::env::temp_dir().join(format!("tst-cu-tcc-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        let stamps = Stamps::new(&dir);
        assert_eq!(stamps.read(GRANTED_SCREEN), None);
        assert!(stamps.write(GRANTED_SCREEN, "abc"));
        assert_eq!(stamps.read(GRANTED_SCREEN).as_deref(), Some("abc"));
        assert!(stamps.write(PROMPTED_AX, ""));
        assert_eq!(stamps.read(PROMPTED_AX), None);
        stamps.clear();
        assert_eq!(stamps.read(GRANTED_SCREEN), None);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn fix_copy_names_the_repair() {
        let adhoc = CodeIdentity {
            signing: "adhoc",
            ..CodeIdentity::unknown()
        };
        let stale = fix_copy(Service::ScreenRecording, true, &adhoc);
        assert!(stale.contains("Reset grants"));
        assert!(stale.contains("tccutil reset ScreenCapture com.thatsimpletech.tstdesk"));
        assert!(stale.contains("Screen & System Audio Recording"));
        assert!(stale.contains("ad-hoc"));
        assert!(stale.contains("lowercase 'tst-desk' row"));

        let signed = CodeIdentity {
            signing: "identity",
            bundled: true,
            ..CodeIdentity::unknown()
        };
        let fresh = fix_copy(Service::Accessibility, false, &signed);
        assert!(fresh.contains("Accessibility"));
        assert!(fresh.contains("Cmd+Q"));
        assert!(fresh.contains("Reset grants"));
        assert!(!fresh.contains("ad-hoc"));
        assert!(!fresh.contains("lowercase"));
    }

    #[test]
    fn identity_serializes_every_field() {
        let value = serde_json::to_value(CodeIdentity::unknown()).unwrap();
        for key in [
            "signing",
            "identifier",
            "cdhash",
            "team_id",
            "designated_requirement",
            "bundle_path",
            "bundled",
        ] {
            assert!(value.get(key).is_some(), "missing {key}");
        }
        assert_eq!(value["signing"], "unsigned");
        assert_eq!(value["bundled"], false);
    }

    #[test]
    fn service_names_match_tccutil_and_settings() {
        assert_eq!(Service::ScreenRecording.tcc_service(), "ScreenCapture");
        assert_eq!(Service::Accessibility.tcc_service(), "Accessibility");
        assert_eq!(BUNDLE_ID, "com.thatsimpletech.tstdesk");
    }
}
