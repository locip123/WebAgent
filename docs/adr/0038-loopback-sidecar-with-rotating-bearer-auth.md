# Protect the loopback sidecar with rotating bearer authentication

The sidecar binds only to `127.0.0.1` and every REST, SSE, and health endpoint requires a random bearer token generated for that child process.  Tauri passes the token through its narrow bootstrap descriptor; it rotates on restart and is never placed in URLs, command lines, browser storage, or logs.
