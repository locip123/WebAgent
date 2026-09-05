# Persist critical events and snapshots atomically

Every run state transition and critical lifecycle event is committed with its run/task snapshot projection in one control-database transaction before SSE publication.  This prevents a client from observing an event that cannot be recovered after reconnect or restart, while high-frequency phase telemetry may still use bounded buffering.
