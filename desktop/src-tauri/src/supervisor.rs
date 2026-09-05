use async_trait::async_trait;
use rand::{distr::Alphanumeric, Rng};
use reqwest::header::AUTHORIZATION;
use serde::{Deserialize, Serialize};
use std::{
    path::PathBuf,
    sync::Arc,
    time::{Duration, Instant},
};
use thiserror::Error;
use tokio::{
    io::{AsyncBufReadExt, BufReader, Lines},
    process::{Child, ChildStdout, Command},
    sync::Mutex,
    time::timeout,
};

pub const SIDECAR_PROTOCOL_VERSION: u16 = 1;
const HANDSHAKE_PREFIX: &str = "WR_SIDECAR_LISTENING ";
const HANDSHAKE_TIMEOUT: Duration = Duration::from_secs(10);
const READINESS_TIMEOUT: Duration = Duration::from_secs(10);
const GRACEFUL_SHUTDOWN_TIMEOUT: Duration = Duration::from_secs(65);

#[derive(Clone, Debug)]
pub struct SupervisorConfig {
    pub state_dir: PathBuf,
    pub bearer_token: String,
    pub launch_nonce: String,
    pub allowed_origins: Vec<String>,
    pub launch: SidecarLaunch,
}

#[derive(Clone, Debug)]
pub enum SidecarLaunch {
    DevelopmentPython,
    BundledBinary { program: PathBuf },
}

impl SupervisorConfig {
    pub fn development(state_dir: PathBuf) -> Self {
        Self {
            state_dir,
            bearer_token: random_secret(64),
            launch_nonce: random_secret(32),
            allowed_origins: vec![
                "tauri://localhost".to_owned(),
                "http://localhost:1420".to_owned(),
            ],
            launch: SidecarLaunch::DevelopmentPython,
        }
    }

    pub fn bundled(state_dir: PathBuf, program: PathBuf) -> Self {
        Self {
            state_dir,
            bearer_token: random_secret(64),
            launch_nonce: random_secret(32),
            allowed_origins: vec!["tauri://localhost".to_owned()],
            launch: SidecarLaunch::BundledBinary { program },
        }
    }

    pub fn for_test(bearer_token: impl Into<String>, launch_nonce: impl Into<String>) -> Self {
        Self {
            state_dir: PathBuf::from("/tmp/webretriever-desktop-test"),
            bearer_token: bearer_token.into(),
            launch_nonce: launch_nonce.into(),
            allowed_origins: vec!["tauri://localhost".to_owned()],
            launch: SidecarLaunch::DevelopmentPython,
        }
    }
}

fn random_secret(length: usize) -> String {
    rand::rng()
        .sample_iter(&Alphanumeric)
        .take(length)
        .map(char::from)
        .collect()
}

#[derive(Clone, Debug, Deserialize)]
struct Handshake {
    protocol_version: u16,
    port: u16,
    pid: u32,
    launch_nonce: String,
}

#[derive(Clone, Debug, Deserialize, PartialEq, Eq, Serialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum BackendState {
    Starting,
    Ready,
    Restarting,
    Draining,
    Stopped,
    Failed,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct BackendDescriptor {
    pub base_url: String,
    pub bearer_token: String,
    pub protocol_version: u16,
}

#[derive(Clone, Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct BackendStatus {
    pub state: BackendState,
    pub message: Option<String>,
}

#[derive(Debug, Error)]
pub enum SupervisorError {
    #[error("sidecar launch failed: {0}")]
    Launch(String),
    #[error("sidecar did not emit a bootstrap handshake before the startup timeout")]
    HandshakeTimeout,
    #[error("sidecar closed stdout before emitting its bootstrap handshake")]
    HandshakeMissing,
    #[error("sidecar emitted an invalid bootstrap handshake: {0}")]
    HandshakeInvalid(String),
    #[error("sidecar launch nonce did not match the current launch")]
    LaunchNonceMismatch,
    #[error("sidecar protocol version {actual} is incompatible with desktop protocol {expected}")]
    ProtocolMismatch { actual: u16, expected: u16 },
    #[error("sidecar PID did not match the launched child")]
    PidMismatch,
    #[error("sidecar failed its authenticated readiness check")]
    ReadinessFailed,
}

#[async_trait]
pub trait SidecarChild: Send + Sync {
    async fn next_stdout_line(&self) -> Option<String>;
    async fn readiness(&self, base_url: &str, bearer_token: &str) -> bool;

    async fn request_shutdown(&self, _base_url: &str, _bearer_token: &str) -> bool {
        false
    }

    async fn terminate(&self);

    async fn wait_for_exit(&self) {
        std::future::pending::<()>().await;
    }

    fn pid(&self) -> Option<u32> {
        None
    }
}

#[async_trait]
pub trait SidecarLauncher: Send + Sync {
    async fn launch(&self, config: &SupervisorConfig) -> Result<Arc<dyn SidecarChild>, String>;
}

pub struct Supervisor {
    config: SupervisorConfig,
    launcher: Arc<dyn SidecarLauncher>,
    state: Mutex<BackendState>,
    descriptor: Mutex<Option<BackendDescriptor>>,
    child: Mutex<Option<Arc<dyn SidecarChild>>>,
}

impl Supervisor {
    pub fn new(config: SupervisorConfig, launcher: Arc<dyn SidecarLauncher>) -> Self {
        Self {
            config,
            launcher,
            state: Mutex::new(BackendState::Stopped),
            descriptor: Mutex::new(None),
            child: Mutex::new(None),
        }
    }

    pub async fn start(&self) -> Result<BackendDescriptor, SupervisorError> {
        *self.state.lock().await = BackendState::Starting;
        *self.descriptor.lock().await = None;
        let child = self
            .launcher
            .launch(&self.config)
            .await
            .map_err(SupervisorError::Launch)?;
        let result = self.verify_startup(child.clone()).await;
        match result {
            Ok(descriptor) => {
                *self.descriptor.lock().await = Some(descriptor.clone());
                *self.child.lock().await = Some(child);
                *self.state.lock().await = BackendState::Ready;
                Ok(descriptor)
            }
            Err(error) => {
                child.terminate().await;
                *self.state.lock().await = BackendState::Failed;
                Err(error)
            }
        }
    }

    pub async fn restart(&self) -> Result<BackendDescriptor, SupervisorError> {
        *self.state.lock().await = BackendState::Restarting;
        self.stop().await;
        self.start().await
    }

    pub async fn stop(&self) {
        *self.state.lock().await = BackendState::Draining;
        let descriptor = self.descriptor.lock().await.take();
        if let Some(child) = self.child.lock().await.take() {
            if let Some(descriptor) = descriptor {
                if child
                    .request_shutdown(&descriptor.base_url, &descriptor.bearer_token)
                    .await
                {
                    if timeout(GRACEFUL_SHUTDOWN_TIMEOUT, child.wait_for_exit())
                        .await
                        .is_ok()
                    {
                        *self.state.lock().await = BackendState::Stopped;
                        return;
                    }
                }
            }
            child.terminate().await;
        }
        *self.state.lock().await = BackendState::Stopped;
    }

    pub async fn state(&self) -> BackendState {
        self.state.lock().await.clone()
    }

    pub async fn descriptor(&self) -> Option<BackendDescriptor> {
        self.descriptor.lock().await.clone()
    }

    pub async fn wait_for_sidecar_exit(&self) -> bool {
        let child = self.child.lock().await.clone();
        if let Some(child) = child {
            child.wait_for_exit().await;
            return self.sidecar_exited().await;
        }
        false
    }

    pub async fn sidecar_exited(&self) -> bool {
        *self.descriptor.lock().await = None;
        *self.child.lock().await = None;
        let mut state = self.state.lock().await;
        if matches!(*state, BackendState::Draining | BackendState::Stopped) {
            return false;
        }
        *state = BackendState::Failed;
        true
    }

    async fn verify_startup(
        &self,
        child: Arc<dyn SidecarChild>,
    ) -> Result<BackendDescriptor, SupervisorError> {
        let deadline = Instant::now() + HANDSHAKE_TIMEOUT;
        let handshake = loop {
            let remaining = deadline
                .checked_duration_since(Instant::now())
                .ok_or(SupervisorError::HandshakeTimeout)?;
            let line = timeout(remaining, child.next_stdout_line())
                .await
                .map_err(|_| SupervisorError::HandshakeTimeout)?
                .ok_or(SupervisorError::HandshakeMissing)?;
            if let Some(payload) = line.strip_prefix(HANDSHAKE_PREFIX) {
                break serde_json::from_str::<Handshake>(payload)
                    .map_err(|error| SupervisorError::HandshakeInvalid(error.to_string()))?;
            }
        };
        if handshake.launch_nonce != self.config.launch_nonce {
            return Err(SupervisorError::LaunchNonceMismatch);
        }
        if handshake.protocol_version != SIDECAR_PROTOCOL_VERSION {
            return Err(SupervisorError::ProtocolMismatch {
                actual: handshake.protocol_version,
                expected: SIDECAR_PROTOCOL_VERSION,
            });
        }
        if handshake.port == 0 {
            return Err(SupervisorError::HandshakeInvalid(
                "port must be within 1..=65535".to_owned(),
            ));
        }
        if handshake.pid == 0 {
            return Err(SupervisorError::HandshakeInvalid(
                "pid must be positive".to_owned(),
            ));
        }
        if let Some(actual_pid) = child.pid() {
            if handshake.pid != actual_pid {
                return Err(SupervisorError::PidMismatch);
            }
        }
        let base_url = format!("http://127.0.0.1:{}", handshake.port);
        let ready = timeout(
            READINESS_TIMEOUT,
            child.readiness(&base_url, &self.config.bearer_token),
        )
        .await
        .unwrap_or(false);
        if !ready {
            return Err(SupervisorError::ReadinessFailed);
        }
        Ok(BackendDescriptor {
            base_url,
            bearer_token: self.config.bearer_token.clone(),
            protocol_version: SIDECAR_PROTOCOL_VERSION,
        })
    }
}

pub struct TokioSidecarLauncher;

#[async_trait]
impl SidecarLauncher for TokioSidecarLauncher {
    async fn launch(&self, config: &SupervisorConfig) -> Result<Arc<dyn SidecarChild>, String> {
        let mut command = match &config.launch {
            SidecarLaunch::DevelopmentPython => {
                let mut command = Command::new("python");
                command.args(["-m", "browser_use.webretriever.desktop.sidecar"]);
                command
            }
            SidecarLaunch::BundledBinary { program } => Command::new(program),
        };
        command
            .arg("--state-dir")
            .arg(&config.state_dir)
            .env("WR_SIDECAR_BEARER_TOKEN", &config.bearer_token)
            .env("WR_SIDECAR_LAUNCH_NONCE", &config.launch_nonce)
            .env(
                "WR_SIDECAR_ALLOWED_ORIGINS",
                config.allowed_origins.join(","),
            )
            .stdout(std::process::Stdio::piped())
            .stderr(std::process::Stdio::piped());
        #[cfg(unix)]
        command.process_group(0);
        let mut child = command.spawn().map_err(|error| error.to_string())?;
        let stdout = child
            .stdout
            .take()
            .ok_or_else(|| "sidecar stdout was not piped".to_owned())?;
        Ok(Arc::new(TokioSidecarChild {
            child: Mutex::new(child),
            stdout: Mutex::new(BufReader::new(stdout).lines()),
            client: reqwest::Client::new(),
        }))
    }
}

struct TokioSidecarChild {
    child: Mutex<Child>,
    stdout: Mutex<Lines<BufReader<ChildStdout>>>,
    client: reqwest::Client,
}

#[async_trait]
impl SidecarChild for TokioSidecarChild {
    async fn next_stdout_line(&self) -> Option<String> {
        self.stdout.lock().await.next_line().await.ok().flatten()
    }

    async fn readiness(&self, base_url: &str, bearer_token: &str) -> bool {
        self.client
            .get(format!("{base_url}/api/v1/health/ready"))
            .header(AUTHORIZATION, format!("Bearer {bearer_token}"))
            .send()
            .await
            .map(|response| response.status().is_success())
            .unwrap_or(false)
    }

    async fn request_shutdown(&self, base_url: &str, bearer_token: &str) -> bool {
        self.client
            .post(format!("{base_url}/api/v1/control/shutdown"))
            .header(AUTHORIZATION, format!("Bearer {bearer_token}"))
            .send()
            .await
            .map(|response| response.status().is_success())
            .unwrap_or(false)
    }

    async fn terminate(&self) {
        let mut child = self.child.lock().await;
        #[cfg(unix)]
        if let Some(pid) = child.id() {
            use nix::{
                sys::signal::{kill, Signal},
                unistd::Pid,
            };
            let process_group = Pid::from_raw(-(pid as i32));
            let _ = kill(process_group, Signal::SIGTERM);
            tokio::time::sleep(Duration::from_secs(1)).await;
            let _ = kill(process_group, Signal::SIGKILL);
        }
        let _ = child.start_kill();
        let _ = child.wait().await;
    }

    async fn wait_for_exit(&self) {
        loop {
            let exited = {
                let mut child = self.child.lock().await;
                child.try_wait().ok().flatten().is_some()
            };
            if exited {
                return;
            }
            tokio::time::sleep(Duration::from_millis(100)).await;
        }
    }

    fn pid(&self) -> Option<u32> {
        self.child.try_lock().ok().and_then(|child| child.id())
    }
}
