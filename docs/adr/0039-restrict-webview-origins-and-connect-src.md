# Restrict WebView access to the sidecar

The sidecar accepts requests only from the Tauri production origin and the current Vite development origin.  The WebView CSP permits connections only to `http://127.0.0.1:*` and loads no remote scripts, pairing the dynamic-port allowance with bearer authentication and explicit CORS policy.
