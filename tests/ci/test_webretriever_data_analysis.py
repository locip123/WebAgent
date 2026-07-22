from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from browser_use.webretriever.data_analysis import (
	AnalysisAnswer,
	DataAnalysisAssistant,
	GeneratedAnalysisCode,
	QueryResult,
)

TASK_IDENTITY = {
	'task_idx': 4,
	'task_id': '0123456789abcdef0123456789abcdef',
	'website': 'https://example.com/chart',
	'task': 'Which month has the highest ratio?',
}


def _sha(path: Path) -> str:
	return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_artifact(task_dir: Path, *, identity: dict[str, Any] | None = None) -> Path:
	data_dir = task_dir / 'chart_data' / 'scan-one'
	table_dir = data_dir / 'tables' / 'visitors'
	table_dir.mkdir(parents=True)
	(data_dir / 'analysis').mkdir()
	csv_path = table_dir / 'monthly.csv'
	csv_path.write_text(
		'month,kansai,total\n2023-10,20,100\n2023-11,30,100\n',
		encoding='utf-8',
	)
	schema_path = table_dir / 'monthly.schema.json'
	schema_path.write_text(
		json.dumps(
			{
				'row_semantics': {
					'row_kind_column': '__row_kind',
					'aggregation_rule': 'Prefer Total; otherwise sum mutually exclusive leaf rows only.',
				},
				'columns': [
					{'name': 'month', 'type': 'string'},
					{'name': 'kansai', 'type': 'integer'},
					{'name': 'total', 'type': 'integer'},
				],
			},
			ensure_ascii=False,
		),
		encoding='utf-8',
	)
	manifest = {
		'schema_version': 1,
		'complete': True,
		'artifact_id': 'scan-one',
		'task_identity': identity or TASK_IDENTITY,
		'warnings': [],
		'datasets': [
			{
				'dataset_id': 'visitors',
				'parser': 'tableau',
				'request_ids': [66],
				'active_filters': {'Year': '2023'},
				'tables': [
					{
						'table_id': 'monthly_visitors',
						'csv_path': csv_path.relative_to(data_dir).as_posix(),
						'schema_path': schema_path.relative_to(data_dir).as_posix(),
						'csv_sha256': _sha(csv_path),
						'schema_sha256': _sha(schema_path),
						'row_count': 2,
						'columns': [
							{'name': 'month', 'type': 'string'},
							{'name': 'kansai', 'type': 'integer'},
							{'name': 'total', 'type': 'integer'},
						],
						'source_request_ids': [66],
					}
				],
			}
		],
	}
	(data_dir / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
	return data_dir


class FakeCodeBackend:
	def __init__(self, codes: list[str], usage: dict[str, int] | None = None) -> None:
		self.codes = iter(codes)
		self.usage = usage or {}
		self.calls: list[tuple[str, list[str]]] = []

	async def generate_code(self, *, llm: Any, analysis_query: str, tables: Any, timeout_seconds: float):
		self.calls.append((analysis_query, [table.sql_name for table in tables]))
		return GeneratedAnalysisCode(code=next(self.codes), usage=self.usage)


class FakeExecutor:
	requires_sqlglot = False

	def __init__(self, result: QueryResult) -> None:
		self.result = result
		self.calls: list[tuple[str, list[str]]] = []

	async def execute(self, *, tables: Any, sql: str, timeout_seconds: float) -> QueryResult:
		self.calls.append((sql, [table.sql_name for table in tables]))
		return self.result


class FailingIfCalledExecutor(FakeExecutor):
	async def execute(self, *, tables: Any, sql: str, timeout_seconds: float) -> QueryResult:
		raise AssertionError('unsafe generated code must never reach the query executor')


class FakeAnswerLLM:
	def __init__(self, answer: AnalysisAnswer) -> None:
		self.answer = answer
		self.calls: list[tuple[list[Any], Any]] = []

	async def ainvoke(self, messages: list[Any], output_format: Any = None) -> Any:
		self.calls.append((messages, output_format))
		return SimpleNamespace(
			completion=self.answer,
			usage=SimpleNamespace(prompt_tokens=11, completion_tokens=7, total_tokens=18),
		)


class FailingIfCalledLLM:
	async def ainvoke(self, messages: list[Any], output_format: Any = None) -> Any:
		raise AssertionError('answer model must not be called')


class PandasAIFlowLLM:
	async def ainvoke(self, messages: list[Any], output_format: Any = None) -> Any:
		if output_format is AnalysisAnswer:
			return SimpleNamespace(
				completion=AnalysisAnswer(
					answer='2023年11月，比例为30%。',
					result_type='string',
					evidence_row_indices=[0],
				),
				usage=None,
			)
		return SimpleNamespace(
			completion=(
				'```python\nresult = execute_sql_query("SELECT month, kansai, total, '
				'kansai * 1.0 / NULLIF(total, 0) AS ratio FROM monthly_visitors '
				'ORDER BY ratio DESC LIMIT 1")\n```'
			),
			usage=None,
		)


@pytest.mark.asyncio
async def test_analysis_validates_manifest_executes_safe_sql_and_saves_result(tmp_path: Path):
	data_dir = _write_artifact(tmp_path)
	backend = FakeCodeBackend(
		[
			'result = execute_sql_query("SELECT month, kansai, total, kansai * 1.0 / NULLIF(total, 0) AS ratio '
			'FROM monthly_visitors ORDER BY ratio DESC LIMIT 1")'
		],
		usage={'prompt_tokens': 5, 'completion_tokens': 3, 'total_tokens': 8},
	)
	executor = FakeExecutor(QueryResult(columns=['month', 'kansai', 'total', 'ratio'], rows=[['2023-11', 30, 100, 0.3]]))
	llm = FakeAnswerLLM(
		AnalysisAnswer(
			answer='2023年11月，比例为30%。',
			result_type='string',
			evidence_row_indices=[0],
		)
	)
	assistant = DataAnalysisAssistant(
		llm,
		task_dir=tmp_path,
		task_identity=TASK_IDENTITY,
		code_backend=backend,
		query_executor=executor,
	)

	execution = await assistant.execute(analysis_query='哪个月比例最高？', data_dir=str(data_dir.resolve()))
	payload = json.loads(execution.output)

	assert payload['status'] == 'ok'
	assert payload['answer'] == '2023年11月，比例为30%。'
	assert payload['evidence_rows'] == [
		{'row_index': 0, 'values': {'month': '2023-11', 'kansai': 30, 'total': 100, 'ratio': 0.3}}
	]
	assert payload['provenance'][0]['source_request_ids'] == [66]
	assert payload['provenance'][0]['active_filters'] == {'Year': '2023'}
	assert payload['provenance'][0]['row_semantics']['row_kind_column'] == '__row_kind'
	assert executor.calls[0][1] == ['monthly_visitors']
	assert llm.calls[0][1] is AnalysisAnswer
	assert execution.usage == {'prompt_tokens': 16, 'completion_tokens': 10, 'total_tokens': 26}
	saved = data_dir / 'analysis' / f'{payload["analysis_id"]}.json'
	assert saved.is_file()
	assert json.loads(saved.read_text(encoding='utf-8')) == payload


@pytest.mark.asyncio
@pytest.mark.skipif(
	any(importlib.util.find_spec(name) is None for name in ('duckdb', 'sqlglot', 'astor', 'jinja2', 'yaml')),
	reason='optional prepared PandasAI runtime dependencies are not installed',
)
async def test_default_backend_uses_prepared_pandasai_and_isolated_duckdb(tmp_path: Path):
	data_dir = _write_artifact(tmp_path)
	assistant = DataAnalysisAssistant(
		PandasAIFlowLLM(),
		task_dir=tmp_path,
		task_identity=TASK_IDENTITY,
		model_timeout_seconds=20,
	)

	payload = json.loads((await assistant.execute(analysis_query='哪个月比例最高？', data_dir=data_dir.resolve())).output)

	assert payload['status'] == 'ok'
	assert payload['answer'] == '2023年11月，比例为30%。'
	assert payload['evidence_rows'][0]['values'] == {
		'month': '2023-11',
		'kansai': 30,
		'total': 100,
		'ratio': 0.3,
	}


def test_prepared_pandasai_runtime_loads_only_generation_core() -> None:
	from browser_use.webretriever.data_analysis import _load_vendored_pandasai

	Agent, Config, _LLM, _DataFrame = _load_vendored_pandasai()

	assert not hasattr(Agent, 'execute_code')
	assert Config.model_fields['save_logs'].default is False
	assert Config.model_fields['max_retries'].default == 0
	for module_name in (
		'pandasai.cli',
		'pandasai.ee',
		'pandasai.helpers.telemetry',
		'pandasai.core.code_execution',
		'pandasai.core.response.chart',
	):
		assert module_name not in sys.modules


@pytest.mark.asyncio
async def test_analysis_rejects_malicious_generated_python_even_after_repair(tmp_path: Path):
	data_dir = _write_artifact(tmp_path)
	backend = FakeCodeBackend(
		[
			'import os\nresult = execute_sql_query("SELECT * FROM monthly_visitors")',
			'result = __import__("os").system("id")',
		]
	)
	assistant = DataAnalysisAssistant(
		FailingIfCalledLLM(),
		task_dir=tmp_path,
		task_identity=TASK_IDENTITY,
		code_backend=backend,
		query_executor=FailingIfCalledExecutor(QueryResult(columns=[], rows=[])),
	)

	execution = await assistant.execute(analysis_query='Analyze it', data_dir=data_dir.resolve())
	payload = json.loads(execution.output)

	assert payload['status'] == 'unsafe_code'
	assert len(backend.calls) == 2
	assert 'rejected' in backend.calls[1][0]


@pytest.mark.asyncio
async def test_analysis_rejects_external_file_sql(tmp_path: Path):
	data_dir = _write_artifact(tmp_path)
	backend = FakeCodeBackend(
		[
			'result = execute_sql_query("SELECT * FROM read_csv_auto(\'/etc/passwd\')")',
			'result = execute_sql_query("COPY monthly_visitors TO \'/tmp/stolen.csv\'")',
		]
	)
	assistant = DataAnalysisAssistant(
		FailingIfCalledLLM(),
		task_dir=tmp_path,
		task_identity=TASK_IDENTITY,
		code_backend=backend,
		query_executor=FailingIfCalledExecutor(QueryResult(columns=[], rows=[])),
	)

	payload = json.loads((await assistant.execute(analysis_query='Analyze it', data_dir=data_dir.resolve())).output)

	assert payload['status'] == 'unsafe_code'


@pytest.mark.asyncio
async def test_analysis_rejects_environment_and_arbitrary_duckdb_functions(tmp_path: Path):
	data_dir = _write_artifact(tmp_path)
	backend = FakeCodeBackend(
		[
			'result = execute_sql_query("SELECT getenv(\'HOME\')")',
			'result = execute_sql_query("SELECT read_text(\'/etc/passwd\')")',
		]
	)
	assistant = DataAnalysisAssistant(
		FailingIfCalledLLM(),
		task_dir=tmp_path,
		task_identity=TASK_IDENTITY,
		code_backend=backend,
		query_executor=FailingIfCalledExecutor(QueryResult(columns=[], rows=[])),
	)

	payload = json.loads((await assistant.execute(analysis_query='Read secrets', data_dir=data_dir.resolve())).output)

	assert payload['status'] == 'unsafe_code'


@pytest.mark.asyncio
@pytest.mark.skipif(importlib.util.find_spec('sqlglot') is None, reason='strict SQL validator dependency is not installed')
async def test_analysis_requires_query_to_read_a_manifest_table(tmp_path: Path):
	data_dir = _write_artifact(tmp_path)
	backend = FakeCodeBackend(
		[
			'result = execute_sql_query("SELECT 42 AS answer")',
			'result = execute_sql_query("WITH answer AS (SELECT 42 AS value) SELECT * FROM answer")',
		]
	)
	executor = FailingIfCalledExecutor(QueryResult(columns=[], rows=[]))
	executor.requires_sqlglot = True
	assistant = DataAnalysisAssistant(
		FailingIfCalledLLM(),
		task_dir=tmp_path,
		task_identity=TASK_IDENTITY,
		code_backend=backend,
		query_executor=executor,
	)

	payload = json.loads((await assistant.execute(analysis_query='Invent an answer', data_dir=data_dir.resolve())).output)

	assert payload['status'] == 'unsafe_code'


@pytest.mark.asyncio
@pytest.mark.skipif(importlib.util.find_spec('sqlglot') is None, reason='strict SQL validator dependency is not installed')
async def test_analysis_rejects_cte_that_shadows_a_manifest_table(tmp_path: Path):
	data_dir = _write_artifact(tmp_path)
	forged = (
		'result = execute_sql_query("WITH monthly_visitors AS (SELECT 42 AS month, 999 AS value) SELECT * FROM monthly_visitors")'
	)
	backend = FakeCodeBackend([forged, forged])
	executor = FailingIfCalledExecutor(QueryResult(columns=[], rows=[]))
	executor.requires_sqlglot = True
	assistant = DataAnalysisAssistant(
		FailingIfCalledLLM(),
		task_dir=tmp_path,
		task_identity=TASK_IDENTITY,
		code_backend=backend,
		query_executor=executor,
	)

	payload = json.loads((await assistant.execute(analysis_query='Invent an answer', data_dir=data_dir.resolve())).output)

	assert payload['status'] == 'unsafe_code'
	assert 'shadow' in payload['error']


@pytest.mark.asyncio
async def test_analysis_rejects_outside_parent_segments_and_symlinks(tmp_path: Path):
	data_dir = _write_artifact(tmp_path)
	outside = tmp_path / 'outside'
	outside.mkdir()
	assistant = DataAnalysisAssistant(
		FailingIfCalledLLM(),
		task_dir=tmp_path,
		task_identity=TASK_IDENTITY,
		code_backend=FakeCodeBackend([]),
		query_executor=FailingIfCalledExecutor(QueryResult(columns=[], rows=[])),
	)

	outside_payload = json.loads((await assistant.execute(analysis_query='Analyze', data_dir=outside.resolve())).output)
	parent_payload = json.loads((await assistant.execute(analysis_query='Analyze', data_dir=f'{data_dir}/../scan-one')).output)
	link = tmp_path / 'chart_data' / 'linked-scan'
	link.symlink_to(data_dir, target_is_directory=True)
	symlink_payload = json.loads((await assistant.execute(analysis_query='Analyze', data_dir=link)).output)

	assert outside_payload['status'] == 'invalid_data_dir'
	assert parent_payload['status'] == 'invalid_data_dir'
	assert symlink_payload['status'] == 'invalid_data_dir'


@pytest.mark.asyncio
async def test_analysis_rejects_symlinked_task_chart_root(tmp_path: Path):
	real_task = tmp_path / 'real-task'
	_write_artifact(real_task)
	linked_task = tmp_path / 'linked-task'
	linked_task.mkdir()
	(linked_task / 'chart_data').symlink_to(real_task / 'chart_data', target_is_directory=True)
	assistant = DataAnalysisAssistant(
		FailingIfCalledLLM(),
		task_dir=linked_task,
		task_identity=TASK_IDENTITY,
		code_backend=FakeCodeBackend([]),
		query_executor=FailingIfCalledExecutor(QueryResult(columns=[], rows=[])),
	)

	payload = json.loads(
		(await assistant.execute(analysis_query='Analyze', data_dir=(linked_task / 'chart_data' / 'scan-one').absolute())).output
	)

	assert payload['status'] == 'invalid_data_dir'
	assert 'symbolic link' in payload['error']


@pytest.mark.asyncio
async def test_analysis_rejects_manifest_checksum_mismatch(tmp_path: Path):
	data_dir = _write_artifact(tmp_path)
	manifest_path = data_dir / 'manifest.json'
	manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
	manifest['datasets'][0]['tables'][0]['csv_sha256'] = '0' * 64
	manifest_path.write_text(json.dumps(manifest), encoding='utf-8')
	assistant = DataAnalysisAssistant(
		FailingIfCalledLLM(),
		task_dir=tmp_path,
		task_identity=TASK_IDENTITY,
		code_backend=FakeCodeBackend([]),
		query_executor=FailingIfCalledExecutor(QueryResult(columns=[], rows=[])),
	)

	payload = json.loads((await assistant.execute(analysis_query='Analyze', data_dir=data_dir.resolve())).output)

	assert payload['status'] == 'invalid_manifest'
	assert 'checksum' in payload['error']
	assert list((data_dir / 'analysis').glob('*.json'))


@pytest.mark.asyncio
async def test_analysis_rejects_manifest_changed_after_find_registration(tmp_path: Path):
	data_dir = _write_artifact(tmp_path)
	manifest_path = data_dir / 'manifest.json'
	trusted_hash = _sha(manifest_path)
	manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
	manifest['warnings'] = ['tampered after find']
	manifest_path.write_text(json.dumps(manifest), encoding='utf-8')
	assistant = DataAnalysisAssistant(
		FailingIfCalledLLM(),
		task_dir=tmp_path,
		task_identity=TASK_IDENTITY,
		code_backend=FakeCodeBackend([]),
		query_executor=FailingIfCalledExecutor(QueryResult(columns=[], rows=[])),
		trusted_manifest_hashes={str(data_dir.resolve()): trusted_hash},
	)

	payload = json.loads((await assistant.execute(analysis_query='Analyze', data_dir=data_dir.resolve())).output)

	assert payload['status'] == 'invalid_manifest'
	assert 'find_chart_data_requests' in payload['error']


@pytest.mark.asyncio
async def test_analysis_rejects_symlinked_manifest_file(tmp_path: Path):
	data_dir = _write_artifact(tmp_path)
	manifest_path = data_dir / 'manifest.json'
	manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
	real_csv = data_dir / manifest['datasets'][0]['tables'][0]['csv_path']
	linked_csv = real_csv.parent / 'linked.csv'
	linked_csv.symlink_to(real_csv)
	manifest['datasets'][0]['tables'][0]['csv_path'] = linked_csv.relative_to(data_dir).as_posix()
	manifest_path.write_text(json.dumps(manifest), encoding='utf-8')
	assistant = DataAnalysisAssistant(
		FailingIfCalledLLM(),
		task_dir=tmp_path,
		task_identity=TASK_IDENTITY,
		code_backend=FakeCodeBackend([]),
		query_executor=FailingIfCalledExecutor(QueryResult(columns=[], rows=[])),
	)

	payload = json.loads((await assistant.execute(analysis_query='Analyze', data_dir=data_dir.resolve())).output)

	assert payload['status'] == 'invalid_manifest'
	assert 'symbolic link' in payload['error']


@pytest.mark.asyncio
async def test_analysis_rejects_cross_task_manifest(tmp_path: Path):
	data_dir = _write_artifact(tmp_path, identity={**TASK_IDENTITY, 'task_id': 'different-task'})
	assistant = DataAnalysisAssistant(
		FailingIfCalledLLM(),
		task_dir=tmp_path,
		task_identity=TASK_IDENTITY,
		code_backend=FakeCodeBackend([]),
		query_executor=FailingIfCalledExecutor(QueryResult(columns=[], rows=[])),
	)

	payload = json.loads((await assistant.execute(analysis_query='Analyze', data_dir=data_dir.resolve())).output)

	assert payload['status'] == 'invalid_manifest'
	assert 'different task' in payload['error']
