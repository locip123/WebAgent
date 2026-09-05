# Freeze the v1 run event taxonomy

Desktop v1 stabilizes `run.accepted`, `run.started`, `worker.state_changed`, `task.started`, `task.phase_changed`, `task.step.completed`, `task.recovery`, `artifact.available`, `task.finished`, `run.cancel_requested`, `run.cancelled`, `run.completed`, `run.failed`, `run.interrupted`, and `telemetry.warning`.  New event types are additive; renaming, removing, or changing an existing payload's meaning requires a new API major.
