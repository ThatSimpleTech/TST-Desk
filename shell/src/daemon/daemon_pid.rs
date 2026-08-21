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

#[cfg(windows)]
fn parent_pid_impl(pid: u32) -> Option<u32> {
    // wmic is the no-dependency parent lookup on Windows. The format is
    // a header line then the number. Fail closed on anything we don't parse.
    let output = std::process::Command::new("wmic")
        .args([
            "process",
            &format!("where processid={pid}"),
            "get",
            "parentprocessid",
        ])
        .output()
        .ok()?;
    if !output.status.success() {
        return None;
    }
    let text = String::from_utf8_lossy(&output.stdout);
    for line in text.lines() {
        let trimmed = line.trim();
        if trimmed.is_empty() || trimmed.eq_ignore_ascii_case("ParentProcessId") {
            continue;
        }
        let parsed = trimmed.parse::<u32>().ok()?;
        return (parsed > 0).then_some(parsed);
    }
    None
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
    let output = std::process::Command::new("tasklist")
        .args(["/FI", &format!("PID eq {pid}"), "/NH"])
        .output();
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
        let _ = std::process::Command::new("taskkill")
            .args(["/PID", &leader_pid.to_string(), "/T", "/F"])
            .status();
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
