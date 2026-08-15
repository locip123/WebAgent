"""Rectangle-union primitives used by WebRetriever's DOM collector.

The full browser-use project also defines a tree-level paint-order remover in
this module.  The competition runtime collects its own CDP snapshots and uses
only these independent geometry primitives, so keeping this narrow copy avoids
pulling in the unrelated browser-use DOM service and its CDP-use dependency.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Rect:
	"""Closed axis-aligned rectangle represented by its two corners."""

	x1: float
	y1: float
	x2: float
	y2: float

	def area(self) -> float:
		return (self.x2 - self.x1) * (self.y2 - self.y1)

	def intersects(self, other: Rect) -> bool:
		return not (self.x2 <= other.x1 or other.x2 <= self.x1 or self.y2 <= other.y1 or other.y2 <= self.y1)

	def contains(self, other: Rect) -> bool:
		return self.x1 <= other.x1 and self.y1 <= other.y1 and self.x2 >= other.x2 and self.y2 >= other.y2


class RectUnionPure:
	"""Maintain a bounded, disjoint union of rectangles without dependencies."""

	__slots__ = ('_rects',)
	_MAX_RECTS = 5_000

	def __init__(self) -> None:
		self._rects: list[Rect] = []

	@staticmethod
	def _split_diff(left: Rect, right: Rect) -> list[Rect]:
		"""Return up to four rectangles for ``left`` minus ``right``."""

		parts: list[Rect] = []
		if left.y1 < right.y1:
			parts.append(Rect(left.x1, left.y1, left.x2, right.y1))
		if right.y2 < left.y2:
			parts.append(Rect(left.x1, right.y2, left.x2, left.y2))
		y_low, y_high = max(left.y1, right.y1), min(left.y2, right.y2)
		if left.x1 < right.x1:
			parts.append(Rect(left.x1, y_low, right.x1, y_high))
		if right.x2 < left.x2:
			parts.append(Rect(right.x2, y_low, left.x2, y_high))
		return parts

	def contains(self, rectangle: Rect) -> bool:
		pending = [rectangle]
		for covered in self._rects:
			next_pending: list[Rect] = []
			for piece in pending:
				if covered.contains(piece):
					continue
				if covered.intersects(piece):
					next_pending.extend(self._split_diff(piece, covered))
				else:
					next_pending.append(piece)
			if not next_pending:
				return True
			pending = next_pending
		return False

	def add(self, rectangle: Rect) -> bool:
		if len(self._rects) >= self._MAX_RECTS or self.contains(rectangle):
			return False
		pending = [rectangle]
		for covered in self._rects:
			next_pending: list[Rect] = []
			for piece in pending:
				if covered.intersects(piece):
					next_pending.extend(self._split_diff(piece, covered))
				else:
					next_pending.append(piece)
			pending = next_pending
		self._rects.extend(pending)
		return True
