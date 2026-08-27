"""Screenshot-only helpers for visible verification widgets.

These helpers never talk to the browser.  They only read pixels that the
already-connected Playwright session captured, then return Playwright click or
drag coordinates.  They do not inject tokens, change identity, or call an
external solver.
"""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO

import numpy as np
from PIL import Image

__all__ = ['CheckboxPlan', 'SliderPlan', 'human_drag_waypoints', 'plan_checkbox_click', 'plan_slider_drag']


@dataclass(frozen=True, slots=True)
class CheckboxPlan:
	"""One visible checkbox click inferred from a screenshot."""

	x: float
	y: float
	widget_box: tuple[int, int, int, int]


@dataclass(frozen=True, slots=True)
class SliderPlan:
	"""One horizontal puzzle-slider drag inferred from a screenshot."""

	start_x: float
	start_y: float
	end_x: float
	end_y: float
	confidence: float
	piece_box: tuple[int, int, int, int]
	gap_box: tuple[int, int, int, int]


def plan_checkbox_click(screenshot: bytes) -> CheckboxPlan | None:
	"""Return a click plan when a visible Cloudflare-style checkbox is on screen."""

	image = _open_rgb(screenshot)
	if image is None:
		return None
	pixels = np.asarray(image, dtype=np.int16)
	widget = _find_checkbox_widget(pixels)
	if widget is None:
		return None
	x0, y0, x1, y1 = widget
	checkbox = _find_checkbox_square(pixels[y0 : y1 + 1, x0 : x1 + 1])
	if checkbox is None:
		return None
	cx, cy = checkbox
	return CheckboxPlan(x=x0 + cx, y=y0 + cy, widget_box=widget)


def plan_slider_drag(screenshot: bytes) -> SliderPlan | None:
	"""Return a drag plan when a visible puzzle slider is on screen."""

	image = _open_rgb(screenshot)
	if image is None:
		return None
	pixels = np.asarray(image, dtype=np.int16)
	card = _find_card(pixels)
	if card is None:
		return None
	x0, y0, x1, y1 = card
	card_pixels = pixels[y0 : y1 + 1, x0 : x1 + 1]
	header_bottom = _header_bottom(card_pixels)
	if header_bottom is None:
		return None
	button = _slider_button(card_pixels)
	if button is None:
		return None
	puzzle_top = header_bottom + 2
	puzzle_bottom = button[1] - 6
	if puzzle_bottom - puzzle_top < 40:
		return None
	puzzle = card_pixels[puzzle_top:puzzle_bottom]
	match = _match_piece_to_gap(puzzle)
	if match is None:
		return None
	piece_x, piece_y, piece_w, piece_h, gap_x, score = match
	distance = float(gap_x - piece_x)
	if distance < 18:
		return None
	button_cx = x0 + (button[0] + button[2]) / 2.0
	button_cy = y0 + (button[1] + button[3]) / 2.0
	end_x = button_cx + distance
	if not (0 <= end_x < pixels.shape[1] and 0 <= button_cy < pixels.shape[0]):
		return None
	return SliderPlan(
		start_x=button_cx,
		start_y=button_cy,
		end_x=end_x,
		end_y=button_cy,
		confidence=score,
		piece_box=(x0 + piece_x, y0 + puzzle_top + piece_y, piece_w, piece_h),
		gap_box=(x0 + gap_x, y0 + puzzle_top + piece_y, piece_w, piece_h),
	)


def human_drag_waypoints(
	start_x: float,
	start_y: float,
	end_x: float,
	end_y: float,
	*,
	steps: int = 28,
	rng: np.random.Generator | None = None,
) -> list[tuple[float, float, int]]:
	"""Build a slightly eased, jittered drag path for Playwright mouse events."""

	if steps < 8:
		raise ValueError('steps must be at least 8')
	engine = rng if rng is not None else np.random.default_rng()
	overshoot = 1.0 + float(engine.uniform(0.015, 0.045))
	control_x = start_x + (end_x - start_x) * overshoot
	control_y = start_y + (end_y - start_y) * overshoot
	points: list[tuple[float, float, int]] = []
	for index in range(1, steps + 1):
		t = index / steps
		eased = t * t * (3.0 - 2.0 * t)
		x = start_x + (control_x - start_x) * eased
		y = start_y + (control_y - start_y) * eased + float(engine.uniform(-1.15, 1.15))
		delay = int(engine.integers(7, 17))
		points.append((x, y, delay))
	points.append((end_x, end_y, int(engine.integers(35, 80))))
	return points


def _open_rgb(screenshot: bytes) -> Image.Image | None:
	if not screenshot or screenshot == b'png':
		return None
	try:
		with Image.open(BytesIO(screenshot)) as image:
			return image.convert('RGB')
	except Exception:
		return None


def _find_checkbox_widget(pixels: np.ndarray) -> tuple[int, int, int, int] | None:
	height, width = pixels.shape[:2]
	luminance = pixels.mean(axis=2)
	if float((luminance > 248).mean()) < 0.85:
		return None
	saturation = np.max(pixels, axis=2) - np.min(pixels, axis=2)
	widget = (saturation < 25) & (luminance > 220) & (luminance < 250)
	row_occupancy = widget.mean(axis=1)
	row_segments = _true_runs(row_occupancy > 0.05)
	candidates: list[tuple[int, int, int, int]] = []
	for top, bottom in row_segments:
		band_height = bottom - top + 1
		if band_height < 28 or band_height > 110:
			continue
		column_occupancy = widget[top : bottom + 1].mean(axis=0)
		column_run = _longest_true_run(column_occupancy > 0.35)
		if column_run is None:
			continue
		left, right = column_run
		band_width = right - left + 1
		if band_width < 160 or band_width > 520:
			continue
		if top < height * 0.12 or bottom > height * 0.82:
			continue
		candidates.append((left, top, right, bottom))
	if not candidates:
		return None
	return min(candidates, key=lambda box: abs(((box[1] + box[3]) / 2) - height * 0.35))


def _find_checkbox_square(widget: np.ndarray) -> tuple[float, float] | None:
	gray = widget.mean(axis=2)
	height, width = gray.shape
	search_width = max(36, min(width // 3, 90))
	region = gray[:, :search_width]
	dark = region < 200
	if int(dark.sum()) < 20:
		return None
	best: tuple[float, int, int, int] | None = None
	for size in range(12, 28):
		for top in range(0, height - size + 1):
			for left in range(0, search_width - size + 1):
				border = _square_border_darkness(dark[top : top + size, left : left + size])
				if border < 0.45:
					continue
				interior = dark[top + 2 : top + size - 2, left + 2 : left + size - 2]
				if interior.size and float(interior.mean()) > 0.35:
					continue
				if best is None or border > best[0]:
					best = (border, left, top, size)
	if best is None:
		return None
	_, left, top, size = best
	return left + size / 2.0, top + size / 2.0


def _square_border_darkness(mask: np.ndarray) -> float:
	if mask.shape[0] < 8 or mask.shape[1] < 8:
		return 0.0
	top = float(mask[0].mean() + mask[1].mean()) / 2.0
	bottom = float(mask[-1].mean() + mask[-2].mean()) / 2.0
	left = float(mask[:, 0].mean() + mask[:, 1].mean()) / 2.0
	right = float(mask[:, -1].mean() + mask[:, -2].mean()) / 2.0
	return (top + bottom + left + right) / 4.0


def _true_runs(mask: np.ndarray) -> list[tuple[int, int]]:
	runs: list[tuple[int, int]] = []
	start: int | None = None
	for index, flag in enumerate(mask.tolist()):
		if flag and start is None:
			start = index
		elif not flag and start is not None:
			runs.append((start, index - 1))
			start = None
	if start is not None:
		runs.append((start, len(mask) - 1))
	return runs


def _find_card(pixels: np.ndarray) -> tuple[int, int, int, int] | None:
	white = (pixels[:, :, 0] > 225) & (pixels[:, :, 1] > 225) & (pixels[:, :, 2] > 225)
	if not white.any() or float(white.mean()) > 0.45:
		return None
	rows, cols = np.where(white)
	x0, y0, x1, y1 = int(cols.min()), int(rows.min()), int(cols.max()), int(rows.max())
	width, height = x1 - x0 + 1, y1 - y0 + 1
	frame_h, frame_w = pixels.shape[:2]
	if width < 180 or height < 180:
		return None
	if width > frame_w * 0.7 or height > frame_h * 0.85:
		return None
	return x0, y0, x1, y1


def _header_bottom(card: np.ndarray) -> int | None:
	height = card.shape[0]
	header = (card[:, :, 2] > 150) & (card[:, :, 0] < 90) & (card[:, :, 1] < 180)
	header[height // 3 :, :] = False
	if not header.any():
		return None
	return int(np.where(header)[0].max())


def _slider_button(card: np.ndarray) -> tuple[int, int, int, int] | None:
	height, width = card.shape[:2]
	saturation = np.max(card, axis=2) - np.min(card, axis=2)
	button = (card[:, :, 2] > 170) & (card[:, :, 0] < 100) & (card[:, :, 1] < 200) & (saturation > 90)
	button[: int(height * 0.58), :] = False
	if int(button.sum()) < 80:
		return None
	columns = button.sum(axis=0) >= 4
	run = _longest_true_run(columns)
	if run is None:
		return None
	x0, x1 = run
	band = button[:, x0 : x1 + 1]
	occupied_rows = np.where(band.sum(axis=1) >= 3)[0]
	if occupied_rows.size == 0:
		return None
	y0, y1 = int(occupied_rows.min()), int(occupied_rows.max())
	box_w, box_h = x1 - x0 + 1, y1 - y0 + 1
	if box_w < 20 or box_h < 12 or box_w > width * 0.45:
		return None
	return x0, y0, x1, y1


def _longest_true_run(mask: np.ndarray) -> tuple[int, int] | None:
	best: tuple[int, int] | None = None
	start: int | None = None
	for index, flag in enumerate(mask.tolist()):
		if flag and start is None:
			start = index
		elif not flag and start is not None:
			candidate = (start, index - 1)
			if best is None or candidate[1] - candidate[0] > best[1] - best[0]:
				best = candidate
			start = None
	if start is not None:
		candidate = (start, len(mask) - 1)
		if best is None or candidate[1] - candidate[0] > best[1] - best[0]:
			best = candidate
	return best


def _match_piece_to_gap(puzzle: np.ndarray) -> tuple[int, int, int, int, int, float] | None:
	gray = puzzle.mean(axis=2)
	edges = _edges(gray)
	height, width = gray.shape
	score = edges.copy()
	score[: max(2, int(height * 0.12))] *= 0.2
	score[int(height * 0.78) :] *= 0.2
	window_w = max(18, int(width * 0.16))
	window_h = max(18, int(height * 0.28))
	best = (-1.0, 0, 0)
	step = 3
	x_limit = max(window_w + 4, width // 2)
	for top in range(int(height * 0.08), max(int(height * 0.08) + 1, int(height * 0.72) - window_h), step):
		for left in range(4, max(5, x_limit - window_w // 3), step):
			energy = float(score[top : top + window_h, left : left + window_w].mean())
			if energy > best[0]:
				best = (energy, left, top)
	_, piece_x, piece_y = best
	if best[0] <= 0:
		return None
	piece_x, piece_y, piece_w, piece_h = _tighten_box(score, piece_x, piece_y, window_w, window_h)
	template = edges[piece_y : piece_y + piece_h, piece_x : piece_x + piece_w]
	if template.size == 0:
		return None
	search_x0 = piece_x + max(12, piece_w // 2)
	best_match = (-1.0, piece_x)
	for gap_x in range(search_x0, width - piece_w - 2, 2):
		candidate = edges[piece_y : piece_y + piece_h, gap_x : gap_x + piece_w]
		if candidate.shape != template.shape:
			continue
		correlation = _normalized_correlation(template, candidate)
		if correlation > best_match[0]:
			best_match = (correlation, gap_x)
	if best_match[0] < 0.08:
		return None
	return piece_x, piece_y, piece_w, piece_h, best_match[1], best_match[0]


def _tighten_box(score: np.ndarray, left: int, top: int, width: int, height: int) -> tuple[int, int, int, int]:
	patch = score[top : top + height, left : left + width]
	if patch.size == 0:
		return left, top, width, height
	threshold = float(np.percentile(patch, 70))
	rows, cols = np.where(patch >= threshold)
	if cols.size < 10:
		return left, top, width, height
	tight_left = left + int(cols.min())
	tight_top = top + int(rows.min())
	tight_w = int(cols.max() - cols.min() + 1)
	tight_h = int(rows.max() - rows.min() + 1)
	if tight_w < 12:
		tight_left, tight_w = left, width
	if tight_h < 12:
		tight_top, tight_h = top, height
	return tight_left, tight_top, tight_w, tight_h


def _edges(gray: np.ndarray) -> np.ndarray:
	gx = np.abs(np.diff(gray, axis=1, prepend=gray[:, :1]))
	gy = np.abs(np.diff(gray, axis=0, prepend=gray[:1, :]))
	edges = gx + gy
	edges[:, :3] = 0
	edges[:, -3:] = 0
	edges[:3] = 0
	edges[-3:] = 0
	return edges


def _normalized_correlation(left: np.ndarray, right: np.ndarray) -> float:
	a = left.astype(np.float32) - float(left.mean())
	b = right.astype(np.float32) - float(right.mean())
	denom = float(np.linalg.norm(a) * np.linalg.norm(b))
	if denom < 1e-6:
		return -1.0
	return float((a * b).sum() / denom)
