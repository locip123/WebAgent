# Use an explicit terminal run state machine

Runs transition only from `STARTING` to `RUNNING`, `CANCELLING`, or `FAILED`; from `RUNNING` to `COMPLETED`, `CANCELLING`, or `FAILED`; and from `CANCELLING` to `CANCELLED`.  `COMPLETED`, `CANCELLED`, `FAILED`, and `INTERRUPTED` are immutable terminal states, while sidecar restart reconciles any active run to `INTERRUPTED`; `DRAINING` remains sidecar-only state.
