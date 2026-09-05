# Default telemetry to sanitized summaries

Persisted events and control-plane data contain only sanitized operational summaries.  They exclude prompts, raw completions, screenshot bytes, response bodies, credentials, authorization data, full CDP URLs, and exception stacks; any future detailed developer telemetry requires explicit opt-in, bounded content, and a privacy warning.
