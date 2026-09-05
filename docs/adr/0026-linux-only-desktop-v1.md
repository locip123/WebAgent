# Support Linux only for desktop v1

Desktop v1 will support Linux only.  The Runner currently relies on Unix `fcntl` file locking, and Windows requires separate lock and process-tree semantics; limiting the initial release lets the sidecar, browser runtime, and packaging receive one reliable regression baseline before platform expansion.
