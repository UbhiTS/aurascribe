use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;

use tauri::{Manager, RunEvent};

struct SidecarState(Mutex<Option<Child>>);

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        // Persists window size / position / maximized state across restarts
        // so the user doesn't have to reposition the window every launch.
        // State lives in the app's data dir (window-state.json).
        .plugin(tauri_plugin_window_state::Builder::default().build())
        .manage(SidecarState(Mutex::new(None)))
        .setup(|app| {
            match spawn_sidecar() {
                Ok(child) => {
                    app.state::<SidecarState>()
                        .0
                        .lock()
                        .expect("sidecar mutex poisoned")
                        .replace(child);
                    println!("[aurascribe] Python sidecar started");
                }
                Err(e) => {
                    eprintln!("[aurascribe] Failed to start sidecar: {e}");
                }
            }
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building tauri application")
        .run(|app_handle, event| {
            if let RunEvent::Exit = event {
                if let Some(state) = app_handle.try_state::<SidecarState>() {
                    if let Ok(mut guard) = state.0.lock() {
                        if let Some(mut child) = guard.take() {
                            let _ = child.kill();
                            let _ = child.wait();
                            println!("[aurascribe] Python sidecar stopped");
                        }
                    }
                }
            }
        });
}

fn repo_root() -> PathBuf {
    // Dev path: CARGO_MANIFEST_DIR is baked in at compile time.
    // TODO(phase 5): resolve relative to the installed app directory for prod.
    let manifest_dir = env!("CARGO_MANIFEST_DIR");
    Path::new(manifest_dir)
        .parent()
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from(manifest_dir))
}

fn spawn_sidecar() -> Result<Child, std::io::Error> {
    let root = repo_root();
    let python = root.join(".venv").join("Scripts").join("python.exe");
    let script = root.join("sidecar").join("main.py");

    if !python.exists() {
        return Err(std::io::Error::new(
            std::io::ErrorKind::NotFound,
            format!("Python venv not found at {}. Run: py -3.13 -m venv .venv && pip install -e ./sidecar", python.display()),
        ));
    }

    Command::new(&python)
        .arg(&script)
        .current_dir(&root)
        .stdout(Stdio::inherit())
        .stderr(Stdio::inherit())
        .spawn()
}
