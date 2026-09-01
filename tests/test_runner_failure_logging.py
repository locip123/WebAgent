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


def test_diagnostic_failure_log_emits_bounded_one_line_details_and_redacts_cdp_token(
	caplog: pytest.LogCaptureFixture,
) -> None:
	logger = logging.getLogger('test.webretriever.failure-log')
	error = 'model response was invalid\nws://sandbox.example/?access_token=secret-token&worker=2\nretry exhausted'

	with caplog.at_level(logging.ERROR, logger=logger.name):
		_log_diagnostic_task_failure(logger, _task(), AgentRunOutcome(status='FAIL_MODEL', error=error))

	messages = [record.getMessage() for record in caplog.records]
	assert messages == [
		'Task 28/task-28 failed with status FAIL_MODEL; error: detail_chunks=1',
		'Task 28/task-28 error: detail[1/1]: '
		'model response was invalid ws://sandbox.example/?access_token=<redacted>&worker=2 retry exhausted',
	]
	assert all('\n' not in message for message in messages)
	assert all('secret-token' not in message for message in messages)


def test_diagnostic_failure_log_chunks_long_detail_into_export_safe_records(caplog: pytest.LogCaptureFixture) -> None:
	logger = logging.getLogger('test.webretriever.failure-log.chunks')
	error = 'x' * 321

	with caplog.at_level(logging.ERROR, logger=logger.name):
		_log_diagnostic_task_failure(logger, _task(), AgentRunOutcome(status='FAIL_MODEL', error=error))

	messages = [record.getMessage() for record in caplog.records]
	assert messages[0].endswith('error: detail_chunks=3')
	assert [message.rsplit(': ', maxsplit=1)[1] for message in messages[1:]] == ['x' * 160, 'x' * 160, 'x']
	assert all('error: detail[' in message for message in messages[1:])


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
		'browser_failure=task_page_unavailable/task_page_recovery_exhausted; error: detail_chunks=1'
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
