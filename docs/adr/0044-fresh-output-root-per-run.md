# Create a fresh output root for every desktop run

Each desktop run writes beneath `{output_root}/{utc_timestamp}_{run_id_short}/`, preserving the established task subdirectory layout within that new root.  Desktop v1 neither reuses existing output directories nor resumes prior runs, preventing stale-success skips, artifact overwrites, and ambiguous interrupted-state recovery.
