# Use a versioned, cursor-addressable SSE envelope

Every persisted SSE event contains `schema`, `run_id`, strictly increasing `event_id`, `type`, UTC `occurred_at`, `level`, optional task identity, and `payload`; the SSE `id` equals `event_id`.  Clients tolerate unknown event types, preserve their cursor, and use the authoritative snapshot for recovery as new event types are introduced.
