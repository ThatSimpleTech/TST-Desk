//! The machine-wide computer-use stop-file (TD-5223).
//!
//! `tst-cu-mcp` checks this file before every input event, so every client
//! (TST Desk's own daemon and any outside agent) halts while it exists. The
//! host reads it too and refuses actuation on `cu-agent.sock`. The path is
//! resolved exactly as `tst_cu_mcp.safety.stop_file_path` resolves it:
//!
//! 1. `killswitch.stop_file` in the tst-cu-mcp config
//!    (`$TST_CU_MCP_CONFIG`, else `~/.tst-cu-mcp/config.yaml`),
//! 2. `$TST_CU_MCP_STOP_FILE`,
//! 3. `~/.tst-cu-mcp/STOP`.
//!
//! `TST_CU_MCP_STOP` set to a truthy value also counts as engaged, as it
//! does for tst-cu-mcp.

// The live readers run on the macOS host today; TD-5224's shortcut uses
// them on every platform.
#![cfg_attr(not(target_os = "macos"), allow(dead_code))]

use std::path::{Path, PathBuf};

pub const CONFIG_ENV: &str = "TST_CU_MCP_CONFIG";
pub const STOP_FILE_ENV: &str = "TST_CU_MCP_STOP_FILE";
pub const STOP_ENV: &str = "TST_CU_MCP_STOP";

/// The inputs to path resolution, so tests never read the real home.
#[derive(Debug, Clone, Default)]
pub struct Sources {
    pub config_env: Option<String>,
    pub stop_file_env: Option<String>,
    pub stop_env: Option<String>,
    pub home: Option<PathBuf>,
}

impl Sources {
    /// The live process environment and home directory.
    pub fn live() -> Self {
        Self {
            config_env: non_empty(std::env::var(CONFIG_ENV).ok()),
            stop_file_env: non_empty(std::env::var(STOP_FILE_ENV).ok()),
            stop_env: std::env::var(STOP_ENV).ok(),
            home: dirs::home_dir(),
        }
    }

    fn config_path(&self) -> Option<PathBuf> {
        if let Some(path) = &self.config_env {
            return Some(PathBuf::from(path));
        }
        self.home
            .as_ref()
            .map(|home| home.join(".tst-cu-mcp").join("config.yaml"))
    }
}

fn non_empty(value: Option<String>) -> Option<String> {
    value.filter(|v| !v.trim().is_empty())
}

/// `killswitch.stop_file` from a tst-cu-mcp config document, if set.
fn configured_stop_file(config_text: &str) -> Option<String> {
    let doc: serde_yml::Value = serde_yml::from_str(config_text).ok()?;
    let value = doc.get("killswitch")?.get("stop_file")?.as_str()?;
    non_empty(Some(value.to_string()))
}

/// Where the stop-file lives for these sources.
pub fn resolve(sources: &Sources) -> Option<PathBuf> {
    if let Some(config) = sources.config_path() {
        if let Ok(text) = std::fs::read_to_string(&config) {
            if let Some(path) = configured_stop_file(&text) {
                return Some(PathBuf::from(path));
            }
        }
    }
    if let Some(path) = &sources.stop_file_env {
        return Some(PathBuf::from(path));
    }
    sources
        .home
        .as_ref()
        .map(|home| home.join(".tst-cu-mcp").join("STOP"))
}

fn truthy(value: Option<&str>) -> bool {
    matches!(
        value.map(|v| v.trim().to_ascii_lowercase()).as_deref(),
        Some("1" | "true" | "yes" | "on")
    )
}

/// True while actuation must be refused for these sources.
pub fn engaged_for(sources: &Sources) -> bool {
    if truthy(sources.stop_env.as_deref()) {
        return true;
    }
    resolve(sources).is_some_and(|path| path.exists())
}

/// True while actuation must be refused (live environment).
pub fn engaged() -> bool {
    engaged_for(&Sources::live())
}

/// Create the stop-file (and its folder). Engage only: nothing here resumes.
pub fn engage_at(path: &Path) -> std::io::Result<()> {
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(path)
        .map(|_| ())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicU32, Ordering};

    static N: AtomicU32 = AtomicU32::new(0);

    /// A fresh temp "home" per test; never the real one.
    fn temp_home() -> PathBuf {
        let dir = std::env::temp_dir().join(format!(
            "tst-cu-stop-{}-{}",
            std::process::id(),
            N.fetch_add(1, Ordering::Relaxed)
        ));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    fn sources(home: &Path) -> Sources {
        Sources {
            home: Some(home.to_path_buf()),
            ..Sources::default()
        }
    }

    #[test]
    fn default_path_is_under_home() {
        let home = temp_home();
        assert_eq!(
            resolve(&sources(&home)),
            Some(home.join(".tst-cu-mcp").join("STOP"))
        );
    }

    #[test]
    fn env_path_beats_default() {
        let home = temp_home();
        let mut s = sources(&home);
        s.stop_file_env = Some(home.join("elsewhere").display().to_string());
        assert_eq!(resolve(&s), Some(home.join("elsewhere")));
    }

    #[test]
    fn config_stop_file_beats_env() {
        let home = temp_home();
        let config = home.join("cfg.yaml");
        std::fs::write(
            &config,
            format!(
                "actuation:\n  enabled: true\nkillswitch:\n  stop_file: {}\n",
                home.join("from-config").display()
            ),
        )
        .unwrap();
        let s = Sources {
            config_env: Some(config.display().to_string()),
            stop_file_env: Some(home.join("from-env").display().to_string()),
            ..sources(&home)
        };
        assert_eq!(resolve(&s), Some(home.join("from-config")));
    }

    #[test]
    fn engaged_follows_the_file_and_the_env_flag() {
        let home = temp_home();
        let s = sources(&home);
        assert!(!engaged_for(&s));
        let path = resolve(&s).unwrap();
        engage_at(&path).unwrap();
        assert!(engaged_for(&s));
        std::fs::remove_file(&path).unwrap();
        assert!(!engaged_for(&s));
        let flagged = Sources {
            stop_env: Some("yes".into()),
            ..sources(&home)
        };
        assert!(engaged_for(&flagged));
    }
}
