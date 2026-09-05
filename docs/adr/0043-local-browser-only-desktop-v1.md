# Expose local browser mode only in desktop v1

Desktop v1 exposes only the bundled Playwright Chromium local-browser mode in RunSpec.  External CDP URLs and their associated remote-session security surface remain available through the compatible legacy CLI, keeping the initial desktop API local, reproducible, and easier to preflight and support.
