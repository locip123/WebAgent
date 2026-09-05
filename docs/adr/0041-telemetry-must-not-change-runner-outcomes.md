# Keep telemetry non-interfering

`RunObserver` defaults to no-op and observer or event-queue failures never fail a Runner task.  The control plane records sanitized diagnostics and may mark its final snapshot degraded when it cannot durably record a run terminal state, but it does not alter the Runner's actual result or legacy execution path.
