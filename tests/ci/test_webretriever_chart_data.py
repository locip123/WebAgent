from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import browser_use.webretriever.chart_data as chart_data
from browser_use.webretriever.chart_data import (
	ChartDataArtifactStore,
	normalize_chart_packets,
	sanitize_network_packet,
	sanitize_packet_metadata,
)


def _packet(request_id: int, url: str, body: str, content_type: str = 'application/json') -> dict[str, object]:
	return {
		'request_id': request_id,
		'timestamp': float(request_id),
		'url': url,
		'method': 'GET',
		'resource_type': 'xhr',
		'status': 200,
		'response_headers': {'content-type': content_type},
		'response_body_state': 'complete',
		'response_body': body,
	}


def _tableau_viz_data(value_offset: int = 0) -> tuple[dict[str, object], dict[str, object]]:
	segments: dict[str, object] = {
		'segment': {
			'dataColumns': [
				{'dataType': 'cstring', 'dataValues': ['Jan.', 'Feb.', 'Kansai', 'Total']},
				{'dataType': 'real', 'dataValues': [10 + value_offset, 100, 20 + value_offset, 100]},
			]
		}
	}
	pane_columns = {
		'vizDataColumns': [
			{'fieldCaption': 'Month', 'dataType': 'cstring', 'paneIndices': [0], 'columnIndices': [0]},
			{'fieldCaption': 'Port Name', 'dataType': 'cstring', 'paneIndices': [0], 'columnIndices': [1]},
			{'fieldCaption': 'SUM(Entries)', 'dataType': 'real', 'paneIndices': [0], 'columnIndices': [2]},
		],
		'paneColumnsList': [
			{
				'vizPaneColumns': [
					{'valueIndices': [0, 0, 1, 1], 'aliasIndices': [0, 0, 1, 1]},
					{'valueIndices': [2, 3, 2, 3], 'aliasIndices': [2, 3, 2, 3]},
					{'valueIndices': [0, 1, 2, 3], 'aliasIndices': [0, 1, 2, 3]},
				]
			}
		],
	}
	return segments, {'paneColumnsData': pane_columns}


def _tableau_packets() -> list[dict[str, object]]:
	segments, viz_data = _tableau_viz_data()
	info = {'sheetName': 'Dashboard', 'newSessionId': 'secret-session'}
	data = {
		'secondaryInfo': {
			'presModelMap': {
				'dataDictionary': {'presModelHolder': {'genDataDictionaryPresModel': {'dataSegments': segments}}},
				'vizData': {
					'presModelHolder': {
						'genPresModelMapPresModel': {
							'presModelMap': {'Port worksheet': {'presModelHolder': {'genVizDataPresModel': viz_data}}}
						}
					}
				},
			}
		}
	}
	info_text = json.dumps(info, ensure_ascii=False, separators=(',', ':'))
	data_text = json.dumps(data, ensure_ascii=False, separators=(',', ':'))
	bootstrap = f'{len(info_text)};{info_text}{len(data_text)};{data_text}'

	updated_segments, updated_viz_data = _tableau_viz_data(value_offset=5)
	filters_json = json.dumps(
		[
			{
				'fieldCaption': 'Year',
				'summary': '2023',
				'table': {'tuples': [{'s': True, 't': [{'v': '2023'}]}]},
			}
		]
	)
	command = {
		'vqlCmdResponse': {
			'layoutStatus': {
				'applicationPresModel': {
					'dataDictionary': {'dataSegments': updated_segments},
					'workbookPresModel': {
						'dashboardPresModel': {
							'zones': {
								'1': {
									'worksheet': 'Port worksheet',
									'presModelHolder': {'visual': {'vizData': updated_viz_data, 'filtersJson': filters_json}},
								}
							}
						}
					},
				}
			}
		}
	}
	return [
		_packet(1, 'https://public.tableau.com/vizql/w/book/v/view/bootstrapSession/sessions/ABC-0:0', bootstrap),
		_packet(
			2,
			'https://public.tableau.com/vizql/w/book/v/view/sessions/ABC-0:0/commands/tabdoc/categorical-filter-by-index',
			json.dumps(command, ensure_ascii=False, separators=(',', ':')),
		),
	]


def _jnto_2023_tableau_packets() -> list[dict[str, object]]:
	months = ['Jan.', 'Feb.', 'Mar.', 'Apr.', 'May', 'Jun.', 'Jul.', 'Aug.', 'Sep.', 'Oct.', 'Nov.', 'Dec.']
	ports = ['Kansai', 'Narita', 'Others(Airport)']

	def presentation(values: list[int]) -> tuple[dict[str, object], dict[str, object]]:
		segments: dict[str, object] = {
			'segment': {
				'dataColumns': [
					{'dataType': 'cstring', 'dataValues': [*months, *ports]},
					{'dataType': 'real', 'dataValues': values},
				]
			}
		}
		month_indices = [month for month in range(12) for _port in ports]
		port_indices = [12 + port for _month in months for port in range(len(ports))]
		viz_data: dict[str, object] = {
			'paneColumnsData': {
				'vizDataColumns': [
					{'fieldCaption': 'Month', 'dataType': 'cstring', 'paneIndices': [0], 'columnIndices': [0]},
					{'fieldCaption': 'Port Name', 'dataType': 'cstring', 'paneIndices': [0], 'columnIndices': [1]},
					{
						'fieldCaption': 'SUM(Foreigners Entries)',
						'dataType': 'real',
						'paneIndices': [0],
						'columnIndices': [2],
					},
				],
				'paneColumnsList': [
					{
						'vizPaneColumns': [
							{'valueIndices': month_indices, 'aliasIndices': month_indices},
							{'valueIndices': port_indices, 'aliasIndices': port_indices},
							{'valueIndices': list(range(len(values))), 'aliasIndices': list(range(len(values)))},
						]
					}
				],
			}
		}
		return segments, viz_data

	initial_values = [value for _month in months for value in (500_000, 1_000_000, 1_000_000)]
	# Every month except November has a Kansai share below 25%. November is
	# 663,794 / 2,460,000 = 26.9835%, matching the JNTO challenge fixture.
	target_values: list[int] = []
	for month in months:
		if month == 'Nov.':
			target_values.extend((663_794, 1_000_000, 796_206))
		else:
			target_values.extend((600_000, 1_200_000, 1_200_000))
	initial_segments, initial_viz = presentation(initial_values)
	target_segments, target_viz = presentation(target_values)
	info = {'sheetName': 'JNTO Port by Month'}
	data = {
		'secondaryInfo': {
			'presModelMap': {
				'dataDictionary': {'presModelHolder': {'genDataDictionaryPresModel': {'dataSegments': initial_segments}}},
				'vizData': {
					'presModelHolder': {
						'genPresModelMapPresModel': {
							'presModelMap': {'Port worksheet': {'presModelHolder': {'genVizDataPresModel': initial_viz}}}
						}
					}
				},
			}
		}
	}
	info_text = json.dumps(info, ensure_ascii=False, separators=(',', ':'))
	data_text = json.dumps(data, ensure_ascii=False, separators=(',', ':'))
	bootstrap = f'{len(info_text)};{info_text}{len(data_text)};{data_text}'
	filters_json = json.dumps(
		[{'fieldCaption': 'Year', 'summary': '2023', 'table': {'tuples': [{'s': True, 't': [{'v': '2023'}]}]}}]
	)
	command = {
		'vqlCmdResponse': {
			'layoutStatus': {
				'applicationPresModel': {
					'dataDictionary': {'dataSegments': target_segments},
					'workbookPresModel': {
						'dashboardPresModel': {
							'zones': {
								'1': {
									'worksheet': 'Port worksheet',
									'presModelHolder': {'visual': {'vizData': target_viz, 'filtersJson': filters_json}},
								}
							}
						}
					},
				}
			}
		}
	}
	return [
		_packet(91, 'https://public.tableau.com/vizql/w/jnto/v/port/bootstrapSession/sessions/JNTO', bootstrap),
		_packet(
			92,
			'https://public.tableau.com/vizql/w/jnto/v/port/sessions/JNTO/commands/tabdoc/categorical-filter-by-index',
			json.dumps(command, ensure_ascii=False, separators=(',', ':')),
		),
	]


def test_sanitize_packet_metadata_redacts_secrets_without_retaining_response_body() -> None:
	packet = {
		'url': 'https://example.test/vizql/sessions/session-secret/chart?year=2023&api_key=secret',
		'page_url': 'https://example.test/?token=secret',
		'headers': {'Authorization': 'Bearer secret', 'Accept': 'application/json'},
		'response_headers': {'Set-Cookie': 'sid=secret', 'content-type': 'application/json'},
		'post_data': '{"year":2023,"password":"secret"}',
		'raw_post_data': b'password=secret'.hex(),
		'json_data': {'filters': {'year': 2023}, 'access_token': 'secret'},
		'response_body': '{"rows":[]}',
	}

	sanitized = sanitize_packet_metadata(packet)

	assert 'secret' not in json.dumps(sanitized)
	assert sanitized['headers']['Authorization'] == '<redacted>'
	assert 'year=2023' in sanitized['url']
	assert 'raw_post_data' not in sanitized
	assert 'response_body' not in sanitized


def test_plain_text_authorization_and_post_credentials_are_fully_redacted() -> None:
	packet = {
		'url': 'https://example.test/chart',
		'headers': {'X-Debug': 'Authorization: Bearer header-supersecret'},
		'post_data': 'Authorization: Basic cG9zdC1zZWNyZXQ=\ntoken=plain-post-secret',
		'response_headers': {'X-Debug': 'Bearer response-supersecret'},
		'response_body': 'Authorization: Bearer body-supersecret',
	}

	sanitized = sanitize_network_packet(packet)
	encoded = json.dumps(sanitized, ensure_ascii=False)

	for secret in (
		'header-supersecret',
		'cG9zdC1zZWNyZXQ=',
		'plain-post-secret',
		'response-supersecret',
		'body-supersecret',
	):
		assert secret not in encoded
	assert '<redacted>' in encoded


def test_auth_scheme_words_in_chart_values_are_not_treated_as_credentials() -> None:
	packet = _packet(
		99,
		'https://example.test/chart.json',
		json.dumps({'rows': [{'key': 'Kansai', 'topic': 'Basic Education', 'note': 'Bearer plants are uncommon'}]}),
	)

	sanitized = sanitize_network_packet(packet)

	assert sanitized['response_json']['rows'][0] == {
		'key': 'Kansai',
		'topic': 'Basic Education',
		'note': 'Bearer plants are uncommon',
	}


def test_nested_url_aws_path_and_csv_credentials_are_redacted() -> None:
	nested_url = (
		'https://example.test/access_token/PATH-SECRET/password/PASSWORD-SECRET'
		'?redirect=https%3A%2F%2Fapi.example%2Fdata%3Ftoken%3DNESTED-SECRET'
		'&AWSAccessKeyId=AKIA-SECRET&year=2023'
	)
	url_packet = _packet(100, nested_url, '{"rows":[]}')
	csv_packet = _packet(
		101,
		'https://example.test/table.csv',
		'accessToken,href,key,label\n'
		'CSV-SECRET,https://user:CSV-PASSWORD@example.test/data?token=CSV-URL-SECRET,Kansai,Basic Education\n',
		'text/csv',
	)

	sanitized_url = sanitize_network_packet(url_packet)
	sanitized_csv = sanitize_network_packet(csv_packet)
	encoded = json.dumps([sanitized_url, sanitized_csv], ensure_ascii=False)

	for secret in (
		'PATH-SECRET',
		'PASSWORD-SECRET',
		'NESTED-SECRET',
		'AKIA-SECRET',
		'CSV-SECRET',
		'CSV-PASSWORD',
		'CSV-URL-SECRET',
	):
		assert secret not in encoded
	assert 'year=2023' in sanitized_url['url']
	assert ',Kansai,Basic Education' in sanitized_csv['response_body']


def test_deep_sanitization_handles_camel_case_query_path_and_response_secrets() -> None:
	packet = {
		'url': (
			'https://user:password@example.test/sessions/redacted-session-REAL-SECRET/token/path-secret'
			'/jwt/jwt-secret/bearer/bearer-secret/keys/key-secret'
			'?session=query-secret&refreshToken=refresh-secret&year=2023'
		),
		'headers': {
			'Referer': 'https://user:referer-password@example.test/chart?token=referer-secret',
			'Location': 'https://example.test/data?sig=location-secret',
		},
		'post_data': json.dumps(
			{
				'refreshToken': 'post-refresh',
				'clientSecret': 'post-client',
				'nested': {'sessionToken': 'post-session'},
				'secretary_count': 7,
				'tokenization_rate': 0.5,
				'access_token_count': 2,
			}
		),
		'response_headers': {'content-type': 'application/json'},
		'response_body': json.dumps(
			{
				'refreshToken': 'body-refresh',
				'clientSecret': 'body-client',
				'sessionToken': 'body-session',
				'href': 'https://user:href-password@example.test/file?token=href-secret',
				'note': 'token=note-secret',
				'secretary_count': 7,
				'tokenization_rate': 0.5,
				'access_token_count': 2,
			}
		),
	}

	sanitized = sanitize_network_packet(packet)
	encoded = json.dumps(sanitized, ensure_ascii=False)

	for secret in (
		'password',
		'REAL-SECRET',
		'path-secret',
		'jwt-secret',
		'bearer-secret',
		'key-secret',
		'query-secret',
		'refresh-secret',
		'referer-password',
		'referer-secret',
		'location-secret',
		'post-refresh',
		'post-client',
		'post-session',
		'body-refresh',
		'body-client',
		'body-session',
		'href-password',
		'href-secret',
		'note-secret',
	):
		assert secret not in encoded
	assert 'year=2023' in sanitized['url']
	assert 'redacted-tsid-' in sanitized['url']
	assert sanitized['response_json']['secretary_count'] == 7
	assert sanitized['response_json']['tokenization_rate'] == 0.5
	assert sanitized['response_json']['access_token_count'] == 2


def test_sanitized_tableau_sessions_remain_distinct_for_local_replay() -> None:
	first = _tableau_packets()
	second = []
	for packet in first:
		clone = dict(packet)
		clone['request_id'] = int(packet['request_id']) + 100
		clone['timestamp'] = float(clone['request_id'])
		clone['url'] = str(packet['url']).replace('ABC-0:0', 'ABC-1:0')
		second.append(clone)

	bundles, warnings = normalize_chart_packets(
		[sanitize_network_packet(packet) for packet in [*first, *second]], active_filters={'page_title': 'Chart'}
	)

	assert warnings == []
	assert len(bundles) == 2
	assert {tuple(bundle.request_ids) for bundle in bundles} == {(1, 2), (101, 102)}
	assert len({bundle.dataset_id for bundle in bundles}) == 2


def test_tableau_row_semantics_recognize_composite_totals_and_fail_closed() -> None:
	rows = [
		{'Port Name-value': 'Kansai'},
		{'Port Name-value': 'Total (All Ports)'},
		{'Port Name-value': 'All Ports'},
		{'Port Name-value': '空港計'},
		{'Port Name-value': 'Regional subtotal'},
		{'Port Name-value': ''},
		{'Port Name-value': '全空港'},
		{'Port Name-value': '全海港'},
		{'Port Name-value': '全口岸'},
		{'Port Name-value': '所有口岸'},
		{'Port Name-value': 'すべての港'},
		{'Port Name-value': '总计'},
		{'Port Name-value': '合计'},
		{'Port Name-value': '總計'},
		{'Port Name-value': '총계'},
		{'Port Name-value': '小计'},
		{'Port Name-value': '소계'},
	]

	semantics = chart_data._mark_tableau_row_semantics(rows)

	assert [row['__row_kind'] for row in rows] == [
		'leaf',
		'aggregate',
		'aggregate',
		'aggregate',
		'unknown',
		'unknown',
		'aggregate',
		'aggregate',
		'aggregate',
		'aggregate',
		'aggregate',
		'aggregate',
		'aggregate',
		'aggregate',
		'aggregate',
		'unknown',
		'unknown',
	]
	assert semantics['unknown_value'] == 'unknown'


def test_generic_json_and_delimited_normalizers() -> None:
	packets = [
		_packet(1, 'https://example.test/records.json', '{"data":[{"month":"Jan","value":1},{"month":"Feb","value":2}]}'),
		_packet(2, 'https://example.test/columns.json', '{"month":["Jan","Feb"],"value":[3,4]}'),
		_packet(3, 'https://example.test/table.tsv', 'name\tvalue\nalpha\t5\nbeta\t6\n', 'text/tab-separated-values'),
	]

	bundles, warnings = normalize_chart_packets(packets)

	assert warnings == []
	assert [bundle.parser for bundle in bundles] == ['json', 'json', 'csv']
	assert bundles[0].tables[0].rows[1] == {'month': 'Feb', 'value': 2}
	assert bundles[1].tables[0].rows[0] == {'month': 'Jan', 'value': 3}
	assert bundles[2].tables[0].rows[1] == {'name': 'beta', 'value': '6'}


def test_owid_pair_restores_entity_metadata_and_rejects_unpaired_data() -> None:
	data_packet = _packet(
		4,
		'https://api.ourworldindata.org/v1/indicators/123.data.json',
		'{"values":[1.5,2.5],"years":[2022,2023],"entities":[14,14]}',
	)
	metadata_packet = _packet(
		5,
		'https://api.ourworldindata.org/v1/indicators/123.metadata.json',
		json.dumps(
			{
				'name': 'Example indicator',
				'unit': 'people',
				'dimensions': {'entities': {'values': [{'id': 14, 'name': 'Japan', 'code': 'JPN'}]}},
			}
		),
	)

	bundles, warnings = normalize_chart_packets([data_packet, metadata_packet])

	assert warnings == []
	assert len(bundles) == 1
	assert bundles[0].parser == 'owid'
	assert bundles[0].tables[0].rows[1] == {
		'entity_id': 14,
		'entity': 'Japan',
		'entity_code': 'JPN',
		'year': 2023,
		'value': 2.5,
		'indicator': 'Example indicator',
		'unit': 'people',
	}

	unpaired, unpaired_warnings = normalize_chart_packets([data_packet])
	assert unpaired == []
	assert 'missing its metadata companion' in unpaired_warnings[0]


def test_tableau_bootstrap_and_command_are_replayed_locally() -> None:
	bundles, warnings = normalize_chart_packets(_tableau_packets(), active_filters={'page_title': 'Chart'})

	assert warnings == []
	assert len(bundles) == 1
	dataset = bundles[0]
	assert dataset.parser == 'tableau'
	assert dataset.active_filters == {'page_title': 'Chart', 'Year': '2023'}
	assert dataset.request_ids == [1, 2]
	table = dataset.tables[0]
	assert table.source_request_ids == [1, 2]
	assert len(table.rows) == 4
	assert table.rows[0]['SUM(Entries)-value'] == 15
	assert table.rows[1]['__row_kind'] == 'aggregate'
	assert table.row_semantics['row_kind_column'] == '__row_kind'


def test_jnto_2023_tableau_fixture_has_twelve_months_and_november_ratio_maximum() -> None:
	bundles, warnings = normalize_chart_packets(_jnto_2023_tableau_packets())

	assert warnings == []
	assert len(bundles) == 1
	table = bundles[0].tables[0]
	assert bundles[0].active_filters['Year'] == '2023'
	assert table.source_request_ids == [91, 92]
	assert len(table.rows) == 36
	assert {row['Month-alias'] for row in table.rows} == {
		'Jan.',
		'Feb.',
		'Mar.',
		'Apr.',
		'May',
		'Jun.',
		'Jul.',
		'Aug.',
		'Sep.',
		'Oct.',
		'Nov.',
		'Dec.',
	}
	monthly: dict[str, dict[str, int]] = {}
	for row in table.rows:
		assert row['__row_kind'] == 'leaf'
		month = str(row['Month-alias'])
		value = int(row['SUM(Foreigners Entries)-value'])
		monthly.setdefault(month, {'kansai': 0, 'total': 0})['total'] += value
		if row['Port Name-alias'] == 'Kansai':
			monthly[month]['kansai'] = value
	winner = max(monthly, key=lambda month: monthly[month]['kansai'] / monthly[month]['total'])
	assert winner == 'Nov.'
	assert monthly[winner] == {'kansai': 663_794, 'total': 2_460_000}


def test_artifact_store_writes_manifest_last_with_checksums_and_normalized_table(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	events: list[str] = []
	original_json = chart_data.atomic_write_json
	original_bytes = chart_data._atomic_write_bytes

	def tracked_json(path: Path | str, payload: object) -> Path:
		events.append(Path(path).name)
		return original_json(path, payload)

	def tracked_bytes(path: Path, payload: bytes) -> Path:
		events.append(path.name)
		return original_bytes(path, payload)

	monkeypatch.setattr(chart_data, 'atomic_write_json', tracked_json)
	monkeypatch.setattr(chart_data, '_atomic_write_bytes', tracked_bytes)
	packet = _packet(
		7,
		'https://example.test/chart.json?token=secret&year=2023',
		'{"rows":[{"month":"Nov","value":42}],"token":"secret"}',
	)
	packet['headers'] = {'Authorization': 'Bearer secret'}
	store = ChartDataArtifactStore(tmp_path / '4_task-id', {'task_idx': 4, 'task_id': 'task-id'})

	result = store.save([packet], page_url='https://example.test/chart', scan_id='scan_1')

	assert result.status == 'ready'
	assert events[-1] == 'manifest.json'
	manifest_path = result.data_dir / 'manifest.json'
	manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
	assert manifest['complete'] is True
	assert manifest['schema_version'] == 1
	assert manifest['task_identity'] == {'task_idx': 4, 'task_id': 'task-id'}
	packet_entry = manifest['packets'][0]
	metadata_path = result.data_dir / packet_entry['metadata_path']
	body_path = result.data_dir / packet_entry['body_path']
	assert hashlib.sha256(metadata_path.read_bytes()).hexdigest() == packet_entry['metadata_sha256']
	assert hashlib.sha256(body_path.read_bytes()).hexdigest() == packet_entry['body_sha256']
	assert b'secret' not in metadata_path.read_bytes()
	assert b'secret' not in body_path.read_bytes()
	table_entry = manifest['datasets'][0]['tables'][0]
	assert hashlib.sha256((result.data_dir / table_entry['csv_path']).read_bytes()).hexdigest() == table_entry['csv_sha256']
	assert hashlib.sha256((result.data_dir / table_entry['schema_path']).read_bytes()).hexdigest() == table_entry['schema_sha256']
	assert len(json.dumps(result.to_action_payload(), ensure_ascii=False)) < 32 * 1024

	with pytest.raises(FileExistsError):
		store.save([packet], scan_id='scan_1')
	with pytest.raises(ValueError, match='scan_id'):
		store.save([packet], scan_id='../escape')


def test_empty_artifact_is_complete_but_reports_no_match(tmp_path: Path) -> None:
	result = ChartDataArtifactStore(tmp_path / 'task', {'task_id': 'task'}).save([], scan_id='empty')

	assert result.status == 'no_match'
	assert result.manifest['complete'] is True
	assert result.manifest['counts']['archived_requests'] == 0
	assert (result.data_dir / 'manifest.json').is_file()


def test_artifact_store_refuses_symlinked_chart_root(tmp_path: Path) -> None:
	task_dir = tmp_path / 'task'
	task_dir.mkdir()
	outside = tmp_path / 'outside'
	outside.mkdir()
	(task_dir / 'chart_data').symlink_to(outside, target_is_directory=True)

	with pytest.raises(ValueError, match='symbolic link'):
		ChartDataArtifactStore(task_dir, {'task_id': 'task'}).save([], scan_id='escape')
