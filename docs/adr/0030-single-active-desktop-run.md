# Allow one active desktop run

The desktop control plane permits one active run at a time; `STARTING`, `RUNNING`, and `CANCELLING` are active states.  A run may still execute multiple tasks concurrently, but a second create request receives `409 active_run_exists` so browser, model, output, cancellation, and recovery ownership remain unambiguous.
