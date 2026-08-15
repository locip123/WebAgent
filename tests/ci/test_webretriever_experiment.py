from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from browser_use.webretriever.connection import BrowserDriver
from browser_use.webretriever.experiment import (
	PATCHRIGHT_EXPERIMENT_ENDPOINT_LABELS,
	PATCHRIGHT_EXPERIMENT_TASK_INDICES,
	REBROWSER_EXPERIMENT_ENDPOINT_LABELS,
	REBROWSER_EXPERIMENT_TASK_INDICES,
	ExperimentRecord,
	patchright_qualification_report_passes,
	rebrowser_qualification_report_passes,
	summarize_experiment,
	write_experiment_summary,
)
from browser_use.webretriever.runner import RunnerConfig


def _record(
	*,
	driver: BrowserDriver,
	endpoint: str,
	episodes: int,
	status: str = 'SUCCESS',
	repeat: int = 0,
) -> ExperimentRecord:
	return ExperimentRecord(
		driver=driver,
		endpoint_label=endpoint,
		task_idx=57,
		task_id=f'loc-{endpoint}-{repeat}',
		repeat_index=repeat,
		challenge_episodes=episodes,
		status=status,
		agent_answer='42' if status == 'SUCCESS' else '',
	)


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


def _qualified_rebrowser_records() -> list[ExperimentRecord]:
	records: list[ExperimentRecord] = []
	for endpoint in REBROWSER_EXPERIMENT_ENDPOINT_LABELS:
		for task_idx in REBROWSER_EXPERIMENT_TASK_INDICES:
			records.append(
				ExperimentRecord(BrowserDriver.PLAYWRIGHT, endpoint, task_idx, f'task-{task_idx}', 0, 1, 'SUCCESS', '42')
			)
			records.append(
				ExperimentRecord(
					BrowserDriver.REBROWSER,
					endpoint,
					task_idx,
					f'task-{task_idx}',
					0,
					0,
					'SUCCESS',
					'42',
					runtime_fix_mode='addBinding',
				)
			)
	return records


def test_summary_qualifies_patchright_only_when_every_endpoint_is_non_regressing() -> None:
	records = [
		_record(driver=BrowserDriver.PLAYWRIGHT, endpoint='cdp-0', episodes=2),
		_record(driver=BrowserDriver.PATCHRIGHT, endpoint='cdp-0', episodes=0),
		_record(driver=BrowserDriver.PLAYWRIGHT, endpoint='cdp-1', episodes=1),
		_record(driver=BrowserDriver.PATCHRIGHT, endpoint='cdp-1', episodes=0),
		_record(driver=BrowserDriver.PLAYWRIGHT, endpoint='cdp-2', episodes=3),
		_record(driver=BrowserDriver.PATCHRIGHT, endpoint='cdp-2', episodes=1),
	]

	summary = summarize_experiment(records)

	assert summary.qualifies_patchright is True
	assert summary.challenge_episodes == {'playwright': 6, 'patchright': 1}
	assert summary.successes == {'playwright': 3, 'patchright': 3}


def test_summary_rejects_a_patchright_regression_on_one_endpoint() -> None:
	records = [
		_record(driver=BrowserDriver.PLAYWRIGHT, endpoint='cdp-0', episodes=1),
		_record(driver=BrowserDriver.PATCHRIGHT, endpoint='cdp-0', episodes=0),
		_record(driver=BrowserDriver.PLAYWRIGHT, endpoint='cdp-1', episodes=0),
		_record(driver=BrowserDriver.PATCHRIGHT, endpoint='cdp-1', episodes=1),
	]

	summary = summarize_experiment(records)

	assert summary.qualifies_patchright is False
	assert any('cdp-1' in reason for reason in summary.reasons)


def test_summary_rejects_missing_driver_pair_data() -> None:
	summary = summarize_experiment([_record(driver=BrowserDriver.PLAYWRIGHT, endpoint='cdp-0', episodes=1)])

	assert summary.qualifies_patchright is False
	assert summary.complete is False
	assert summary.reasons == ['missing Patchright records for endpoint cdp-0']


def test_summary_rejects_patchright_rows_that_used_the_startup_fallback() -> None:
	summary = summarize_experiment(
		[
			_record(driver=BrowserDriver.PLAYWRIGHT, endpoint='cdp-0', episodes=1),
			ExperimentRecord(
				driver=BrowserDriver.PATCHRIGHT,
				endpoint_label='cdp-0',
				task_idx=57,
				task_id='loc-cdp-0',
				repeat_index=0,
				challenge_episodes=0,
				status='SUCCESS',
				agent_answer='42',
				fallback_reason='RuntimeError: CDP mismatch',
			),
		]
	)

	assert summary.qualifies_patchright is False
	assert summary.reasons == ['Patchright fell back to Playwright on endpoint cdp-0']


def test_persisted_qualification_report_must_contain_a_passing_gate(tmp_path) -> None:
	passing_records = _qualified_matrix_records()
	passing_path = tmp_path / 'passing.json'
	write_experiment_summary(passing_path, passing_records)

	assert patchright_qualification_report_passes(passing_path) is True
	assert patchright_qualification_report_passes(tmp_path / 'missing.json') is False

	incomplete_path = tmp_path / 'incomplete.json'
	write_experiment_summary(incomplete_path, passing_records[:2])
	assert patchright_qualification_report_passes(incomplete_path) is False


def test_persisted_qualification_report_rejects_missing_verification_evidence(tmp_path) -> None:
	records = _qualified_matrix_records()
	records[0] = replace(records[0], artifact_complete=False)
	report_path = tmp_path / 'missing-verification.json'
	write_experiment_summary(report_path, records)

	assert patchright_qualification_report_passes(report_path) is False


def test_rebrowser_summary_requires_the_agreed_single_endpoint_single_round_pair(tmp_path) -> None:
	records = _qualified_rebrowser_records()
	summary = summarize_experiment(records, candidate_driver=BrowserDriver.REBROWSER)

	assert summary.complete is True
	assert summary.qualifies_rebrowser is True
	assert summary.qualifies_patchright is False
	assert summary.challenge_episodes == {'playwright': 6, 'rebrowser': 0}

	report_path = tmp_path / 'rebrowser.json'
	write_experiment_summary(report_path, records, candidate_driver=BrowserDriver.REBROWSER)
	assert rebrowser_qualification_report_passes(report_path) is True

	incomplete_path = tmp_path / 'incomplete-rebrowser.json'
	write_experiment_summary(incomplete_path, records[:-1], candidate_driver=BrowserDriver.REBROWSER)
	assert rebrowser_qualification_report_passes(incomplete_path) is False


def test_formal_rebrowser_run_requires_a_passing_single_endpoint_report(tmp_path) -> None:
	config = RunnerConfig(
		input_path=Path('data/data/protocol3.json'),
		output_dir=tmp_path / 'output',
		model='gpt-4.1',
		api_key='test-key',
		api_base=None,
		cdp_urls=['ws://127.0.0.1:9222/devtools/browser/test'],
		browser_driver=BrowserDriver.REBROWSER,
	)
	with pytest.raises(ValueError, match='rebrowser-qualification-report'):
		config.validate()

	report_path = tmp_path / 'rebrowser.json'
	write_experiment_summary(report_path, _qualified_rebrowser_records(), candidate_driver=BrowserDriver.REBROWSER)
	config.rebrowser_qualification_report = report_path
	config.validate()
