# Use credential profiles for the desktop API

The desktop API will reference a model credential profile by `profile_id`, while the sidecar resolves its secret through operating-system credential storage.  Existing `config.json` files may be imported to create a profile and the legacy CLI retains its file-based compatibility path, but raw API keys never appear in RunSpec, API responses, events, logs, or the control database.
