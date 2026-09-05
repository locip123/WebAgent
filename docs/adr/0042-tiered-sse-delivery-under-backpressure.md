# Preserve lifecycle events and coalesce high-frequency telemetry

Run and task lifecycle, error, cancellation, and terminal events are never dropped.  Under subscriber backpressure, `task.phase_changed` may be coalesced to the latest state per task and dropped debug telemetry must produce a durable `telemetry.warning`, making degraded visibility explicit without blocking execution.
