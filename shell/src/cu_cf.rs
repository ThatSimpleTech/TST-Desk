//! Core Foundation reads for the host's accessibility tools (TD-5225).
//!
//! Accessibility attributes are loosely typed. `AXValue` is a string on a
//! text field, a number on a checkbox or slider, and an AXValueRef on a
//! scroll area. Reading every value as a CFString aborted the whole app on
//! the first checkbox, so each read checks the CF type first.
//!
//! Items taken out of a CFArray are retained: the array owns them, and
//! releasing it would free elements the tree walk is still reading.

#![cfg(target_os = "macos")]

use std::ffi::{c_char, c_void, CStr, CString};
use std::ptr;

const K_CF_STRING_ENCODING_UTF8: u32 = 0x0800_0100;
// i32, matching the declaration in cu_identity.rs (the extern must agree).
const K_CF_NUMBER_DOUBLE_TYPE: i32 = 13;

#[link(name = "CoreFoundation", kind = "framework")]
extern "C" {
    fn CFRetain(cf: *const c_void) -> *const c_void;
    fn CFRelease(cf: *const c_void);
    fn CFGetTypeID(cf: *const c_void) -> usize;
    fn CFStringGetTypeID() -> usize;
    fn CFNumberGetTypeID() -> usize;
    fn CFBooleanGetTypeID() -> usize;
    fn CFArrayGetTypeID() -> usize;
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
    fn CFNumberGetValue(number: *const c_void, the_type: i32, value_ptr: *mut c_void) -> u8;
    fn CFBooleanGetValue(boolean: *const c_void) -> u8;
    fn CFArrayGetCount(the_array: *const c_void) -> isize;
    fn CFArrayGetValueAtIndex(the_array: *const c_void, idx: isize) -> *const c_void;
    static kCFBooleanTrue: *const c_void;
}

/// One reference to a CF object, released on drop.
pub struct Owned(*const c_void);

impl Owned {
    /// Adopt a +1 reference (a Create or Copy result). `None` for null.
    ///
    /// # Safety
    /// `cf` must be null or a CF object whose reference the caller owns.
    pub unsafe fn adopt(cf: *const c_void) -> Option<Self> {
        (!cf.is_null()).then_some(Self(cf))
    }

    /// Retain a borrowed reference so it outlives its container.
    ///
    /// # Safety
    /// `cf` must be null or a live CF object.
    pub unsafe fn retain(cf: *const c_void) -> Option<Self> {
        if cf.is_null() {
            return None;
        }
        CFRetain(cf);
        Some(Self(cf))
    }

    pub fn as_ptr(&self) -> *mut c_void {
        self.0.cast_mut()
    }
}

impl Drop for Owned {
    fn drop(&mut self) {
        // SAFETY: an Owned always holds exactly one reference it took.
        unsafe { CFRelease(self.0) }
    }
}

pub fn cfstring(s: &str) -> Option<Owned> {
    let c = CString::new(s).ok()?;
    // SAFETY: Create returns a +1 reference or null.
    unsafe {
        Owned::adopt(CFStringCreateWithCString(
            ptr::null_mut(),
            c.as_ptr(),
            K_CF_STRING_ENCODING_UTF8,
        ))
    }
}

pub fn boolean_true() -> *const c_void {
    // SAFETY: a CoreFoundation constant that lives for the process.
    unsafe { kCFBooleanTrue }
}

/// Text for an attribute value: strings as-is, numbers and booleans
/// written out, anything else (AXValueRef, arrays, elements) empty.
///
/// # Safety
/// `cf` must be null or a live CF object.
pub unsafe fn text(cf: *const c_void) -> String {
    if cf.is_null() {
        return String::new();
    }
    let type_id = CFGetTypeID(cf);
    if type_id == CFStringGetTypeID() {
        string_of(cf)
    } else if type_id == CFNumberGetTypeID() {
        number_of(cf).map_or_else(String::new, format_number)
    } else if type_id == CFBooleanGetTypeID() {
        (CFBooleanGetValue(cf) != 0).to_string()
    } else {
        String::new()
    }
}

/// A boolean attribute: CFBoolean, or a number where nonzero is true
/// (checkbox values). `None` for any other type.
///
/// # Safety
/// `cf` must be null or a live CF object.
pub unsafe fn flag(cf: *const c_void) -> Option<bool> {
    if cf.is_null() {
        return None;
    }
    let type_id = CFGetTypeID(cf);
    if type_id == CFBooleanGetTypeID() {
        Some(CFBooleanGetValue(cf) != 0)
    } else if type_id == CFNumberGetTypeID() {
        number_of(cf).map(|n| n != 0.0)
    } else {
        None
    }
}

/// The items of a CFArray, each retained. Empty when `cf` is not an array.
///
/// # Safety
/// `cf` must be null or a live CF object.
pub unsafe fn items(cf: *const c_void) -> Vec<Owned> {
    if cf.is_null() || CFGetTypeID(cf) != CFArrayGetTypeID() {
        return Vec::new();
    }
    let count = CFArrayGetCount(cf).max(0);
    (0..count)
        .filter_map(|idx| Owned::retain(CFArrayGetValueAtIndex(cf, idx)))
        .collect()
}

unsafe fn string_of(cf: *const c_void) -> String {
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

unsafe fn number_of(cf: *const c_void) -> Option<f64> {
    let mut value = 0.0_f64;
    // A lossy conversion returns false but still stores the nearest double,
    // which is close enough to show.
    CFNumberGetValue(cf, K_CF_NUMBER_DOUBLE_TYPE, ptr::addr_of_mut!(value).cast());
    value.is_finite().then_some(value)
}

fn format_number(n: f64) -> String {
    if n.fract() == 0.0 && n.abs() < 1e15 {
        format!("{}", n as i64)
    } else {
        n.to_string()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[link(name = "CoreFoundation", kind = "framework")]
    extern "C" {
        fn CFNumberCreate(
            alloc: *const c_void,
            the_type: i32,
            value_ptr: *const c_void,
        ) -> *const c_void;
        fn CFArrayCreate(
            alloc: *const c_void,
            values: *const *const c_void,
            num_values: isize,
            callbacks: *const c_void,
        ) -> *const c_void;
        fn CFStringCreateMutable(alloc: *const c_void, max_length: isize) -> *const c_void;
        fn CFStringAppendCString(s: *const c_void, c_str: *const c_char, encoding: u32);
        fn CFGetRetainCount(cf: *const c_void) -> isize;
        static kCFTypeArrayCallBacks: c_void;
        static kCFBooleanFalse: *const c_void;
    }

    fn number(n: f64) -> Owned {
        unsafe {
            Owned::adopt(CFNumberCreate(
                ptr::null(),
                K_CF_NUMBER_DOUBLE_TYPE,
                ptr::addr_of!(n).cast(),
            ))
            .unwrap()
        }
    }

    fn array(values: &[*const c_void]) -> Owned {
        unsafe {
            Owned::adopt(CFArrayCreate(
                ptr::null(),
                values.as_ptr(),
                values.len() as isize,
                ptr::addr_of!(kCFTypeArrayCallBacks),
            ))
            .unwrap()
        }
    }

    /// A heap string (not a constant or tagged pointer), so its retain
    /// count is meaningful.
    fn heap_string(text: &str) -> Owned {
        let c = CString::new(text).unwrap();
        unsafe {
            let s = Owned::adopt(CFStringCreateMutable(ptr::null(), 0)).unwrap();
            CFStringAppendCString(s.0, c.as_ptr(), K_CF_STRING_ENCODING_UTF8);
            s
        }
    }

    #[test]
    fn text_reads_strings_numbers_and_booleans() {
        let s = cfstring("Save").unwrap();
        unsafe {
            assert_eq!(text(s.0), "Save");
            assert_eq!(text(number(1.0).0), "1");
            assert_eq!(text(number(0.5).0), "0.5");
            assert_eq!(text(boolean_true()), "true");
            assert_eq!(text(kCFBooleanFalse), "false");
            assert_eq!(text(ptr::null()), "");
        }
    }

    #[test]
    fn text_of_a_non_string_value_is_empty_not_a_crash() {
        // An AXValue can be an array or an AXValueRef; reading it as a
        // string aborted the host (TD-5225).
        let inner = cfstring("x").unwrap();
        let list = array(&[inner.0]);
        unsafe {
            assert_eq!(text(list.0), "");
        }
    }

    #[test]
    fn flag_reads_booleans_and_numbers_only() {
        let s = cfstring("yes").unwrap();
        unsafe {
            assert_eq!(flag(boolean_true()), Some(true));
            assert_eq!(flag(kCFBooleanFalse), Some(false));
            assert_eq!(flag(number(1.0).0), Some(true));
            assert_eq!(flag(number(0.0).0), Some(false));
            assert_eq!(flag(s.0), None);
            assert_eq!(flag(ptr::null()), None);
        }
    }

    #[test]
    fn items_hold_their_own_reference() {
        let child = heap_string("child");
        let list = array(&[child.0]);
        drop(child); // the array is now the only owner
        unsafe {
            assert_eq!(CFGetRetainCount(CFArrayGetValueAtIndex(list.0, 0)), 1);
            let got = items(list.0);
            assert_eq!(got.len(), 1);
            assert_eq!(CFGetRetainCount(got[0].0), 2);
            drop(list);
            assert_eq!(CFGetRetainCount(got[0].0), 1);
            assert_eq!(text(got[0].0), "child");
        }
    }

    #[test]
    fn items_of_a_non_array_is_empty() {
        let s = cfstring("not a list").unwrap();
        unsafe {
            assert!(items(s.0).is_empty());
            assert!(items(ptr::null()).is_empty());
        }
    }

    #[test]
    fn whole_numbers_print_without_a_decimal_point() {
        assert_eq!(format_number(3.0), "3");
        assert_eq!(format_number(-2.0), "-2");
        assert_eq!(format_number(0.25), "0.25");
        assert_eq!(format_number(1e20), "100000000000000000000");
    }
}
