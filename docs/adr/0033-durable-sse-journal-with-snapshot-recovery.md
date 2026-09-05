# Persist run events and recover from snapshots

Each run's SSE events receive strictly increasing `event_id` values and are stored in a durable journal for replay after reconnect.  Run snapshots remain the authoritative complete state; when the requested event range is unavailable, clients rebuild from the snapshot and resume from its cursor.
