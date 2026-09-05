# Flush terminal state before closing SSE

For every terminal run state, the sidecar durably flushes the snapshot and terminal event before sending that SSE event and closing the stream.  The client then fetches the final snapshot, treating the event as prompt notification rather than the sole source of truth.
