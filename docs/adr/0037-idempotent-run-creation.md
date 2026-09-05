# Require idempotency keys when creating runs

`POST /runs` requires a client-supplied UUID `Idempotency-Key`.  Retrying the same RunSpec with that key returns the original run, while reusing the key for a different RunSpec returns `409 idempotency_key_reused`, preventing duplicate execution after an uncertain client retry.
