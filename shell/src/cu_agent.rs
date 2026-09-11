//! Computer-use actuator in the TST Desk host process.
//!
//! Checkout Python and the `tstd` sidecar are the wrong TCC identity.
//! Capture (`CGWindowListCreateImage`) and input (`CGEventPost`) run here so
//! System Settings attributes both to **TST Desk**, not a helper named
//! `tst-desk` / `tstd`. The protocol matches `core/tstd/cu_host.py` so
//! Darwin `tst-cu-mcp` stays a Unix-socket client.

use std::path::{Path, PathBuf};
use std::sync::OnceLock;

pub const SOCK_ENV: &str = "TST_CU_AGENT_SOCK";
pub const SOCK_NAME: &str = "cu-agent.sock";

static DATA_DIR: OnceLock<PathBuf> = OnceLock::new();

pub fn sock_path(data_dir: &Path) -> PathBuf {
    data_dir.join(SOCK_NAME)
}

/// Bind `{data_dir}/cu-agent.sock` before the daemon starts, then serve.
///
/// Binding is synchronous so `tstd` sees a live socket and does not take
/// over. The accept loop is a plain thread so it does not depend on the
/// Tauri Tokio runtime being online during `setup`. Failure is non-fatal:
/// the daemon still has a Python fallback.
pub fn bind_and_serve(data_dir: &Path) {
    #[cfg(not(target_os = "macos"))]
    {
        let _ = data_dir;
    }
    #[cfg(target_os = "macos")]
    {
        let _ = DATA_DIR.set(data_dir.to_path_buf());
        match macos::bind_listener(data_dir) {
            Ok(listener) => {
                let _ = std::fs::remove_file(data_dir.join("cu-agent.bind-error"));
                let _ = std::thread::Builder::new()
                    .name("cu-agent".into())
                    .spawn(move || macos::serve(listener));
            }
            Err(e) => {
                let path = sock_path(data_dir);
                let msg = format!("{e}\npath={path}\n", path = path.display());
                log::warn!("cu-agent socket not bound: {e}; daemon will actuate");
                let _ = std::fs::write(data_dir.join("cu-agent.bind-error"), msg);
            }
        }
    }
}

#[derive(Debug, PartialEq)]
enum Command<'a> {
    Quit,
    Permissions {
        request: bool,
        reset: bool,
    },
    Capture {
        x: i32,
        y: i32,
        w: i32,
        h: i32,
    },
    Move {
        x: f64,
        y: f64,
    },
    Click {
        x: f64,
        y: f64,
        button: &'a str,
        count: i32,
    },
    Key {
        combo: &'a str,
    },
    Scroll {
        dx: i32,
        dy: i32,
    },
    Type {
        b64: &'a str,
    },
    Json(&'a str),
    Unknown,
}

fn parse_line(text: &str) -> Command<'_> {
    let text = text.trim();
    if text.is_empty() || text == "quit" {
        return Command::Quit;
    }
    if text == "permissions" {
        return Command::Permissions {
            request: false,
            reset: false,
        };
    }
    if text == "permissions request" {
        return Command::Permissions {
            request: true,
            reset: false,
        };
    }
    if text == "permissions reset" {
        // Reset implies re-request: the user asked for a fresh grant.
        return Command::Permissions {
            request: true,
            reset: true,
        };
    }
    if let Some(rest) = text.strip_prefix("capture ") {
        let mut parts = rest.split_whitespace();
        if let (Some(x), Some(y), Some(w), Some(h)) =
            (parts.next(), parts.next(), parts.next(), parts.next())
        {
            if let (Ok(x), Ok(y), Ok(w), Ok(h)) = (x.parse(), y.parse(), w.parse(), h.parse()) {
                return Command::Capture { x, y, w, h };
            }
        }
        return Command::Unknown;
    }
    if let Some(rest) = text.strip_prefix("move ") {
        let mut parts = rest.split_whitespace();
        if let (Some(x), Some(y)) = (parts.next(), parts.next()) {
            if let (Ok(x), Ok(y)) = (x.parse(), y.parse()) {
                return Command::Move { x, y };
            }
        }
        return Command::Unknown;
    }
    if let Some(rest) = text.strip_prefix("click ") {
        let parts: Vec<&str> = rest.split_whitespace().collect();
        if parts.len() >= 2 {
            if let (Ok(x), Ok(y)) = (parts[0].parse(), parts[1].parse()) {
                let button = parts.get(2).copied().unwrap_or("left");
                let count = parts.get(3).and_then(|s| s.parse().ok()).unwrap_or(1);
                return Command::Click {
                    x,
                    y,
                    button,
                    count,
                };
            }
        }
        return Command::Unknown;
    }
    if let Some(combo) = text.strip_prefix("key ") {
        return Command::Key {
            combo: combo.trim(),
        };
    }
    if let Some(rest) = text.strip_prefix("scroll ") {
        let mut parts = rest.split_whitespace();
        if let (Some(dx), Some(dy)) = (parts.next(), parts.next()) {
            if let (Ok(dx), Ok(dy)) = (dx.parse(), dy.parse()) {
                return Command::Scroll { dx, dy };
            }
        }
        return Command::Unknown;
    }
    if let Some(b64) = text.strip_prefix("type ") {
        return Command::Type { b64: b64.trim() };
    }
    if let Some(rest) = text.strip_prefix("json ") {
        return Command::Json(rest.trim());
    }
    Command::Unknown
}

#[cfg(target_os = "macos")]
mod macos {
    use super::{parse_line, sock_path, Command};
    use std::ffi::{c_char, c_void};
    use std::io::{self, BufRead, BufReader, Write};
    use std::os::unix::fs::PermissionsExt;
    use std::os::unix::net::{UnixListener, UnixStream};
    use std::path::Path;
    use std::ptr;
    use std::sync::atomic::{AtomicBool, Ordering};
    use std::time::Duration;

    use base64::Engine;

    use crate::cu_identity::{self, Service, Stamps};

    // Per-process guard behind the on-disk prompt stamps (TD-4823): the
    // stamps are keyed by build, these only stop a loop when the data dir
    // is unwritable.
    static SCREEN_PROMPTED: AtomicBool = AtomicBool::new(false);
    static AX_PROMPTED: AtomicBool = AtomicBool::new(false);
    static SCREEN_CAPABLE: AtomicBool = AtomicBool::new(false);

    const K_CG_HID_EVENT_TAP: u32 = 0;
    const K_CG_EVENT_LEFT_MOUSE_DOWN: u32 = 1;
    const K_CG_EVENT_LEFT_MOUSE_UP: u32 = 2;
    const K_CG_EVENT_RIGHT_MOUSE_DOWN: u32 = 3;
    const K_CG_EVENT_RIGHT_MOUSE_UP: u32 = 4;
    const K_CG_EVENT_MOUSE_MOVED: u32 = 5;
    const K_CG_MOUSE_BUTTON_LEFT: u32 = 0;
    const K_CG_MOUSE_BUTTON_RIGHT: u32 = 1;
    const K_CG_MOUSE_EVENT_CLICK_STATE: u32 = 1;
    const K_CG_WINDOW_LIST_OPTION_ON_SCREEN_ONLY: u32 = 1;
    const K_CF_STRING_ENCODING_UTF8: u32 = 0x0800_0100;
    const K_CG_SCROLL_EVENT_UNIT_LINE: u32 = 0;

    const MODS: &[(&str, u64)] = &[
        ("cmd", 1 << 20),
        ("command", 1 << 20),
        ("shift", 1 << 17),
        ("alt", 1 << 19),
        ("option", 1 << 19),
        ("ctrl", 1 << 18),
        ("control", 1 << 18),
        ("fn", 1 << 23),
    ];

    fn keycode(name: &str) -> Option<u16> {
        Some(match name {
            "a" => 0,
            "s" => 1,
            "d" => 2,
            "f" => 3,
            "h" => 4,
            "g" => 5,
            "z" => 6,
            "x" => 7,
            "c" => 8,
            "v" => 9,
            "b" => 11,
            "q" => 12,
            "w" => 13,
            "e" => 14,
            "r" => 15,
            "y" => 16,
            "t" => 17,
            "1" => 18,
            "2" => 19,
            "3" => 20,
            "4" => 21,
            "6" => 22,
            "5" => 23,
            "9" => 25,
            "7" => 26,
            "minus" => 27,
            "8" => 28,
            "0" => 29,
            "rightbracket" => 30,
            "o" => 31,
            "u" => 32,
            "leftbracket" => 33,
            "i" => 34,
            "p" => 35,
            "return" | "enter" => 36,
            "l" => 37,
            "j" => 38,
            "quote" => 39,
            "k" => 40,
            "semicolon" => 41,
            "backslash" => 42,
            "comma" => 43,
            "slash" => 44,
            "n" => 45,
            "m" => 46,
            "period" => 47,
            "tab" => 48,
            "space" => 49,
            "grave" => 50,
            "delete" | "backspace" => 51,
            "escape" | "esc" => 53,
            "equal" => 24,
            "home" => 115,
            "pageup" => 116,
            "forward_delete" => 117,
            "end" => 119,
            "pagedown" => 121,
            "left" => 123,
            "right" => 124,
            "down" => 125,
            "up" => 126,
            _ => return None,
        })
    }

    #[repr(C)]
    struct CGPoint {
        x: f64,
        y: f64,
    }

    #[repr(C)]
    struct CGSize {
        width: f64,
        height: f64,
    }

    #[repr(C)]
    struct CGRect {
        origin: CGPoint,
        size: CGSize,
    }

    #[repr(C)]
    struct CFDictionaryKeyCallBacks {
        version: isize,
        retain: *const c_void,
        release: *const c_void,
        copy_description: *const c_void,
        equal: *const c_void,
        hash: *const c_void,
    }

    type CFDictionaryValueCallBacks = CFDictionaryKeyCallBacks;

    #[link(name = "CoreGraphics", kind = "framework")]
    extern "C" {
        fn CGEventCreateMouseEvent(
            source: *mut c_void,
            mouse_type: u32,
            pos: CGPoint,
            button: u32,
        ) -> *mut c_void;
        fn CGEventCreateKeyboardEvent(
            source: *mut c_void,
            keycode: u16,
            key_down: u8,
        ) -> *mut c_void;
        fn CGEventCreateScrollWheelEvent(
            source: *mut c_void,
            units: u32,
            wheel_count: u32,
            wheel1: i32,
            wheel2: i32,
        ) -> *mut c_void;
        fn CGEventSetIntegerValueField(event: *mut c_void, field: u32, value: i64);
        fn CGEventSetFlags(event: *mut c_void, flags: u64);
        fn CGEventKeyboardSetUnicodeString(event: *mut c_void, length: u32, string: *const u16);
        fn CGEventPost(tap: u32, event: *mut c_void);
        fn CGWindowListCreateImage(
            rect: CGRect,
            list_option: u32,
            window_id: u32,
            image_option: u32,
        ) -> *mut c_void;
        fn CGImageGetWidth(image: *mut c_void) -> usize;
        fn CGImageGetHeight(image: *mut c_void) -> usize;
        fn CGPreflightScreenCaptureAccess() -> u8;
        fn CGRequestScreenCaptureAccess() -> u8;
    }

    #[link(name = "ApplicationServices", kind = "framework")]
    extern "C" {
        fn AXIsProcessTrusted() -> u8;
        fn AXIsProcessTrustedWithOptions(options: *const c_void) -> u8;
    }

    #[link(name = "CoreFoundation", kind = "framework")]
    extern "C" {
        fn CFRelease(cf: *const c_void);
        fn CFStringCreateWithCString(
            alloc: *mut c_void,
            c_str: *const c_char,
            encoding: u32,
        ) -> *mut c_void;
        fn CFDataCreateMutable(alloc: *mut c_void, capacity: isize) -> *mut c_void;
        fn CFDataGetLength(data: *const c_void) -> isize;
        fn CFDataGetBytePtr(data: *const c_void) -> *const u8;
        fn CFDictionaryCreate(
            alloc: *mut c_void,
            keys: *const *const c_void,
            values: *const *const c_void,
            num_values: isize,
            key_call_backs: *const CFDictionaryKeyCallBacks,
            value_call_backs: *const CFDictionaryValueCallBacks,
        ) -> *mut c_void;
        static kCFBooleanTrue: *const c_void;
        static kCFTypeDictionaryKeyCallBacks: CFDictionaryKeyCallBacks;
        static kCFTypeDictionaryValueCallBacks: CFDictionaryValueCallBacks;
    }

    #[link(name = "ImageIO", kind = "framework")]
    extern "C" {
        fn CGImageDestinationCreateWithData(
            data: *mut c_void,
            uti: *mut c_void,
            count: usize,
            options: *mut c_void,
        ) -> *mut c_void;
        fn CGImageDestinationAddImage(
            dest: *mut c_void,
            image: *mut c_void,
            properties: *mut c_void,
        );
        fn CGImageDestinationFinalize(dest: *mut c_void) -> u8;
    }

    pub(super) fn bind_listener(data_dir: &Path) -> io::Result<UnixListener> {
        std::fs::create_dir_all(data_dir)?;
        let path = sock_path(data_dir);
        let _ = std::fs::remove_file(&path);
        let listener = UnixListener::bind(&path)?;
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o600))?;
        Ok(listener)
    }

    pub(super) fn serve(listener: UnixListener) {
        for incoming in listener.incoming() {
            match incoming {
                Ok(stream) => {
                    std::thread::Builder::new()
                        .name("cu-agent-client".into())
                        .spawn(move || handle_client(stream))
                        .ok();
                }
                Err(e) => {
                    log::error!("cu-agent socket ended: {e}");
                    break;
                }
            }
        }
    }

    fn handle_client(stream: UnixStream) {
        let _ = stream.set_read_timeout(Some(Duration::from_secs(120)));
        let mut reader = BufReader::new(stream);
        loop {
            let mut line = String::new();
            match reader.read_line(&mut line) {
                Ok(0) => return,
                Ok(_) => {}
                Err(_) => return,
            }
            let (header, blob) = dispatch(&line);
            let sock = reader.get_mut();
            if sock.write_all(&header).is_err() {
                return;
            }
            if let Some(blob) = blob {
                if sock.write_all(&blob).is_err() {
                    return;
                }
            }
            let _ = sock.flush();
            if matches!(parse_line(&line), Command::Quit) {
                return;
            }
        }
    }

    fn dispatch(text: &str) -> (Vec<u8>, Option<Vec<u8>>) {
        match parse_line(text) {
            Command::Quit => (b"ok\n".to_vec(), None),
            Command::Permissions { request, reset } => {
                let reset_error = if reset { reset_tcc_grants() } else { None };
                if request {
                    maybe_request_tcc();
                }
                (
                    permissions_json(reset_error.as_deref()).into_bytes(),
                    None,
                )
            }
            Command::Capture { x, y, w, h } => match capture_png(x, y, w, h) {
                Some(png) => {
                    SCREEN_CAPABLE.store(true, Ordering::Relaxed);
                    if let Some(stamps) = stamps() {
                        let cdhash = &cu_identity::identity().cdhash;
                        stamps.write(cu_identity::GRANTED_SCREEN, cdhash);
                    }
                    (format!("png {}\n", png.len()).into_bytes(), Some(png))
                }
                None => (b"err\n".to_vec(), None),
            },
            Command::Move { x, y } => (ok_err(mouse_move(x, y)), None),
            Command::Click {
                x,
                y,
                button,
                count,
            } => (ok_err(mouse_click(x, y, button, count)), None),
            Command::Key { combo } => (ok_err(press_combo(combo)), None),
            Command::Scroll { dx, dy } => (ok_err(scroll(dx, dy)), None),
            Command::Type { b64 } => match decode_type(b64) {
                Some(typed) => (ok_err(type_text(&typed)), None),
                None => (b"err\n".to_vec(), None),
            },
            Command::Json(raw) => (json_reply(raw), None),
            Command::Unknown => (b"err\n".to_vec(), None),
        }
    }

    fn json_reply(raw: &str) -> Vec<u8> {
        let body = match serde_json::from_str::<serde_json::Value>(raw) {
            Ok(value) => crate::cu_ax::handle(&value),
            Err(_) => serde_json::json!({"ok": false, "error": "bad json"}),
        };
        let mut bytes = body.to_string().into_bytes();
        bytes.push(b'\n');
        bytes
    }

    fn ok_err(ok: bool) -> Vec<u8> {
        if ok {
            b"ok\n".to_vec()
        } else {
            b"err\n".to_vec()
        }
    }

    fn decode_type(b64: &str) -> Option<String> {
        let raw = base64::engine::general_purpose::STANDARD.decode(b64).ok()?;
        String::from_utf8(raw).ok()
    }

    fn permissions_json(reset_error: Option<&str>) -> String {
        let ident = cu_identity::identity();
        let stamps = stamps();
        let screen = screen_is_capable();
        let access = ax_trusted();
        let preflight = cg_preflight();
        // Stale = last seen granted under another build and denied now. Read
        // before this probe records the current build as granted.
        let stale_screen = stale_now(&stamps, cu_identity::GRANTED_SCREEN, screen);
        let stale_ax = stale_now(&stamps, cu_identity::GRANTED_AX, access);
        if let Some(stamps) = &stamps {
            if screen {
                stamps.write(cu_identity::GRANTED_SCREEN, &ident.cdhash);
            }
            if access {
                stamps.write(cu_identity::GRANTED_AX, &ident.cdhash);
            }
        }
        let mut fix = serde_json::Map::new();
        if !screen {
            fix.insert(
                "screen_recording".to_string(),
                cu_identity::fix_copy(Service::ScreenRecording, stale_screen, ident).into(),
            );
        }
        if !access {
            fix.insert(
                "accessibility".to_string(),
                cu_identity::fix_copy(Service::Accessibility, stale_ax, ident).into(),
            );
        }
        let mut report = serde_json::json!({
            "platform": "macos",
            "screen_recording": {
                "granted": screen,
                "required_for": "capturing the screen (screenshot / vision)",
            },
            "accessibility": {
                "granted": access,
                "required_for": "controlling the mouse and keyboard",
            },
            "all_granted": screen && access,
            "actuation_path": "host",
            "process_trusted": {
                "screen_recording": preflight,
                "accessibility": access,
            },
            "identity": ident,
            "stale_grant_suspected": {
                "screen_recording": stale_screen,
                "accessibility": stale_ax,
            },
            "unbundled_dev_binary": !ident.bundled,
            "fix": fix,
            "reset_supported": true,
            "host_caveat": cu_identity::HOST_CAVEAT,
        });
        if let Some(err) = reset_error {
            report["reset_error"] = serde_json::Value::String(err.to_string());
        }
        report.to_string() + "\n"
    }

    /// The stamp store, once the host has a data dir (always, after setup).
    fn stamps() -> Option<Stamps> {
        super::DATA_DIR.get().map(|dir| Stamps::new(dir.as_path()))
    }

    fn stale_now(stamps: &Option<Stamps>, name: &str, granted_now: bool) -> bool {
        let seen = stamps.as_ref().and_then(|s| s.read(name));
        cu_identity::stale_grant(seen.as_deref(), &cu_identity::identity().cdhash, granted_now)
    }

    fn screen_is_capable() -> bool {
        if SCREEN_CAPABLE.load(Ordering::Relaxed) {
            return true;
        }
        // Do not call CGWindowListCreateImage here. A probe capture is what
        // re-shows the Screen Recording dialog on every check_permissions.
        cg_preflight()
    }

    fn maybe_request_tcc() {
        // Prompt once per build (on-disk stamp), never for a stale grant —
        // the dialog cannot repair a row pinned to an older build; the
        // pane's reset can. Grok's request=true every turn must not loop.
        let cdhash = &cu_identity::identity().cdhash;
        let stamps = stamps();
        if !screen_is_capable()
            && !stale_now(&stamps, cu_identity::GRANTED_SCREEN, false)
            && first_prompt(&stamps, cu_identity::PROMPTED_SCREEN, cdhash, &SCREEN_PROMPTED)
        {
            unsafe {
                CGRequestScreenCaptureAccess();
            }
        }
        if !ax_trusted()
            && !stale_now(&stamps, cu_identity::GRANTED_AX, false)
            && first_prompt(&stamps, cu_identity::PROMPTED_AX, cdhash, &AX_PROMPTED)
        {
            request_ax();
        }
    }

    /// True the first time this build asks for `name`. The in-process flag
    /// is the fallback when the stamp dir is unwritable.
    fn first_prompt(
        stamps: &Option<Stamps>,
        name: &str,
        cdhash: &str,
        in_process: &AtomicBool,
    ) -> bool {
        if in_process.swap(true, Ordering::SeqCst) {
            return false;
        }
        let Some(stamps) = stamps else {
            return true;
        };
        if !cdhash.is_empty() && stamps.read(name).as_deref() == Some(cdhash) {
            return false;
        }
        stamps.write(name, cdhash);
        true
    }

    /// `tccutil reset` both services, forget the stamps, and let the next
    /// `permissions request` prompt again. Window action only, never a tool.
    fn reset_tcc_grants() -> Option<String> {
        let error = cu_identity::reset_tcc_grants();
        if let Some(stamps) = stamps() {
            stamps.clear();
        }
        SCREEN_CAPABLE.store(false, Ordering::Relaxed);
        SCREEN_PROMPTED.store(false, Ordering::SeqCst);
        AX_PROMPTED.store(false, Ordering::SeqCst);
        error
    }

    fn cg_preflight() -> bool {
        unsafe { CGPreflightScreenCaptureAccess() != 0 }
    }

    fn ax_trusted() -> bool {
        unsafe { AXIsProcessTrusted() != 0 }
    }

    fn request_ax() {
        unsafe {
            let key = CFStringCreateWithCString(
                ptr::null_mut(),
                c"AXTrustedCheckOptionPrompt".as_ptr().cast(),
                K_CF_STRING_ENCODING_UTF8,
            );
            if key.is_null() {
                let _ = AXIsProcessTrusted();
                return;
            }
            let keys = [key as *const c_void];
            let values = [kCFBooleanTrue];
            let dict = CFDictionaryCreate(
                ptr::null_mut(),
                keys.as_ptr(),
                values.as_ptr(),
                1,
                &kCFTypeDictionaryKeyCallBacks,
                &kCFTypeDictionaryValueCallBacks,
            );
            if !dict.is_null() {
                AXIsProcessTrustedWithOptions(dict);
                CFRelease(dict);
            }
            CFRelease(key);
        }
    }

    fn mouse_move(x: f64, y: f64) -> bool {
        ax_trusted() && post_mouse(K_CG_EVENT_MOUSE_MOVED, x, y, K_CG_MOUSE_BUTTON_LEFT, 0)
    }

    fn mouse_click(x: f64, y: f64, button: &str, count: i32) -> bool {
        if !ax_trusted() {
            return false;
        }
        let (down, up, btn) = if button == "right" {
            (
                K_CG_EVENT_RIGHT_MOUSE_DOWN,
                K_CG_EVENT_RIGHT_MOUSE_UP,
                K_CG_MOUSE_BUTTON_RIGHT,
            )
        } else {
            (
                K_CG_EVENT_LEFT_MOUSE_DOWN,
                K_CG_EVENT_LEFT_MOUSE_UP,
                K_CG_MOUSE_BUTTON_LEFT,
            )
        };
        let n = count.clamp(1, 3);
        for i in 0..n {
            if !post_mouse(down, x, y, btn, i + 1) {
                return false;
            }
            if !post_mouse(up, x, y, btn, i + 1) {
                return false;
            }
        }
        true
    }

    fn post_mouse(kind: u32, x: f64, y: f64, button: u32, click: i32) -> bool {
        unsafe {
            let event = CGEventCreateMouseEvent(ptr::null_mut(), kind, CGPoint { x, y }, button);
            if event.is_null() {
                return false;
            }
            if click != 0 {
                CGEventSetIntegerValueField(event, K_CG_MOUSE_EVENT_CLICK_STATE, i64::from(click));
            }
            CGEventPost(K_CG_HID_EVENT_TAP, event);
            CFRelease(event);
            true
        }
    }

    fn parse_combo(combo: &str) -> Option<(u64, u16)> {
        let parts: Vec<&str> = combo
            .split('+')
            .map(str::trim)
            .filter(|p| !p.is_empty())
            .collect();
        if parts.is_empty() {
            return None;
        }
        let (base, modifiers) = parts.split_last()?;
        let mut flags = 0_u64;
        for modifier in modifiers {
            let found = MODS
                .iter()
                .find(|(name, _)| *name == modifier.to_ascii_lowercase())
                .map(|(_, bit)| *bit);
            flags |= found?;
        }
        let code = keycode(&base.to_ascii_lowercase())?;
        Some((flags, code))
    }

    fn press_combo(combo: &str) -> bool {
        if !ax_trusted() {
            return false;
        }
        let Some((flags, code)) = parse_combo(combo) else {
            return false;
        };
        unsafe {
            for pressed in [1_u8, 0_u8] {
                let event = CGEventCreateKeyboardEvent(ptr::null_mut(), code, pressed);
                if event.is_null() {
                    return false;
                }
                if flags != 0 {
                    CGEventSetFlags(event, flags);
                }
                CGEventPost(K_CG_HID_EVENT_TAP, event);
                CFRelease(event);
            }
        }
        true
    }

    fn scroll(dx: i32, dy: i32) -> bool {
        if !ax_trusted() {
            return false;
        }
        unsafe {
            let event = CGEventCreateScrollWheelEvent(
                ptr::null_mut(),
                K_CG_SCROLL_EVENT_UNIT_LINE,
                2,
                dy,
                dx,
            );
            if event.is_null() {
                return false;
            }
            CGEventPost(K_CG_HID_EVENT_TAP, event);
            CFRelease(event);
            true
        }
    }

    fn type_text(text: &str) -> bool {
        if !ax_trusted() {
            return false;
        }
        unsafe {
            for ch in text.chars() {
                let mut buf = [0u16; 2];
                let units = ch.encode_utf16(&mut buf);
                let n = units.len().max(1) as u32;
                for pressed in [1_u8, 0_u8] {
                    let event = CGEventCreateKeyboardEvent(ptr::null_mut(), 0, pressed);
                    if event.is_null() {
                        return false;
                    }
                    CGEventKeyboardSetUnicodeString(event, n, units.as_ptr());
                    CGEventPost(K_CG_HID_EVENT_TAP, event);
                    CFRelease(event);
                }
            }
        }
        true
    }

    fn capture_png(x: i32, y: i32, w: i32, h: i32) -> Option<Vec<u8>> {
        if w <= 0 || h <= 0 {
            return None;
        }
        unsafe {
            let image = CGWindowListCreateImage(
                CGRect {
                    origin: CGPoint {
                        x: f64::from(x),
                        y: f64::from(y),
                    },
                    size: CGSize {
                        width: f64::from(w),
                        height: f64::from(h),
                    },
                },
                K_CG_WINDOW_LIST_OPTION_ON_SCREEN_ONLY,
                0,
                0,
            );
            if image.is_null() {
                return None;
            }
            if CGImageGetWidth(image) < 1 || CGImageGetHeight(image) < 1 {
                CFRelease(image);
                return None;
            }
            let uti = CFStringCreateWithCString(
                ptr::null_mut(),
                c"public.png".as_ptr().cast(),
                K_CF_STRING_ENCODING_UTF8,
            );
            let data = CFDataCreateMutable(ptr::null_mut(), 0);
            let dest = if !data.is_null() && !uti.is_null() {
                CGImageDestinationCreateWithData(data, uti, 1, ptr::null_mut())
            } else {
                ptr::null_mut()
            };
            let png = if dest.is_null() {
                None
            } else {
                CGImageDestinationAddImage(dest, image, ptr::null_mut());
                if CGImageDestinationFinalize(dest) == 0 {
                    None
                } else {
                    let length = CFDataGetLength(data);
                    let ptr = CFDataGetBytePtr(data);
                    if ptr.is_null() || length < 8 {
                        None
                    } else {
                        let slice = std::slice::from_raw_parts(ptr, length as usize);
                        if slice.starts_with(b"\x89PNG") {
                            Some(slice.to_vec())
                        } else {
                            None
                        }
                    }
                }
            };
            if !dest.is_null() {
                CFRelease(dest);
            }
            if !data.is_null() {
                CFRelease(data);
            }
            if !uti.is_null() {
                CFRelease(uti);
            }
            CFRelease(image);
            png
        }
    }
}

#[cfg(test)]
mod tests {
    use super::{parse_line, sock_path, Command};
    use std::path::Path;

    #[test]
    fn sock_path_joins_data_dir() {
        assert_eq!(
            sock_path(Path::new("/tmp/tst-data")),
            Path::new("/tmp/tst-data/cu-agent.sock")
        );
    }

    #[test]
    fn parse_quit_and_permissions() {
        assert_eq!(parse_line("quit"), Command::Quit);
        assert_eq!(parse_line(""), Command::Quit);
        assert_eq!(
            parse_line("permissions"),
            Command::Permissions {
                request: false,
                reset: false
            }
        );
        assert_eq!(
            parse_line("permissions request"),
            Command::Permissions {
                request: true,
                reset: false
            }
        );
        // Reset implies re-request.
        assert_eq!(
            parse_line("permissions reset"),
            Command::Permissions {
                request: true,
                reset: true
            }
        );
    }

    #[test]
    fn parse_input_commands() {
        assert_eq!(
            parse_line("capture 0 1 10 20"),
            Command::Capture {
                x: 0,
                y: 1,
                w: 10,
                h: 20
            }
        );
        assert_eq!(parse_line("move 1.5 2"), Command::Move { x: 1.5, y: 2.0 });
        assert_eq!(
            parse_line("click 3 4 right 2"),
            Command::Click {
                x: 3.0,
                y: 4.0,
                button: "right",
                count: 2
            }
        );
        assert_eq!(
            parse_line("click 3 4"),
            Command::Click {
                x: 3.0,
                y: 4.0,
                button: "left",
                count: 1
            }
        );
        assert_eq!(
            parse_line("key cmd+space"),
            Command::Key { combo: "cmd+space" }
        );
        assert_eq!(parse_line("scroll 1 -3"), Command::Scroll { dx: 1, dy: -3 });
        assert_eq!(
            parse_line("type aGVsbG8="),
            Command::Type { b64: "aGVsbG8=" }
        );
        assert_eq!(parse_line("nope"), Command::Unknown);
        assert_eq!(
            parse_line("json {\"op\":\"ui_snapshot\",\"pid\":1}"),
            Command::Json("{\"op\":\"ui_snapshot\",\"pid\":1}")
        );
    }
}
