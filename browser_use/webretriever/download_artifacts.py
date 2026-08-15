"""Normalize browser-downloaded tabular data into auditable local artifacts.

This module deliberately accepts only files already saved from the browser
trajectory.  It never fetches URLs.  The resulting manifest is consumed by
``DataAnalysisAssistant`` through the same checksum-verified contract used for
chart artifacts.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import shutil
import stat
import tempfile
import zipfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from browser_use.webretriever.artifacts import atomic_write_json

_MAX_SOURCE_BYTES = 25 * 1024 * 1024
_MAX_TABLES = 32
_MAX_COLUMNS = 256
_MAX_ROWS = 500_000
_MAX_CELL_CHARS = 8_000
_MAX_JSON_DEPTH = 12
_SUPPORTED_SUFFIXES = frozenset({'.json', '.csv', '.tsv', '.xlsx', '.zip'})
_SAFE_NAME_RE = re.compile(r'[^A-Za-z0-9_.-]+')
_XLSX_NS = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'
_REL_NS = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}'
_PACKAGE_REL_NS = '{http://schemas.openxmlformats.org/package/2006/relationships}'


class DownloadArtifactError(ValueError):
	"""The downloaded source cannot become a bounded analysis artifact."""


@dataclass(frozen=True, slots=True)
class _Table:
	name: str
	location: str
	parser: str
	columns: list[str]
	rows: list[list[Any]]


def _sha256_bytes(value: bytes) -> str:
	return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
	digest = hashlib.sha256()
	with path.open('rb') as handle:
		for chunk in iter(lambda: handle.read(1024 * 1024), b''):
			digest.update(chunk)
	return digest.hexdigest()


def _safe_name(value: str, *, fallback: str) -> str:
	name = _SAFE_NAME_RE.sub('_', value).strip('._')
	return (name[:100] or fallback).casefold()


def _scalar(value: Any) -> Any:
	if value is None or isinstance(value, (bool, int)):
		return value
	if isinstance(value, float):
		return value if value == value and value not in {float('inf'), float('-inf')} else str(value)
	if isinstance(value, (Mapping, list, tuple)):
		text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), default=str)
	else:
		text = str(value)
	return text if len(text) <= _MAX_CELL_CHARS else text[:_MAX_CELL_CHARS] + '…'


def _column_name(value: Any, index: int, used: set[str]) -> str:
	base = str(value).strip() if value is not None else ''
	base = base[:512] or f'column_{index + 1}'
	name = base
	counter = 2
	while name in used:
		name = f'{base[:500]}_{counter}'
		counter += 1
	used.add(name)
	return name


def _columns_from_header(values: Sequence[Any]) -> list[str]:
	used: set[str] = set()
	return [_column_name(value, index, used) for index, value in enumerate(values[:_MAX_COLUMNS])]


def _table_from_matrix(name: str, location: str, parser: str, matrix: Iterable[Sequence[Any]]) -> _Table | None:
	header: list[str] | None = None
	rows: list[list[Any]] = []
	for raw_row in matrix:
		values = list(raw_row)
		if not any(value not in (None, '') for value in values):
			continue
		if header is None:
			header = _columns_from_header(values)
			if not header:
				return None
			continue
		if len(rows) >= _MAX_ROWS:
			raise DownloadArtifactError(f'{location} exceeds the {_MAX_ROWS}-row safety limit')
		rows.append([_scalar(values[index]) if index < len(values) else None for index in range(len(header))])
	if header is None:
		return None
	return _Table(name=name, location=location, parser=parser, columns=header, rows=rows)


def _flatten(value: Any, prefix: str = '') -> dict[str, Any]:
	result: dict[str, Any] = {}

	def visit(current: Any, path: str, depth: int) -> None:
		if depth >= _MAX_JSON_DEPTH or not isinstance(current, Mapping):
			result[path or 'value'] = _scalar(current)
			return
		for key, child in current.items():
			label = str(key).replace('\x00', '')[:256] or 'field'
			next_path = f'{path}.{label}' if path else label
			if isinstance(child, Mapping):
				visit(child, next_path, depth + 1)
			elif isinstance(child, list):
				result[next_path] = _scalar(child)
			else:
				result[next_path] = _scalar(child)

	visit(value, prefix, 0)
	return result


def _table_from_json_records(name: str, location: str, records: Sequence[Mapping[str, Any]]) -> _Table:
	if len(records) > _MAX_ROWS:
		raise DownloadArtifactError(f'{location} exceeds the {_MAX_ROWS}-row safety limit')
	flattened = [_flatten(record) for record in records]
	columns: list[str] = []
	for record in flattened:
		for key in record:
			if key not in columns:
				columns.append(key)
				if len(columns) > _MAX_COLUMNS:
					raise DownloadArtifactError(f'{location} exceeds the {_MAX_COLUMNS}-column safety limit')
	if not columns:
		raise DownloadArtifactError(f'{location} has no scalar fields')
	return _Table(
		name=name,
		location=location,
		parser='json',
		columns=columns,
		rows=[[record.get(column) for column in columns] for record in flattened],
	)


def _json_tables(value: Any, location: str = '$') -> list[_Table]:
	tables: list[_Table] = []

	def visit(current: Any, path: str, depth: int) -> None:
		if len(tables) >= _MAX_TABLES or depth >= _MAX_JSON_DEPTH:
			return
		if isinstance(current, list):
			if current and all(isinstance(item, Mapping) for item in current):
				name = _safe_name(path.replace('$', 'root').replace('.', '_').replace('[', '_').replace(']', ''), fallback='records')
				tables.append(_table_from_json_records(name, path, list(current)))
				return
			for index, item in enumerate(current[:_MAX_TABLES]):
				visit(item, f'{path}[{index}]', depth + 1)
		elif isinstance(current, Mapping):
			for key, item in current.items():
				visit(item, f'{path}.{key}', depth + 1)

	visit(value, location, 0)
	if not tables and isinstance(value, Mapping):
		tables.append(_table_from_json_records('root', '$', [value]))
	return tables[:_MAX_TABLES]


def _csv_table(raw: bytes, *, name: str, location: str, delimiter: str) -> _Table | None:
	try:
		text = raw.decode('utf-8-sig')
	except UnicodeDecodeError as exc:
		raise DownloadArtifactError(f'{location} is not UTF-8 text') from exc
	try:
		reader = csv.reader(io.StringIO(text), delimiter=delimiter)
	except csv.Error as exc:
		raise DownloadArtifactError(f'{location} is not valid delimited text') from exc
	try:
		return _table_from_matrix(name, location, 'tsv' if delimiter == '\t' else 'csv', reader)
	except csv.Error as exc:
		raise DownloadArtifactError(f'{location} is not valid delimited text') from exc


def _xlsx_column_index(reference: str) -> int:
	letters = ''.join(character for character in reference if character.isalpha()).upper()
	if not letters:
		return 0
	value = 0
	for character in letters:
		value = value * 26 + ord(character) - ord('A') + 1
	return max(0, value - 1)


def _xlsx_shared_strings(archive: zipfile.ZipFile) -> list[str]:
	if 'xl/sharedStrings.xml' not in archive.namelist():
		return []
	root = ElementTree.fromstring(archive.read('xl/sharedStrings.xml'))
	return [''.join(node.text or '' for node in item.iter(f'{_XLSX_NS}t')) for item in root.findall(f'{_XLSX_NS}si')]


def _xlsx_tables(raw: bytes, location: str) -> list[_Table]:
	try:
		with zipfile.ZipFile(io.BytesIO(raw)) as archive:
			shared = _xlsx_shared_strings(archive)
			workbook = ElementTree.fromstring(archive.read('xl/workbook.xml'))
			relationships = ElementTree.fromstring(archive.read('xl/_rels/workbook.xml.rels'))
			targets = {
				item.attrib.get('Id', ''): item.attrib.get('Target', '')
				for item in relationships.findall(f'{_PACKAGE_REL_NS}Relationship')
			}
			tables: list[_Table] = []
			for sheet_index, sheet in enumerate(workbook.findall(f'{_XLSX_NS}sheets/{_XLSX_NS}sheet')):
				if len(tables) >= _MAX_TABLES:
					break
				rel_id = sheet.attrib.get(f'{_REL_NS}id', '')
				target = targets.get(rel_id, '')
				if not target:
					continue
				path = target if target.startswith('xl/') else f'xl/{target.lstrip("/")}'
				root = ElementTree.fromstring(archive.read(path))
				def rows() -> Iterable[list[Any]]:
					for row in root.iter(f'{_XLSX_NS}row'):
						values: list[Any] = []
						for cell in row.findall(f'{_XLSX_NS}c'):
							column = _xlsx_column_index(cell.attrib.get('r', ''))
							while len(values) <= column:
								values.append(None)
							kind = cell.attrib.get('t', '')
							value_node = cell.find(f'{_XLSX_NS}v')
							value: Any = value_node.text if value_node is not None else ''
							if kind == 's' and str(value).isdigit() and int(value) < len(shared):
								value = shared[int(value)]
							elif kind == 'inlineStr':
								value = ''.join(node.text or '' for node in cell.iter(f'{_XLSX_NS}t'))
							elif kind == 'b':
								value = str(value) == '1'
							elif kind not in {'str', 'e'}:
								with_value = str(value)
								try:
									value = int(with_value) if re.fullmatch(r'-?\d+', with_value) else float(with_value)
								except ValueError:
									value = with_value
							values[column] = value
						yield values
				name = _safe_name(sheet.attrib.get('name', f'sheet_{sheet_index + 1}'), fallback=f'sheet_{sheet_index + 1}')
				table = _table_from_matrix(name, f'{location}#{sheet.attrib.get("name", name)}', 'xlsx', rows())
				if table is not None:
					tables.append(table)
			return tables
	except (KeyError, OSError, ElementTree.ParseError, zipfile.BadZipFile) as exc:
		raise DownloadArtifactError(f'{location} is not a supported XLSX workbook') from exc


def _is_safe_zip_member(info: zipfile.ZipInfo) -> bool:
	path = Path(info.filename.replace('\\', '/'))
	return not info.is_dir() and not path.is_absolute() and '..' not in path.parts and not stat.S_ISLNK(info.external_attr >> 16)


def _tables_for_bytes(raw: bytes, suffix: str, location: str) -> list[_Table]:
	if len(raw) > _MAX_SOURCE_BYTES:
		raise DownloadArtifactError(f'{location} exceeds the {_MAX_SOURCE_BYTES}-byte safety limit')
	if suffix == '.json':
		try:
			return _json_tables(json.loads(raw.decode('utf-8-sig')), f'{location}:$')
		except (UnicodeDecodeError, json.JSONDecodeError) as exc:
			raise DownloadArtifactError(f'{location} is not valid JSON') from exc
	if suffix == '.csv':
		table = _csv_table(raw, name=_safe_name(Path(location).stem, fallback='data'), location=location, delimiter=',')
		return [table] if table is not None else []
	if suffix == '.tsv':
		table = _csv_table(raw, name=_safe_name(Path(location).stem, fallback='data'), location=location, delimiter='\t')
		return [table] if table is not None else []
	if suffix == '.xlsx':
		return _xlsx_tables(raw, location)
	if suffix != '.zip':
		return []
	try:
		with zipfile.ZipFile(io.BytesIO(raw)) as archive:
			tables: list[_Table] = []
			for info in archive.infolist():
				member_suffix = Path(info.filename).suffix.casefold()
				if len(tables) >= _MAX_TABLES:
					break
				if not _is_safe_zip_member(info) or member_suffix not in _SUPPORTED_SUFFIXES - {'.zip'}:
					continue
				if info.file_size > _MAX_SOURCE_BYTES:
					raise DownloadArtifactError(f'ZIP member {info.filename!r} exceeds the source size limit')
				with archive.open(info) as member:
					member_raw = member.read(_MAX_SOURCE_BYTES + 1)
				member_location = f'{location}!{info.filename}'
				tables.extend(_tables_for_bytes(member_raw, member_suffix, member_location))
			return tables[:_MAX_TABLES]
	except zipfile.BadZipFile as exc:
		raise DownloadArtifactError(f'{location} is not a valid ZIP archive') from exc


def _column_type(values: Sequence[Any]) -> str:
	nonempty = [value for value in values if value is not None and value != '']
	if nonempty and all(isinstance(value, bool) for value in nonempty):
		return 'boolean'
	if nonempty and all(isinstance(value, int) and not isinstance(value, bool) for value in nonempty):
		return 'integer'
	if nonempty and all(isinstance(value, (int, float)) and not isinstance(value, bool) for value in nonempty):
		return 'number'
	return 'string'


def _write_table(data_dir: Path, table: _Table, index: int, *, artifact_id: str, source_url: str, source_sha256: str) -> dict[str, Any]:
	name = _safe_name(table.name, fallback=f'table_{index + 1}')
	table_dir = data_dir / 'tables' / f'{index + 1:02d}_{name}'
	table_dir.mkdir(parents=True, exist_ok=False)
	csv_path = table_dir / 'data.csv'
	with csv_path.open('w', encoding='utf-8', newline='') as handle:
		writer = csv.writer(handle)
		writer.writerow(table.columns)
		writer.writerows(table.rows)
	columns = [
		{'name': column, 'type': _column_type([row[position] for row in table.rows])}
		for position, column in enumerate(table.columns)
	]
	schema_path = table_dir / 'schema.json'
	atomic_write_json(
		schema_path,
		{
			'columns': columns,
			'row_semantics': {'source_location': table.location, 'source_kind': 'browser_download'},
		},
	)
	return {
		'table_id': name,
		'csv_path': csv_path.relative_to(data_dir).as_posix(),
		'schema_path': schema_path.relative_to(data_dir).as_posix(),
		'csv_sha256': _sha256_file(csv_path),
		'schema_sha256': _sha256_file(schema_path),
		'row_count': len(table.rows),
		'columns': columns,
		'source_request_ids': [],
		'source_urls': [source_url],
		'source_artifact_id': artifact_id,
		'source_sha256': source_sha256,
		'source_location': table.location,
	}


def prepare_download_artifact(
	*,
	task_dir: Path,
	task_identity: Mapping[str, Any],
	source_path: Path,
	source_url: str,
	content_type: str = '',
) -> dict[str, Any]:
	"""Create a manifest-verified artifact from one already-downloaded file.

	``DownloadArtifactError`` means the browser download remains preserved but is
	not safe or tabular enough for the analysis action.
	"""
	source_candidate = Path(source_path)
	if source_candidate.is_symlink():
		raise DownloadArtifactError('downloaded source must not be a symbolic link')
	try:
		source = source_candidate.resolve(strict=True)
	except (OSError, FileNotFoundError) as exc:
		raise DownloadArtifactError('downloaded source file no longer exists') from exc
	if not source.is_file() or source.is_symlink():
		raise DownloadArtifactError('downloaded source is not a regular file')
	if source.stat().st_size > _MAX_SOURCE_BYTES:
		raise DownloadArtifactError(f'downloaded source exceeds the {_MAX_SOURCE_BYTES}-byte safety limit')
	suffix = source.suffix.casefold()
	if suffix not in _SUPPORTED_SUFFIXES:
		raise DownloadArtifactError(f'{suffix or "unknown"} is not a supported structured download format')
	raw = source.read_bytes()
	source_sha256 = _sha256_bytes(raw)
	artifact_id = 'download-' + hashlib.sha256(f'{source_url}\x00{source_sha256}'.encode('utf-8')).hexdigest()[:24]
	root = Path(task_dir).resolve() / 'data_artifacts'
	data_dir = root / artifact_id
	manifest_path = data_dir / 'manifest.json'
	if manifest_path.is_file():
		try:
			manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
			if manifest.get('source', {}).get('sha256') == source_sha256:
				return {
					'status': 'ready',
					'artifact_id': artifact_id,
					'data_dir': str(data_dir),
					'manifest_sha256': _sha256_file(manifest_path),
					'table_count': sum(len(dataset.get('tables', [])) for dataset in manifest.get('datasets', [])),
					'source_url': source_url,
				}
		except (OSError, UnicodeError, json.JSONDecodeError, AttributeError):
			pass
		raise DownloadArtifactError('download artifact directory already contains a conflicting manifest')
	root.mkdir(parents=True, exist_ok=True)
	temporary_dir: Path | None = Path(tempfile.mkdtemp(dir=root, prefix=f'.{artifact_id}.'))
	try:
		assert temporary_dir is not None
		(temporary_dir / 'analysis').mkdir()
		raw_dir = temporary_dir / 'raw'
		raw_dir.mkdir()
		raw_name = _safe_name(source.stem, fallback='download') + suffix
		raw_copy = raw_dir / raw_name
		shutil.copyfile(source, raw_copy)
		tables = _tables_for_bytes(raw, suffix, source.name)
		if not tables:
			raise DownloadArtifactError('download contains no supported tabular data')
		if len(tables) > _MAX_TABLES:
			tables = tables[:_MAX_TABLES]
		manifest_tables = [
			_write_table(temporary_dir, table, index, artifact_id=artifact_id, source_url=source_url, source_sha256=source_sha256)
			for index, table in enumerate(tables)
		]
		manifest = {
			'schema_version': 1,
			'complete': True,
			'artifact_id': artifact_id,
			'artifact_kind': 'download',
			'task_identity': dict(task_identity),
			'warnings': [],
			'source': {
				'url': source_url,
				'sha256': source_sha256,
				'content_type': content_type,
				'raw_path': raw_copy.relative_to(temporary_dir).as_posix(),
			},
			'datasets': [
				{
					'dataset_id': artifact_id,
					'parser': f'download_{suffix.lstrip(".")}',
					'active_filters': {},
					'source_urls': [source_url],
					'source_artifact_id': artifact_id,
					'source_sha256': source_sha256,
					'tables': manifest_tables,
				}
			],
		}
		temporary_manifest = temporary_dir / 'manifest.json'
		atomic_write_json(temporary_manifest, manifest)
		try:
			os.replace(temporary_dir, data_dir)
			temporary_dir = None
		except FileExistsError:
			if manifest_path.is_file():
				try:
					existing = json.loads(manifest_path.read_text(encoding='utf-8'))
					if existing.get('source', {}).get('sha256') == source_sha256:
						return {
							'status': 'ready',
							'artifact_id': artifact_id,
							'data_dir': str(data_dir),
							'manifest_sha256': _sha256_file(manifest_path),
							'table_count': sum(len(dataset.get('tables', [])) for dataset in existing.get('datasets', [])),
							'source_url': source_url,
						}
				except (OSError, UnicodeError, json.JSONDecodeError, AttributeError):
					pass
			raise DownloadArtifactError('download artifact directory already contains a conflicting manifest')
		return {
			'status': 'ready',
			'artifact_id': artifact_id,
			'data_dir': str(data_dir),
			'manifest_sha256': _sha256_file(manifest_path),
			'table_count': len(manifest_tables),
			'row_count': sum(int(table['row_count']) for table in manifest_tables),
			'source_url': source_url,
		}
	finally:
		if temporary_dir is not None and temporary_dir.exists():
			shutil.rmtree(temporary_dir)
