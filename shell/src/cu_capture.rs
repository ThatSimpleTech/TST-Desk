//! Screen capture that leaves out agent-desktop's cursor windows (TD-5227).
//!
//! agent-desktop draws each agent's cursor in ordinary, capturable windows.
//! A plain on-screen capture would show those cursors to the model, and one
//! agent's cursor in another agent's screenshot. When such a window is on
//! screen, the image is composed from the on-screen window list without
//! them; otherwise the plain capture runs unchanged.

/// The window ids a capture draws: every window in `windows` (id and owner
/// name, front to back) except agent-desktop's, in the same order.
#[cfg_attr(not(target_os = "macos"), allow(dead_code))]
pub fn kept_windows(windows: &[(u32, String)]) -> Vec<u32> {
    const EXCLUDED_OWNER: &str = "agent-desktop";
    windows
        .iter()
        .filter(|(_, owner)| owner != EXCLUDED_OWNER)
        .map(|(id, _)| *id)
        .collect()
}

#[cfg(target_os = "macos")]
pub use macos::screen_image;

#[cfg(target_os = "macos")]
mod macos {
    use std::collections::HashMap;
    use std::ffi::c_void;
    use std::ptr;

    use super::kept_windows;
    use crate::cu_cf::{self, Owned};

    const K_CG_WINDOW_LIST_OPTION_ON_SCREEN_ONLY: u32 = 1;
    const K_CG_NULL_WINDOW_ID: u32 = 0;
    const K_CG_WINDOW_IMAGE_DEFAULT: u32 = 0;

    #[repr(C)]
    #[derive(Clone, Copy)]
    struct CGPoint {
        x: f64,
        y: f64,
    }

    #[repr(C)]
    #[derive(Clone, Copy)]
    struct CGSize {
        width: f64,
        height: f64,
    }

    #[repr(C)]
    #[derive(Clone, Copy)]
    struct CGRect {
        origin: CGPoint,
        size: CGSize,
    }

    #[link(name = "CoreGraphics", kind = "framework")]
    extern "C" {
        fn CGWindowListCreate(option: u32, relative_to_window: u32) -> *const c_void;
        fn CGWindowListCreateDescriptionFromArray(window_array: *const c_void) -> *const c_void;
        fn CGWindowListCreateImage(
            rect: CGRect,
            list_option: u32,
            window_id: u32,
            image_option: u32,
        ) -> *mut c_void;
        fn CGWindowListCreateImageFromArray(
            rect: CGRect,
            window_array: *const c_void,
            image_option: u32,
        ) -> *mut c_void;
        static kCGWindowNumber: *const c_void;
        static kCGWindowOwnerName: *const c_void;
    }

    #[link(name = "CoreFoundation", kind = "framework")]
    extern "C" {
        fn CFGetTypeID(cf: *const c_void) -> usize;
        fn CFArrayGetTypeID() -> usize;
        fn CFDictionaryGetTypeID() -> usize;
        fn CFArrayGetCount(the_array: *const c_void) -> isize;
        fn CFArrayGetValueAtIndex(the_array: *const c_void, idx: isize) -> *const c_void;
        fn CFArrayCreate(
            alloc: *const c_void,
            values: *const *const c_void,
            num_values: isize,
            callbacks: *const c_void,
        ) -> *const c_void;
        fn CFDictionaryGetValue(dict: *const c_void, key: *const c_void) -> *const c_void;
    }

    /// The on-screen pixels of a rect in global top-left points, as a
    /// CGImage, with agent-desktop's windows left out.
    pub fn screen_image(x: f64, y: f64, width: f64, height: f64) -> Option<Owned> {
        let rect = CGRect {
            origin: CGPoint { x, y },
            size: CGSize { width, height },
        };
        // SAFETY: each call gets live CF objects or null, and every Create
        // result is adopted exactly once.
        unsafe {
            if let Some(kept) = kept_on_screen() {
                let image = id_array(&kept).and_then(|ids| {
                    Owned::adopt(CGWindowListCreateImageFromArray(
                        rect,
                        ids.as_ptr(),
                        K_CG_WINDOW_IMAGE_DEFAULT,
                    ))
                });
                if image.is_some() {
                    return image;
                }
            }
            Owned::adopt(CGWindowListCreateImage(
                rect,
                K_CG_WINDOW_LIST_OPTION_ON_SCREEN_ONLY,
                K_CG_NULL_WINDOW_ID,
                K_CG_WINDOW_IMAGE_DEFAULT,
            ))
        }
    }

    /// The window ids to draw, front to back, or `None` when no
    /// agent-desktop window is on screen: the plain capture is then the
    /// same picture and cheaper.
    unsafe fn kept_on_screen() -> Option<Vec<u32>> {
        let ids = Owned::adopt(CGWindowListCreate(
            K_CG_WINDOW_LIST_OPTION_ON_SCREEN_ONLY,
            K_CG_NULL_WINDOW_ID,
        ))?;
        let windows = owners(ids.as_ptr());
        let kept = kept_windows(&windows);
        (kept.len() < windows.len()).then_some(kept)
    }

    /// (id, owner name) for each window of a CGWindowList id array, in the
    /// array's order. A window with no description (it closed meanwhile)
    /// gets an empty owner, so it is kept.
    unsafe fn owners(ids: *const c_void) -> Vec<(u32, String)> {
        let mut names: HashMap<u32, String> = HashMap::new();
        if let Some(described) = Owned::adopt(CGWindowListCreateDescriptionFromArray(ids)) {
            for entry in cu_cf::items(described.as_ptr()) {
                let entry = entry.as_ptr();
                if CFGetTypeID(entry) != CFDictionaryGetTypeID() {
                    continue;
                }
                let number = cu_cf::number_value(CFDictionaryGetValue(entry, kCGWindowNumber));
                if let Some(id) = number.and_then(window_id) {
                    let owner = CFDictionaryGetValue(entry, kCGWindowOwnerName);
                    names.insert(id, cu_cf::text(owner));
                }
            }
        }
        window_ids(ids)
            .into_iter()
            .map(|id| (id, names.remove(&id).unwrap_or_default()))
            .collect()
    }

    /// The ids in a CGWindowList array. Its values are CGWindowIDs stored as
    /// pointer-sized integers, not CF objects, so nothing is retained.
    unsafe fn window_ids(ids: *const c_void) -> Vec<u32> {
        if ids.is_null() || CFGetTypeID(ids) != CFArrayGetTypeID() {
            return Vec::new();
        }
        (0..CFArrayGetCount(ids).max(0))
            .filter_map(|idx| u32::try_from(CFArrayGetValueAtIndex(ids, idx) as usize).ok())
            .collect()
    }

    /// The same array shape CGWindowListCreate returns. NULL callbacks: the
    /// values are integers, not objects to retain or release.
    unsafe fn id_array(ids: &[u32]) -> Option<Owned> {
        let values: Vec<*const c_void> =
            ids.iter().map(|&id| id as usize as *const c_void).collect();
        Owned::adopt(CFArrayCreate(
            ptr::null(),
            values.as_ptr(),
            values.len() as isize,
            ptr::null(),
        ))
    }

    fn window_id(number: f64) -> Option<u32> {
        let whole = number.fract() == 0.0 && (0.0..=f64::from(u32::MAX)).contains(&number);
        whole.then_some(number as u32)
    }

    #[cfg(test)]
    mod tests {
        use super::*;

        #[test]
        fn window_ids_survive_the_pointer_sized_array() {
            let ids = [3, 10_238, u32::MAX];
            let array = unsafe { id_array(&ids) }.unwrap();
            assert_eq!(unsafe { window_ids(array.as_ptr()) }, ids);
        }

        #[test]
        fn window_ids_of_a_non_array_is_empty() {
            let text = cu_cf::cfstring("not a list").unwrap();
            unsafe {
                assert!(window_ids(text.as_ptr()).is_empty());
                assert!(window_ids(ptr::null()).is_empty());
            }
        }

        #[test]
        fn window_numbers_must_be_whole_u32s() {
            assert_eq!(window_id(42.0), Some(42));
            assert_eq!(window_id(f64::from(u32::MAX)), Some(u32::MAX));
            assert_eq!(window_id(1.5), None);
            assert_eq!(window_id(-1.0), None);
            assert_eq!(window_id(f64::from(u32::MAX) + 1.0), None);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::kept_windows;

    fn listing(rows: &[(u32, &str)]) -> Vec<(u32, String)> {
        rows.iter()
            .map(|(id, owner)| (*id, owner.to_string()))
            .collect()
    }

    #[test]
    fn agent_desktop_windows_are_left_out_in_order() {
        let windows = listing(&[
            (91, "agent-desktop"),
            (40, "Finder"),
            (92, "agent-desktop"),
            (12, "TST Desk"),
            (3, "Window Server"),
        ]);
        assert_eq!(kept_windows(&windows), vec![40, 12, 3]);
    }

    #[test]
    fn only_the_exact_owner_name_is_left_out() {
        let windows = listing(&[
            (1, "agent-desktop-helper"),
            (2, "Agent-Desktop"),
            (3, " agent-desktop"),
            (4, ""),
        ]);
        assert_eq!(kept_windows(&windows), vec![1, 2, 3, 4]);
    }

    #[test]
    fn nothing_to_leave_out_keeps_every_window() {
        let windows = listing(&[(5, "Safari"), (6, "Dock")]);
        assert_eq!(kept_windows(&windows), vec![5, 6]);
        assert!(kept_windows(&[]).is_empty());
    }
}
