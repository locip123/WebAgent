# Use bounded shutdown with explicit forced termination

On desktop exit, the sidecar enters `DRAINING`, rejects new runs, cancels active work, flushes state, and closes browser resources.  It targets five seconds without active work and waits at most 65 seconds with active work; after ten seconds the UI may explicitly terminate the whole sidecar process group, whose unfinished run later reconciles as `INTERRUPTED`.
