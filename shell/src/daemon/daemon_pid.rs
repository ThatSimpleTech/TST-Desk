//! Process-tree helpers for daemon supervision (TD-1304).
//!
//! PyInstaller's `--onefile` sidecar is a bootloader that spawns the real
//! daemon as a child. The port file names the listener (`os.getpid()`), not
//! the bootloader the host spawned. Matching on exact equality then times
//! out, restarts, and leaves the grandchild listening.
//!
//! The host therefore accepts a port-file pid that is the spawned child
//! *or a live descendant of it*, and reaps the whole process group.

use std::collections::HashSet;

/// True when `candidate` is `spawned` or a descendant of it.
///
/// `parent_of` is injected so the walk is unit-testable without a real
/// process tree. Cycles and a missing parent stop the walk (refuse).
pub fn pid_is_spawned_or_descendant(
    spawned: u32,
    candidate: u32,
    parent_of: impl Fn(u32) -> Option<u32>,
) -> bool {
    if spawned == 0 || candidate == 0 {
        return false;
    }
    if candidate == spawned {
        return true;
    }
    let mut seen = HashSet::new();
    let mut cur = candidate;
    while seen.insert(cur) {
        match parent_of(cur) {
            Some(p) if p == spawned => return true,
            Some(p) if p == 0 || p == cur => return false,
            Some(p) => cur = p,
            None => return false,
        }
    }
    false
}

/// Parent pid of `pid`, or `None` if it cannot be read (dead, or no
/// platform support). Used by [`port_file_belongs_to_spawn`].
pub fn parent_pid(pid: u32) -> Option<u32> {
    parent_pid_impl(pid)
}

#[cfg(unix)]
fn parent_pid_impl(pid: u32) -> Option<u32> {
    let output = std::process::Command::new("ps")
        .args(["-o", "ppid=", "-p", &pid.to_string()])
        .output()
        .ok()?;
    if !output.status.success() {
        return None;
    }
    let parsed = String::from_utf8_lossy(&output.stdout)
        .trim()
        .parse::<u32>()
        .ok()?;
    (parsed > 0).then_some(parsed)
}

/// First integer token in `text` that is greater than 0.
///
/// CIM stdout is a bare number. The old WMIC shape (`ParentProcessId`
/// header, then the value) is still accepted so a mixed-host install
/// cannot fail closed on leftover WMIC-shaped text.
#[cfg(any(windows, test))]
fn parse_parent_pid_output(text: &str) -> Option<u32> {
    for token in text.split_whitespace() {
        if let Ok(parsed) = token.parse::<u32>() {
            return (parsed > 0).then_some(parsed);
        }
    }
    None
}

#[cfg(windows)]
fn hide_window(cmd: &mut std::process::Command) {
    use std::os::windows::process::CommandExt;
    const CREATE_NO_WINDOW: u32 = 0x08000000;
    cmd.creation_flags(CREATE_NO_WINDOW);
}

#[cfg(windows)]
fn parent_pid_impl(pid: u32) -> Option<u32> {
    // WMIC is deprecated and removed as an on-demand feature on
    // Windows 11 24H2 / newer Server. PowerShell CIM is the
    // no-dependency replacement. Fail closed on anything we don't parse.
    let mut cmd = std::process::Command::new("powershell");
    cmd.args([
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        &format!("(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}').ParentProcessId"),
    ]);
    hide_window(&mut cmd);
    let output = cmd.output().ok()?;
    if !output.status.success() {
        return None;
    }
    parse_parent_pid_output(&String::from_utf8_lossy(&output.stdout))
}

#[cfg(not(any(unix, windows)))]
fn parent_pid_impl(_pid: u32) -> Option<u32> {
    None
}

/// Whether a port-file pid is the process the host spawned, or a
/// descendant of it (the PyInstaller onefile shape).
pub fn port_file_belongs_to_spawn(spawned_pid: u32, file_pid: u32) -> bool {
    pid_is_spawned_or_descendant(spawned_pid, file_pid, parent_pid)
}

/// True when `pid` is a live process. Used to attach to a leftover
/// `tstd` instead of spawning a second one (TD-2902).
pub fn pid_is_alive(pid: u32) -> bool {
    if pid == 0 {
        return false;
    }
    pid_is_alive_impl(pid)
}

#[cfg(unix)]
fn pid_is_alive_impl(pid: u32) -> bool {
    std::process::Command::new("kill")
        .args(["-0", &pid.to_string()])
        .stderr(std::process::Stdio::null())
        .status()
        .map(|s| s.success())
        .unwrap_or(false)
}

#[cfg(windows)]
fn pid_is_alive_impl(pid: u32) -> bool {
    let mut cmd = std::process::Command::new("tasklist");
    cmd.args(["/FI", &format!("PID eq {pid}"), "/NH"]);
    hide_window(&mut cmd);
    let output = cmd.output();
    let Ok(output) = output else {
        return false;
    };
    if !output.status.success() {
        return false;
    }
    let text = String::from_utf8_lossy(&output.stdout);
    let pid_s = pid.to_string();
    text.split_whitespace().any(|tok| tok == pid_s)
}

#[cfg(not(any(unix, windows)))]
fn pid_is_alive_impl(_pid: u32) -> bool {
    false
}

/// Put the child in its own process group so a later group-kill reaps
/// bootloader and grandchild together. No-op on Windows — [`kill_spawned_group`]
/// uses `taskkill /T` there instead.
pub fn apply_process_group(cmd: &mut tokio::process::Command) {
    #[cfg(unix)]
    {
        cmd.process_group(0);
    }
    let _ = cmd;
}

/// Kill the spawned tree. On Unix the leader is the process-group id
/// (we set `process_group(0)` at spawn). On Windows, `taskkill /T` walks
/// the tree. Missing processes are fine — this is a backstop.
pub fn kill_spawned_group(leader_pid: u32) {
    if leader_pid == 0 {
        return;
    }
    #[cfg(unix)]
    {
        let _ = std::process::Command::new("kill")
            .args(["-KILL", &format!("-{leader_pid}")])
            .status();
    }
    #[cfg(windows)]
    {
        let mut cmd = std::process::Command::new("taskkill");
        cmd.args(["/PID", &leader_pid.to_string(), "/T", "/F"]);
        hide_window(&mut cmd);
        let _ = cmd.status();
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashMap;

    fn parents(pairs: &[(u32, u32)]) -> impl Fn(u32) -> Option<u32> + '_ {
        let map: HashMap<u32, u32> = pairs.iter().copied().collect();
        move |pid| map.get(&pid).copied()
    }

    #[test]
    fn exact_pid_matches() {
        assert!(pid_is_spawned_or_descendant(7, 7, |_| None));
    }

    #[test]
    fn zero_never_matches() {
        assert!(!pid_is_spawned_or_descendant(0, 7, |_| None));
        assert!(!pid_is_spawned_or_descendant(7, 0, |_| None));
    }

    #[test]
    fn direct_child_matches() {
        // bootloader 10, real daemon 11
        assert!(pid_is_spawned_or_descendant(10, 11, parents(&[(11, 10)])));
    }

    #[test]
    fn grandchild_matches() {
        assert!(pid_is_spawned_or_descendant(
            10,
            12,
            parents(&[(12, 11), (11, 10)]),
        ));
    }

    #[test]
    fn unrelated_pid_does_not_match() {
        assert!(!pid_is_spawned_or_descendant(
            10,
            99,
            parents(&[(99, 1), (11, 10)]),
        ));
    }

    #[test]
    fn missing_parent_refuses() {
        assert!(!pid_is_spawned_or_descendant(10, 11, |_| None));
    }

    #[test]
    fn cycle_refuses() {
        assert!(!pid_is_spawned_or_descendant(
            10,
            11,
            parents(&[(11, 12), (12, 11)]),
        ));
    }

    #[test]
    fn windows_parent_lookup_is_cim_not_wmic() {
        // TD-1304 / TD-1406: the Windows impl is cfg-gated, so Linux CI
        // cannot execute it. This pins the command the host will run.
        let src = include_str!("daemon_pid.rs");
        assert!(
            src.contains("Get-CimInstance Win32_Process"),
            "Windows parent_pid must use PowerShell CIM",
        );
        assert!(
            !src.contains("Command::new(\"wmic\")"),
            "WMIC is deprecated and must not be invoked",
        );
    }

    #[test]
    fn parse_parent_pid_output_table() {
        let cases: &[(&str, Option<u32>)] = &[
            ("123\n", Some(123)),
            ("ParentProcessId\n456\n", Some(456)),
            ("", None),
            ("ParentProcessId\n", None),
            ("0\n", None),
            ("not-a-number\n", None),
        ];
        for (text, expected) in cases {
            assert_eq!(
                parse_parent_pid_output(text),
                *expected,
                "parse_parent_pid_output({text:?})",
            );
        }
    }

    #[test]
    fn live_self_is_descendant_of_its_parent() {
        let me = std::process::id();
        let Some(parent) = parent_pid(me) else {
            return;
        };
        assert!(pid_is_spawned_or_descendant(parent, me, parent_pid));
        assert!(!pid_is_spawned_or_descendant(me, parent, parent_pid));
        assert!(port_file_belongs_to_spawn(me, me));
        assert!(port_file_belongs_to_spawn(parent, me));
        assert!(pid_is_alive(me));
        assert!(!pid_is_alive(0));
    }
}
