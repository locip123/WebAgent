use async_trait::async_trait;
use std::sync::{
    atomic::{AtomicBool, Ordering},
    Arc,
};
use wr_desktop_shell::supervisor::{
	bundled_sidecar_program, BackendDescriptor, BackendState, SidecarChild, SidecarLaunch,
	SidecarLauncher, Supervisor, SupervisorConfig,
};

#[test]
fn release_configuration_uses_only_the_fixed_bundled_sidecar() {
	let program = bundled_sidecar_program(std::path::Path::new("/opt/WebRetriever/resources"));
	let config = SupervisorConfig::bundled(std::path::PathBuf::from("/var/lib/webretriever"), program.clone());

	assert_eq!(config.allowed_origins, vec!["tauri://localhost"]);
	assert!(matches!(config.launch, SidecarLaunch::BundledBinary { program: configured } if configured == program));
}

#[test]
fn bundled_sidecar_uses_the_native_program_name() {
	let program = bundled_sidecar_program(std::path::Path::new("C:/Program Files/WebRetriever/resources"));
	#[cfg(windows)]
	assert!(program.ends_with("sidecar/webretriever-sidecar.exe"));
	#[cfg(not(windows))]
	assert!(program.ends_with("sidecar/webretriever-sidecar"));
}

#[derive(Clone)]
struct ReadyChild;

#[async_trait]
impl SidecarChild for ReadyChild {
    async fn next_stdout_line(&self) -> Option<String> {
        Some(
			"WR_SIDECAR_LISTENING {\"protocol_version\":1,\"port\":43127,\"pid\":42,\"launch_nonce\":\"nonce-1\"}"
				.to_owned(),
		)
    }

    async fn readiness(&self, base_url: &str, bearer_token: &str) -> bool {
        base_url == "http://127.0.0.1:43127" && bearer_token == "launch-token"
    }

    async fn terminate(&self) {}
}

struct ReadyLauncher;

#[async_trait]
impl SidecarLauncher for ReadyLauncher {
    async fn launch(&self, config: &SupervisorConfig) -> Result<Arc<dyn SidecarChild>, String> {
        assert_eq!(config.launch_nonce, "nonce-1");
        assert_eq!(config.bearer_token, "launch-token");
        Ok(Arc::new(ReadyChild))
    }
}

#[tokio::test]
async fn verified_handshake_and_readiness_publish_a_descriptor_to_the_desktop_client() {
    let supervisor = Supervisor::new(
        SupervisorConfig::for_test("launch-token", "nonce-1"),
        Arc::new(ReadyLauncher),
    );

    let descriptor = supervisor.start().await.expect("sidecar becomes ready");

    assert_eq!(
        descriptor,
        BackendDescriptor {
            base_url: "http://127.0.0.1:43127".to_owned(),
            bearer_token: "launch-token".to_owned(),
            protocol_version: 1,
        }
    );
    assert_eq!(supervisor.state().await, BackendState::Ready);
}

#[derive(Clone)]
struct WrongNonceChild;

#[async_trait]
impl SidecarChild for WrongNonceChild {
    async fn next_stdout_line(&self) -> Option<String> {
        Some(
			"WR_SIDECAR_LISTENING {\"protocol_version\":1,\"port\":43127,\"pid\":42,\"launch_nonce\":\"another-nonce\"}"
				.to_owned(),
		)
    }

    async fn readiness(&self, _base_url: &str, _bearer_token: &str) -> bool {
        true
    }

    async fn terminate(&self) {}
}

struct WrongNonceLauncher;

#[async_trait]
impl SidecarLauncher for WrongNonceLauncher {
    async fn launch(&self, _config: &SupervisorConfig) -> Result<Arc<dyn SidecarChild>, String> {
        Ok(Arc::new(WrongNonceChild))
    }
}

#[tokio::test]
async fn invalid_handshake_never_exposes_a_descriptor_and_reports_startup_failure() {
    let supervisor = Supervisor::new(
        SupervisorConfig::for_test("launch-token", "nonce-1"),
        Arc::new(WrongNonceLauncher),
    );

    let error = supervisor
        .start()
        .await
        .expect_err("mismatched nonce is rejected");

    assert!(error.to_string().contains("launch nonce"));
    assert_eq!(supervisor.state().await, BackendState::Failed);
}

#[tokio::test]
async fn unexpected_sidecar_exit_revokes_the_descriptor_before_the_desktop_can_retry() {
    let supervisor = Supervisor::new(
        SupervisorConfig::for_test("launch-token", "nonce-1"),
        Arc::new(ReadyLauncher),
    );
    supervisor.start().await.expect("sidecar becomes ready");

    assert!(supervisor.sidecar_exited().await);

    assert_eq!(supervisor.descriptor().await, None);
    assert_eq!(supervisor.state().await, BackendState::Failed);
}

#[derive(Clone)]
struct ShutdownChild {
    shutdown_requested: Arc<AtomicBool>,
}

#[async_trait]
impl SidecarChild for ShutdownChild {
    async fn next_stdout_line(&self) -> Option<String> {
        Some(
            "WR_SIDECAR_LISTENING {\"protocol_version\":1,\"port\":43127,\"pid\":42,\"launch_nonce\":\"nonce-1\"}"
                .to_owned(),
        )
    }

    async fn readiness(&self, _base_url: &str, _bearer_token: &str) -> bool {
        true
    }

    async fn request_shutdown(&self, base_url: &str, bearer_token: &str) -> bool {
        self.shutdown_requested.store(
            base_url == "http://127.0.0.1:43127" && bearer_token == "launch-token",
            Ordering::SeqCst,
        );
        true
    }

    async fn terminate(&self) {}

    async fn wait_for_exit(&self) {}
}

struct ShutdownLauncher(Arc<AtomicBool>);

#[async_trait]
impl SidecarLauncher for ShutdownLauncher {
    async fn launch(&self, _config: &SupervisorConfig) -> Result<Arc<dyn SidecarChild>, String> {
        Ok(Arc::new(ShutdownChild {
            shutdown_requested: self.0.clone(),
        }))
    }
}

#[tokio::test]
async fn stop_requests_authenticated_graceful_shutdown_before_process_tree_termination() {
    let requested = Arc::new(AtomicBool::new(false));
    let supervisor = Supervisor::new(
        SupervisorConfig::for_test("launch-token", "nonce-1"),
        Arc::new(ShutdownLauncher(requested.clone())),
    );
    supervisor.start().await.expect("sidecar becomes ready");

    supervisor.stop().await;

    assert!(requested.load(Ordering::SeqCst));
    assert_eq!(supervisor.descriptor().await, None);
    assert_eq!(supervisor.state().await, BackendState::Stopped);
}
