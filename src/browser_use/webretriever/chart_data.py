"""Task-scoped storage and deterministic normalization for chart network data.

All inputs to this module are packets already captured by ``BrowserRuntime``.
The normalizers never perform network I/O.  The Tableau data-dictionary reader
is an independent, small implementation of the response model documented by
the MIT-licensed tableau-scraper project by Bertrand Martel.
"""

from __future__ import annotations

import base64
import binascii
import copy
import csv
import hashlib
import io
import json
import os
import re
import tempfile
import uuid
from collections import OrderedDict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from browser_use.webretriever.artifacts import atomic_write_json

ChartArtifactStatus = Literal['ready', 'saved_raw_only', 'no_match', 'too_large']

_SCHEMA_VERSION = 1
_MAX_PACKET_COUNT = 16
_MAX_PACKET_BODY_BYTES = 25 * 1024 * 1024
_MAX_TOTAL_BODY_BYTES = 64 * 1024 * 1024
_MAX_PREVIEW_BYTES = 8 * 1024
_MAX_TABLES = 32
_MAX_ROWS_PER_TABLE = 1_000_000
_SAFE_ID_RE = re.compile(r'^[A-Za-z0-9_-]{1,128}$')
_TABLEAU_SESSION_RE = re.compile(r'/sessions/([^/?#]+)', re.IGNORECASE)
_OWID_URL_RE = re.compile(r'^(.*)\.(data|metadata)\.json(?:[?#].*)?$', re.IGNORECASE)
_REDACTED = '<redacted>'
_REDACTED_SESSION_PREFIX = 'redacted-tsid-'
_PSEUDONYM_SALT = os.urandom(32)

_SENSITIVE_KEYS = frozenset(
	{
		'aws_access_key_id',
		'aws_secret_access_key',
		'access_token',
		'apikey',
		'api_key',
		'auth',
		'authorization',
		'bearer',
		'cookie',
		'global-session-header',
		'jwt',
		'newsessionid',
		'password',
		'proxy-authorization',
		'secret',
		'session',
		'sid',
		'sig',
		'sessionid',
		'set-cookie',
		'signature',
		'stickysessionkey',
		'token',
		'x-access-token',
		'x-api-key',
		'x-amz-security-token',
		'x-xsrf-token',
	}
)
_COLLAPSED_SENSITIVE_KEYS = frozenset(re.sub(r'[^a-z0-9]+', '', value.casefold()) for value in _SENSITIVE_KEYS)
_SENSITIVE_TEXT_KEY_RE = re.compile(
	r'(?i)(?:access[-_ ]?key|api[-_ ]?key|authorization|cookie|credential|password|passwd|private[-_ ]?key|'
	r'refresh[-_ ]?token|secret|session(?:[-_ ]?(?:id|key|token))?|signature|token)'
)
_PACKET_BODY_FIELDS = frozenset({'response_body', 'response_body_base64', 'response_json'})
_URL_FIELDS = frozenset({'url', 'page_url', 'frame_url'})
_HEADER_FIELDS = frozenset({'headers', 'response_headers'})
_PACKET_METADATA_FIELDS = (
	'request_id',
	'timestamp',
	'url',
	'method',
	'headers',
	'resource_type',
	'post_data',
	'post_text',
	'json_data',
	'page_id',
	'document_generation',
	'page_url',
	'frame_url',
	'status',
	'response_headers',
	'response_body_state',
	'response_body_bytes',
	'response_body_truncated',
	'response_body_encoding',
	'response_error',
	'failure',
)


@dataclass(slots=True)
class NormalizedTable:
	"""One rectangular table reconstructed without model assistance."""

	table_id: str
	rows: list[dict[str, Any]]
	source_request_ids: list[int]
	source_name: str = ''
	column_descriptions: dict[str, str] = field(default_factory=dict)
	row_semantics: dict[str, Any] = field(default_factory=dict)

	@property
	def columns(self) -> list[str]:
		seen: set[str] = set()
		result: list[str] = []
		for row in self.rows:
			for name in row:
				if name not in seen:
					seen.add(name)
					result.append(name)
		return result


@dataclass(slots=True)
class DatasetBundle:
	"""A logical dataset and its provenance-preserving normalized tables."""

	dataset_id: str
	parser: Literal['json', 'csv', 'owid', 'tableau']
	request_ids: list[int]
	tables: list[NormalizedTable]
	active_filters: dict[str, Any] = field(default_factory=dict)
	warnings: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ChartArtifactResult:
	status: ChartArtifactStatus
	artifact_id: str
	data_dir: Path
	manifest: dict[str, Any]
	packet_preview: dict[str, Any] | None

	def to_action_payload(self) -> dict[str, Any]:
		dataset_filters = [
			dataset.get('active_filters', {}) for dataset in self.manifest.get('datasets', []) if isinstance(dataset, Mapping)
		]
		active_filters = (
			dataset_filters[0] if dataset_filters and all(item == dataset_filters[0] for item in dataset_filters) else {}
		)
		return {
			'action': 'find_chart_data_requests',
			'status': self.status,
			'artifact_id': self.artifact_id,
			'data_dir': str(self.data_dir),
			'active_filters': active_filters,
			'counts': self.manifest.get('counts', {}),
			'requests': [
				{key: packet.get(key) for key in ('request_id', 'url', 'method', 'status', 'resource_type', 'body_bytes')}
				for packet in self.manifest.get('packets', [])
			],
			'datasets': [
				{
					'dataset_id': dataset.get('dataset_id'),
					'parser': dataset.get('parser'),
					'request_ids': dataset.get('request_ids', []),
					'active_filters': dataset.get('active_filters', {}),
					'table_count': len(dataset.get('tables', [])),
					'row_count': sum(int(table.get('row_count', 0)) for table in dataset.get('tables', [])),
					'warnings': dataset.get('warnings', []),
				}
				for dataset in self.manifest.get('datasets', [])
			],
			'packet_preview': self.packet_preview,
			'manifest_sha256': _sha256_path(self.data_dir / 'manifest.json'),
		}


def _is_sensitive_key(key: object) -> bool:
	raw = re.sub(r'(?<=[a-z0-9])(?=[A-Z])', '_', str(key))
	words = [word for word in re.split(r'[^a-z0-9]+', raw.casefold()) if word]
	if not words:
		return False
	normalized = ''.join(words)
	if normalized in _COLLAPSED_SENSITIVE_KEYS:
		return True
	if words[-1] in {
		'authorization',
		'bearer',
		'cookie',
		'credential',
		'jwt',
		'passwd',
		'password',
		'secret',
		'signature',
		'token',
	}:
		return True
	return tuple(words[-2:]) in {('access', 'key'), ('api', 'key'), ('private', 'key'), ('session', 'id'), ('session', 'key')}


def _is_transport_sensitive_key(key: object) -> bool:
	"""Treat ambiguous transport parameter names more strictly than table fields."""

	normalized = re.sub(r'[^a-z0-9]+', '', str(key).casefold())
	return _is_sensitive_key(key) or normalized == 'key'


def _redact_free_text(value: str) -> str:
	if re.match(r'(?i)^https?://', value.strip()):
		return _redact_url(value)
	# Handle authentication schemes before the generic key/value rule.  If the
	# generic rule ran first it would redact only the word ``Bearer`` and leave
	# the actual credential behind (for example, ``Authorization: Bearer abc``).
	value = re.sub(
		r'(?i)(\b(?:proxy[-_ ]?)?authorization\s*[=:]\s*)(?:bearer|basic)\s+[^\s,;]+',
		rf'\1{_REDACTED}',
		value,
	)
	return re.sub(
		rf'(?i)(\b{_SENSITIVE_TEXT_KEY_RE.pattern.removeprefix("(?i)")}\b\s*[=:]\s*)([^&\s,;]+)',
		rf'\1{_REDACTED}',
		value,
	)


def _redact_mapping(value: Any) -> Any:
	if isinstance(value, Mapping):
		result: dict[str, Any] = {}
		for key, item in value.items():
			name = str(key)
			if _is_sensitive_key(name):
				result[name] = _REDACTED
			elif isinstance(item, str) and (
				name.casefold()
				in {
					'content-location',
					'frame_url',
					'href',
					'location',
					'origin',
					'page_url',
					'referer',
					'referrer',
					'src',
					'url',
					'website',
				}
				or re.match(r'(?i)^https?://', item.strip())
			):
				result[name] = _redact_url(item)
			else:
				result[name] = _redact_mapping(item)
		return result
	if isinstance(value, list):
		return [_redact_mapping(item) for item in value]
	if isinstance(value, tuple):
		return [_redact_mapping(item) for item in value]
	if isinstance(value, str):
		return _redact_free_text(value)
	if value is None or isinstance(value, (int, float, bool)):
		return value
	return str(value)


def _redact_headers(value: Any) -> Any:
	if not isinstance(value, Mapping):
		return _redact_mapping(value)
	result: dict[str, Any] = {}
	for key, item in value.items():
		name = str(key)
		if _is_transport_sensitive_key(name):
			result[name] = _REDACTED
		elif isinstance(item, str) and re.fullmatch(r'(?is)\s*(?:bearer|basic)\s+\S+\s*', item):
			result[name] = _REDACTED
		else:
			result[name] = _redact_mapping(item)
	return result


def _session_pseudonym(value: str) -> str:
	if re.fullmatch(rf'{re.escape(_REDACTED_SESSION_PREFIX)}[0-9a-f]{{16}}', value):
		return value
	digest = hashlib.sha256(_PSEUDONYM_SALT + value.encode('utf-8', errors='replace')).hexdigest()[:16]
	return f'{_REDACTED_SESSION_PREFIX}{digest}'


def _redact_url(url: str, *, _depth: int = 0) -> str:
	try:
		parts = urlsplit(url)
	except ValueError:
		return url
	query: list[tuple[str, str]] = []
	for key, value in parse_qsl(parts.query, keep_blank_values=True):
		if _is_transport_sensitive_key(key):
			redacted_value = _REDACTED
		elif re.match(r'(?i)^https?://', value.strip()):
			redacted_value = _redact_url(value, _depth=_depth + 1) if _depth < 4 else _REDACTED
		else:
			redacted_value = _redact_free_text(value)
		query.append((key, redacted_value))

	def redact_path_secret(match: re.Match[str]) -> str:
		prefix, value = match.group(1), match.group(2)
		replacement = _session_pseudonym(value) if prefix.casefold().startswith(('/session/', '/sessions/')) else _REDACTED
		return f'{prefix}{replacement}'

	path = re.sub(
		r'(?i)(/(?:sessions?|tokens?|access[-_]?tokens?|refresh[-_]?tokens?|auth(?:orization)?|'
		r'api[-_]?keys?|aws[-_]?access[-_]?key[-_]?ids?|private[-_]?keys?|keys?|secrets?|'
		r'passwords?|passwd|credentials?|signatures?|jwt|bearer)/)([^/?#]+)',
		redact_path_secret,
		parts.path,
	)
	fragment = parts.fragment
	if '=' in fragment:
		fragment_pairs = parse_qsl(fragment, keep_blank_values=True)
		fragment = urlencode(
			[(key, _REDACTED if _is_transport_sensitive_key(key) else _redact_free_text(value)) for key, value in fragment_pairs],
			doseq=True,
		)
	elif _SENSITIVE_TEXT_KEY_RE.search(fragment):
		fragment = _REDACTED
	netloc = parts.netloc
	if '@' in netloc:
		netloc = f'{_REDACTED}@{netloc.rsplit("@", 1)[1]}'
	return urlunsplit((parts.scheme, netloc, path, urlencode(query, doseq=True), fragment))


def sanitize_url(url: str) -> str:
	"""Redact credentials while retaining a stable, unlinkable Tableau session key."""

	return _redact_url(url)


def _redact_post_text(value: str) -> str:
	try:
		decoded = json.loads(value)
	except (json.JSONDecodeError, TypeError):
		decoded = None
	if isinstance(decoded, (dict, list)):
		return json.dumps(_redact_mapping(decoded), ensure_ascii=False, separators=(',', ':'))

	# URL-encoded request bodies are common for BI endpoints.
	if '=' in value and '\n' not in value and len(value) < 2_000_000:
		pairs = parse_qsl(value, keep_blank_values=True)
		if pairs:
			return urlencode([(key, _REDACTED if _is_sensitive_key(key) else item) for key, item in pairs], doseq=True)

	# Preserve multipart framing while replacing credential-like field values,
	# including camelCase names such as refreshToken and clientSecret.
	multipart = re.compile(r'(?is)(name=["\']([^"\']+)["\'][^\r\n]*\r?\n\r?\n)(.*?)(?=\r?\n--|$)')
	value = multipart.sub(
		lambda match: f'{match.group(1)}{_REDACTED}' if _is_sensitive_key(match.group(2)) else match.group(0), value
	)
	return _redact_free_text(value)


def sanitize_packet_metadata(packet: Mapping[str, Any]) -> dict[str, Any]:
	"""Return a JSON-safe packet metadata object with credentials removed."""

	result: dict[str, Any] = {}
	for field_name in _PACKET_METADATA_FIELDS:
		if field_name not in packet:
			continue
		value = copy.deepcopy(packet[field_name])
		if field_name in _URL_FIELDS and isinstance(value, str):
			value = _redact_url(value)
		elif field_name in _HEADER_FIELDS:
			value = _redact_headers(value)
		elif field_name in {'post_data', 'post_text', 'raw_post_data'} and isinstance(value, str):
			value = _redact_post_text(value)
		else:
			value = _redact_mapping(value)
		result[field_name] = value
	return result


def _tableau_json_segments(text: str) -> list[Any] | None:
	"""Decode Tableau's ``character-count;JSON`` bootstrap stream."""

	position = 0
	segments: list[Any] = []
	while position < len(text):
		match = re.match(r'(\d+);', text[position:])
		if match is None:
			return None
		length = int(match.group(1))
		start = position + match.end()
		end = start + length
		if end > len(text):
			return None
		try:
			segments.append(json.loads(text[start:end]))
		except json.JSONDecodeError:
			return None
		position = end
	return segments if segments else None


def _sanitize_body(body: bytes, content_type: str) -> bytes:
	if not body:
		return body
	textual = any(marker in content_type.casefold() for marker in ('json', 'text', 'csv', 'javascript', 'xml'))
	if not textual and b'\x00' in body[:1024]:
		return body
	try:
		text = body.decode('utf-8')
	except UnicodeDecodeError:
		return body

	try:
		decoded = json.loads(text)
	except json.JSONDecodeError:
		decoded = None
	if isinstance(decoded, (dict, list)):
		return json.dumps(_redact_mapping(decoded), ensure_ascii=False, separators=(',', ':')).encode('utf-8')

	segments = _tableau_json_segments(text)
	if segments is not None:
		parts: list[str] = []
		for segment in segments:
			encoded = json.dumps(_redact_mapping(segment), ensure_ascii=False, separators=(',', ':'))
			parts.append(f'{len(encoded)};{encoded}')
		return ''.join(parts).encode('utf-8')
	if any(marker in content_type.casefold() for marker in ('csv', 'tab-separated-values')):
		try:
			dialect = csv.Sniffer().sniff(text[:16_384], delimiters=',\t;|')
			reader = csv.reader(io.StringIO(text), dialect=dialect)
			rows = list(reader)
		except csv.Error:
			rows = []
		if rows:
			sensitive_columns = {index for index, name in enumerate(rows[0]) if _is_sensitive_key(name)}
			changed = False
			for row in rows[1:]:
				for index, item in enumerate(row):
					replacement = _REDACTED if index in sensitive_columns else _redact_free_text(item)
					if replacement != item:
						row[index] = replacement
						changed = True
			if changed:
				output = io.StringIO(newline='')
				csv.writer(output, dialect=dialect, lineterminator='\n').writerows(rows)
				return output.getvalue().encode('utf-8')
	# Last-resort handling for non-JSON textual bodies that expose a familiar
	# credential field.  Keep delimiters intact so parsers can still inspect it.
	return _redact_free_text(text).encode('utf-8')


def sanitize_network_packet(packet: Mapping[str, Any]) -> dict[str, Any]:
	"""Return one complete, JSON-safe packet with credentials removed.

	The same representation is suitable for the archive writer and for explicit
	cursor inspection.  Keeping this transformation in one place prevents a raw
	request body from reaching the model through a diagnostic cursor after the
	on-disk artifact has already been sanitized.
	"""

	result = sanitize_packet_metadata(packet)
	body = _packet_body(packet)
	if body is None:
		return result
	response_headers = result.get('response_headers')
	content_type = ''
	if isinstance(response_headers, Mapping):
		content_type = str(response_headers.get('content-type', ''))
	sanitized = _sanitize_body(body, content_type)
	if isinstance(packet.get('response_body'), str) or packet.get('response_json') is not None:
		text = sanitized.decode('utf-8', errors='replace')
		result['response_body'] = text
		if 'json' in content_type.casefold():
			try:
				result['response_json'] = json.loads(text)
			except json.JSONDecodeError:
				pass
	else:
		result['response_body_base64'] = base64.b64encode(sanitized).decode('ascii')
		result['response_body_encoding'] = 'base64'
	return result


def _packet_body(packet: Mapping[str, Any]) -> bytes | None:
	body = packet.get('response_body')
	if isinstance(body, str):
		return body.encode('utf-8')
	encoded = packet.get('response_body_base64')
	if isinstance(encoded, str):
		try:
			return base64.b64decode(encoded, validate=True)
		except (ValueError, binascii.Error):
			return None
	payload = packet.get('response_json')
	if isinstance(payload, (dict, list)):
		return json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
	return None


def _packet_text(packet: Mapping[str, Any]) -> str | None:
	body = packet.get('response_body')
	if isinstance(body, str):
		return body
	payload = packet.get('response_json')
	if isinstance(payload, (dict, list)):
		return json.dumps(payload, ensure_ascii=False, separators=(',', ':'))
	encoded = packet.get('response_body_base64')
	if isinstance(encoded, str):
		try:
			return base64.b64decode(encoded, validate=True).decode('utf-8')
		except (ValueError, UnicodeDecodeError, binascii.Error):
			return None
	return None


def _atomic_write_bytes(path: Path, payload: bytes) -> Path:
	path.parent.mkdir(parents=True, exist_ok=True)
	temporary: Path | None = None
	try:
		with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f'.{path.name}.', suffix='.tmp', delete=False) as handle:
			temporary = Path(handle.name)
			handle.write(payload)
			handle.flush()
			os.fsync(handle.fileno())
		os.replace(temporary, path)
		temporary = None
	except Exception:
		if temporary is not None:
			temporary.unlink(missing_ok=True)
		raise
	return path


def _sha256_bytes(payload: bytes) -> str:
	return hashlib.sha256(payload).hexdigest()


def _utf8_prefix(value: str, max_bytes: int) -> tuple[str, bool]:
	encoded = value.encode('utf-8')
	if len(encoded) <= max_bytes:
		return value, False
	return encoded[:max_bytes].decode('utf-8', errors='ignore'), True


def _sha256_path(path: Path) -> str:
	return _sha256_bytes(path.read_bytes())


def _slug(value: str, *, fallback: str = 'table') -> str:
	normalized = re.sub(r'[^a-z0-9]+', '_', value.casefold()).strip('_')[:72]
	if normalized:
		return normalized
	return f'{fallback}_{hashlib.sha256(value.encode("utf-8")).hexdigest()[:10]}'


def _request_id(packet: Mapping[str, Any], fallback: int = 0) -> int:
	value = packet.get('request_id', fallback)
	if isinstance(value, bool):
		return fallback
	try:
		return max(0, int(value))
	except (TypeError, ValueError):
		return fallback


def _json_value(text: str | None) -> Any | None:
	if text is None:
		return None
	try:
		return json.loads(text)
	except json.JSONDecodeError:
		return None


def _cell(value: Any) -> Any:
	if value is None or isinstance(value, (str, int, float, bool)):
		return value
	return json.dumps(value, ensure_ascii=False, separators=(',', ':'), default=str)


def _record_rows(records: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
	return [{str(key): _cell(value) for key, value in record.items()} for record in records[:_MAX_ROWS_PER_TABLE]]


def _tables_from_json(value: Any, request_id: int, prefix: str = 'root') -> list[NormalizedTable]:
	result: list[NormalizedTable] = []
	seen_objects: set[int] = set()

	def visit(item: Any, path: str, depth: int) -> None:
		if len(result) >= _MAX_TABLES or depth > 6:
			return
		if isinstance(item, (dict, list)):
			identity = id(item)
			if identity in seen_objects:
				return
			seen_objects.add(identity)

		if isinstance(item, list):
			if item and all(isinstance(row, Mapping) for row in item):
				rows = _record_rows(item)  # type: ignore[arg-type]
				if rows:
					result.append(NormalizedTable(_slug(path), rows, [request_id], source_name=path))
				return
			for index, child in enumerate(item[:32]):
				if isinstance(child, (dict, list)):
					visit(child, f'{path}_{index}', depth + 1)
			return

		if not isinstance(item, Mapping):
			return
		arrays = {str(key): child for key, child in item.items() if isinstance(child, list)}
		if len(arrays) >= 2:
			lengths = {len(array) for array in arrays.values()}
			if len(lengths) == 1 and next(iter(lengths), 0) > 0:
				row_count = min(next(iter(lengths)), _MAX_ROWS_PER_TABLE)
				rows = [{name: _cell(array[index]) for name, array in arrays.items()} for index in range(row_count)]
				result.append(NormalizedTable(_slug(path), rows, [request_id], source_name=path))
		for key, child in item.items():
			if isinstance(child, (dict, list)):
				visit(child, f'{path}_{key}', depth + 1)

	visit(value, prefix, 0)
	return result


def _looks_like_delimited(packet: Mapping[str, Any], text: str) -> bool:
	url = str(packet.get('url', '')).casefold().split('?', 1)[0]
	content_type = ''
	headers = packet.get('response_headers')
	if isinstance(headers, Mapping):
		content_type = str(headers.get('content-type', '')).casefold()
	return url.endswith(('.csv', '.tsv')) or any(marker in content_type for marker in ('text/csv', 'tab-separated-values'))


def _table_from_delimited(packet: Mapping[str, Any], text: str) -> NormalizedTable | None:
	if not _looks_like_delimited(packet, text):
		return None
	try:
		dialect = csv.Sniffer().sniff(text[:16_384], delimiters=',\t;|')
	except csv.Error:
		dialect = csv.excel_tab if '\t' in text.partition('\n')[0] else csv.excel
	reader = csv.DictReader(io.StringIO(text), dialect=dialect)
	if not reader.fieldnames:
		return None
	rows: list[dict[str, Any]] = []
	for row in reader:
		rows.append({str(key): value for key, value in row.items() if key is not None})
		if len(rows) >= _MAX_ROWS_PER_TABLE:
			break
	if not rows:
		return None
	request_id = _request_id(packet)
	return NormalizedTable(f'request_{request_id}', rows, [request_id], source_name=str(packet.get('url', '')))


def _owid_stem(url: str) -> tuple[str, str] | None:
	match = _OWID_URL_RE.match(url)
	return (match.group(1), match.group(2).casefold()) if match else None


def _normalize_owid(
	packets: Sequence[Mapping[str, Any]], active_filters: Mapping[str, Any]
) -> tuple[list[DatasetBundle], set[int], list[str]]:
	groups: dict[str, dict[str, Mapping[str, Any]]] = {}
	warnings: list[str] = []
	for packet in packets:
		matched = _owid_stem(str(packet.get('url', '')))
		if matched is not None:
			stem, role = matched
			groups.setdefault(stem, {})[role] = packet

	bundles: list[DatasetBundle] = []
	consumed: set[int] = set()
	for stem, group in groups.items():
		if 'data' not in group or 'metadata' not in group:
			missing = 'metadata' if 'metadata' not in group else 'data'
			warnings.append(f'OWID request group {stem!r} is missing its {missing} companion')
			consumed.update(_request_id(packet) for packet in group.values())
			continue
		data_packet = group['data']
		metadata_packet = group['metadata']
		data = _json_value(_packet_text(data_packet))
		metadata = _json_value(_packet_text(metadata_packet))
		if not isinstance(data, Mapping) or not isinstance(metadata, Mapping):
			warnings.append(f'OWID request group {stem!r} contains invalid JSON')
			consumed.update((_request_id(data_packet), _request_id(metadata_packet)))
			continue

		values = data.get('values')
		years = data.get('years')
		entities = data.get('entities')
		if not isinstance(values, list) or not isinstance(years, list) or not isinstance(entities, list):
			warnings.append(f'OWID request group {stem!r} has misaligned values/years/entities arrays')
			consumed.update((_request_id(data_packet), _request_id(metadata_packet)))
			continue
		if len({len(values), len(years), len(entities)}) != 1:
			warnings.append(f'OWID request group {stem!r} has misaligned values/years/entities arrays')
			consumed.update((_request_id(data_packet), _request_id(metadata_packet)))
			continue

		entity_values = metadata.get('dimensions', {})
		entity_values = entity_values.get('entities', {}) if isinstance(entity_values, Mapping) else {}
		entity_values = entity_values.get('values', []) if isinstance(entity_values, Mapping) else []
		entity_map = {item.get('id'): item for item in entity_values if isinstance(item, Mapping) and item.get('id') is not None}
		indicator_name = str(metadata.get('name') or metadata.get('title') or metadata.get('shortName') or 'value')
		rows: list[dict[str, Any]] = []
		for entity_id, year, value in zip(entities, years, values, strict=True):  # type: ignore[arg-type]
			entity = entity_map.get(entity_id, {})
			rows.append(
				{
					'entity_id': entity_id,
					'entity': entity.get('name') if isinstance(entity, Mapping) else None,
					'entity_code': entity.get('code') if isinstance(entity, Mapping) else None,
					'year': year,
					'value': value,
					'indicator': indicator_name,
					'unit': metadata.get('unit') or metadata.get('shortUnit'),
				}
			)
			if len(rows) >= _MAX_ROWS_PER_TABLE:
				break
		request_ids = [_request_id(data_packet), _request_id(metadata_packet)]
		indicator_id = stem.rsplit('/', 1)[-1]
		bundles.append(
			DatasetBundle(
				dataset_id=f'owid_{_slug(indicator_id)}',
				parser='owid',
				request_ids=request_ids,
				tables=[
					NormalizedTable(
						table_id='indicator_data',
						rows=rows,
						source_request_ids=request_ids,
						source_name=indicator_name,
						column_descriptions={
							'entity': 'Entity name restored from the companion metadata response.',
							'value': indicator_name,
						},
					)
				],
				active_filters=dict(active_filters),
			)
		)
		consumed.update(request_ids)
	return bundles, consumed, warnings


def _tableau_segments_from_pres_model(pres_model: Mapping[str, Any]) -> Mapping[str, Any]:
	data_dictionary = pres_model.get('dataDictionary', {})
	if not isinstance(data_dictionary, Mapping):
		return {}
	if 'dataSegments' in data_dictionary and isinstance(data_dictionary['dataSegments'], Mapping):
		return data_dictionary['dataSegments']
	holder = data_dictionary.get('presModelHolder', {})
	if not isinstance(holder, Mapping):
		return {}
	generated = holder.get('genDataDictionaryPresModel', {})
	if not isinstance(generated, Mapping):
		return {}
	segments = generated.get('dataSegments', {})
	return segments if isinstance(segments, Mapping) else {}


def _tableau_data_pool(segments: Mapping[str, Any]) -> dict[str, list[Any]]:
	pool: dict[str, list[Any]] = {}
	for segment in segments.values():
		if not isinstance(segment, Mapping):
			continue
		columns = segment.get('dataColumns', [])
		if not isinstance(columns, list):
			continue
		for column in columns:
			if not isinstance(column, Mapping):
				continue
			data_type = str(column.get('dataType', 'cstring'))
			values = column.get('dataValues', [])
			if isinstance(values, list):
				pool.setdefault(data_type, []).extend(values)
	return pool


def _tableau_index_values(indices: Any, values: Sequence[Any], cstrings: Sequence[Any]) -> list[Any]:
	if not isinstance(indices, list):
		return []
	result: list[Any] = []
	for raw_index in indices:
		if isinstance(raw_index, bool) or not isinstance(raw_index, int):
			result.append(None)
		elif raw_index < 0:
			index = abs(raw_index) - 1
			result.append(cstrings[index] if index < len(cstrings) else None)
		else:
			result.append(values[raw_index] if raw_index < len(values) else None)
	return result


def _unique_column_name(existing: Mapping[str, Any], base: str, function_name: str, suffix: str) -> str:
	name = f'{base}-{suffix}'
	if name not in existing:
		return name
	function_name = function_name or 'duplicate'
	index = 2
	candidate = f'{base}-{function_name}-{suffix}'
	while candidate in existing:
		candidate = f'{base}-{function_name}-{index}-{suffix}'
		index += 1
	return candidate


def _mark_tableau_row_semantics(rows: list[dict[str, Any]]) -> dict[str, Any]:
	name_columns = [
		name
		for name in (rows[0].keys() if rows else [])
		if any(marker in name.casefold() for marker in ('port', 'name', 'category', 'entity', '口岸', '港'))
	]
	if not name_columns:
		return {}
	aggregate_values = {
		'(all)',
		'all',
		'all airports',
		'all ports',
		'all seaports',
		'airport total',
		'grand total',
		'seaport total',
		'total',
		'total (all airports)',
		'total (all ports)',
		'total (all seaports)',
		'全港',
		'全口岸',
		'全海港',
		'全空港',
		'合计',
		'合計',
		'总数',
		'总计',
		'總數',
		'總計',
		'所有口岸',
		'総計',
		'空港計',
		'港計',
		'すべての港',
		'全ての港',
		'합계',
		'총계',
	}

	def classify_label(value: Any) -> str:
		label = re.sub(r'\s+', ' ', str(value or '').strip().casefold())
		if not label:
			return 'missing'
		canonical = re.sub(r'[_-]+', ' ', label)
		if canonical in aggregate_values:
			return 'aggregate'
		if re.fullmatch(r'(?:grand\s+)?total(?:\s*\([^)]*\))?', canonical):
			return 'aggregate'
		if re.fullmatch(r'all\s+(?:airport|port|seaport)s?', canonical):
			return 'aggregate'
		if any(marker in canonical for marker in ('subtotal', 'total', 'all ports', 'all airports', 'all seaports')):
			return 'unknown'
		if any(
			marker in label for marker in ('合计', '合計', '总数', '总计', '總數', '總計', '総計', '합계', '총계')
		) or re.search(r'(?:空港|海港|港)計$', label):
			return 'aggregate'
		if any(marker in label for marker in ('小计', '小計', '소계')):
			return 'unknown'
		return 'leaf'

	for row in rows:
		kinds = {classify_label(row.get(name)) for name in name_columns}
		if 'aggregate' in kinds:
			row['__row_kind'] = 'aggregate'
		elif 'unknown' in kinds:
			row['__row_kind'] = 'unknown'
		elif 'leaf' in kinds:
			row['__row_kind'] = 'leaf'
		else:
			row['__row_kind'] = 'unknown'
	return {
		'row_kind_column': '__row_kind',
		'aggregate_value': 'aggregate',
		'leaf_value': 'leaf',
		'unknown_value': 'unknown',
		'aggregation_rule': (
			'Prefer an authoritative Total row; otherwise aggregate mutually exclusive leaf rows only. '
			'Never include unknown rows in a derived total.'
		),
	}


def _tableau_table_from_viz_data(
	worksheet: str,
	viz_data: Mapping[str, Any],
	segments: Mapping[str, Any],
	request_id: int,
) -> NormalizedTable | None:
	pane_data = viz_data.get('paneColumnsData')
	if not isinstance(pane_data, Mapping):
		return None
	viz_columns = pane_data.get('vizDataColumns')
	panes = pane_data.get('paneColumnsList')
	if not isinstance(viz_columns, list) or not isinstance(panes, list):
		return None
	pool = _tableau_data_pool(segments)
	cstrings = pool.get('cstring', [])
	frame: OrderedDict[str, list[Any]] = OrderedDict()
	column_descriptions: dict[str, str] = {}
	for descriptor in viz_columns:
		if not isinstance(descriptor, Mapping):
			continue
		caption = descriptor.get('fieldCaption')
		if not isinstance(caption, str) or not caption.strip():
			continue
		pane_indices = descriptor.get('paneIndices', [])
		column_indices = descriptor.get('columnIndices', [])
		if not isinstance(pane_indices, list) or not isinstance(column_indices, list):
			continue
		for pane_index, column_index in zip(pane_indices, column_indices):
			if not isinstance(pane_index, int) or not isinstance(column_index, int):
				continue
			try:
				pane = panes[pane_index]
				pane_columns = pane['vizPaneColumns']
				indices = pane_columns[column_index]
			except (IndexError, KeyError, TypeError):
				continue
			if not isinstance(indices, Mapping):
				continue
			data_type = str(descriptor.get('dataType', 'cstring'))
			values = pool.get(data_type, cstrings)
			function_name = str(descriptor.get('fn', ''))
			for index_key, suffix in (('valueIndices', 'value'), ('aliasIndices', 'alias')):
				resolved = _tableau_index_values(indices.get(index_key), values, cstrings)
				if not resolved:
					continue
				name = _unique_column_name(frame, caption, function_name, suffix)
				frame[name] = resolved
				column_descriptions[name] = f'Tableau {suffix} for {caption}; data type {data_type}.'
	if not frame:
		return None
	row_count = min(max(len(values) for values in frame.values()), _MAX_ROWS_PER_TABLE)
	rows = [
		{name: values[index] if index < len(values) else None for name, values in frame.items()} for index in range(row_count)
	]
	row_semantics = _mark_tableau_row_semantics(rows)
	return NormalizedTable(
		table_id=_slug(worksheet, fallback='worksheet'),
		rows=rows,
		source_request_ids=[request_id],
		source_name=worksheet,
		column_descriptions=column_descriptions,
		row_semantics=row_semantics,
	)


def _tableau_bootstrap_state(payload: str) -> tuple[dict[str, Any], dict[str, Any]] | None:
	segments = _tableau_json_segments(payload)
	if segments is None or len(segments) < 2:
		return None
	info, data = segments[0], segments[1]
	if not isinstance(info, dict) or not isinstance(data, dict):
		return None
	return info, data


def _tableau_bootstrap_tables(
	data: Mapping[str, Any],
	state_segments: Mapping[str, Any],
	request_id: int,
) -> list[NormalizedTable]:
	secondary = data.get('secondaryInfo', {})
	model_map = secondary.get('presModelMap', {}) if isinstance(secondary, Mapping) else {}
	if not isinstance(model_map, Mapping):
		return []
	viz_data = model_map.get('vizData', {})
	holder = viz_data.get('presModelHolder', {}) if isinstance(viz_data, Mapping) else {}
	generated = holder.get('genPresModelMapPresModel', {}) if isinstance(holder, Mapping) else {}
	worksheets = generated.get('presModelMap', {}) if isinstance(generated, Mapping) else {}
	if not isinstance(worksheets, Mapping):
		return []
	result: list[NormalizedTable] = []
	for worksheet, model in worksheets.items():
		if not isinstance(model, Mapping):
			continue
		model_holder = model.get('presModelHolder', {})
		generated_viz = model_holder.get('genVizDataPresModel', {}) if isinstance(model_holder, Mapping) else {}
		if not isinstance(generated_viz, Mapping):
			continue
		table = _tableau_table_from_viz_data(str(worksheet), generated_viz, state_segments, request_id)
		if table is not None:
			result.append(table)
	return result


def _tableau_command_tables(
	payload: Mapping[str, Any],
	state_segments: dict[str, Any],
	state_zones: dict[str, Any],
	request_id: int,
) -> list[NormalizedTable]:
	response = payload.get('vqlCmdResponse', {})
	layout = response.get('layoutStatus', {}) if isinstance(response, Mapping) else {}
	application = layout.get('applicationPresModel', {}) if isinstance(layout, Mapping) else {}
	if not isinstance(application, Mapping):
		return []
	for key, segment in _tableau_segments_from_pres_model(application).items():
		if segment is not None:
			state_segments[str(key)] = copy.deepcopy(segment)

	workbook = application.get('workbookPresModel', {})
	dashboard = workbook.get('dashboardPresModel', {}) if isinstance(workbook, Mapping) else {}
	zones = dashboard.get('zones', {}) if isinstance(dashboard, Mapping) else {}
	if isinstance(zones, Mapping):
		for key, zone in zones.items():
			if zone is None:
				continue
			old_zone = state_zones.get(str(key))
			holder = zone.get('presModelHolder', {}) if isinstance(zone, Mapping) else {}
			has_viz = isinstance(holder, Mapping) and isinstance(holder.get('visual'), Mapping) and 'vizData' in holder['visual']
			if has_viz or old_zone is None:
				state_zones[str(key)] = copy.deepcopy(zone)

	result: list[NormalizedTable] = []
	for zone in state_zones.values():
		if not isinstance(zone, Mapping):
			continue
		worksheet = zone.get('worksheet')
		holder = zone.get('presModelHolder', {})
		visual = holder.get('visual', {}) if isinstance(holder, Mapping) else {}
		viz_data = visual.get('vizData', {}) if isinstance(visual, Mapping) else {}
		if not isinstance(worksheet, str) or not isinstance(viz_data, Mapping):
			continue
		table = _tableau_table_from_viz_data(worksheet, viz_data, state_segments, request_id)
		if table is not None:
			result.append(table)
	return result


def _tableau_filters_from_pres_model(pres_model: Mapping[str, Any]) -> dict[str, Any]:
	"""Read the selected summaries Tableau returns with each worksheet zone."""

	workbook = pres_model.get('workbookPresModel', {})
	dashboard = workbook.get('dashboardPresModel', {}) if isinstance(workbook, Mapping) else {}
	zones = dashboard.get('zones', {}) if isinstance(dashboard, Mapping) else {}
	if not isinstance(zones, Mapping):
		return {}
	filters: dict[str, Any] = {}
	for zone in zones.values():
		if not isinstance(zone, Mapping):
			continue
		holder = zone.get('presModelHolder', {})
		if not isinstance(holder, Mapping):
			continue
		visual = holder.get('visual', {})
		filters_json = visual.get('filtersJson') if isinstance(visual, Mapping) else None
		if isinstance(filters_json, str):
			filters_json = _json_value(filters_json)
		if isinstance(filters_json, list):
			for entry in filters_json:
				if not isinstance(entry, Mapping):
					continue
				caption = entry.get('fieldCaption') or entry.get('caption')
				if not isinstance(caption, str) or not caption:
					continue
				summary = entry.get('summary')
				if summary is not None:
					filters[caption] = summary
				elif entry.get('all') or entry.get('allChecked'):
					filters[caption] = '(All)'
				else:
					table = entry.get('table', {})
					tuples = table.get('tuples', []) if isinstance(table, Mapping) else []
					selected: list[Any] = []
					for item in tuples if isinstance(tuples, list) else []:
						if not isinstance(item, Mapping) or item.get('s') is not True:
							continue
						values = item.get('t', [])
						if isinstance(values, list) and values and isinstance(values[0], Mapping):
							selected.append(values[0].get('v'))
					if selected:
						filters[caption] = selected[0] if len(selected) == 1 else selected

		quick_display = holder.get('quickFilterDisplay', {})
		quick_filter = quick_display.get('quickFilter', {}) if isinstance(quick_display, Mapping) else {}
		if isinstance(quick_filter, Mapping):
			column_names = quick_filter.get('columnFullNames', [])
			caption = None
			if isinstance(column_names, list) and column_names:
				caption = str(column_names[0]).strip('[]')
			summary = quick_filter.get('selectionSummary')
			if caption and summary is not None:
				filters[caption] = summary
	return filters


def _normalize_tableau(
	packets: Sequence[Mapping[str, Any]], active_filters: Mapping[str, Any]
) -> tuple[list[DatasetBundle], set[int], list[str]]:
	groups: dict[str, list[Mapping[str, Any]]] = {}
	for packet in packets:
		url = str(packet.get('url', ''))
		if '/vizql/' not in url.casefold():
			continue
		match = _TABLEAU_SESSION_RE.search(url)
		if match is not None:
			groups.setdefault(match.group(1), []).append(packet)

	bundles: list[DatasetBundle] = []
	consumed: set[int] = set()
	warnings: list[str] = []
	for session_id, session_packets in groups.items():
		session_packets.sort(key=lambda packet: (float(packet.get('timestamp', 0) or 0), _request_id(packet)))
		state_segments: dict[str, Any] = {}
		state_zones: dict[str, Any] = {}
		tables_by_name: OrderedDict[str, NormalizedTable] = OrderedDict()
		request_ids: list[int] = []
		has_bootstrap = False
		session_filters = dict(active_filters)
		for packet in session_packets:
			request_id = _request_id(packet)
			request_ids.append(request_id)
			consumed.add(request_id)
			text = _packet_text(packet)
			if text is None:
				continue
			url = str(packet.get('url', ''))
			if '/bootstrapSession/' in url:
				state = _tableau_bootstrap_state(text)
				if state is None:
					warnings.append(f'Tableau bootstrap request {request_id} is incomplete or invalid')
					continue
				has_bootstrap = True
				info, data = state
				world_update = info.get('worldUpdate', {})
				application = world_update.get('applicationPresModel', {}) if isinstance(world_update, Mapping) else {}
				if isinstance(application, Mapping):
					session_filters.update(_tableau_filters_from_pres_model(application))
				secondary = data.get('secondaryInfo', {})
				model_map = secondary.get('presModelMap', {}) if isinstance(secondary, Mapping) else {}
				for key, segment in (
					_tableau_segments_from_pres_model(model_map).items() if isinstance(model_map, Mapping) else ()
				):
					if segment is not None:
						state_segments[str(key)] = copy.deepcopy(segment)
				for table in _tableau_bootstrap_tables(data, state_segments, request_id):
					tables_by_name[table.source_name] = table
				continue

			payload = _json_value(text)
			if isinstance(payload, Mapping) and 'vqlCmdResponse' in payload:
				response = payload.get('vqlCmdResponse', {})
				layout = response.get('layoutStatus', {}) if isinstance(response, Mapping) else {}
				application = layout.get('applicationPresModel', {}) if isinstance(layout, Mapping) else {}
				if isinstance(application, Mapping):
					session_filters.update(_tableau_filters_from_pres_model(application))
				for table in _tableau_command_tables(payload, state_segments, state_zones, request_id):
					old = tables_by_name.get(table.source_name)
					if old is not None:
						table.source_request_ids = list(dict.fromkeys([*old.source_request_ids, request_id]))
					tables_by_name[table.source_name] = table
		if not has_bootstrap and tables_by_name:
			warnings.append(f'Tableau session {session_id!r} has command data but no bootstrap dictionary')
		if tables_by_name:
			bundles.append(
				DatasetBundle(
					dataset_id=f'tableau_{hashlib.sha256(session_id.encode()).hexdigest()[:12]}',
					parser='tableau',
					request_ids=list(dict.fromkeys(request_ids)),
					tables=list(tables_by_name.values()),
					active_filters=session_filters,
					warnings=[]
					if has_bootstrap
					else ['No complete bootstrap response was available; aliases may be incomplete.'],
				)
			)
	return bundles, consumed, warnings


def normalize_chart_packets(
	packets: Sequence[Mapping[str, Any]],
	*,
	active_filters: Mapping[str, Any] | None = None,
) -> tuple[list[DatasetBundle], list[str]]:
	"""Normalize selected packets, preferring protocol-aware paired parsers."""

	filters = dict(active_filters or {})
	bundles: list[DatasetBundle] = []
	warnings: list[str] = []

	owid, consumed_owid, owid_warnings = _normalize_owid(packets, filters)
	tableau, consumed_tableau, tableau_warnings = _normalize_tableau(packets, filters)
	bundles.extend(owid)
	bundles.extend(tableau)
	warnings.extend(owid_warnings)
	warnings.extend(tableau_warnings)
	consumed = consumed_owid | consumed_tableau

	for packet in packets:
		request_id = _request_id(packet)
		if request_id in consumed:
			continue
		text = _packet_text(packet)
		if text is None:
			continue
		delimited = _table_from_delimited(packet, text)
		if delimited is not None:
			bundles.append(
				DatasetBundle(
					dataset_id=f'csv_{request_id}',
					parser='csv',
					request_ids=[request_id],
					tables=[delimited],
					active_filters=filters,
				)
			)
			continue
		decoded = _json_value(text)
		if decoded is None:
			continue
		tables = _tables_from_json(decoded, request_id)
		if tables:
			bundles.append(
				DatasetBundle(
					dataset_id=f'json_{request_id}',
					parser='json',
					request_ids=[request_id],
					tables=tables,
					active_filters=filters,
				)
			)
	return bundles[:_MAX_TABLES], warnings


def _column_type(values: Iterable[Any]) -> str:
	types: set[str] = set()
	for value in values:
		if value is None or value == '':
			continue
		if isinstance(value, bool):
			types.add('boolean')
		elif isinstance(value, int):
			types.add('integer')
		elif isinstance(value, float):
			types.add('number')
		elif isinstance(value, (dict, list)):
			types.add('json')
		else:
			types.add('string')
	if not types:
		return 'null'
	if types <= {'integer', 'number'}:
		return 'number' if 'number' in types else 'integer'
	return next(iter(types)) if len(types) == 1 else 'mixed'


def _csv_payload(table: NormalizedTable) -> bytes:
	output = io.StringIO(newline='')
	columns = table.columns
	writer = csv.DictWriter(output, fieldnames=columns, extrasaction='ignore', lineterminator='\n')
	writer.writeheader()
	for row in table.rows:
		writer.writerow({name: _cell(row.get(name)) for name in columns})
	return output.getvalue().encode('utf-8')


class ChartDataArtifactStore:
	"""Persist selected chart packets and normalized tables beneath one task."""

	def __init__(self, task_dir: Path | str, task_identity: Mapping[str, Any]) -> None:
		self.task_dir = Path(task_dir).resolve()
		self.task_identity = _redact_mapping(dict(task_identity))
		self.chart_root = self.task_dir / 'chart_data'

	def save(
		self,
		packets: Sequence[Mapping[str, Any]],
		*,
		page_url: str = '',
		active_filters: Mapping[str, Any] | None = None,
		scan_id: str | None = None,
	) -> ChartArtifactResult:
		artifact_id = scan_id or uuid.uuid4().hex
		if not _SAFE_ID_RE.fullmatch(artifact_id) or artifact_id in {'.', '..'}:
			raise ValueError('scan_id must contain only letters, digits, underscores, and hyphens')
		self.chart_root.mkdir(parents=True, exist_ok=True)
		if self.chart_root.is_symlink() or self.chart_root.resolve() != self.task_dir / 'chart_data':
			raise ValueError('task chart_data directory must not be a symbolic link')
		data_dir = self.chart_root / artifact_id
		data_dir.mkdir(mode=0o700)
		packets_dir = data_dir / 'packets'
		tables_dir = data_dir / 'tables'
		analysis_dir = data_dir / 'analysis'
		packets_dir.mkdir()
		tables_dir.mkdir()
		analysis_dir.mkdir()

		warnings: list[str] = []
		stored_packets: list[dict[str, Any]] = []
		normalizer_packets: list[dict[str, Any]] = []
		total_body_bytes = 0
		preview: dict[str, Any] | None = None
		too_large = False
		for ordinal, packet in enumerate(packets):
			if ordinal >= _MAX_PACKET_COUNT:
				too_large = True
				warnings.append(f'Only the first {_MAX_PACKET_COUNT} selected requests were archived')
				break
			request_id = _request_id(packet, ordinal)
			metadata = sanitize_packet_metadata(packet)
			content_type = ''
			response_headers = metadata.get('response_headers')
			if isinstance(response_headers, Mapping):
				content_type = str(response_headers.get('content-type', ''))
			body = _packet_body(packet)
			body_path: Path | None = None
			body_sha256: str | None = None
			if body is not None:
				body = _sanitize_body(body, content_type)
				if len(body) > _MAX_PACKET_BODY_BYTES or total_body_bytes + len(body) > _MAX_TOTAL_BODY_BYTES:
					too_large = True
					metadata['body_omitted'] = 'response exceeds chart artifact size limit'
					warnings.append(f'Response body for request {request_id} exceeded the archive limit')
					body = None
				else:
					body_path = packets_dir / f'{request_id}.body'
					_atomic_write_bytes(body_path, body)
					body_sha256 = _sha256_bytes(body)
					total_body_bytes += len(body)
					try:
						text = body.decode('utf-8')
						normalizer_packets.append({**metadata, 'response_body': text})
						if preview is None:
							prefix = '--- BEGIN UNTRUSTED NETWORK DATA ---\n'
							suffix = '\n--- END UNTRUSTED NETWORK DATA ---'
							preview_text, preview_truncated = _utf8_prefix(
								text, _MAX_PREVIEW_BYTES - len((prefix + suffix).encode('utf-8'))
							)
							preview = {
								'request_id': request_id,
								'text': f'{prefix}{preview_text}{suffix}',
								'truncated': preview_truncated,
							}
					except UnicodeDecodeError:
						normalizer_packets.append({**metadata, 'response_body_base64': base64.b64encode(body).decode('ascii')})
			else:
				normalizer_packets.append(dict(metadata))

			metadata_payload = {
				**metadata,
				'body_path': body_path.relative_to(data_dir).as_posix() if body_path is not None else None,
				'body_sha256': body_sha256,
				'body_bytes': len(body) if body is not None else None,
			}
			metadata_path = packets_dir / f'{request_id}.json'
			atomic_write_json(metadata_path, metadata_payload)
			stored_packets.append(
				{
					'request_id': request_id,
					'url': metadata.get('url', ''),
					'method': metadata.get('method'),
					'status': metadata.get('status'),
					'resource_type': metadata.get('resource_type'),
					'metadata_path': metadata_path.relative_to(data_dir).as_posix(),
					'metadata_sha256': _sha256_path(metadata_path),
					'body_path': metadata_payload['body_path'],
					'body_sha256': body_sha256,
					'body_bytes': metadata_payload['body_bytes'],
				}
			)

		filters = _redact_mapping(dict(active_filters or {}))
		bundles, parse_warnings = normalize_chart_packets(normalizer_packets, active_filters=filters)
		warnings.extend(parse_warnings)
		manifest_datasets: list[dict[str, Any]] = []
		used_dataset_ids: set[str] = set()
		for bundle in bundles:
			dataset_id = bundle.dataset_id
			suffix = 2
			while dataset_id in used_dataset_ids:
				dataset_id = f'{bundle.dataset_id}_{suffix}'
				suffix += 1
			used_dataset_ids.add(dataset_id)
			dataset_dir = tables_dir / dataset_id
			dataset_dir.mkdir()
			manifest_tables: list[dict[str, Any]] = []
			used_table_ids: set[str] = set()
			for table in bundle.tables:
				table_id = table.table_id
				suffix = 2
				while table_id in used_table_ids:
					table_id = f'{table.table_id}_{suffix}'
					suffix += 1
				used_table_ids.add(table_id)
				csv_path = dataset_dir / f'{table_id}.csv'
				csv_bytes = _csv_payload(table)
				_atomic_write_bytes(csv_path, csv_bytes)
				columns = [
					{
						'name': name,
						'type': _column_type(row.get(name) for row in table.rows),
						'description': table.column_descriptions.get(name, ''),
					}
					for name in table.columns
				]
				schema_payload = {
					'schema_version': _SCHEMA_VERSION,
					'table_id': table_id,
					'source_name': table.source_name,
					'source_request_ids': table.source_request_ids,
					'row_count': len(table.rows),
					'columns': columns,
					'row_semantics': table.row_semantics,
				}
				schema_path = dataset_dir / f'{table_id}.schema.json'
				atomic_write_json(schema_path, schema_payload)
				manifest_tables.append(
					{
						'table_id': table_id,
						'csv_path': csv_path.relative_to(data_dir).as_posix(),
						'schema_path': schema_path.relative_to(data_dir).as_posix(),
						'csv_sha256': _sha256_path(csv_path),
						'schema_sha256': _sha256_path(schema_path),
						'row_count': len(table.rows),
						'columns': columns,
						'source_request_ids': table.source_request_ids,
					}
				)
			manifest_datasets.append(
				{
					'dataset_id': dataset_id,
					'parser': bundle.parser,
					'request_ids': bundle.request_ids,
					'active_filters': bundle.active_filters,
					'warnings': bundle.warnings,
					'tables': manifest_tables,
				}
			)

		if not stored_packets:
			status: ChartArtifactStatus = 'no_match'
		elif too_large:
			status = 'too_large'
		elif manifest_datasets:
			status = 'ready'
		else:
			status = 'saved_raw_only'
		manifest = {
			'schema_version': _SCHEMA_VERSION,
			'complete': True,
			'artifact_id': artifact_id,
			'created_at': datetime.now(timezone.utc).isoformat(),
			'task_identity': self.task_identity,
			'page_url': _redact_url(page_url),
			'active_filters': filters,
			'status': status,
			'counts': {
				'selected_requests': len(packets),
				'archived_requests': len(stored_packets),
				'archived_body_bytes': total_body_bytes,
				'datasets': len(manifest_datasets),
				'tables': sum(len(dataset['tables']) for dataset in manifest_datasets),
			},
			'packets': stored_packets,
			'datasets': manifest_datasets,
			'warnings': warnings,
		}
		# The completion marker is intentionally the final write in this directory.
		atomic_write_json(data_dir / 'manifest.json', manifest)
		return ChartArtifactResult(status, artifact_id, data_dir, manifest, preview)


__all__ = [
	'ChartArtifactResult',
	'ChartArtifactStatus',
	'ChartDataArtifactStore',
	'DatasetBundle',
	'NormalizedTable',
	'normalize_chart_packets',
	'sanitize_network_packet',
	'sanitize_packet_metadata',
	'sanitize_url',
]
