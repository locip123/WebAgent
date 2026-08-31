from __future__ import annotations

import logging

import pytest

from browser_use.webretriever.agent import AgentRunOutcome
from browser_use.webretriever.models import CompetitionTask
from browser_use.webretriever.runner import _log_diagnostic_task_failure


def _task() -> CompetitionTask:
	return CompetitionTask(
		task_idx=28,
		task_id='task-28',
		website='https://example.com',
		task='Inspect the result',
	)


def test_diagnostic_failure_log_preserves_multiline_error_and_redacts_cdp_token(caplog: pytest.LogCaptureFixture) -> None:
	logger = logging.getLogger('test.webretriever.failure-log')
	error = 'model response was invalid\nws://sandbox.example/?access_token=secret-token&worker=2\nretry exhausted'

	with caplog.at_level(logging.ERROR, logger=logger.name):
		_log_diagnostic_task_failure(logger, _task(), AgentRunOutcome(status='FAIL_MODEL', error=error))

	assert len(caplog.records) == 1
	record = caplog.records[0]
	assert record.levelno == logging.ERROR
	assert record.getMessage() == (
		'Task 28/task-28 failed with status FAIL_MODEL; error:\n'
		'model response was invalid\n'
		'ws://sandbox.example/?access_token=<redacted>&worker=2\n'
		'retry exhausted'
	)
	assert 'secret-token' not in record.getMessage()


def test_diagnostic_failure_log_includes_browser_failure_subtype(caplog: pytest.LogCaptureFixture) -> None:
	logger = logging.getLogger('test.webretriever.failure-log.browser')
	outcome = AgentRunOutcome(
		status='FAIL_BROWSER_TASK_PAGE_UNAVAILABLE',
		error='Observation failed: RuntimeError: BrowserRuntime has no active task page',
		browser_failure={
			'category': 'task_page_unavailable',
			'subtype': 'task_page_recovery_exhausted',
			'phase': 'observation',
			'exception_type': 'RuntimeError',
			'recovery_attempted': True,
		},
	)

	with caplog.at_level(logging.ERROR, logger=logger.name):
		_log_diagnostic_task_failure(logger, _task(), outcome)

	assert caplog.records[0].getMessage().startswith(
		'Task 28/task-28 failed with status FAIL_BROWSER_TASK_PAGE_UNAVAILABLE; '
		'browser_failure=task_page_unavailable/task_page_recovery_exhausted; error:'
	)


@pytest.mark.parametrize(
	('status', 'error'),
	[
		('SUCCESS', 'should not be logged'),
		('FAIL_MODEL', None),
		('FAIL_MODEL', ''),
	],
)
def test_diagnostic_failure_log_skips_non_diagnostic_outcomes(
	caplog: pytest.LogCaptureFixture, status: str, error: str | None
) -> None:
	logger = logging.getLogger('test.webretriever.failure-log.skip')

	with caplog.at_level(logging.ERROR, logger=logger.name):
		_log_diagnostic_task_failure(logger, _task(), AgentRunOutcome(status=status, error=error))

	assert not caplog.records
