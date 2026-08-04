from __future__ import annotations

from pathlib import Path

import pytest

from browser_use.webretriever.connection import BrowserDriver
from browser_use.webretriever.experiment import (
	PATCHRIGHT_EXPERIMENT_ENDPOINT_LABELS,
	PATCHRIGHT_EXPERIMENT_TASK_INDICES,
	ExperimentRecord,
	write_experiment_summary,
)
from browser_use.webretriever.runner import RunnerConfig, run_patchright_experiment


def _config(tmp_path: Path, **updates: object) -> RunnerConfig:
	values: dict[str, object] = {
		'input_path': tmp_path / 'tasks.json',
		'output_dir': tmp_path / 'output',
		'model': 'gpt-5.4',
		'api_key': 'test-key',
		'api_base': None,
		'cdp_urls': ['http://127.0.0.1:9222'],
	}
	values.update(updates)
	return RunnerConfig(**values)  # type: ignore[arg-type]


def _qualified_matrix_records() -> list[ExperimentRecord]:
	records: list[ExperimentRecord] = []
	for endpoint in PATCHRIGHT_EXPERIMENT_ENDPOINT_LABELS:
		for task_idx in PATCHRIGHT_EXPERIMENT_TASK_INDICES:
			for repeat in range(2):
				records.append(
					ExperimentRecord(BrowserDriver.PLAYWRIGHT, endpoint, task_idx, f'task-{task_idx}', repeat, 1, 'SUCCESS', '42')
				)
				records.append(
					ExperimentRecord(BrowserDriver.PATCHRIGHT, endpoint, task_idx, f'task-{task_idx}', repeat, 0, 'SUCCESS', '42')
				)
	return records


def test_patchright_formal_runner_requires_a_passing_qualification_report(tmp_path: Path) -> None:
	config = _config(tmp_path, browser_driver=BrowserDriver.PATCHRIGHT)

	with pytest.raises(ValueError, match='qualification-report'):
		config.validate()

	report_path = tmp_path / 'experiment_summary.json'
	write_experiment_summary(report_path, _qualified_matrix_records())
	_config(tmp_path, browser_driver=BrowserDriver.PATCHRIGHT, patchright_qualification_report=report_path).validate()


@pytest.mark.asyncio
async def test_patchright_experiment_rejects_an_incomplete_cdp_matrix(tmp_path: Path) -> None:
	with pytest.raises(ValueError, match='exactly three CDP URLs'):
		await run_patchright_experiment(_config(tmp_path))
