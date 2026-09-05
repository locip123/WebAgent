# Preserve the Runner CLI and artifact contract

The desktop control plane will wrap rather than replace the existing Runner.  `scripts/run.sh` and the established per-task artifact layout, including `result.json` and `capture.json`, remain compatibility contracts and receive regression coverage; any future removal requires an explicit versioned migration.
