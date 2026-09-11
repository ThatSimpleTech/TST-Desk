//! Accessibility-tree snapshot and actions in the TST Desk host.
//!
//! Checkout Python is the wrong TCC identity. AXUIElement reads and
//! AXPress/AXSetValue run here so System Settings attributes them to
//! **TST Desk**. Nested tree JSON is flattened by the Python MCP layer.

#![cfg(target_os = "macos")]

use std::ffi::{c_char, c_void, CStr, CString};
use std::ptr;

use serde_json::{json, Value};

const K_CF_STRING_ENCODING_UTF8: u32 = 0x0800_0100;
const AX_OK: i32 = 0;
const MAX_CHILDREN: usize = 200;
const MAX_DEPTH: usize = 24;

#[link(name = "ApplicationServices", kind = "framework")]
extern "C" {
    fn AXUIElementCreateApplication(pid: i32) -> *mut c_void;
    fn AXUIElementCopyAttributeValue(
        element: *mut c_void,
        attribute: *const c_void,
        value: *mut *mut c_void,
    ) -> i32;
    fn AXUIElementCopyActionNames(element: *mut c_void, names: *mut *mut c_void) -> i32;
    fn AXUIElementPerformAction(element: *mut c_void, action: *const c_void) -> i32;
    fn AXUIElementSetAttributeValue(
        element: *mut c_void,
        attribute: *const c_void,
        value: *const c_void,
    ) -> i32;
}

#[link(name = "CoreFoundation", kind = "framework")]
extern "C" {
    fn CFRelease(cf: *const c_void);
    fn CFStringCreateWithCString(
        alloc: *mut c_void,
        c_str: *const c_char,
        encoding: u32,
    ) -> *mut c_void;
    fn CFStringGetLength(the_string: *const c_void) -> isize;
    fn CFStringGetCString(
        the_string: *const c_void,
        buffer: *mut c_char,
        buffer_size: isize,
        encoding: u32,
    ) -> u8;
    fn CFArrayGetCount(the_array: *const c_void) -> isize;
    fn CFArrayGetValueAtIndex(the_array: *const c_void, idx: isize) -> *const c_void;
    fn CFBooleanGetValue(boolean: *const c_void) -> u8;
    static kCFBooleanTrue: *const c_void;
}

pub fn handle(body: &Value) -> Value {
    let op = body.get("op").and_then(Value::as_str).unwrap_or("");
    match op {
        "ui_snapshot" => snapshot(body),
        "ui_act" => act(body),
        _ => json!({"ok": false, "error": format!("unknown op {op}")}),
    }
}

fn snapshot(body: &Value) -> Value {
    let pid = body.get("pid").and_then(Value::as_i64).unwrap_or(0) as i32;
    if pid <= 0 {
        return json!({"ok": false, "error": "pid must be a positive process id"});
    }
    unsafe {
        let app = AXUIElementCreateApplication(pid);
        if app.is_null() {
            return json!({"ok": false, "error": format!("no accessibility element for pid {pid}")});
        }
        let tree = read_app(app);
        CFRelease(app);
        json!({"ok": true, "pid": pid, "tree": tree, "path": "host"})
    }
}

fn act(body: &Value) -> Value {
    let pid = body.get("pid").and_then(Value::as_i64).unwrap_or(0) as i32;
    let id = body.get("id").and_then(Value::as_str).unwrap_or("");
    let action = body.get("action").and_then(Value::as_str).unwrap_or("");
    let value = body.get("value").and_then(Value::as_str).unwrap_or("");
    if pid <= 0 {
        return json!({"ok": false, "error": "pid must be a positive process id"});
    }
    if id.is_empty() {
        return json!({"ok": false, "error": "element id is empty"});
    }
    unsafe {
        let app = AXUIElementCreateApplication(pid);
        if app.is_null() {
            return json!({"ok": false, "error": format!("no accessibility element for pid {pid}")});
        }
        let result = match element_at(app, id) {
            Some(el) => perform(el, action, value),
            None => json!({"ok": false, "error": format!("element {id} is not in the tree")}),
        };
        CFRelease(app);
        result
    }
}

unsafe fn read_app(app: *mut c_void) -> Value {
    let windows = attr_list(app, "AXWindows");
    let children: Vec<Value> = windows
        .into_iter()
        .take(MAX_CHILDREN)
        .map(|child| read_element(child as *mut c_void, 1))
        .collect();
    json!({
        "role": "AXApplication",
        "title": attr_string(app, "AXTitle"),
        "value": "",
        "description": "",
        "enabled": true,
        "focused": false,
        "actions": ["raise"],
        "children": children,
    })
}

unsafe fn read_element(element: *mut c_void, depth: usize) -> Value {
    let children = if depth >= MAX_DEPTH {
        Vec::new()
    } else {
        attr_list(element, "AXChildren")
            .into_iter()
            .take(MAX_CHILDREN)
            .map(|child| read_element(child as *mut c_void, depth + 1))
            .collect()
    };
    let mut description = attr_string(element, "AXDescription");
    if description.is_empty() {
        description = attr_string(element, "AXHelp");
    }
    json!({
        "role": attr_string(element, "AXRole"),
        "title": attr_string(element, "AXTitle"),
        "value": attr_string(element, "AXValue"),
        "description": description,
        "enabled": attr_bool(element, "AXEnabled", true),
        "focused": attr_bool(element, "AXFocused", false),
        "actions": action_names(element),
        "children": children,
    })
}

unsafe fn element_at(app: *mut c_void, element_id: &str) -> Option<*mut c_void> {
    let mut node = app;
    for (index, part) in element_id.split('.').enumerate() {
        let pos: usize = part.parse().ok()?;
        let siblings = if index == 0 {
            attr_list(node, "AXWindows")
        } else {
            attr_list(node, "AXChildren")
        };
        node = *siblings.get(pos)? as *mut c_void;
        if node.is_null() {
            return None;
        }
    }
    Some(node)
}

unsafe fn perform(element: *mut c_void, action: &str, value: &str) -> Value {
    let err = match action {
        "press" => perform_named(element, "AXPress"),
        "raise" => perform_named(element, "AXRaise"),
        "show_menu" => perform_named(element, "AXShowMenu"),
        "focus" => set_bool(element, "AXFocused"),
        "set_value" => set_string(element, "AXValue", value),
        other => {
            return json!({"ok": false, "error": format!("unknown ui action {other}")});
        }
    };
    if err != AX_OK {
        return json!({"ok": false, "error": format!("AX {action} refused (error {err})")});
    }
    json!({"ok": true, "action": action, "path": "host"})
}

unsafe fn perform_named(element: *mut c_void, name: &str) -> i32 {
    let cf = cfstring(name);
    if cf.is_null() {
        return -1;
    }
    let err = AXUIElementPerformAction(element, cf);
    CFRelease(cf);
    err
}

unsafe fn set_string(element: *mut c_void, name: &str, value: &str) -> i32 {
    let attr = cfstring(name);
    let cf_val = cfstring(value);
    if attr.is_null() || cf_val.is_null() {
        if !attr.is_null() {
            CFRelease(attr);
        }
        if !cf_val.is_null() {
            CFRelease(cf_val);
        }
        return -1;
    }
    let err = AXUIElementSetAttributeValue(element, attr, cf_val);
    CFRelease(attr);
    CFRelease(cf_val);
    err
}

unsafe fn set_bool(element: *mut c_void, name: &str) -> i32 {
    let attr = cfstring(name);
    if attr.is_null() {
        return -1;
    }
    let err = AXUIElementSetAttributeValue(element, attr, kCFBooleanTrue);
    CFRelease(attr);
    err
}

unsafe fn attr_copy(element: *mut c_void, name: &str) -> *mut c_void {
    let attr = cfstring(name);
    if attr.is_null() {
        return ptr::null_mut();
    }
    let mut out: *mut c_void = ptr::null_mut();
    let err = AXUIElementCopyAttributeValue(element, attr, &mut out);
    CFRelease(attr);
    if err != AX_OK {
        return ptr::null_mut();
    }
    out
}

unsafe fn attr_string(element: *mut c_void, name: &str) -> String {
    let out = attr_copy(element, name);
    if out.is_null() {
        return String::new();
    }
    let text = cf_to_string(out);
    CFRelease(out);
    text
}

unsafe fn attr_bool(element: *mut c_void, name: &str, default: bool) -> bool {
    let out = attr_copy(element, name);
    if out.is_null() {
        return default;
    }
    let value = CFBooleanGetValue(out) != 0;
    CFRelease(out);
    value
}

unsafe fn attr_list(element: *mut c_void, name: &str) -> Vec<*const c_void> {
    let out = attr_copy(element, name);
    if out.is_null() {
        return Vec::new();
    }
    let count = CFArrayGetCount(out);
    let mut items = Vec::new();
    if count > 0 {
        for idx in 0..count {
            let item = CFArrayGetValueAtIndex(out, idx);
            if !item.is_null() {
                items.push(item);
            }
        }
    }
    CFRelease(out);
    items
}

unsafe fn action_names(element: *mut c_void) -> Vec<String> {
    let mut names_ref: *mut c_void = ptr::null_mut();
    let err = AXUIElementCopyActionNames(element, &mut names_ref);
    let mut names: Vec<String> = Vec::new();
    if err == AX_OK && !names_ref.is_null() {
        let count = CFArrayGetCount(names_ref);
        for idx in 0..count {
            let item = CFArrayGetValueAtIndex(names_ref, idx);
            let token = cf_to_string(item);
            let mapped = match token.as_str() {
                "AXPress" | "AXConfirm" => Some("press"),
                "AXRaise" => Some("raise"),
                "AXShowMenu" => Some("show_menu"),
                _ => None,
            };
            if let Some(name) = mapped {
                if !names.iter().any(|existing| existing == name) {
                    names.push(name.to_string());
                }
            }
        }
        CFRelease(names_ref);
    }
    names.push("set_value".into());
    names.push("focus".into());
    names
}

fn cfstring(s: &str) -> *mut c_void {
    let Ok(c) = CString::new(s) else {
        return ptr::null_mut();
    };
    unsafe { CFStringCreateWithCString(ptr::null_mut(), c.as_ptr(), K_CF_STRING_ENCODING_UTF8) }
}

unsafe fn cf_to_string(cf: *const c_void) -> String {
    if cf.is_null() {
        return String::new();
    }
    let len = CFStringGetLength(cf);
    if len < 0 {
        return String::new();
    }
    let cap = (len as usize).saturating_mul(4).saturating_add(1);
    let mut buf = vec![0u8; cap];
    let ok = CFStringGetCString(
        cf,
        buf.as_mut_ptr().cast::<c_char>(),
        cap as isize,
        K_CF_STRING_ENCODING_UTF8,
    );
    if ok == 0 {
        return String::new();
    }
    CStr::from_ptr(buf.as_ptr().cast::<c_char>())
        .to_string_lossy()
        .into_owned()
}
