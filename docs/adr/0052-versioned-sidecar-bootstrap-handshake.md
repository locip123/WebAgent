# Freeze a versioned sidecar bootstrap handshake

The sidecar binds itself to `127.0.0.1:0` and, after readiness, emits one fixed-prefix stdout JSON record containing its protocol version, port, nonce, and PID.  Tauri verifies the nonce, protocol, and startup timeout; the bearer token travels only through the controlled environment and bootstrap descriptor, never stdout.
