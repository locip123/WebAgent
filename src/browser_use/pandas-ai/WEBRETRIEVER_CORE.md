# Embedded PandasAI core

WebRetriever uses a generation-only subset derived from PandasAI v3, Copyright
(c) 2023 Sinaptik GmbH, under the MIT License in `LICENSE`.

The prepared upstream project remains in this directory as development
reference. The distributable subset retains dataframe schema serialization,
prompt rendering, the `Agent.generate_code` pipeline, and the LLM base adapter.
It excludes Enterprise (`pandasai/ee`), telemetry, CLI, connectors/loaders,
global configuration, plotting/response handling, and PandasAI's unrestricted
code executor. WebRetriever validates generated code independently and executes
only allowlisted read-only SQL in an isolated DuckDB subprocess.
