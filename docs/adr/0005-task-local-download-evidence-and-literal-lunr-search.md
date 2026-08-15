# Preserve download provenance and literal task-local search

WebRetriever now exposes every downloaded file's metadata plus a bounded head/tail content preview, and builds `find_text`'s Lunr index only from the current page and downloads of the active task. Search keeps numeric identifiers literal (`12`, `012`, and `0012` remain distinct), because provenance and exact document notation are more important than broad numeric recall; Lunr relevance remains a fallback around exact matches.
