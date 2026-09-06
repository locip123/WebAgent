use crate::supervisor::{
    bundled_sidecar_program, BackendDescriptor, BackendState, BackendStatus, Supervisor, SupervisorConfig,
    TokioSidecarLauncher,
};
use std::{
    path::PathBuf,
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc,
    },
};
use tauri::{
    menu::{Menu, MenuItem, PredefinedMenuItem, Submenu},
    AppHandle, Emitter, Manager, State, Window, WindowEvent,
};
use tokio::time::{sleep, Duration};

pub struct AppSupervisor(pub Arc<Supervisor>);

pub struct QuitState(pub AtomicBool);

#[tauri::command]
async fn sidecar_descriptor(state: State<'_, AppSupervisor>) -> Result<BackendDescriptor, String> {
    state
        .0
        .descriptor()
        .await
        .ok_or_else(|| "本地后端尚未就绪".to_owned())
}

#[tauri::command]
async fn retry_sidecar(
    app: AppHandle,
    state: State<'_, AppSupervisor>,
) -> Result<BackendDescriptor, String> {
    emit_backend_state(&app, BackendState::Restarting, None);
    match state.0.restart().await {
        Ok(descriptor) => {
            emit_backend_state(&app, BackendState::Ready, None);
            monitor_sidecar(app.clone(), state.0.clone(), 0);
            Ok(descriptor)
        }
        Err(error) => {
            emit_backend_state(&app, BackendState::Failed, Some(error.to_string()));
            Err("本地后端未能启动，请检查本地诊断后重试。".to_owned())
        }
    }
}

#[tauri::command]
async fn request_sidecar_shutdown(
    app: AppHandle,
    state: State<'_, AppSupervisor>,
) -> Result<(), String> {
    emit_backend_state(&app, BackendState::Draining, None);
    state.0.stop().await;
    emit_backend_state(&app, BackendState::Stopped, None);
    Ok(())
}

pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .setup(|app| {
            let state_dir = app.path().app_local_data_dir()?.join("sidecar");
            std::fs::create_dir_all(&state_dir)?;
            let config = if cfg!(debug_assertions) {
                SupervisorConfig::development(state_dir)
            } else {
                let program = bundled_sidecar_program(&app.path().resource_dir()?);
                SupervisorConfig::bundled(state_dir, program)
            };
            let supervisor = Arc::new(Supervisor::new(
                config,
                Arc::new(TokioSidecarLauncher),
            ));
            app.manage(AppSupervisor(supervisor.clone()));
            app.manage(QuitState(AtomicBool::new(false)));
            install_menu(app)?;
            let handle = app.handle().clone();
            tauri::async_runtime::spawn(async move {
                if start_sidecar_with_retries(&handle, supervisor.clone()).await {
                    monitor_sidecar(handle, supervisor, 0);
                }
            });
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            sidecar_descriptor,
            retry_sidecar,
            request_sidecar_shutdown
        ])
        .on_window_event(|window, event| {
            if let WindowEvent::CloseRequested { api, .. } = event {
                handle_close_requested(window, api);
            }
        })
        .run(tauri::generate_context!())
        .expect("failed to run WebRetriever desktop shell");
}

fn install_menu(app: &tauri::App) -> tauri::Result<()> {
    let new_run = MenuItem::with_id(app, "new-run", "新建运行", true, Some("CmdOrCtrl+N"))?;
    let quit = MenuItem::with_id(app, "quit", "退出", true, Some("CmdOrCtrl+Q"))?;
    let separator = PredefinedMenuItem::separator(app)?;
    let file = Submenu::with_items(app, "文件", true, &[&new_run, &separator, &quit])?;
    let menu = Menu::with_items(app, &[&file])?;
    app.set_menu(menu)?;
    app.on_menu_event(|app, event| match event.id().as_ref() {
        "new-run" => {
            let _ = app.emit("desktop-menu", "new-run");
        }
        "quit" => {
            if let Some(window) = app.get_webview_window("main") {
                let _ = window.close();
            }
        }
        _ => {}
    });
    Ok(())
}

fn handle_close_requested(window: &Window, api: &tauri::CloseRequestApi) {
    let app = window.app_handle();
    let quit_state = app.state::<QuitState>();
    if quit_state.0.swap(true, Ordering::SeqCst) {
        return;
    }
    api.prevent_close();
    let handle = app.clone();
    tauri::async_runtime::spawn(async move {
        let supervisor = handle.state::<AppSupervisor>().0.clone();
        emit_backend_state(
            &handle,
            BackendState::Draining,
            Some("正在停止本地后端…".to_owned()),
        );
        supervisor.stop().await;
        emit_backend_state(&handle, BackendState::Stopped, None);
        if let Some(main_window) = handle.get_webview_window("main") {
            let _ = main_window.close();
        }
    });
}

fn emit_backend_state(app: &AppHandle, state: BackendState, message: Option<String>) {
    let _ = app.emit("backend-state-changed", BackendStatus { state, message });
}

async fn start_sidecar_with_retries(app: &AppHandle, supervisor: Arc<Supervisor>) -> bool {
    for (attempt, delay) in [0_u64, 500, 1_000, 2_000].into_iter().enumerate() {
        if delay > 0 {
            sleep(Duration::from_millis(delay)).await;
        }
        emit_backend_state(
            app,
            BackendState::Starting,
            if attempt == 0 {
                None
            } else {
                Some("正在重试本地后端…".to_owned())
            },
        );
        match supervisor.start().await {
            Ok(_) => {
                emit_backend_state(app, BackendState::Ready, None);
                return true;
            }
            Err(error) if attempt == 3 => {
                emit_backend_state(app, BackendState::Failed, Some(error.to_string()));
            }
            Err(_) => {}
        }
    }
    false
}

fn monitor_sidecar(app: AppHandle, supervisor: Arc<Supervisor>, automatic_restarts: u8) {
    tauri::async_runtime::spawn(async move {
        if !supervisor.wait_for_sidecar_exit().await {
            return;
        }
        emit_backend_state(
            &app,
            BackendState::Failed,
            Some("后端连接已丢失；当前运行不会自动重放。".to_owned()),
        );
        if automatic_restarts >= 2 {
            return;
        }
        sleep(Duration::from_millis(
            500 * u64::from(automatic_restarts + 1),
        ))
        .await;
        emit_backend_state(&app, BackendState::Restarting, None);
        match supervisor.restart().await {
            Ok(_) => {
                emit_backend_state(&app, BackendState::Ready, None);
                monitor_sidecar(app, supervisor, automatic_restarts + 1);
            }
            Err(error) => emit_backend_state(&app, BackendState::Failed, Some(error.to_string())),
        }
    });
}

#[allow(dead_code)]
fn sidecar_state_dir(app_data_dir: PathBuf) -> PathBuf {
    app_data_dir.join("sidecar")
}
