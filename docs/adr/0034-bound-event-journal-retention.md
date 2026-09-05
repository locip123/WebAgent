# Bound retained run events

Each run retains at most 30 days and 50,000 journal events; whichever limit is reached first removes older events.  Requests before the retained window receive `410 events_expired` with the earliest available event ID and a snapshot URL, preserving deterministic client recovery while bounding control-plane storage.
