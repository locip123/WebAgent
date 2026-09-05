# Keep run preflight side-effect free

`POST /run-preflights` validates and normalizes local paths, task input, credential-profile availability, and supported limits, returning a task summary and warnings.  It never creates a run or artifacts, launches a browser, or calls a model, so callers may repeat it safely and test it without execution dependencies.
