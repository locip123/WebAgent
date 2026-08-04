"""Auditable result records and gate logic for Patchright CDP experiments."""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path

from browser_use.webretriever.artifacts import atomic_write_json
from browser_use.webretriever.connection import BrowserDriver

__all__ = [
	'PATCHRIGHT_EXPERIMENT_ENDPOINT_LABELS',
	'PATCHRIGHT_EXPERIMENT_TASK_INDICES',
	'ExperimentRecord',
	'ExperimentSummary',
	'patchright_qualification_report_passes',
	'summarize_experiment',
	'write_experiment_summary',
]

PATCHRIGHT_EXPERIMENT_ENDPOINT_LABELS = ('cdp-0', 'cdp-1', 'cdp-2')
PATCHRIGHT_EXPERIMENT_TASK_INDICES = frozenset({55, 57, 69, 76, 84, 96})


@dataclass(frozen=True, slots=True)
class ExperimentRecord:
	"""One task outcome from an isolated CDP experiment context."""

	driver: BrowserDriver
	endpoint_label: str
	task_idx: int
	task_id: str
	repeat_index: int
	challenge_episodes: int
	status: str
	agent_answer: str
	fallback_reason: str | None = None
	artifact_complete: bool = True

	@property
	def succeeded(self) -> bool:
		return self.status == 'SUCCESS' and bool(self.agent_answer.strip())

	def to_dict(self) -> dict[str, object]:
		payload = asdict(self)
		payload['driver'] = self.driver.value
		payload['succeeded'] = self.succeeded
		return payload


@dataclass(frozen=True, slots=True)
class ExperimentSummary:
	"""Decision-ready aggregate of all recorded experiment runs."""

	complete: bool
	qualifies_patchright: bool
	challenge_episodes: dict[str, int]
	successes: dict[str, int]
	endpoint_episodes: dict[str, dict[str, int]]
	reasons: list[str] = field(default_factory=list)

	def to_dict(self) -> dict[str, object]:
		return asdict(self)


def summarize_experiment(records: Iterable[ExperimentRecord]) -> ExperimentSummary:
	"""Apply the agreed non-regression and strict-improvement gate.

	Every endpoint needs records for both drivers.  Patchright may not have more
	visible verification episodes on any endpoint, must have fewer in aggregate,
	and may not produce fewer successful answers.
	"""
	by_endpoint: dict[str, dict[BrowserDriver, list[ExperimentRecord]]] = defaultdict(lambda: defaultdict(list))
	for record in records:
		by_endpoint[record.endpoint_label][record.driver].append(record)

	reasons: list[str] = []
	endpoint_episodes: dict[str, dict[str, int]] = {}
	challenge_episodes = {BrowserDriver.PLAYWRIGHT.value: 0, BrowserDriver.PATCHRIGHT.value: 0}
	successes = {BrowserDriver.PLAYWRIGHT.value: 0, BrowserDriver.PATCHRIGHT.value: 0}
	complete = bool(by_endpoint)
	if any(not record.artifact_complete for drivers in by_endpoint.values() for records_for_driver in drivers.values() for record in records_for_driver):
		reasons.append('one or more task artifacts lacked complete verification evidence')
		complete = False

	for endpoint_label in sorted(by_endpoint):
		per_driver = by_endpoint[endpoint_label]
		baseline = per_driver.get(BrowserDriver.PLAYWRIGHT, [])
		patchright = per_driver.get(BrowserDriver.PATCHRIGHT, [])
		if not baseline:
			reasons.append(f'missing Playwright records for endpoint {endpoint_label}')
			complete = False
		if not patchright:
			reasons.append(f'missing Patchright records for endpoint {endpoint_label}')
			complete = False

		baseline_episodes = sum(record.challenge_episodes for record in baseline)
		patchright_episodes = sum(record.challenge_episodes for record in patchright)
		endpoint_episodes[endpoint_label] = {
			BrowserDriver.PLAYWRIGHT.value: baseline_episodes,
			BrowserDriver.PATCHRIGHT.value: patchright_episodes,
		}
		challenge_episodes[BrowserDriver.PLAYWRIGHT.value] += baseline_episodes
		challenge_episodes[BrowserDriver.PATCHRIGHT.value] += patchright_episodes
		successes[BrowserDriver.PLAYWRIGHT.value] += sum(record.succeeded for record in baseline)
		successes[BrowserDriver.PATCHRIGHT.value] += sum(record.succeeded for record in patchright)
		if baseline and patchright and patchright_episodes > baseline_episodes:
			reasons.append(
				f'Patchright had more verification episodes on endpoint {endpoint_label}: '
				f'{patchright_episodes} > {baseline_episodes}'
			)
		if any(record.fallback_reason for record in patchright):
			reasons.append(f'Patchright fell back to Playwright on endpoint {endpoint_label}')

	if not by_endpoint:
		reasons.append('no experiment records were provided')
	if complete and challenge_episodes[BrowserDriver.PATCHRIGHT.value] >= challenge_episodes[BrowserDriver.PLAYWRIGHT.value]:
		reasons.append(
			'Patchright did not strictly reduce total verification episodes: '
			f'{challenge_episodes[BrowserDriver.PATCHRIGHT.value]} >= {challenge_episodes[BrowserDriver.PLAYWRIGHT.value]}'
		)
	if complete and successes[BrowserDriver.PATCHRIGHT.value] < successes[BrowserDriver.PLAYWRIGHT.value]:
		reasons.append(
			'Patchright produced fewer successful answers: '
			f'{successes[BrowserDriver.PATCHRIGHT.value]} < {successes[BrowserDriver.PLAYWRIGHT.value]}'
		)

	return ExperimentSummary(
		complete=complete,
		qualifies_patchright=complete and not reasons,
		challenge_episodes=challenge_episodes,
		successes=successes,
		endpoint_episodes=endpoint_episodes,
		reasons=reasons,
	)


def write_experiment_summary(path: Path, records: Iterable[ExperimentRecord]) -> ExperimentSummary:
	"""Write records and their gate decision without exposing CDP URLs."""

	recorded = list(records)
	summary = summarize_experiment(recorded)
	atomic_write_json(
		path,
		{
			'records': [record.to_dict() for record in recorded],
			'summary': summary.to_dict(),
		},
	)
	return summary


def patchright_qualification_report_passes(path: Path) -> bool:
	"""Whether a report independently proves the agreed full Patchright matrix."""

	try:
		with Path(path).open(encoding='utf-8') as report_file:
			payload = json.load(report_file)
	except (FileNotFoundError, OSError, json.JSONDecodeError):
		return False
	if not isinstance(payload, dict) or not isinstance(payload.get('records'), list):
		return False
	try:
		records = [_experiment_record_from_dict(record) for record in payload['records']]
	except (KeyError, TypeError, ValueError):
		return False
	if not _is_complete_patchright_matrix(records):
		return False
	return summarize_experiment(records).qualifies_patchright


def _experiment_record_from_dict(value: object) -> ExperimentRecord:
	if not isinstance(value, dict):
		raise TypeError('experiment record must be an object')
	driver = BrowserDriver(str(value['driver']))
	endpoint_label = value['endpoint_label']
	task_id = value['task_id']
	status = value['status']
	agent_answer = value['agent_answer']
	fallback_reason = value.get('fallback_reason')
	artifact_complete = value['artifact_complete']
	if not all(isinstance(item, str) for item in (endpoint_label, task_id, status, agent_answer)):
		raise TypeError('experiment record string fields must be strings')
	if fallback_reason is not None and not isinstance(fallback_reason, str):
		raise TypeError('fallback_reason must be a string or null')
	if artifact_complete is not True:
		raise ValueError('experiment record must have complete verification evidence')
	return ExperimentRecord(
		driver=driver,
		endpoint_label=endpoint_label,
		task_idx=int(value['task_idx']),
		task_id=task_id,
		repeat_index=int(value['repeat_index']),
		challenge_episodes=int(value['challenge_episodes']),
		status=status,
		agent_answer=agent_answer,
		fallback_reason=fallback_reason,
		artifact_complete=artifact_complete,
	)


def _is_complete_patchright_matrix(records: list[ExperimentRecord]) -> bool:
	expected = {
		(endpoint_label, task_idx, repeat_index, driver)
		for endpoint_label in PATCHRIGHT_EXPERIMENT_ENDPOINT_LABELS
		for task_idx in PATCHRIGHT_EXPERIMENT_TASK_INDICES
		for repeat_index in range(2)
		for driver in BrowserDriver
	}
	observed = {(record.endpoint_label, record.task_idx, record.repeat_index, record.driver) for record in records}
	return len(records) == len(expected) and observed == expected
