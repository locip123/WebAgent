"""Safe, opaque indexing for files produced below one accepted run directory."""

from __future__ import annotations

import hashlib
import mimetypes
from dataclasses import dataclass
from pathlib import Path

from browser_use.webretriever.desktop.contracts import Artifact


@dataclass(frozen=True, slots=True)
class IndexedArtifact:
	artifact: Artifact
	path: Path


class ArtifactIndex:
	"""Resolve only sidecar-issued IDs; callers can never submit a filesystem path."""

	def __init__(self, output_dir: str | Path) -> None:
		self._output_dir = Path(output_dir)

	def page(self, *, cursor: str | None, limit: int) -> tuple[list[Artifact], str | None]:
		items = self._items()
		if cursor is not None:
			try:
				start = next(index + 1 for index, item in enumerate(items) if item.artifact.artifact_id == cursor)
			except StopIteration:
				return [], None
			items = items[start:]
		page = items[:limit]
		next_cursor = page[-1].artifact.artifact_id if len(items) > limit and page else None
		return [item.artifact for item in page], next_cursor

	def resolve(self, artifact_id: str) -> Path | None:
		for item in self._items():
			if item.artifact.artifact_id == artifact_id:
				return item.path
		return None

	def _items(self) -> list[IndexedArtifact]:
		try:
			root = self._output_dir.resolve(strict=True)
		except OSError:
			return []
		if not root.is_dir():
			return []
		items: list[IndexedArtifact] = []
		for path in sorted(root.rglob("*")):
			try:
				resolved = path.resolve(strict=True)
			except OSError:
				continue
			if not resolved.is_file() or not resolved.is_relative_to(root):
				continue
			relative = resolved.relative_to(root)
			artifact_id = "art-" + hashlib.sha256(relative.as_posix().encode("utf-8")).hexdigest()[:32]
			mime_type = mimetypes.guess_type(resolved.name)[0] or "application/octet-stream"
			task_id = relative.parts[0].split("_", maxsplit=1)[-1] if len(relative.parts) > 1 else None
			items.append(
				IndexedArtifact(
					artifact=Artifact(
						artifact_id=artifact_id,
						kind=_kind(relative),
						task_id=task_id,
						mime_type=mime_type,
						size=resolved.stat().st_size,
					),
					path=resolved,
				)
			)
		return items


def _kind(relative: Path) -> str:
	if relative.name in {"result.json", "capture.json", "summary.json"}:
		return relative.stem
	if relative.parts and relative.parts[0] == "logs":
		return "log"
	return "file"


__all__ = ["ArtifactIndex"]
