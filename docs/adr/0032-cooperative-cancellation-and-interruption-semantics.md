# Distinguish cooperative cancellation from interruption

A cancel request moves a run to `CANCELLING`; after safe-boundary cleanup and durable task artifacts, unfinished tasks receive `FAIL_CANCELLED` and the run becomes `CANCELLED`.  A sidecar crash or forced termination instead becomes `INTERRUPTED`, never `CANCELLED`, because cleanup and artifact finalization were not confirmed.
