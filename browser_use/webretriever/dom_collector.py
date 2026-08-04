"""Non-mutating CDP DOM collection for the WebRetriever runtime.

The competition runtime is Playwright based, but the organiser permits raw CDP
queries through Playwright's ``CDPSession``.  This module uses that narrow
bridge to obtain the same evidence sources as browser-use's DOM pipeline:
the deep DOM tree, DOM snapshot, accessibility tree, and DevTools listener
metadata. It deliberately does not write identifiers or overlays into the page
DOM, dispatch input, focus nodes, or otherwise change browser state.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin

from playwright.async_api import BrowserContext, CDPSession, Frame, Page
from playwright.async_api import Error as PlaywrightError

from browser_use.dom.serializer.paint_order import Rect, RectUnionPure

__all__ = ['CdpCollectionError', 'CollectedElement', 'collect_interactive_elements']


# Keep these aligned with ``browser_use.dom.enhanced_snapshot``.  Snapshot
# bounds in current Chromium are already CSS pixels, so unlike that legacy
# helper this collector must *not* divide coordinates by devicePixelRatio.
_COMPUTED_STYLES = (
	'display',
	'visibility',
	'opacity',
	'overflow',
	'overflow-x',
	'overflow-y',
	'cursor',
	'pointer-events',
	'position',
	'background-color',
)
_INTERACTIVE_TAGS = frozenset(
	{
		'a',
		'button',
		'input',
		'select',
		'textarea',
		'details',
		'summary',
		'option',
		'optgroup',
	}
)
_INTERACTIVE_ROLES = frozenset(
	{
		'button',
		'link',
		'menuitem',
		'option',
		'radio',
		'checkbox',
		'tab',
		'textbox',
		'combobox',
		'slider',
		'spinbutton',
		'listbox',
		'search',
		'searchbox',
		'row',
		'cell',
		'gridcell',
	}
)
_AX_INTERACTIVE_PROPERTIES = frozenset(
	{
		'focusable',
		'editable',
		'settable',
		'checked',
		'expanded',
		'pressed',
		'selected',
		'required',
		'autocomplete',
		'keyshortcuts',
	}
)
_SEARCH_INDICATORS = (
	'search',
	'magnify',
	'glass',
	'lookup',
	'find',
	'query',
	'search-icon',
	'search-btn',
	'search-button',
	'searchbox',
)
_INLINE_INTERACTIVE_EVENTS = frozenset(
	{
		'onclick',
		'onmousedown',
		'onmouseup',
		'onpointerdown',
		'onpointerup',
		'onkeydown',
		'onkeyup',
	}
)
_ICON_INTERACTIVE_ATTRIBUTES = frozenset({'class', 'role', 'onclick', 'data-action', 'aria-label'})
_SCROLLABLE_OVERFLOW_VALUES = frozenset({'auto', 'scroll', 'overlay'})
_TEXT_IGNORED_TAGS = frozenset({'script', 'style', 'noscript', 'template'})
_MAX_TEXT = 240
# Keep a substantially larger ranked selector map than the model's normal
# prompt window. The prompt composer can trim text while the observation and
# its binding map retain the complete ranked near-viewport set.
# Native browser-use keeps the complete selector map.  Retain a substantially
# larger bounded map here so a dense but ordinary page does not lose the
# near-viewport fringe merely because its first screen has many controls.
_MAX_ELEMENTS = 800
_MAX_FRAMES = 100
_MAX_LISTENER_SCAN_NODES = 10_000
_MAX_LISTENER_BACKEND_IDS = 500
_LISTENER_DESCRIBE_CONCURRENCY = 48
_COLLECTION_TIMEOUT_SECONDS = 8.0
_LISTENER_TIMEOUT_SECONDS = 3.0


class CdpCollectionError(RuntimeError):
	"""Raised only when the essential CDP collection path cannot run."""


@dataclass(frozen=True, slots=True)
class _Rect:
	x: float
	y: float
	width: float
	height: float

	@property
	def right(self) -> float:
		return self.x + self.width

	@property
	def bottom(self) -> float:
		return self.y + self.height

	def intersects(self, other: '_Rect') -> bool:
		return self.x < other.right and self.right > other.x and self.y < other.bottom and self.bottom > other.y


@dataclass(slots=True)
class _SnapshotMeta:
	bounds: _Rect | None = None
	client_rect: _Rect | None = None
	scroll_rect: _Rect | None = None
	styles: dict[str, str] = field(default_factory=dict)
	is_clickable: bool = False
	paint_order: int | None = None


@dataclass(slots=True)
class _DocumentMeta:
	frame_id: str
	scroll_x: float = 0.0
	scroll_y: float = 0.0
	content_width: float = 0.0
	content_height: float = 0.0


@dataclass(slots=True)
class _AxMeta:
	role: str = ''
	name: str = ''
	properties: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class _FrameGeometry:
	frame_id: str
	frame: Frame | None
	viewport_width: float
	viewport_height: float
	scroll_x: float
	scroll_y: float
	global_x: float
	global_y: float
	chain_visible: bool


@dataclass(slots=True)
class _DomNode:
	backend_node_id: int
	frame_id: str
	tag: str
	attributes: dict[str, str]
	raw: Mapping[str, Any]
	parent_backend_node_id: int | None
	dom_order: int
	snapshot: _SnapshotMeta | None
	ax: _AxMeta | None
	has_js_listener: bool
	is_shadow_node: bool
	is_scrollable: bool
	children: list[int] = field(default_factory=list)
	signals: frozenset[str] = frozenset()
	visible: bool = False
	in_viewport: bool = False
	global_bounds: _Rect | None = None


@dataclass(frozen=True, slots=True)
class CollectedElement:
	"""An interactive node and the CDP scope needed to act on it later."""

	backend_node_id: int
	cdp_target: Page | Frame
	coordinate_frame: Frame | None
	frame: Frame
	frame_id: str
	frame_index: int
	frame_url: str
	tag: str
	text: str
	role: str
	name: str
	placeholder: str
	href: str
	input_type: str
	x: float
	y: float
	width: float
	height: float
	signals: tuple[str, ...]
	read_text: str
	options: tuple[tuple[str, str], ...]


def _value(raw: Any) -> Any:
	if isinstance(raw, Mapping):
		return raw.get('value')
	return raw


def _as_string(raw: Any) -> str:
	value = _value(raw)
	return str(value) if value is not None else ''


def _truthy(value: Any) -> bool:
	if isinstance(value, str):
		return value.strip().casefold() not in {'', '0', 'false', 'none', 'undefined', 'null'}
	return bool(value)


def _float(value: Any, default: float = 0.0) -> float:
	try:
		parsed = float(value)
	except (TypeError, ValueError):
		return default
	return parsed if math.isfinite(parsed) else default


def _rect(value: Any) -> _Rect | None:
	if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or len(value) < 4:
		return None
	x, y, width, height = (_float(value[index]) for index in range(4))
	if width <= 0 or height <= 0:
		return None
	return _Rect(x=x, y=y, width=width, height=height)


def _string_at(strings: Sequence[Any], value: Any) -> str:
	if isinstance(value, int) and 0 <= value < len(strings):
		return str(strings[value])
	return str(value) if value is not None else ''


def _attributes(node: Mapping[str, Any]) -> dict[str, str]:
	values = node.get('attributes')
	if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
		return {}
	result: dict[str, str] = {}
	for index in range(0, len(values) - 1, 2):
		result[str(values[index]).casefold()] = str(values[index + 1])
	return result


def _snapshot_data(snapshot: Mapping[str, Any]) -> tuple[dict[int, _SnapshotMeta], dict[str, _DocumentMeta]]:
	strings = list(snapshot.get('strings') or [])
	by_backend_id: dict[int, _SnapshotMeta] = {}
	documents: dict[str, _DocumentMeta] = {}
	for raw_document in snapshot.get('documents') or []:
		if not isinstance(raw_document, Mapping):
			continue
		frame_id = _string_at(strings, raw_document.get('frameId'))
		if frame_id:
			documents[frame_id] = _DocumentMeta(
				frame_id=frame_id,
				scroll_x=_float(raw_document.get('scrollOffsetX')),
				scroll_y=_float(raw_document.get('scrollOffsetY')),
				content_width=_float(raw_document.get('contentWidth')),
				content_height=_float(raw_document.get('contentHeight')),
			)
		nodes = raw_document.get('nodes')
		layout = raw_document.get('layout')
		if not isinstance(nodes, Mapping) or not isinstance(layout, Mapping):
			continue
		backend_ids = nodes.get('backendNodeId') or []
		layout_index_by_node_index: dict[int, int] = {}
		for layout_index, node_index in enumerate(layout.get('nodeIndex') or []):
			if isinstance(node_index, int) and node_index not in layout_index_by_node_index:
				layout_index_by_node_index[node_index] = layout_index
		clickable = nodes.get('isClickable')
		clickable_indices = set(clickable.get('index') or []) if isinstance(clickable, Mapping) else set()
		styles = layout.get('styles') or []
		bounds = layout.get('bounds') or []
		client_rects = layout.get('clientRects') or []
		scroll_rects = layout.get('scrollRects') or []
		paint_orders = layout.get('paintOrders') or []
		for node_index, backend_node_id in enumerate(backend_ids):
			if not isinstance(backend_node_id, int):
				continue
			layout_index = layout_index_by_node_index.get(node_index)
			meta = _SnapshotMeta(is_clickable=node_index in clickable_indices)
			if layout_index is not None:
				if layout_index < len(bounds):
					meta.bounds = _rect(bounds[layout_index])
				if layout_index < len(client_rects):
					meta.client_rect = _rect(client_rects[layout_index])
				if layout_index < len(scroll_rects):
					meta.scroll_rect = _rect(scroll_rects[layout_index])
				if layout_index < len(styles) and isinstance(styles[layout_index], Sequence):
					meta.styles = {
						style_name: _string_at(strings, style_index)
						for style_name, style_index in zip(_COMPUTED_STYLES, styles[layout_index], strict=False)
					}
				if layout_index < len(paint_orders):
					paint_order = paint_orders[layout_index]
					if isinstance(paint_order, int):
						meta.paint_order = paint_order
			by_backend_id[backend_node_id] = meta
	return by_backend_id, documents


def _snapshot_is_actually_scrollable(meta: _SnapshotMeta | None) -> bool:
	"""Mirror browser-use's CSS + scroll/client-rect fallback detection."""

	if meta is None or meta.scroll_rect is None or meta.client_rect is None:
		return False
	if (
		meta.scroll_rect.width <= meta.client_rect.width + 1
		and meta.scroll_rect.height <= meta.client_rect.height + 1
	):
		return False
	styles = meta.styles
	overflow = styles.get('overflow', 'visible').casefold()
	return any(
		styles.get(name, overflow).casefold() in _SCROLLABLE_OVERFLOW_VALUES
		for name in ('overflow', 'overflow-x', 'overflow-y')
	)


def _ax_data(results: Iterable[Any]) -> dict[int, _AxMeta]:
	lookup: dict[int, _AxMeta] = {}
	for result in results:
		if not isinstance(result, Mapping):
			continue
		for raw_node in result.get('nodes') or []:
			if not isinstance(raw_node, Mapping):
				continue
			backend_node_id = raw_node.get('backendDOMNodeId')
			if not isinstance(backend_node_id, int):
				continue
			properties: dict[str, Any] = {}
			for property_value in raw_node.get('properties') or []:
				if not isinstance(property_value, Mapping):
					continue
				name = property_value.get('name')
				if isinstance(name, str):
					properties[name.casefold()] = _value(property_value.get('value'))
			lookup[backend_node_id] = _AxMeta(
				role=_as_string(raw_node.get('role')).casefold(),
				name=_as_string(raw_node.get('name')),
				properties=properties,
			)
	return lookup


def _frame_tree_entries(frame_tree: Mapping[str, Any]) -> list[Mapping[str, Any]]:
	entries: list[Mapping[str, Any]] = []

	def visit(item: Mapping[str, Any]) -> None:
		frame = item.get('frame')
		if isinstance(frame, Mapping):
			entries.append(frame)
		for child in item.get('childFrames') or []:
			if isinstance(child, Mapping):
				visit(child)

	root = frame_tree.get('frameTree')
	if isinstance(root, Mapping):
		visit(root)
	return entries


def _frame_ids_from_tree(frame_tree: Mapping[str, Any]) -> list[str]:
	return [str(item['id']) for item in _frame_tree_entries(frame_tree) if isinstance(item.get('id'), str)]


def _map_playwright_frames(page: Page, frame_tree: Mapping[str, Any]) -> dict[str, Frame]:
	"""Match CDP frame IDs to public Playwright Frame objects without private APIs."""

	entries = _frame_tree_entries(frame_tree)
	if not entries:
		return {}
	root = next((entry for entry in entries if not entry.get('parentId')), entries[0])
	root_id = str(root.get('id', ''))
	mapped: dict[str, Frame] = {root_id: page.main_frame} if root_id else {}
	all_frames = list(page.frames)
	used = {id(page.main_frame)}
	children_by_parent: dict[int, list[Frame]] = {}
	for frame in all_frames:
		parent = frame.parent_frame
		if parent is not None:
			children_by_parent.setdefault(id(parent), []).append(frame)

	for entry in entries:
		frame_id = entry.get('id')
		parent_id = entry.get('parentId')
		if not isinstance(frame_id, str) or frame_id in mapped or not isinstance(parent_id, str):
			continue
		parent = mapped.get(parent_id)
		if parent is None:
			continue
		candidates = [frame for frame in children_by_parent.get(id(parent), []) if id(frame) not in used]
		if not candidates:
			continue
		url = str(entry.get('url', ''))
		name = str(entry.get('name', ''))
		matching = [frame for frame in candidates if frame.url == url]
		# URL/name are useful discriminators only when they identify one child.
		# Duplicate srcdoc/about:blank siblings are intentionally kept in public
		# Playwright frame-tree order instead of being guessed from a shared URL.
		if name:
			by_name = [frame for frame in matching if getattr(frame, 'name', '') == name]
			if len(by_name) == 1:
				matching = by_name
		chosen = matching[0] if len(matching) == 1 else candidates[0]
		mapped[frame_id] = chosen
		used.add(id(chosen))
	return mapped


async def _listener_backend_ids(session: CDPSession, logger: logging.Logger) -> set[int]:
	"""Return direct click/mouse/pointer listener owners without DOM mutation."""

	expression = f"""
	(() => {{
		if (typeof getEventListeners !== 'function') return null;
		const elements = [];
		const seen = new Set();
		const walk = (root) => {{
			for (const element of root.querySelectorAll('*')) {{
				if (seen.has(element)) continue;
				seen.add(element);
				elements.push(element);
				if (element.shadowRoot) walk(element.shadowRoot);
			}}
		}};
		walk(document);
		if (elements.length > {_MAX_LISTENER_SCAN_NODES}) return null;
		const result = [];
		for (const element of elements) {{
			try {{
				const listeners = getEventListeners(element);
				if (listeners.click || listeners.mousedown || listeners.mouseup || listeners.pointerdown || listeners.pointerup) {{
					result.push(element);
				}}
			}} catch (_error) {{}}
		}}
		return result;
	}})()
	"""

	array_object_id: str | None = None
	try:
		initial = await asyncio.wait_for(
			session.send(
				'Runtime.evaluate',
				{'expression': expression, 'includeCommandLineAPI': True, 'returnByValue': False},
			),
			timeout=_LISTENER_TIMEOUT_SECONDS,
		)
		result = initial.get('result') if isinstance(initial, Mapping) else None
		if not isinstance(result, Mapping):
			return set()
		array_object_id = result.get('objectId') if isinstance(result.get('objectId'), str) else None
		if not array_object_id:
			return set()
		properties = await asyncio.wait_for(
			session.send('Runtime.getProperties', {'objectId': array_object_id, 'ownProperties': True}),
			timeout=_LISTENER_TIMEOUT_SECONDS,
		)
		object_ids: list[str] = []
		for property_value in properties.get('result') or []:
			if not isinstance(property_value, Mapping) or not str(property_value.get('name', '')).isdigit():
				continue
			value = property_value.get('value')
			if isinstance(value, Mapping) and isinstance(value.get('objectId'), str):
				object_ids.append(value['objectId'])
		if len(object_ids) > _MAX_LISTENER_BACKEND_IDS:
			logger.debug(
				'CDP listener scan found %s listener owners; limiting backend-ID resolution to %s',
				len(object_ids),
				_MAX_LISTENER_BACKEND_IDS,
			)
			object_ids = object_ids[:_MAX_LISTENER_BACKEND_IDS]

		async def describe(object_id: str) -> int | None:
			try:
				value = await session.send('DOM.describeNode', {'objectId': object_id})
				node = value.get('node') if isinstance(value, Mapping) else None
				backend_node_id = node.get('backendNodeId') if isinstance(node, Mapping) else None
				return backend_node_id if isinstance(backend_node_id, int) else None
			except Exception:
				return None

		backend_ids: list[int | None] = []
		for start in range(0, len(object_ids), _LISTENER_DESCRIBE_CONCURRENCY):
			batch = object_ids[start : start + _LISTENER_DESCRIBE_CONCURRENCY]
			backend_ids.extend(
				await asyncio.wait_for(
					asyncio.gather(*(describe(object_id) for object_id in batch)),
					timeout=_LISTENER_TIMEOUT_SECONDS,
				)
			)
		return {backend_node_id for backend_node_id in backend_ids if backend_node_id is not None}
	except Exception as exc:
		logger.debug('CDP listener detection unavailable: %s', exc)
		return set()
	finally:
		if array_object_id:
			with contextlib.suppress(Exception):
				await session.send('Runtime.releaseObject', {'objectId': array_object_id})


def _text_from_raw(node: Mapping[str, Any], *, limit: int = _MAX_TEXT) -> str:
	parts: list[str] = []

	def visit(current: Mapping[str, Any]) -> None:
		if sum(len(part) for part in parts) >= limit:
			return
		node_type = current.get('nodeType')
		tag = str(current.get('nodeName', '')).casefold()
		if node_type == 3:
			value = str(current.get('nodeValue', '')).strip()
			if value:
				parts.append(value)
			return
		if tag in _TEXT_IGNORED_TAGS:
			return
		for child in current.get('children') or []:
			if isinstance(child, Mapping):
				visit(child)
		for shadow_root in current.get('shadowRoots') or []:
			if isinstance(shadow_root, Mapping):
				visit(shadow_root)

	visit(node)
	return re.sub(r'\s+', ' ', ' '.join(parts)).strip()[:limit]


def _is_css_visible(node: _DomNode) -> bool:
	meta = node.snapshot
	if meta is None or meta.bounds is None:
		return False
	styles = meta.styles
	if styles.get('display', '').casefold() == 'none' or styles.get('visibility', '').casefold() == 'hidden':
		return False
	if _float(styles.get('opacity', '1'), 1.0) <= 0:
		return False
	if styles.get('pointer-events', '').casefold() == 'none':
		return False
	attributes = node.attributes
	if 'hidden' in attributes or 'inert' in attributes or attributes.get('aria-hidden', '').casefold() == 'true':
		return False
	return True


def _has_form_control_descendant(node: _DomNode, by_backend_id: Mapping[int, _DomNode], depth: int = 2) -> bool:
	if depth <= 0:
		return False
	for child_id in node.children:
		child = by_backend_id.get(child_id)
		if child is None:
			continue
		if child.tag in {'input', 'select', 'textarea'}:
			return True
		if _has_form_control_descendant(child, by_backend_id, depth - 1):
			return True
	return False


def _has_interactive_descendant(node: _DomNode, by_backend_id: Mapping[int, _DomNode]) -> bool:
	for child_id in node.children:
		child = by_backend_id.get(child_id)
		if child is None:
			continue
		if child.signals or _has_interactive_descendant(child, by_backend_id):
			return True
	return False


def _interactive_signals(node: _DomNode, by_backend_id: Mapping[int, _DomNode]) -> frozenset[str]:
	attributes = node.attributes
	meta = node.snapshot
	ax = node.ax
	if node.tag in {'html', 'body'}:
		return frozenset()
	if (
		'disabled' in attributes
		or attributes.get('aria-disabled', '').casefold() == 'true'
		or (ax is not None and (_truthy(ax.properties.get('disabled')) or _truthy(ax.properties.get('hidden'))))
	):
		return frozenset()
	if node.tag == 'input' and attributes.get('type', '').casefold() == 'hidden':
		return frozenset()
	# Labels that proxy through ``for`` routinely produce duplicate target IDs.
	# Keep an explicitly registered listener, which browser-use also prioritises.
	if node.tag == 'label' and 'for' in attributes and not node.has_js_listener:
		return frozenset()
	signals: set[str] = set()
	if node.has_js_listener:
		signals.add('js_listener')
	if node.tag in _INTERACTIVE_TAGS:
		signals.add('native_tag')
	if node.tag in {'iframe', 'frame'} and meta is not None and meta.bounds is not None and meta.bounds.width > 100 and meta.bounds.height > 100:
		signals.add('iframe')
	if attributes.get('contenteditable', '').casefold() not in {'', 'false'}:
		signals.add('contenteditable')
	if node.tag in {'label', 'span'} and (node.tag != 'label' or 'for' not in attributes) and _has_form_control_descendant(
		node, by_backend_id
	):
		signals.add('label_wrapper')
	if any(name in _INLINE_INTERACTIVE_EVENTS for name in attributes):
		signals.add('inline_event')
	if 'tabindex' in attributes:
		signals.add('tabindex')
	role = attributes.get('role', '').casefold()
	if role in _INTERACTIVE_ROLES:
		signals.add('aria_role')
	if ax is not None:
		if ax.role in _INTERACTIVE_ROLES:
			signals.add('ax_role')
		if any(_truthy(ax.properties.get(name)) for name in _AX_INTERACTIVE_PROPERTIES):
			signals.add('ax_property')
	if meta is not None:
		if meta.is_clickable:
			signals.add('snapshot_clickable')
		if meta.styles.get('cursor', '').casefold() == 'pointer':
			signals.add('cursor_pointer')
	searchable = ' '.join(
		value.casefold()
		for name, value in attributes.items()
		if name in {'class', 'id'} or name.startswith('data-')
	)
	if searchable and any(indicator in searchable for indicator in _SEARCH_INDICATORS):
		signals.add('search_affordance')
	if (
		meta is not None
		and meta.bounds is not None
		and 10 <= meta.bounds.width <= 50
		and 10 <= meta.bounds.height <= 50
		and any(name in attributes for name in _ICON_INTERACTIVE_ATTRIBUTES)
	):
		signals.add('small_icon_affordance')
	return frozenset(signals)


def _is_opaque(meta: _SnapshotMeta | None) -> bool:
	if meta is None or meta.bounds is None:
		return False
	styles = meta.styles
	if _float(styles.get('opacity', '1'), 1.0) < 0.8:
		return False
	background = styles.get('background-color', '').replace(' ', '').casefold()
	if not background or background == 'transparent' or background.endswith(',0)') or background == 'rgba(0,0,0,0)':
		return False
	return True


def _paint_filtered(nodes: Sequence[_DomNode]) -> set[int]:
	"""Return candidates completely hidden by later opaque paint rectangles.

	This intentionally mirrors browser-use's conservative paint-order filter.  A
	union cap inside ``RectUnionPure`` prevents pathological pages from turning
	this visibility hint into an unbounded computation.
	"""
	ignored: set[int] = set()
	by_frame: dict[str, list[_DomNode]] = {}
	for node in nodes:
		if node.visible and node.snapshot is not None and node.snapshot.bounds is not None and node.snapshot.paint_order is not None:
			by_frame.setdefault(node.frame_id, []).append(node)
	for frame_nodes in by_frame.values():
		union = RectUnionPure()
		for node in sorted(frame_nodes, key=lambda item: int(item.snapshot.paint_order or 0), reverse=True):
			bounds = node.snapshot.bounds if node.snapshot else None
			if bounds is None:
				continue
			rect = Rect(bounds.x, bounds.y, bounds.right, bounds.bottom)
			if node.signals and union.contains(rect):
				ignored.add(node.backend_node_id)
			if _is_opaque(node.snapshot):
				union.add(rect)
	return ignored


def _contained(child: _Rect, parent: _Rect, threshold: float = 0.99) -> bool:
	intersection_width = max(0.0, min(child.right, parent.right) - max(child.x, parent.x))
	intersection_height = max(0.0, min(child.bottom, parent.bottom) - max(child.y, parent.y))
	area = child.width * child.height
	return area > 0 and (intersection_width * intersection_height) / area >= threshold


def _independently_interactive(node: _DomNode) -> bool:
	return bool(node.signals & {'js_listener', 'inline_event', 'aria_role', 'ax_role', 'ax_property', 'native_tag', 'label_wrapper'})


def _dedupe_nested_candidates(candidates: Sequence[_DomNode], by_backend_id: Mapping[int, _DomNode]) -> list[_DomNode]:
	candidate_ids = {node.backend_node_id for node in candidates}
	result: list[_DomNode] = []
	for node in candidates:
		if node.global_bounds is None or _independently_interactive(node):
			result.append(node)
			continue
		parent_id = node.parent_backend_node_id
		redundant = False
		while parent_id is not None:
			parent = by_backend_id.get(parent_id)
			if parent is None:
				break
			if parent.backend_node_id in candidate_ids and parent.global_bounds is not None and _contained(node.global_bounds, parent.global_bounds):
				redundant = True
				break
			parent_id = parent.parent_backend_node_id
		if not redundant:
			result.append(node)
	return result


def _candidate_text(node: _DomNode) -> tuple[str, str, str]:
	attributes = node.attributes
	ax = node.ax
	text = _text_from_raw(node.raw)
	name = (
		attributes.get('aria-label')
		or attributes.get('title')
		or (ax.name if ax is not None else '')
		or text
	)
	if not text:
		text = attributes.get('value', '') or name
	role = attributes.get('role') or (ax.role if ax is not None else '')
	return text[:_MAX_TEXT], name[:_MAX_TEXT], role[:_MAX_TEXT]


def _selection_options(node: _DomNode, by_backend_id: Mapping[int, _DomNode]) -> tuple[tuple[str, str], ...]:
	"""Capture native-select options while the DOM observation is current."""

	if node.tag != 'select':
		return ()
	result: list[tuple[str, str]] = []

	def visit(current: _DomNode) -> None:
		for child_id in current.children:
			child = by_backend_id.get(child_id)
			if child is None:
				continue
			if child.tag == 'option':
				label = child.attributes.get('label') or _text_from_raw(child.raw)
				value = child.attributes.get('value', label)
				result.append((value[:_MAX_TEXT], label[:_MAX_TEXT]))
			else:
				visit(child)

	visit(node)
	return tuple(result)


def _rank_key(node: _DomNode, frame_index: int, geometry: _FrameGeometry) -> tuple[float, ...]:
	bounds = node.global_bounds
	if bounds is None:
		return (9.0, float('inf'), float(frame_index), float(node.dom_order))
	local_top = bounds.y - geometry.global_y
	local_bottom = bounds.bottom - geometry.global_y
	if local_bottom > 0 and local_top < geometry.viewport_height:
		tier = 0.0
		distance = 0.0
	elif local_bottom <= 0:
		tier = 1.0
		distance = abs(local_bottom)
	else:
		tier = 1.0
		distance = local_top - geometry.viewport_height
	strong = 0.0 if node.signals & {'js_listener', 'snapshot_clickable', 'native_tag', 'aria_role', 'ax_role'} else 1.0
	return (tier, distance, strong, float(frame_index), bounds.y, bounds.x, float(node.dom_order))


def _viewport_from_metrics(metrics: Mapping[str, Any], fallback_width: float, fallback_height: float) -> tuple[float, float, float, float]:
	visual = metrics.get('cssVisualViewport') if isinstance(metrics.get('cssVisualViewport'), Mapping) else {}
	width = _float(visual.get('clientWidth'), fallback_width)
	height = _float(visual.get('clientHeight'), fallback_height)
	scroll_x = _float(visual.get('pageX'), _float(visual.get('offsetX')))
	scroll_y = _float(visual.get('pageY'), _float(visual.get('offsetY')))
	return width, height, scroll_x, scroll_y


def _frame_host_visible(global_x: float, global_y: float, width: float, height: float, root_width: float, root_height: float) -> bool:
	return global_x < root_width and global_x + width > 0 and global_y < root_height + 1000 and global_y + height > -1000


async def _capture_scope(
	session: CDPSession,
	*,
	logger: logging.Logger,
	frame_ids: Sequence[str],
) -> tuple[Mapping[str, Any], Mapping[str, Any], Mapping[str, Any], set[int], list[Any]]:
	async def ax_tree(frame_id: str) -> Any:
		try:
			return await session.send('Accessibility.getFullAXTree', {'frameId': frame_id})
		except Exception as exc:
			logger.debug('Skipping AX tree for frame %s: %s', frame_id, exc)
			return None

	try:
		dom_result, snapshot, metrics, listener_ids = await asyncio.wait_for(
			asyncio.gather(
				session.send('DOM.getDocument', {'depth': -1, 'pierce': True}),
				session.send(
					'DOMSnapshot.captureSnapshot',
					{
						'computedStyles': list(_COMPUTED_STYLES),
						'includePaintOrder': True,
						'includeDOMRects': True,
					},
				),
				session.send('Page.getLayoutMetrics'),
				_listener_backend_ids(session, logger),
			),
			timeout=_COLLECTION_TIMEOUT_SECONDS,
		)
	except Exception as exc:
		raise CdpCollectionError(f'essential CDP DOM collection failed: {type(exc).__name__}: {exc}') from exc
	if not isinstance(dom_result, Mapping) or not isinstance(dom_result.get('root'), Mapping):
		raise CdpCollectionError('DOM.getDocument returned no root node')
	if not isinstance(snapshot, Mapping):
		snapshot = {'documents': [], 'strings': []}
	if not isinstance(metrics, Mapping):
		metrics = {}
	ax_trees = await asyncio.gather(*(ax_tree(frame_id) for frame_id in frame_ids), return_exceptions=False)
	return dom_result, snapshot, metrics, listener_ids if isinstance(listener_ids, set) else set(), ax_trees


def _flatten_scope(
	root: Mapping[str, Any],
	*,
	root_frame_id: str,
	snapshot_by_backend_id: Mapping[int, _SnapshotMeta],
	ax_by_backend_id: Mapping[int, _AxMeta],
	listener_ids: set[int],
) -> tuple[list[_DomNode], set[str], dict[str, tuple[int, _SnapshotMeta | None]]]:
	nodes: list[_DomNode] = []
	by_backend_id: dict[int, _DomNode] = {}
	seen_frame_ids: set[str] = {root_frame_id} if root_frame_id else set()
	# Child frame id -> (iframe backend id, iframe snapshot), used to construct
	# local viewport and top-level coordinates for the embedded document.
	frame_hosts: dict[str, tuple[int, _SnapshotMeta | None]] = {}
	next_order = 0

	def visit(
		raw: Mapping[str, Any],
		*,
		frame_id: str,
		parent_backend_node_id: int | None,
		is_shadow_node: bool,
	) -> None:
		nonlocal next_order
		backend_node_id = raw.get('backendNodeId')
		node_type = raw.get('nodeType')
		current_parent = parent_backend_node_id
		current: _DomNode | None = None
		if node_type == 1 and isinstance(backend_node_id, int):
			snapshot = snapshot_by_backend_id.get(backend_node_id)
			current = _DomNode(
				backend_node_id=backend_node_id,
				frame_id=frame_id,
				tag=str(raw.get('nodeName', '')).casefold(),
				attributes=_attributes(raw),
				raw=raw,
				parent_backend_node_id=parent_backend_node_id,
				dom_order=next_order,
				snapshot=snapshot,
				ax=ax_by_backend_id.get(backend_node_id),
				has_js_listener=backend_node_id in listener_ids,
				is_shadow_node=is_shadow_node,
				is_scrollable=bool(raw.get('isScrollable')) or _snapshot_is_actually_scrollable(snapshot),
			)
			next_order += 1
			nodes.append(current)
			by_backend_id[backend_node_id] = current
			if parent_backend_node_id is not None and parent_backend_node_id in by_backend_id:
				by_backend_id[parent_backend_node_id].children.append(backend_node_id)
			current_parent = backend_node_id

		for child in raw.get('children') or []:
			if isinstance(child, Mapping):
				visit(child, frame_id=frame_id, parent_backend_node_id=current_parent, is_shadow_node=is_shadow_node)
		for shadow_root in raw.get('shadowRoots') or []:
			if isinstance(shadow_root, Mapping):
				visit(shadow_root, frame_id=frame_id, parent_backend_node_id=current_parent, is_shadow_node=True)
		content_document = raw.get('contentDocument')
		if isinstance(content_document, Mapping):
			# Chromium puts the child frame id on the iframe owner in the
			# DOM.getDocument tree. Some versions also expose it on the document
			# root or documentElement, so retain both compatibility fallbacks.
			document_element_frame_id = next(
				(
					child.get('frameId')
					for child in content_document.get('children') or []
					if isinstance(child, Mapping) and child.get('frameId')
				),
				'',
			)
			child_frame_id = str(raw.get('frameId') or content_document.get('frameId') or document_element_frame_id or '')
			if child_frame_id:
				seen_frame_ids.add(child_frame_id)
				frame_hosts[child_frame_id] = (
					current.backend_node_id if current is not None else -1,
					current.snapshot if current is not None else None,
				)
				visit(content_document, frame_id=child_frame_id, parent_backend_node_id=None, is_shadow_node=False)

	visit(root, frame_id=root_frame_id, parent_backend_node_id=None, is_shadow_node=False)
	return nodes, seen_frame_ids, frame_hosts


def _frame_geometries(
	*,
	nodes: Sequence[_DomNode],
	documents: Mapping[str, _DocumentMeta],
	frame_hosts: Mapping[str, tuple[int, _SnapshotMeta | None]],
	frame_by_id: Mapping[str, Frame],
	root_frame_id: str,
	root_width: float,
	root_height: float,
	root_scroll_x: float,
	root_scroll_y: float,
	root_global_x: float = 0.0,
	root_global_y: float = 0.0,
	root_chain_visible: bool = True,
) -> dict[str, _FrameGeometry]:
	by_backend_id = {node.backend_node_id: node for node in nodes}
	geometries: dict[str, _FrameGeometry] = {
		root_frame_id: _FrameGeometry(
			frame_id=root_frame_id,
			frame=frame_by_id.get(root_frame_id),
			viewport_width=max(1.0, root_width),
			viewport_height=max(1.0, root_height),
			scroll_x=root_scroll_x,
			scroll_y=root_scroll_y,
			global_x=root_global_x,
			global_y=root_global_y,
			chain_visible=root_chain_visible,
		)
	}
	# A DOM preorder naturally visits iframe owners before their contentDocument.
	# Repeating until no progress also handles benign snapshot/document ordering
	# races without assigning a child a guessed parent offset.
	for _ in range(max(1, len(frame_hosts) + 1)):
		progress = False
		for frame_id, (host_backend_id, host_snapshot) in frame_hosts.items():
			if frame_id in geometries:
				continue
			host = by_backend_id.get(host_backend_id)
			if host is None or host_snapshot is None or host_snapshot.bounds is None:
				continue
			parent_geometry = geometries.get(host.frame_id)
			if parent_geometry is None:
				continue
			client = host_snapshot.client_rect or _Rect(0.0, 0.0, host_snapshot.bounds.width, host_snapshot.bounds.height)
			document = documents.get(frame_id, _DocumentMeta(frame_id))
			global_x = parent_geometry.global_x + host_snapshot.bounds.x - parent_geometry.scroll_x + client.x
			global_y = parent_geometry.global_y + host_snapshot.bounds.y - parent_geometry.scroll_y + client.y
			chain_visible = parent_geometry.chain_visible and _is_css_visible(host) and _frame_host_visible(
				global_x,
				global_y,
				client.width,
				client.height,
				root_width,
				root_height,
			)
			geometries[frame_id] = _FrameGeometry(
				frame_id=frame_id,
				frame=frame_by_id.get(frame_id),
				viewport_width=max(1.0, client.width),
				viewport_height=max(1.0, client.height),
				scroll_x=document.scroll_x,
				scroll_y=document.scroll_y,
				global_x=global_x,
				global_y=global_y,
				chain_visible=chain_visible,
			)
			progress = True
		if not progress:
			break
	return geometries


def _assign_visibility(nodes: Sequence[_DomNode], geometries: Mapping[str, _FrameGeometry]) -> None:
	for node in nodes:
		geometry = geometries.get(node.frame_id)
		if geometry is None or not geometry.chain_visible or not _is_css_visible(node):
			continue
		bounds = node.snapshot.bounds if node.snapshot is not None else None
		if bounds is None:
			continue
		local_x = bounds.x - geometry.scroll_x
		local_y = bounds.y - geometry.scroll_y
		# Native browser-use only extends the vertical window.  It does not make
		# horizontally off-screen candidates available merely because they are
		# close to the viewport.
		if local_x >= geometry.viewport_width or local_x + bounds.width <= 0:
			continue
		if local_y >= geometry.viewport_height + 1000 or local_y + bounds.height <= -1000:
			continue
		node.visible = True
		node.in_viewport = local_y < geometry.viewport_height and local_y + bounds.height > 0
		node.global_bounds = _Rect(
			x=geometry.global_x + local_x,
			y=geometry.global_y + local_y,
			width=bounds.width,
			height=bounds.height,
		)


def _is_dropdown_container(node: _DomNode) -> bool:
	"""Match browser-use's exception for scrollable dropdown containers."""

	role = node.attributes.get('role', '').casefold()
	classes = node.attributes.get('class', '').casefold()
	class_tokens = set(classes.split())
	return (
		role in {'listbox', 'menu', 'combobox', 'menubar', 'tree', 'grid'}
		or node.tag == 'select'
		or bool({'dropdown', 'dropdown-menu', 'select-menu'} & class_tokens)
		or ('ui' in class_tokens and 'dropdown' in classes)
	)


def _finalize_nodes(nodes: Sequence[_DomNode]) -> dict[int, _DomNode]:
	by_backend_id = {node.backend_node_id: node for node in nodes}
	for node in nodes:
		node.signals = _interactive_signals(node, by_backend_id)
	# Scroll containers are candidates only when they do not wrap another
	# actionable descendant. This must be a second pass: testing it while the
	# first pass is still assigning signals depends on DOM traversal order.
	for node in nodes:
		if node.is_scrollable and (_is_dropdown_container(node) or not _has_interactive_descendant(node, by_backend_id)):
			node.signals = frozenset((*node.signals, 'scrollable'))
	return by_backend_id


async def _scope_origin_for_oopif(frame: Frame, root_width: float, root_height: float) -> tuple[float, float, bool]:
	try:
		owner = await frame.frame_element()
		box = await owner.bounding_box()
		border = await owner.evaluate('(element) => ({left: element.clientLeft, top: element.clientTop})')
		if not box:
			return 0.0, 0.0, False
		left = _float(border.get('left') if isinstance(border, Mapping) else 0.0)
		top = _float(border.get('top') if isinstance(border, Mapping) else 0.0)
		x = _float(box.get('x')) + left
		y = _float(box.get('y')) + top
		return x, y, _frame_host_visible(x, y, _float(box.get('width')), _float(box.get('height')), root_width, root_height)
	except Exception:
		return 0.0, 0.0, False


async def _collect_scope(
	context: BrowserContext,
	*,
	cdp_target: Page | Frame,
	root_frame_id: str,
	frame_by_id: Mapping[str, Frame],
	frame_index_by_object_id: Mapping[int, int],
	root_width: float,
	root_height: float,
	root_global_x: float = 0.0,
	root_global_y: float = 0.0,
	root_chain_visible: bool = True,
	coordinate_frame: Frame | None = None,
	logger: logging.Logger,
) -> tuple[list[CollectedElement], set[str]]:
	session: CDPSession | None = None
	try:
		session = await context.new_cdp_session(cdp_target)
		frame_tree = await asyncio.wait_for(session.send('Page.getFrameTree'), timeout=_COLLECTION_TIMEOUT_SECONDS)
		if not isinstance(frame_tree, Mapping):
			raise CdpCollectionError('Page.getFrameTree returned an invalid value')
		frame_ids = _frame_ids_from_tree(frame_tree)
		if root_frame_id and root_frame_id not in frame_ids:
			frame_ids = [root_frame_id, *frame_ids]
		dom_result, snapshot, metrics, listener_ids, ax_results = await _capture_scope(
			session, logger=logger, frame_ids=frame_ids[:_MAX_FRAMES]
		)
		root = dom_result['root']
		snapshot_by_backend_id, documents = _snapshot_data(snapshot)
		ax_by_backend_id = _ax_data(ax_results)
		nodes, seen_frame_ids, frame_hosts = _flatten_scope(
			root,
			root_frame_id=root_frame_id,
			snapshot_by_backend_id=snapshot_by_backend_id,
			ax_by_backend_id=ax_by_backend_id,
			listener_ids=listener_ids,
		)
		fallback_width = root_width
		fallback_height = root_height
		local_width, local_height, local_scroll_x, local_scroll_y = _viewport_from_metrics(metrics, fallback_width, fallback_height)
		geometries = _frame_geometries(
			nodes=nodes,
			documents=documents,
			frame_hosts=frame_hosts,
			frame_by_id=frame_by_id,
			root_frame_id=root_frame_id,
			root_width=local_width,
			root_height=local_height,
			root_scroll_x=local_scroll_x,
			root_scroll_y=local_scroll_y,
			root_global_x=root_global_x,
			root_global_y=root_global_y,
			root_chain_visible=root_chain_visible,
		)
		_assign_visibility(nodes, geometries)
		by_backend_id = _finalize_nodes(nodes)
		paint_hidden = _paint_filtered(nodes)
		candidates = [
			node
			for node in nodes
			if node.visible
			and node.signals
			and node.backend_node_id not in paint_hidden
			and node.global_bounds is not None
			and node.frame_id in geometries
			and geometries[node.frame_id].frame is not None
		]
		candidates = _dedupe_nested_candidates(candidates, by_backend_id)
		candidates.sort(
			key=lambda node: _rank_key(
				node,
				frame_index_by_object_id.get(id(geometries[node.frame_id].frame), 0),
				geometries[node.frame_id],
			)
		)
		result: list[CollectedElement] = []
		for node in candidates:
			if len(result) >= _MAX_ELEMENTS:
				break
			geometry = geometries[node.frame_id]
			frame = geometry.frame
			bounds = node.global_bounds
			if frame is None or bounds is None:
				continue
			text, name, role = _candidate_text(node)
			attributes = node.attributes
			href = attributes.get('href', '')
			if href:
				href = urljoin(frame.url, href)
			result.append(
				CollectedElement(
					backend_node_id=node.backend_node_id,
					cdp_target=cdp_target,
					coordinate_frame=coordinate_frame,
					frame=frame,
					frame_id=node.frame_id,
					frame_index=frame_index_by_object_id.get(id(frame), 0),
					frame_url=frame.url,
					tag=node.tag or 'element',
					text=text,
					role=role,
					name=name,
					placeholder=attributes.get('placeholder', '')[:_MAX_TEXT],
					href=href[:2000],
					input_type=attributes.get('type', ''),
					x=bounds.x,
					y=bounds.y,
					width=bounds.width,
					height=bounds.height,
					signals=tuple(sorted(node.signals)),
					read_text=(text or name)[:_MAX_TEXT],
					options=_selection_options(node, by_backend_id),
				)
			)
		return result, seen_frame_ids
	except CdpCollectionError:
		raise
	except Exception as exc:
		raise CdpCollectionError(f'CDP collection scope failed: {type(exc).__name__}: {exc}') from exc
	finally:
		if session is not None:
			with contextlib.suppress(Exception):
				await session.detach()


async def collect_interactive_elements(
	context: BrowserContext,
	page: Page,
	*,
	logger: logging.Logger,
) -> list[CollectedElement]:
	"""Collect ranked interactive nodes without modifying the business DOM.

	The main page CDP target covers normal same-process iframes.  Frames absent
	from that deep tree are OOPIFs; each gets its own CDP target and a coordinate
	transform for later Playwright mouse actions.
	"""
	try:
		main_session = await context.new_cdp_session(page)
		try:
			frame_tree = await asyncio.wait_for(main_session.send('Page.getFrameTree'), timeout=_COLLECTION_TIMEOUT_SECONDS)
			metrics = await asyncio.wait_for(main_session.send('Page.getLayoutMetrics'), timeout=_COLLECTION_TIMEOUT_SECONDS)
		finally:
			with contextlib.suppress(Exception):
				await main_session.detach()
	except Exception as exc:
		raise CdpCollectionError(f'could not create the main Playwright CDP session: {type(exc).__name__}: {exc}') from exc
	if not isinstance(frame_tree, Mapping):
		raise CdpCollectionError('Page.getFrameTree returned an invalid value')
	frame_by_id = _map_playwright_frames(page, frame_tree)
	entries = _frame_tree_entries(frame_tree)
	if not entries:
		raise CdpCollectionError('Page.getFrameTree did not contain a root frame')
	root_frame_id = str(entries[0].get('id', ''))
	if not root_frame_id:
		raise CdpCollectionError('root CDP frame did not have an id')
	viewport_width, viewport_height, _scroll_x, _scroll_y = _viewport_from_metrics(
		metrics if isinstance(metrics, Mapping) else {},
		float((page.viewport_size or {}).get('width', 0)),
		float((page.viewport_size or {}).get('height', 0)),
	)
	frame_index_by_object_id = {id(frame): index for index, frame in enumerate(page.frames)}
	main_elements, seen_frame_ids = await _collect_scope(
		context,
		cdp_target=page,
		root_frame_id=root_frame_id,
		frame_by_id=frame_by_id,
		frame_index_by_object_id=frame_index_by_object_id,
		root_width=viewport_width,
		root_height=viewport_height,
		logger=logger,
	)
	all_elements = list(main_elements)
	# OOPIFs are represented in the page frame tree but not in the main target's
	# DOM.getDocument result.  A successful frame CDP session identifies them;
	# normal same-target frames reject this call and are already covered above.
	for frame_id, frame in frame_by_id.items():
		if frame is page.main_frame or frame_id in seen_frame_ids or len(all_elements) >= _MAX_ELEMENTS:
			continue
		try:
			origin_x, origin_y, visible = await _scope_origin_for_oopif(frame, viewport_width, viewport_height)
			if not visible:
				continue
			elements, _ = await _collect_scope(
				context,
				cdp_target=frame,
				root_frame_id=frame_id,
				frame_by_id={frame_id: frame},
				frame_index_by_object_id=frame_index_by_object_id,
				root_width=viewport_width,
				root_height=viewport_height,
				root_global_x=origin_x,
				root_global_y=origin_y,
				root_chain_visible=visible,
				coordinate_frame=frame,
				logger=logger,
			)
			all_elements.extend(elements)
		except PlaywrightError as exc:
			# This is the expected path for an iframe which shares the main CDP
			# target.  Do not turn it into a legacy-collector fallback.
			if 'part of the parent frame' not in str(exc):
				logger.debug('Skipping inaccessible frame %s: %s', frame.url, exc)
		except Exception as exc:
			logger.debug('Skipping OOPIF DOM collection for %s: %s', frame.url, exc)
	# The two scopes each rank locally.  Re-rank globally so the prompt keeps
	# currently visible controls before the +/-1000px fringe.
	all_elements.sort(
		key=lambda item: (
			0 if 0 < item.y + item.height and item.y < viewport_height else 1,
			0.0
			if 0 < item.y + item.height and item.y < viewport_height
			else min(abs(item.y + item.height), abs(item.y - viewport_height)),
			item.frame_index,
			item.y,
			item.x,
		)
	)
	return all_elements[:_MAX_ELEMENTS]
