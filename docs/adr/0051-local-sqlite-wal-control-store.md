# Store local control-plane state in SQLite WAL

The single-user desktop sidecar stores run and task projections, event journals, and artifact indexes in local SQLite with WAL and a single writer task using short transactions.  This provides durable replay and crash recovery without introducing an external service while the product permits only one active run.
