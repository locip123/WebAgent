# Canonicalize local paths and expose artifacts by opaque ID

Preflight canonicalizes input and output paths and accepts only accessible regular inputs and permitted output directories.  Artifact APIs accept only sidecar-issued `artifact_id` values, never caller-composed relative or absolute paths, so local file selection does not become arbitrary filesystem access through HTTP.
