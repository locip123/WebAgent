# Import model profiles without sending raw secrets over HTTP

Desktop v1 creates model credential profiles through a sidecar import endpoint that receives a user-selected local configuration path.  The sidecar reads the secret directly into operating-system credential storage and returns only non-sensitive profile metadata; RunSpec references `profile_id`, and no HTTP JSON endpoint accepts a raw API key.
