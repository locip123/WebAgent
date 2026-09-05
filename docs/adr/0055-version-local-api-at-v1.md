# Version the local API explicitly

All local-control-plane endpoints live under `/api/v1` and successful responses carry `schema_version: 1`.  Breaking semantic or structural changes require a new API major path rather than silently changing v1, giving the OpenAPI contract and generated clients a stable compatibility boundary.
