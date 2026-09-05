# Never auto-replay an interrupted run

If the sidecar crashes during an active run, the UI reports lost backend connectivity and a restarted sidecar reconciles the persisted run to `INTERRUPTED` with an interruption event.  Existing artifacts remain inspectable, but the product never resumes or repeats browser actions automatically because their external side effects are not knowable.
