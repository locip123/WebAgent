import asyncio
import json
import logging

import pytest

from browser_use.webretriever.browser import BrowserObservation, BrowserRuntime
from browser_use.webretriever.models import (
	ACTION_PARAMETER_CONTRACTS,
	AgentDecision,
	render_action_parameter_contracts,
)
from browser_use.webretriever.prompts import build_system_prompt


def test_inspect_network_is_absent_from_public_action_schema_and_contract() -> None:
	assert 'inspect_network' not in ACTION_PARAMETER_CONTRACTS
	assert 'inspect_network' not in json.dumps(AgentDecision.model_json_schema())
	assert 'inspect_network' not in render_action_parameter_contracts()

	with pytest.raises(ValueError):
		AgentDecision(action='inspect_network')


def test_inspect_network_is_absent_from_the_model_system_prompt() -> None:
	assert 'inspect_network' not in build_system_prompt()


def test_direct_inspect_network_submission_is_rejected_before_runtime_start(tmp_path) -> None:
	runtime = BrowserRuntime(context=None, task_dir=tmp_path, logger=logging.getLogger('test'))

	with pytest.raises(ValueError, match="inspect_network.*disabled"):
		asyncio.run(runtime.execute({'action': 'inspect_network'}))


def test_recent_network_observation_remains_model_visible() -> None:
	observation = BrowserObservation(
		screenshot=b'',
		url='https://example.com',
		title='Example',
		tabs=[],
		viewport_width=1280,
		viewport_height=720,
		elements=[],
		page_text='Example',
		recent_network=[{'method': 'GET', 'url': 'https://example.com/data', 'status': 200}],
		downloads=[],
	)

	rendered = observation.render_text()

	assert 'Recent XHR/Fetch:' in rendered
	assert 'https://example.com/data' in rendered
