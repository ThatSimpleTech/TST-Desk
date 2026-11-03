//! Accessibility-tree snapshot and actions in the TST Desk host.
//!
//! Checkout Python is the wrong TCC identity. AXUIElement reads and
//! AXPress/AXSetValue run here so System Settings attributes them to
//! **TST Desk**. Nested tree JSON is flattened by the Python MCP layer.
//! Every CF value goes through `cu_cf`, which checks its type and owns its
//! reference (TD-5225).

#![cfg(target_os = "macos")]

use std::ffi::c_void;

use serde_json::{json, Value};

use crate::cu_cf::{self, Owned};

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
        let Some(app) = Owned::adopt(AXUIElementCreateApplication(pid)) else {
            return json!({"ok": false, "error": format!("no accessibility element for pid {pid}")});
        };
        let tree = read_app(app.as_ptr());
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
        let Some(app) = Owned::adopt(AXUIElementCreateApplication(pid)) else {
            return json!({"ok": false, "error": format!("no accessibility element for pid {pid}")});
        };
        match element_at(app.as_ptr(), id) {
            Some(el) => perform(el.as_ptr(), action, value),
            None => json!({"ok": false, "error": format!("element {id} is not in the tree")}),
        }
    }
}

unsafe fn read_app(app: *mut c_void) -> Value {
    let children: Vec<Value> = attr_list(app, "AXWindows")
        .iter()
        .take(MAX_CHILDREN)
        .map(|child| read_element(child.as_ptr(), 1))
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
            .iter()
            .take(MAX_CHILDREN)
            .map(|child| read_element(child.as_ptr(), depth + 1))
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

/// The element at a dotted path (`window.child.child…`), holding its own
/// reference so it outlives the lists it was found in.
unsafe fn element_at(app: *mut c_void, element_id: &str) -> Option<Owned> {
    let mut node: Option<Owned> = None;
    for (index, part) in element_id.split('.').enumerate() {
        let pos: usize = part.parse().ok()?;
        let parent = node.as_ref().map_or(app, Owned::as_ptr);
        let name = if index == 0 {
            "AXWindows"
        } else {
            "AXChildren"
        };
        let mut siblings = attr_list(parent, name);
        if pos >= siblings.len() {
            return None;
        }
        node = Some(siblings.swap_remove(pos));
    }
    node
}

unsafe fn perform(element: *mut c_void, action: &str, value: &str) -> Value {
    // Read first: a press can close the element or move it.
    let frame = frame(element);
    let err = match action {
        "press" => perform_named(element, "AXPress"),
        "raise" => perform_named(element, "AXRaise"),
        "show_menu" => perform_named(element, "AXShowMenu"),
        "focus" => set_value(element, "AXFocused", cu_cf::boolean_true()),
        "set_value" => match cu_cf::cfstring(value) {
            Some(text) => set_value(element, "AXValue", text.as_ptr()),
            None => -1,
        },
        other => {
            return json!({"ok": false, "error": format!("unknown ui action {other}")});
        }
    };
    if err != AX_OK {
        return json!({"ok": false, "error": format!("AX {action} refused (error {err})")});
    }
    done(action, frame)
}

/// The success reply, with the element's frame when it could be read.
fn done(action: &str, frame: Option<Value>) -> Value {
    let mut reply = json!({"ok": true, "action": action, "path": "host"});
    if let Some(frame) = frame {
        reply["frame"] = frame;
    }
    reply
}

/// Where the element is, in global top-left points (the CGEvent and
/// CGWindow space), so the caller can show the action there. Best effort:
/// `None` when either attribute is missing or not the AXValue it should be.
unsafe fn frame(element: *mut c_void) -> Option<Value> {
    let position = attr_copy(element, "AXPosition")?;
    let size = attr_copy(element, "AXSize")?;
    frame_of(position.as_ptr(), size.as_ptr())
}

unsafe fn frame_of(position: *const c_void, size: *const c_void) -> Option<Value> {
    let (x, y) = cu_cf::ax_point(position)?;
    let (width, height) = cu_cf::ax_size(size)?;
    Some(json!({"x": x, "y": y, "width": width, "height": height}))
}

unsafe fn perform_named(element: *mut c_void, name: &str) -> i32 {
    match cu_cf::cfstring(name) {
        Some(action) => AXUIElementPerformAction(element, action.as_ptr()),
        None => -1,
    }
}

unsafe fn set_value(element: *mut c_void, name: &str, value: *const c_void) -> i32 {
    match cu_cf::cfstring(name) {
        Some(attr) => AXUIElementSetAttributeValue(element, attr.as_ptr(), value),
        None => -1,
    }
}

unsafe fn attr_copy(element: *mut c_void, name: &str) -> Option<Owned> {
    let attr = cu_cf::cfstring(name)?;
    let mut out: *mut c_void = std::ptr::null_mut();
    if AXUIElementCopyAttributeValue(element, attr.as_ptr(), &mut out) != AX_OK {
        return None;
    }
    Owned::adopt(out)
}

unsafe fn attr_string(element: *mut c_void, name: &str) -> String {
    attr_copy(element, name).map_or_else(String::new, |v| cu_cf::text(v.as_ptr()))
}

unsafe fn attr_bool(element: *mut c_void, name: &str, default: bool) -> bool {
    attr_copy(element, name)
        .and_then(|v| cu_cf::flag(v.as_ptr()))
        .unwrap_or(default)
}

unsafe fn attr_list(element: *mut c_void, name: &str) -> Vec<Owned> {
    attr_copy(element, name).map_or_else(Vec::new, |v| cu_cf::items(v.as_ptr()))
}

unsafe fn action_names(element: *mut c_void) -> Vec<String> {
    let mut names_ref: *mut c_void = std::ptr::null_mut();
    let mut names: Vec<String> = Vec::new();
    if AXUIElementCopyActionNames(element, &mut names_ref) == AX_OK {
        if let Some(list) = Owned::adopt(names_ref) {
            for item in cu_cf::items(list.as_ptr()) {
                let mapped = match cu_cf::text(item.as_ptr()).as_str() {
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
        }
    }
    names.push("set_value".into());
    names.push("focus".into());
    names
}

#[cfg(test)]
mod tests {
    use super::*;

    #[link(name = "ApplicationServices", kind = "framework")]
    extern "C" {
        fn AXValueCreate(the_type: u32, value_ptr: *const c_void) -> *const c_void;
    }

    const POINT: u32 = 1;
    const SIZE: u32 = 2;

    fn ax_value(kind: u32, fields: [f64; 2]) -> Owned {
        unsafe { Owned::adopt(AXValueCreate(kind, fields.as_ptr().cast())).unwrap() }
    }

    #[test]
    fn frame_is_position_and_size_in_points() {
        let position = ax_value(POINT, [120.0, 48.5]);
        let size = ax_value(SIZE, [64.0, 22.0]);
        let got = unsafe { frame_of(position.as_ptr(), size.as_ptr()) };
        assert_eq!(
            got,
            Some(json!({"x": 120.0, "y": 48.5, "width": 64.0, "height": 22.0}))
        );
    }

    #[test]
    fn frame_is_none_when_an_attribute_is_the_wrong_type() {
        let position = ax_value(POINT, [1.0, 2.0]);
        let size = ax_value(SIZE, [3.0, 4.0]);
        let text = cu_cf::cfstring("AXSize").unwrap();
        unsafe {
            assert_eq!(frame_of(size.as_ptr(), position.as_ptr()), None);
            assert_eq!(frame_of(position.as_ptr(), text.as_ptr()), None);
            assert_eq!(frame_of(position.as_ptr(), std::ptr::null()), None);
        }
    }

    #[test]
    fn success_reply_carries_the_frame_only_when_read() {
        let frame = json!({"x": 1.0, "y": 2.0, "width": 3.0, "height": 4.0});
        let with = done("press", Some(frame.clone()));
        assert_eq!(with["frame"], frame);
        assert_eq!(with["ok"], true);
        assert_eq!(with["action"], "press");
        let without = done("focus", None);
        assert!(without.get("frame").is_none());
        assert_eq!(without["ok"], true);
    }
}
