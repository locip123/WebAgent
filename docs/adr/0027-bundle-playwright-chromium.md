# Bundle Playwright Chromium for local browser runs

The Linux desktop release will bundle the Chromium revision matched to its pinned Playwright version.  Local runs use this isolated browser rather than the user's installed Chrome, avoiding profile, policy, update, and CDN-download variability; a system-browser channel may be considered later only as an explicit advanced option.
