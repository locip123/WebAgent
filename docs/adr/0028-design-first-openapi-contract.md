# Define the local API contract before implementing the sidecar

The repository will keep a versioned `openapi.yaml` as the authoritative local-control-plane API contract.  FastAPI is implemented later and its generated OpenAPI document must receive semantic CI comparison against this contract, so DTOs, errors, and endpoint behavior cannot drift from the agreed design.
