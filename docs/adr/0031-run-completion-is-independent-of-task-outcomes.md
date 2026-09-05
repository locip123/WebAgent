# Treat normal batch completion separately from task outcomes

`COMPLETED` means the Runner ended normally and produced a reliable batch summary, even when individual tasks have `FAIL_*` domain statuses.  `FAILED` is reserved for a run-level or control-plane failure that prevents a trustworthy summary, keeping operational failure distinct from task-quality outcomes.
