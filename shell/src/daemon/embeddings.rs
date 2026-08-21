//! Optional embeddings sidecar supervision (TD-2204).
//!
//! Parallel to `tstd` supervision and **not** on that restart budget.
//! Spawn is gated on `embeddings.command` in the user `config.yaml`.
//! A filled `base_url` alone is attach-only — the Python client POSTs
//! there, and a down sidecar is heading-match (TD-2201), not a daemon
//! crash.

use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::sync::atomic::{AtomicBool, AtomicI32, Ordering};
use std::sync::Arc;
use std::time::Duration;

use tokio::sync::Notify;

use super::daemon_pid::{apply_process_group, kill_spawned_group};

/// Own restart cap. Must stay independent of [`super::MAX_RESTARTS`].
pub const MAX_EMBEDDINGS_RESTARTS: u32 = 3;

type YamlMap = serde_yaml::Mapping;

/// Handle the app uses to shut down and reap the embeddings child.
#[derive(Clone)]
pub struct EmbeddingsHandle {
    data_dir: PathBuf,
    shutdown: Arc<AtomicBool>,
    shutdown_notify: Arc<Notify>,
    child_pid: Arc<AtomicI32>,
    finished: Arc<AtomicBool>,
    done: Arc<Notify>,
}

impl EmbeddingsHandle {
    pub fn new(data_dir: PathBuf) -> Self {
        Self {
            data_dir,
            shutdown: Arc::new(AtomicBool::new(false)),
            shutdown_notify: Arc::new(Notify::new()),
            child_pid: Arc::new(AtomicI32::new(-1)),
            finished: Arc::new(AtomicBool::new(false)),
            done: Arc::new(Notify::new()),
        }
    }

    /// PID of the live sidecar child, if the host spawned one.
    pub fn child_pid(&self) -> Option<u32> {
        let pid = self.child_pid.load(Ordering::Relaxed);
        (pid > 0).then_some(pid as u32)
    }

    pub fn request_shutdown(&self) {
        self.shutdown.store(true, Ordering::SeqCst);
        self.shutdown_notify.notify_waiters();
    }

    pub async fn wait_for_done(&self) {
        let notified = self.done.notified();
        tokio::pin!(notified);
        if self.finished.load(Ordering::SeqCst) {
            return;
        }
        notified.await;
    }
}

/// Spawn the embeddings supervisor for the life of the app.
pub fn start(data_dir: PathBuf) -> EmbeddingsHandle {
    let handle = EmbeddingsHandle::new(data_dir);
    let loop_handle = handle.clone();
    tauri::async_runtime::spawn(async move {
        run_supervision(loop_handle).await;
    });
    handle
}

/// Synchronous kill for `RunEvent::Exit`, same group-kill as `tstd`.
pub fn best_effort_kill(handle: &EmbeddingsHandle) {
    if let Some(pid) = handle.child_pid() {
        kill_spawned_group(pid);
    }
}

/// Supervise until quit. Death is logged; `tstd` is never restarted here.
pub async fn run_supervision(h: EmbeddingsHandle) {
    let Some(argv) = load_embeddings_argv(&h.data_dir.join("config.yaml")) else {
        log::info!("embeddings: no command configured; attach-only");
        finish(&h);
        return;
    };

    let mut restart: u32 = 0;
    loop {
        if h.shutdown.load(Ordering::SeqCst) {
            break;
        }

        let mut child = match spawn_embeddings(&argv) {
            Ok(c) => c,
            Err(e) => {
                log::error!("failed to spawn embeddings sidecar: {e}");
                if !bump_restart(&mut restart).await {
                    break;
                }
                continue;
            }
        };
        let Some(pid) = child.id() else {
            log::warn!("embeddings sidecar exited before handing over its pid");
            let _ = child.wait().await;
            if !bump_restart(&mut restart).await {
                break;
            }
            continue;
        };
        h.child_pid.store(pid as i32, Ordering::Relaxed);

        let shutdown_req = h.shutdown_notify.clone();
        let shutdown_wait = async {
            while !h.shutdown.load(Ordering::SeqCst) {
                shutdown_req.notified().await;
            }
        };
        tokio::pin!(shutdown_wait);
        tokio::select! {
            res = child.wait() => {
                log::warn!(
                    "embeddings sidecar exited ({res:?}); daemon is unchanged"
                );
                kill_spawned_group(pid);
                h.child_pid.store(-1, Ordering::Relaxed);
                if h.shutdown.load(Ordering::SeqCst) {
                    break;
                }
                if !bump_restart(&mut restart).await {
                    break;
                }
            }
            _ = &mut shutdown_wait => {
                reap_tree(&mut child, pid).await;
                h.child_pid.store(-1, Ordering::Relaxed);
                break;
            }
        }
    }

    finish(&h);
}

fn finish(h: &EmbeddingsHandle) {
    h.child_pid.store(-1, Ordering::Relaxed);
    h.finished.store(true, Ordering::SeqCst);
    h.done.notify_waiters();
}

async fn bump_restart(restart: &mut u32) -> bool {
    *restart += 1;
    if *restart > MAX_EMBEDDINGS_RESTARTS {
        log::error!(
            "embeddings sidecar kept dying; giving up after {MAX_EMBEDDINGS_RESTARTS} restarts (daemon unchanged)"
        );
        return false;
    }
    tokio::time::sleep(crash_backoff(*restart)).await;
    true
}

fn crash_backoff(restart: u32) -> Duration {
    Duration::from_millis(500 * restart as u64)
}

/// Read `embeddings.command` from a config file. Missing file, missing
/// key, empty value, or a `base_url` with no command → `None`.
pub fn load_embeddings_argv(path: &Path) -> Option<Vec<String>> {
    let raw = match std::fs::read_to_string(path) {
        Ok(s) => s,
        Err(_) => return None,
    };
    embeddings_argv_from_yaml(&raw)
}

/// Parse `embeddings.command` from a YAML body. Never looks at `base_url`.
pub fn embeddings_argv_from_yaml(text: &str) -> Option<Vec<String>> {
    let root: serde_yaml::Value = serde_yaml::from_str(text).ok()?;
    let map = root.as_mapping()?;
    let embeddings = map.get(serde_yaml::Value::from("embeddings"))?;
    let section = embeddings.as_mapping()?;
    let command = section.get(serde_yaml::Value::from("command"))?;
    argv_from_command(command)
}

fn argv_from_command(value: &serde_yaml::Value) -> Option<Vec<String>> {
    match value {
        serde_yaml::Value::String(s) => split_command_string(s),
        serde_yaml::Value::Sequence(items) => {
            let argv: Vec<String> = items.iter().filter_map(yaml_arg).collect();
            nonempty_argv(argv)
        }
        serde_yaml::Value::Null => None,
        _ => None,
    }
}

fn yaml_arg(value: &serde_yaml::Value) -> Option<String> {
    match value {
        serde_yaml::Value::String(s) => {
            let trimmed = s.trim();
            (!trimmed.is_empty()).then(|| trimmed.to_string())
        }
        serde_yaml::Value::Number(n) => Some(n.to_string()),
        serde_yaml::Value::Bool(b) => Some(b.to_string()),
        _ => None,
    }
}

fn split_command_string(s: &str) -> Option<Vec<String>> {
    nonempty_argv(s.split_whitespace().map(str::to_string).collect())
}

fn nonempty_argv(argv: Vec<String>) -> Option<Vec<String>> {
    if argv.is_empty() || argv[0].is_empty() {
        None
    } else {
        Some(argv)
    }
}

/// Spawn the configured argv. Callers must not pass an empty command —
/// [`embeddings_argv_from_yaml`] already dropped those so we never
/// `Command::new` on attach-only.
pub fn spawn_embeddings(argv: &[String]) -> Result<tokio::process::Child, String> {
    let binary = argv
        .first()
        .map(|s| s.trim())
        .filter(|s| !s.is_empty())
        .ok_or_else(|| "embeddings command is empty".to_string())?;
    let mut cmd = tokio::process::Command::new(binary);
    cmd.args(&argv[1..])
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    apply_process_group(&mut cmd);
    cmd.spawn()
        .map_err(|e| format!("embeddings spawn failed: {e}"))
}

async fn reap_tree(child: &mut tokio::process::Child, spawned_pid: u32) {
    kill_spawned_group(spawned_pid);
    let _ = child.wait().await;
}

/// True when `text` has an embeddings mapping but no spawnable command.
/// Used by tests to prove a packaged-style `base_url` is attach-only.
pub fn yaml_is_attach_only(text: &str) -> bool {
    let Ok(root) = serde_yaml::from_str::<serde_yaml::Value>(text) else {
        return true;
    };
    let Some(map) = root.as_mapping() else {
        return true;
    };
    if !has_embeddings_section(map) {
        return true;
    }
    embeddings_argv_from_yaml(text).is_none()
}

fn has_embeddings_section(map: &YamlMap) -> bool {
    matches!(
        map.get(serde_yaml::Value::from("embeddings")),
        Some(serde_yaml::Value::Mapping(_))
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn backoff_is_independent_scale() {
        assert_eq!(crash_backoff(1), Duration::from_millis(500));
        assert_eq!(crash_backoff(3), Duration::from_millis(1500));
        assert_ne!(MAX_EMBEDDINGS_RESTARTS, 0);
    }

    #[test]
    fn missing_embeddings_is_attach_only() {
        assert_eq!(embeddings_argv_from_yaml("active_preset: local\n"), None);
        assert!(yaml_is_attach_only("active_preset: local\n"));
    }

    #[test]
    fn base_url_alone_does_not_spawn() {
        let text = "embeddings:\n  base_url: http://127.0.0.1:8080/v1\n  model: nomic-embed-text\n";
        assert_eq!(embeddings_argv_from_yaml(text), None);
        assert!(yaml_is_attach_only(text));
    }

    #[test]
    fn empty_command_string_is_attach_only() {
        assert_eq!(
            embeddings_argv_from_yaml("embeddings:\n  command: \"\"\n"),
            None
        );
        assert_eq!(
            embeddings_argv_from_yaml("embeddings:\n  command: []\n"),
            None
        );
    }

    #[test]
    fn string_command_splits_on_whitespace() {
        assert_eq!(
            embeddings_argv_from_yaml("embeddings:\n  command: llama-server --embeddings\n"),
            Some(vec!["llama-server".to_string(), "--embeddings".to_string()])
        );
    }

    #[test]
    fn list_command_keeps_args_and_stringifies_ports() {
        let text = "embeddings:\n  command: [llama-server, --port, 8080]\n";
        assert_eq!(
            embeddings_argv_from_yaml(text),
            Some(vec![
                "llama-server".to_string(),
                "--port".to_string(),
                "8080".to_string()
            ])
        );
    }

    #[test]
    fn missing_file_is_attach_only() {
        assert_eq!(
            load_embeddings_argv(Path::new("/no/such/tst-desk-embeddings.yaml")),
            None
        );
    }
}
