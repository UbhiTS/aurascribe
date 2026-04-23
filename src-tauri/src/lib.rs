use std::fs::OpenOptions;
use std::io::Write;
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;

use tauri::{AppHandle, Manager, RunEvent};

struct SidecarState(Mutex<Option<Child>>);

/// Panic hook: writes a crash dump to the shared log dir the Python
/// sidecar uses, then falls through to the default hook (stderr output).
///
/// Rust panics in a GUI binary are otherwise invisible — the process just
/// exits silently. Having a dated file on disk gives the user something to
/// attach to a bug report.
fn install_panic_hook() {
    let default_hook = std::panic::take_hook();
    std::panic::set_hook(Box::new(move |panic_info| {
        if let Err(e) = write_panic_file(panic_info) {
            eprintln!("[aurascribe] could not write panic file: {e}");
        }
        default_hook(panic_info);
    }));
}

fn write_panic_file(info: &std::panic::PanicHookInfo<'_>) -> std::io::Result<()> {
    let logs_dir = logs_dir();
    std::fs::create_dir_all(&logs_dir)?;
    let stamp = chrono_ish_now();
    let path = logs_dir.join(format!("crash-{stamp}-rust.log"));
    let mut f = OpenOptions::new().create(true).write(true).truncate(true).open(path)?;
    writeln!(f, "AuraScribe Rust shell panic")?;
    writeln!(f, "payload: {info}")?;
    if let Some(location) = info.location() {
        writeln!(
            f, "location: {}:{}:{}",
            location.file(), location.line(), location.column()
        )?;
    }
    Ok(())
}

/// Platform-appropriate log directory — mirrors the logic in Python's config.py
/// so crash dumps from both the Rust shell and Python sidecar land together.
///
///   Windows → %APPDATA%\AuraScribe\logs
///   macOS   → ~/Library/Application Support/AuraScribe/logs
///   Linux   → $XDG_DATA_HOME/AuraScribe/logs  (falls back to ~/.local/share)
fn logs_dir() -> PathBuf {
    #[cfg(target_os = "windows")]
    {
        let base = std::env::var("APPDATA")
            .map(PathBuf::from)
            .unwrap_or_else(|_| PathBuf::from("."));
        base.join("AuraScribe").join("logs")
    }
    #[cfg(target_os = "macos")]
    {
        let home = std::env::var("HOME")
            .map(PathBuf::from)
            .unwrap_or_else(|_| PathBuf::from("."));
        home.join("Library")
            .join("Application Support")
            .join("AuraScribe")
            .join("logs")
    }
    #[cfg(not(any(target_os = "windows", target_os = "macos")))]
    {
        let base = std::env::var("XDG_DATA_HOME")
            .map(PathBuf::from)
            .unwrap_or_else(|_| {
                std::env::var("HOME")
                    .map(|h| PathBuf::from(h).join(".local").join("share"))
                    .unwrap_or_else(|_| PathBuf::from("."))
            });
        base.join("AuraScribe").join("logs")
    }
}

/// Basic timestamp string without pulling in a date crate — YYYYMMDD-HHMMSS
/// from SystemTime. Good enough to make filenames unique.
fn chrono_ish_now() -> String {
    use std::time::{SystemTime, UNIX_EPOCH};
    let secs = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);
    // Plain "unix-seconds" is enough for uniqueness + chronological sort.
    format!("{secs}")
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    install_panic_hook();

    let app = match tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        // Persists window size / position / maximized state across restarts
        // so the user doesn't have to reposition the window every launch.
        // State lives in the app's data dir (window-state.json).
        .plugin(tauri_plugin_window_state::Builder::default().build())
        .manage(SidecarState(Mutex::new(None)))
        .setup(|app| {
            match spawn_sidecar(app.handle()) {
                Ok(child) => {
                    if let Ok(mut guard) = app.state::<SidecarState>().0.lock() {
                        guard.replace(child);
                    }
                    println!("[aurascribe] Python sidecar started");
                }
                Err(e) => {
                    // Fatal: the app is useless without the sidecar. Surface
                    // the real reason so the user knows what to fix (missing
                    // runtime, broken install, permissions), then exit cleanly
                    // rather than presenting a half-dead window.
                    show_fatal_error(
                        "AuraScribe — startup failed",
                        &format!(
                            "The Python sidecar could not be started:\n\n{e}\n\n\
                             AuraScribe can't run without it. Try reinstalling; \
                             if the problem persists, contact support."
                        ),
                    );
                    std::process::exit(1);
                }
            }
            Ok(())
        })
        .build(tauri::generate_context!())
    {
        Ok(app) => app,
        Err(e) => {
            show_fatal_error(
                "AuraScribe — startup failed",
                &format!("Tauri failed to initialise:\n\n{e}"),
            );
            std::process::exit(1);
        }
    };

    app.run(|app_handle, event| {
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

/// Synchronous native error dialog for pre-window failures.
/// Also mirrors to stderr so headless/CI runs still capture the reason.
fn show_fatal_error(title: &str, message: &str) {
    eprintln!("[aurascribe] FATAL {title}\n{message}");
    rfd::MessageDialog::new()
        .set_title(title)
        .set_description(message)
        .set_level(rfd::MessageLevel::Error)
        .show();
}

/// Resolved locations for starting the sidecar.
///
/// * Dev build: we run `.venv/Scripts/python.exe sidecar/main.py` against
///   the checked-out repo.
/// * Release build: the sidecar is a standalone PyInstaller bundle sitting
///   next to the installed app's resources, so we invoke its `.exe`
///   directly — no interpreter needed on the customer's machine.
struct SidecarLaunch {
    /// Interpreter to invoke. `None` in release where the sidecar is a
    /// self-contained bundle.
    python: Option<PathBuf>,
    /// Either the .py entry point (dev) or the bundled .exe (release).
    target: PathBuf,
    /// Working directory for the child process. Keeps relative paths inside
    /// the sidecar (bundled prompt files, etc.) resolvable.
    cwd: PathBuf,
}

fn resolve_sidecar_launch(app: &AppHandle) -> Result<SidecarLaunch, String> {
    if cfg!(debug_assertions) {
        let manifest_dir = env!("CARGO_MANIFEST_DIR");
        let root = PathBuf::from(manifest_dir)
            .parent()
            .ok_or_else(|| "Could not resolve repo root from CARGO_MANIFEST_DIR".to_string())?
            .to_path_buf();
        // Platform-specific venv layout:
        //   Windows → .venv\Scripts\python.exe
        //   macOS / Linux → .venv/bin/python3
        let python = if cfg!(windows) {
            root.join(".venv").join("Scripts").join("python.exe")
        } else {
            root.join(".venv").join("bin").join("python3")
        };
        Ok(SidecarLaunch {
            python: Some(python),
            target: root.join("sidecar").join("main.py"),
            cwd: root,
        })
    } else {
        // tauri.conf.json ships `sidecar/dist/aurascribe-sidecar/**/*` under
        // `bundle.resources`, so PyInstaller's onedir output lands here at
        // `resources/aurascribe-sidecar/`.
        let resources = app
            .path()
            .resource_dir()
            .map_err(|e| format!("Could not resolve resource dir: {e}"))?;
        let bundle_dir = resources.join("aurascribe-sidecar");
        let exe_name = if cfg!(windows) {
            "aurascribe-sidecar.exe"
        } else {
            "aurascribe-sidecar"
        };
        Ok(SidecarLaunch {
            python: None,
            target: bundle_dir.join(exe_name),
            cwd: bundle_dir,
        })
    }
}

fn spawn_sidecar(app: &AppHandle) -> Result<Child, String> {
    let launch = resolve_sidecar_launch(app)?;

    if let Some(py) = launch.python.as_ref() {
        if !py.exists() {
            let setup_cmd = if cfg!(windows) {
                "py -3.13 -m venv .venv && .venv\\Scripts\\pip install -e ./sidecar[all]"
            } else {
                "python3.13 -m venv .venv && .venv/bin/pip install -e ./sidecar[all]"
            };
            return Err(format!(
                "Python interpreter not found at {}.\n\nDev setup:\n  {}",
                py.display(),
                setup_cmd,
            ));
        }
    }
    if !launch.target.exists() {
        return Err(format!(
            "Sidecar entry point not found at {}.\n\n\
             Run `npm run build:sidecar` to produce the bundled sidecar before packaging.",
            launch.target.display()
        ));
    }

    // Clear any orphan sidecars from a prior crashed/force-killed run before
    // spawning our own. Otherwise the oldest zombie keeps port 8765 and the
    // frontend happily talks to stale code without the user ever knowing.
    kill_orphan_sidecars();

    let mut cmd = if let Some(py) = launch.python {
        let mut c = Command::new(py);
        c.arg(&launch.target);
        c
    } else {
        Command::new(&launch.target)
    };

    cmd.current_dir(&launch.cwd)
        .stdout(Stdio::inherit())
        .stderr(Stdio::inherit())
        .spawn()
        .map_err(|e| format!("Could not spawn sidecar process: {e}"))
}

/// Scan the OS process table and terminate every live process that looks
/// like an AuraScribe sidecar — matching either the bundled binary
/// (`aurascribe-sidecar[.exe]`) or a Python interpreter running
/// `sidecar/main.py`. Called before each fresh spawn so a crashed shell's
/// zombies can't linger on 8765–8774 and confuse the frontend proxy.
///
/// Best-effort: a failure to kill (permissions, already exited) is logged
/// and ignored. Never blocks startup on errors.
fn kill_orphan_sidecars() {
    use sysinfo::{Pid, ProcessesToUpdate, System};

    let mut sys = System::new();
    sys.refresh_processes(ProcessesToUpdate::All, true);
    let self_pid = Pid::from_u32(std::process::id());

    let mut killed = 0usize;
    for (pid, proc) in sys.processes() {
        if *pid == self_pid {
            continue;
        }
        let name = proc.name().to_string_lossy().to_lowercase();
        let cmdline = proc
            .cmd()
            .iter()
            .map(|s| s.to_string_lossy().into_owned())
            .collect::<Vec<_>>()
            .join(" ")
            .to_lowercase();

        // The bundled release binary is named aurascribe-sidecar[.exe];
        // in dev mode it's python.exe running a path ending in
        // sidecar/main.py (or sidecar\main.py on Windows).
        let looks_like_sidecar = name.contains("aurascribe-sidecar")
            || cmdline.contains("aurascribe-sidecar")
            || cmdline.contains("sidecar/main.py")
            || cmdline.contains("sidecar\\main.py");

        if looks_like_sidecar {
            if proc.kill() {
                killed += 1;
                eprintln!(
                    "[aurascribe] killed orphan sidecar pid={} name={}",
                    pid, name
                );
            } else {
                eprintln!(
                    "[aurascribe] could not kill orphan sidecar pid={} name={}",
                    pid, name
                );
            }
        }
    }

    if killed > 0 {
        // TCP sockets land in TIME_WAIT briefly after a process exits.
        // Give them a beat so the fresh sidecar binds to 8765 cleanly
        // instead of falling through to 8766/8767/...
        std::thread::sleep(std::time::Duration::from_millis(300));
    }
}
