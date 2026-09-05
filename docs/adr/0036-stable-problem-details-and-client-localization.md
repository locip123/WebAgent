# Use stable Problem Details codes and client-side localization

Every API error uses a sanitized Problem Details response with stable `type` and `error_code` machine contracts.  The React client maps `error_code` values to Chinese user-facing messages and does not depend on backend prose; exception stacks, credentials, tokens, and raw URLs remain outside the response.
