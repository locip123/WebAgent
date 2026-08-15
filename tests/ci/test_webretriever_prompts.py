from __future__ import annotations

import json

import pytest
import tiktoken

from browser_use.webretriever.browser import BrowserObservation, ElementRef
from browser_use.webretriever.models import CompetitionTask
from browser_use.webretriever.prompts import (
	PromptBudgetExceeded,
	PromptComposer,
	PromptTarget,
	StepContext,
	build_system_prompt,
	describe_system_prompt,
)
from browser_use.webretriever.strategy import StrategyCheckpoint, StrategyReviewRequest


def _task(
	*, task_idx: int = 1, website: str = 'https://example.com/start', task: str = 'Find the requested fact.'
) -> CompetitionTask:
	return CompetitionTask(
		task_idx=task_idx,
		task_id='c022cb291f864aa1a22138ec449bedf9' if task_idx == 36 else f'task-{task_idx}',
		website=website,
		task=task,
	)


def _observation(**updates: object) -> BrowserObservation:
	values: dict[str, object] = {
		'screenshot': b'png',
		'url': 'https://example.com/results?page=2',
		'title': 'Results',
		'tabs': [{'index': 0, 'title': 'Results', 'url': 'https://example.com/results?page=2', 'active': True}],
		'viewport_width': 1440,
		'viewport_height': 900,
		'elements': [ElementRef(index=7, tag='button', name='Next page')],
		'page_text': 'Verified result: 42',
		'recent_network': [{'method': 'GET', 'url': 'https://example.com/api/results', 'status': 200}],
		'downloads': [{'filename': 'results.csv', 'text': 'row,value\\nanswer,42'}],
	}
	values.update(updates)
	return BrowserObservation(**values)  # type: ignore[arg-type]


def _compose(task: CompetitionTask, observation: BrowserObservation, *, last_outcome: str = 'Loaded results.'):
	composer = PromptComposer(task, PromptTarget(model_id='gpt-5.4'), max_steps=100, thought_language='简体中文')
	document = composer.compose_step(
		StepContext(
			step_index=2,
			observation=observation,
			history=(
				{
					'step': 0,
					'url': task.website,
					'thought': 'must disappear',
					'action': {'action': 'click'},
					'outcome': 'Opened results.',
				},
				{
					'step': 1,
					'url': observation.url,
					'thought': 'must disappear too',
					'action': {'action': 'wait'},
					'outcome': last_outcome,
				},
			),
			memory='Constraints: exact year.\\nVerified: result page is open.\\nNext: verify the value.',
			last_outcome=last_outcome,
		)
	)
	return composer, document


def test_system_is_deterministic_compact_and_has_no_conditional_bls_content() -> None:
	first = PromptComposer(_task(), PromptTarget(model_id='gpt-5.4'), max_steps=100, thought_language='简体中文')
	second = PromptComposer(_task(task_idx=2), PromptTarget(model_id='gpt-5.4'), max_steps=100, thought_language='简体中文')
	encoding = tiktoken.get_encoding('o200k_base')

	assert first.system.text == second.system.text
	assert len(encoding.encode(first.system.text)) <= 2_000
	assert first.system.metrics['estimated_tokens'] == len(encoding.encode(first.system.text))
	assert 'CES5000000001' not in first.system.text
	assert 'https://api.bls.gov/publicAPI' not in build_system_prompt()
	assert '2895' not in first.system.text
	assert 'evidence;request_id' not in first.system.text
	assert 'Never estimate unlabelled numeric chart values from geometry.' in first.system.text
	assert 'You may directly read visibly labelled values, table text, tooltips' in first.system.text


def test_legacy_system_description_accepts_headings_with_commas() -> None:
	sections = describe_system_prompt('ROLE, SCOPE\nFirst body.\n\nNEXT ACTION\nSecond body.')

	assert [section['title'] for section in sections] == ['ROLE, SCOPE', 'NEXT ACTION']


def test_step_prompt_renders_runtime_artifact_and_download_recovery_notices() -> None:
	task = _task()
	composer = PromptComposer(task, PromptTarget(model_id='gpt-5.4'), max_steps=100, thought_language='简体中文')
	document = composer.compose_step(
		StepContext(
			step_index=0,
			observation=_observation(),
			history=(),
			memory='',
			last_outcome='',
			data_artifact_notice='A ready data artifact is available at /task/data_artifacts/download-1.',
			download_recovery_notice='A timed-out download did not end the task; choose another first-party route.',
		)
	)

	assert '===== ONE-TIME DATA ARTIFACT NOTICE =====' in document.text
	assert 'call_data_analysis_assistant' not in document.text.split('===== ONE-TIME DATA ARTIFACT NOTICE =====')[1].split(
		'===== END ONE-TIME DATA ARTIFACT NOTICE ====='
	)[0]
	assert '===== DOWNLOAD RECOVERY NOTICE =====' in document.text
	assert 'did not end the task' in document.text
	execution = next(section for section in document.sections if section['id'] == 'execution_state')
	assert execution['fields']['data_artifact_notice'].startswith('A ready data artifact')
	assert execution['fields']['download_recovery_notice'].startswith('A timed-out download')


def test_step_prompt_preserves_download_metadata_and_head_tail_content_preview() -> None:
	observation = _observation(
		downloads=[
			{
				'filename': 'bid-summary.pdf',
				'url': 'https://example.com/bid-summary.pdf',
				'source': 'browser_download',
				'status': 'ready',
				'size_bytes': 4096,
				'mime_type': 'application/pdf',
				'text': 'PROMPT-HEAD ' + ('middle ' * 500) + ' PROMPT-TAIL',
			}
		]
	)
	_, document = _compose(_task(task='Read the PDF download.'), observation)

	assert 'bid-summary.pdf' in document.text
	assert 'https://example.com/bid-summary.pdf' in document.text
	assert 'browser_download' in document.text
	assert 'application/pdf' in document.text
	assert 'PROMPT-HEAD' in document.text
	assert 'PROMPT-TAIL' in document.text
	assert 'content_preview_truncated' in document.text
	assert 'content_characters' in document.text
	assert '"text"' not in document.text
	assert document.metrics['sources']['observation_downloads']['retained_tokens'] <= 8_000


def test_step_prompt_compresses_download_previews_without_dropping_file_metadata() -> None:
	downloads = [
		{
			'filename': f'official-{index}.csv',
			'url': f'https://example.com/official-{index}.csv',
			'source': 'browser_download',
			'status': 'ready',
			'size_bytes': index + 100,
			'text': f'HEAD-{index} ' + ('middle ' * 2_000) + f' TAIL-{index}',
		}
		for index in range(100)
	]
	observation = _observation(page_text='page evidence ' * 30_000, downloads=downloads)
	_, document = _compose(_task(task='Read the downloaded files.'), observation)

	for index in range(100):
		assert f'official-{index}.csv' in document.text
		assert f'https://example.com/official-{index}.csv' in document.text
	assert document.metrics['sources']['observation_downloads']['retained_tokens'] <= 8_000
	assert 'content_preview_head' in document.text


@pytest.mark.parametrize(
	'task_text,observation_updates,expected,absent',
	[
		('Read the annual PDF report.', {}, 'document', ('chart', 'derived', 'bls_access')),
		('Read the exact value from this chart.', {'downloads': []}, 'chart', ('document', 'derived', 'bls_access')),
		('Which category had the fastest growth rate?', {'downloads': []}, 'derived', ('document', 'chart', 'bls_access')),
	],
)
def test_playbooks_are_selected_without_leaking_unrelated_guidance(
	task_text: str,
	observation_updates: dict[str, object],
	expected: str,
	absent: tuple[str, ...],
) -> None:
	task = _task(task=task_text)
	_, document = _compose(task, _observation(**observation_updates))

	assert expected in document.metrics['selected_playbooks']
	assert all(playbook not in document.metrics['selected_playbooks'] for playbook in absent)
	assert '===== TRUSTED OPERATIONAL GUIDANCE =====' in document.text


@pytest.mark.parametrize(
	('status', 'required_guidance'),
	[
		(
			'no_match',
			(
				'no target chart packet was found',
				'Directly read the current chart',
				'observed first-party table, export, or download',
			),
		),
		(
			'saved_raw_only',
			(
				'no normalized data is available',
				'Do not decode raw packets',
				'Do not decode raw packets or call call_data_analysis_assistant',
				'Directly read the current chart',
			),
		),
	],
)
def test_unavailable_chart_data_guidance_uses_visual_or_first_party_export_fallback(
	status: str, required_guidance: tuple[str, ...]
) -> None:
	_, document = _compose(
		_task(task='Read the requested value from the current chart.'),
		_observation(downloads=[]),
		last_outcome=json.dumps({'action': 'find_chart_data_requests', 'status': status}),
	)

	assert 'chart' in document.metrics['selected_playbooks']
	for expected in required_guidance:
		assert expected in document.text
	assert 'Do not repeat the unchanged scan' in document.text


def test_bls_access_guidance_is_conditional_and_never_contains_reference_answer() -> None:
	public_bls_task = _task(
		task_idx=36,
		website='https://data.bls.gov/',
		task='查询美国劳工统计局(BLS)数据，2024年12月Information行业就业人数（季节性调整，千人）。',
	)
	blocked = _observation(
		url='https://data.bls.gov/',
		title='Access Denied',
		page_text='403 Access Denied: bot activity prohibited',
		downloads=[],
		recent_network=[],
	)
	_, document = _compose(public_bls_task, blocked, last_outcome='ERROR: 403 Access Denied')

	assert 'bls_access' in document.metrics['selected_playbooks']
	assert 'https://api.bls.gov/publicAPI/v2/timeseries/data/<SERIES_ID>' in document.text
	assert 'CES5000000001' in document.text
	assert '`S` means seasonally adjusted and `U` means not seasonally adjusted' in document.text
	assert 'Do not alter browser fingerprints, use a proxy, forge credentials' in document.text
	assert '2895' not in document.text

	_, ordinary = _compose(_task(), _observation(page_text='403 Access Denied', downloads=[]), last_outcome='403')
	assert 'bls_access' not in ordinary.metrics['selected_playbooks']
	assert 'CES5000000001' not in ordinary.text

	_, unblocked = _compose(
		public_bls_task,
		_observation(url='https://data.bls.gov/', page_text='Official BLS results are available.', downloads=[]),
		last_outcome='Loaded the official result page.',
	)
	assert 'bls_access' not in unblocked.metrics['selected_playbooks']
	assert 'CES5000000001' not in unblocked.text

	wrong_host = CompetitionTask(
		task_idx=public_bls_task.task_idx,
		task_id=public_bls_task.task_id,
		website='https://bls.gov.evil.example/',
		task=public_bls_task.task,
	)
	_, wrong_host_document = _compose(wrong_host, blocked, last_outcome='429 bot block')
	assert 'bls_access' not in wrong_host_document.metrics['selected_playbooks']
	assert 'CES5000000001' not in wrong_host_document.text

	lookalike = CompetitionTask(
		task_idx=36,
		task_id='not-the-public-task-id',
		website='https://data.bls.gov/',
		task=public_bls_task.task,
	)
	_, lookalike_document = _compose(lookalike, blocked, last_outcome='ERROR: 403 Access Denied')
	assert 'bls_access' in lookalike_document.metrics['selected_playbooks']
	assert 'CES5000000001' not in lookalike_document.text

	semantic_mismatch = CompetitionTask(
		task_idx=public_bls_task.task_idx,
		task_id=public_bls_task.task_id,
		website=public_bls_task.website,
		task='Read the current CPI headline from BLS.',
	)
	_, semantic_mismatch_document = _compose(semantic_mismatch, blocked, last_outcome='429 bot block')
	assert 'bls_access' in semantic_mismatch_document.metrics['selected_playbooks']
	assert 'CES5000000001' not in semantic_mismatch_document.text
	assert '2895' not in semantic_mismatch_document.text


def test_step_prompt_is_token_bounded_structured_and_deduplicates_latest_outcome() -> None:
	huge = 'PAGE-EVIDENCE ' * 30_000
	observation = _observation(
		page_text=huge,
		elements=[ElementRef(index=index, tag='button', name=f'Element {index} ' + 'x' * 500) for index in range(500)],
		recent_network=[{'method': 'GET', 'url': f'https://example.com/api/{index}', 'status': 200} for index in range(500)],
		downloads=[{'filename': f'data-{index}.csv', 'text': 'D' * 2_000} for index in range(100)],
	)
	composer = PromptComposer(_task(), PromptTarget(model_id='gpt-5.4'), max_steps=100, thought_language='简体中文')
	last_outcome = 'UNIQUE-LATEST-OUTCOME'
	document = composer.compose_step(
		StepContext(
			step_index=9,
			observation=observation,
			history=tuple(
				{
					'step': index,
					'url': f'https://example.com/{index}',
					'thought': 'private chain of thought ' * 100,
					'action': {'action': 'click', 'element_id': index},
					'outcome': last_outcome if index == 9 else 'O' * 10_000,
				}
				for index in range(10)
			),
			memory='Constraints: keep exact filters.\\n' + 'M' * 20_000,
			last_outcome=last_outcome,
		)
	)
	encoding = tiktoken.get_encoding('o200k_base')

	assert len(encoding.encode(document.text)) <= 20_000
	assert document.metrics['estimated_tokens'] == len(encoding.encode(document.text))
	assert document.metrics['characters'] == len(document.text)
	assert document.text.count(last_outcome) == 1
	assert 'private chain of thought' not in document.text
	assert _task().website in document.text
	assert '===== BEGIN UNTRUSTED BROWSER OBSERVATION =====' in document.text
	assert '===== END UNTRUSTED BROWSER OBSERVATION =====' in document.text
	assert 'https://example.com/results?page=2' in document.text
	assert '[7]' not in document.text or 'Interactive elements' in document.text
	assert 'Recent XHR/Fetch' in document.text
	assert 'Downloads' in document.text
	assert document.metrics['truncations']

	execution = next(section for section in document.sections if section['id'] == 'execution_state')
	history = execution['fields']['recent_trajectory']
	assert isinstance(history, list) and len(history) == 10
	assert all('thought' not in item for item in history)
	json.loads(json.dumps(history, ensure_ascii=False))


def test_mandatory_prompt_content_over_budget_raises_instead_of_silent_truncation() -> None:
	composer = PromptComposer(
		_task(task='required-task-token ' * 500),
		PromptTarget(model_id='gpt-5.4', step_text_token_budget=100),
		max_steps=100,
		thought_language='简体中文',
	)

	with pytest.raises(PromptBudgetExceeded):
		composer.compose_step(StepContext(step_index=0, observation=_observation(), history=(), memory='', last_outcome='start'))


def test_small_viable_budget_keeps_history_as_json_while_reducing_optional_context() -> None:
	composer = PromptComposer(
		_task(),
		PromptTarget(model_id='gpt-5.4', step_text_token_budget=500),
		max_steps=100,
		thought_language='简体中文',
	)
	document = composer.compose_step(
		StepContext(
			step_index=4,
			observation=_observation(page_text='page evidence ' * 20_000),
			history=tuple(
				{
					'step': index,
					'url': 'https://example.com/' + 'u' * 1_000,
					'action': {'action': 'click'},
					'outcome': 'o' * 2_000,
				}
				for index in range(5)
			),
			memory='m' * 20_000,
			last_outcome='z' * 20_000,
		)
	)
	execution = next(section for section in document.sections if section['id'] == 'execution_state')

	assert document.metrics['estimated_tokens'] <= 500
	assert isinstance(execution['fields']['recent_trajectory'], list)
	json.loads(json.dumps(execution['fields']['recent_trajectory']))


def test_browser_text_that_looks_like_a_tokenizer_special_token_is_treated_as_untrusted_text() -> None:
	composer = PromptComposer(
		_task(task='Find the literal marker <|endoftext|> on the page.'),
		PromptTarget(model_id='gpt-5.4', step_text_token_budget=1_000),
		max_steps=100,
		thought_language='简体中文',
	)
	document = composer.compose_step(
		StepContext(
			step_index=0,
			observation=_observation(page_text='<|endoftext|> ' * 10_000),
			history=(),
			memory='',
			last_outcome='start',
		)
	)

	assert document.metrics['estimated_tokens'] <= 1_000
	assert '<|endoftext|>' in document.text


def test_step_prompt_carries_remaining_time_and_a_loop_visible_history_window() -> None:
	composer = PromptComposer(_task(), PromptTarget(model_id='gpt-5.4'), max_steps=100, thought_language='简体中文')
	document = composer.compose_step(
		StepContext(
			step_index=29,
			observation=_observation(),
			history=tuple(
				{
					'step': index,
					'url': 'https://example.com/report',
					'thought': 'private chain of thought',
					'action': {'action': 'scroll', 'direction': 'up' if index % 2 else 'down', 'pages': 1},
					'outcome': 'Scrolled.',
				}
				for index in range(30)
			),
			memory='Verified: report page is open.',
			last_outcome='Scrolled.',
			remaining_task_seconds=132.4,
		)
	)

	execution = next(section for section in document.sections if section['id'] == 'execution_state')
	history = execution['fields']['recent_trajectory']
	assert len(history) >= 12, 'the window must be wide enough for the model to see its own loop'
	assert 'Remaining task time' in document.text
	assert '132' in document.text
	assert execution['fields']['remaining_task_seconds'] == pytest.approx(132.4)
	assert 'private chain of thought' not in document.text


def test_step_prompt_omits_remaining_time_when_no_deadline_is_configured() -> None:
	_, document = _compose(_task(), _observation())

	execution = next(section for section in document.sections if section['id'] == 'execution_state')
	assert execution['fields']['remaining_task_seconds'] is None
	assert 'Remaining task time' not in document.text


def test_initial_page_strategy_review_has_an_empty_trajectory_and_requires_a_plan() -> None:
	composer = PromptComposer(_task(), PromptTarget(model_id='gpt-5.4'), max_steps=100, thought_language='简体中文')
	document = composer.compose_step(
		StepContext(
			step_index=0,
			observation=_observation(),
			history=(),
			memory='',
			last_outcome='The task has just started.',
			strategy_review=StrategyReviewRequest(
				completed_decisions=0,
				trajectory=(),
				trigger='initial_page',
			),
		)
	)

	assert '===== REQUIRED INITIAL-PAGE STRATEGY REVIEW =====' in document.text
	assert 'trajectory is intentionally empty' in document.text
	assert 'all four non-empty checkpoint_* fields' in document.text
	assert 'output each field only as a Markdown-style list' in document.text
	assert '- [tried] first distinct strategy and its browser basis' in document.text
	assert 'Never use inline numbering or combine items with semicolons.' in document.text
	execution = next(section for section in document.sections if section['id'] == 'execution_state')
	assert execution['fields']['required_strategy_review'] == {
		'completed_decisions': 0,
		'trigger': 'initial_page',
		'trajectory': [],
	}


def test_page_entry_checkpoint_uses_every_decision_since_the_prior_review_and_stays_visible_afterward() -> None:
	composer = PromptComposer(_task(), PromptTarget(model_id='gpt-5.4'), max_steps=100, thought_language='简体中文')
	checkpoint = StrategyCheckpoint(
		completed_decisions=15,
		strategy_catalog='- [tried] Site navigation.\n- [untried] official export, table view, and captured first-party chart data.',
		active_strategy='- Inspect the official table route for the requested filters.',
		confirmed_infeasible='- No route is confirmed infeasible yet.',
		next_strategies='- Prioritize the table.\n- Inspect the export.\n- Inspect chart traffic.',
	)
	review = StrategyReviewRequest(
		completed_decisions=15,
		trajectory=tuple(
			{
				'decision': index + 1,
				'step': index + 1,
				'url': f'https://example.com/page-{index + 1}',
				'title': f'Observed page {index + 1}',
				'page_observation': f'Browser evidence {index + 1}',
				'action': {'action': 'navigate', 'url': f'https://example.com/page-{index + 1}'},
				'outcome': f'Completed action {index + 1}',
			}
			for index in range(15)
		),
		trigger='page_entry',
	)

	checkpoint_document = composer.compose_step(
		StepContext(
			step_index=15,
			observation=_observation(),
			history=(),
			memory='Verified: current source is official.',
			last_outcome='Completed decision 15.',
			strategy_checkpoint=checkpoint,
			strategy_review=review,
		)
	)

	assert '===== REQUIRED PAGE-ENTRY STRATEGY REVIEW =====' in checkpoint_document.text
	assert 'different valid page' in checkpoint_document.text
	assert 'checkpoint_strategy_catalog' in checkpoint_document.text
	assert 'Browser evidence 1' in checkpoint_document.text
	assert 'Browser evidence 15' in checkpoint_document.text
	assert 'No strategy review is due' not in checkpoint_document.text
	execution = next(section for section in checkpoint_document.sections if section['id'] == 'execution_state')
	assert execution['fields']['exploration_checkpoint']['covered_through_decision'] == 15
	assert execution['fields']['durable_memory'] == 'Verified: current source is official.'
	assert execution['fields']['required_strategy_review']['trigger'] == 'page_entry'
	assert len(execution['fields']['required_strategy_review']['trajectory']) == 15

	afterward_document = composer.compose_step(
		StepContext(
			step_index=16,
			observation=_observation(),
			history=(),
			memory='Verified: current source is official.',
			last_outcome='Completed decision 16.',
			strategy_checkpoint=checkpoint,
		)
	)

	assert '===== PERSISTENT EXPLORATION CHECKPOINT' in afterward_document.text
	assert 'official export, table view' in afterward_document.text
	assert 'No strategy review is due. Return null for every checkpoint_* field.' in afterward_document.text


def test_persistent_checkpoint_renders_every_section_as_an_indented_bullet_list() -> None:
	composer = PromptComposer(_task(), PromptTarget(model_id='gpt-5.4'), max_steps=100, thought_language='简体中文')
	checkpoint = StrategyCheckpoint(
		completed_decisions=3,
		strategy_catalog=(
			'- [tried] Read the first-party results table.\n'
			'- [untried] Open the first-party export.\n'
			'- [low probability, untried] Inspect captured first-party chart traffic.'
		),
		active_strategy='- Read the first-party results table.',
		confirmed_infeasible='- None confirmed.',
		next_strategies='- Open the first-party export next.\n- Inspect captured first-party chart traffic.',
	)

	document = composer.compose_step(
		StepContext(
			step_index=3,
			observation=_observation(),
			history=(),
			memory='Verified: current source is official.',
			last_outcome='Completed decision 3.',
			strategy_checkpoint=checkpoint,
		)
	)

	assert '''1. All viable strategy classes (tried and untried):
   - [tried] Read the first-party results table.
   - [untried] Open the first-party export.
   - [low probability, untried] Inspect captured first-party chart traffic.

2. Strategy currently being tried:
   - Read the first-party results table.

3. Confirmed infeasible strategy classes:
   - None confirmed.

4. Remaining worthwhile strategy classes:
   - Open the first-party export next.
   - Inspect captured first-party chart traffic.''' in document.text


def test_strategy_checkpoint_compacts_but_never_drops_since_review_decisions() -> None:
	composer = PromptComposer(_task(), PromptTarget(model_id='gpt-5.4'), max_steps=100, thought_language='简体中文')
	checkpoint = StrategyCheckpoint(
		completed_decisions=15,
		strategy_catalog='- ' + 'catalog ' * 100,
		active_strategy='- ' + 'active ' * 25,
		confirmed_infeasible='- ' + 'blocked ' * 50,
		next_strategies='- ' + 'next ' * 45,
	)
	review = StrategyReviewRequest(
		completed_decisions=15,
		trajectory=tuple(
			{
				'decision': index + 1,
				'step': index + 1,
				'url': f'https://example.com/very-long-route-{index}/' + 'u' * 5_000,
				'title': 'title ' * 2_000,
				'page_observation': 'browser observation ' * 5_000,
				'action': {'action': 'navigate', 'url': f'https://example.com/{index}/' + 'a' * 5_000},
				'outcome': 'action outcome ' * 5_000,
			}
			for index in range(15)
		),
		trigger='page_entry',
	)
	document = composer.compose_step(
		StepContext(
			step_index=15,
			observation=_observation(page_text='current evidence ' * 20_000),
			history=(),
			memory='Verified: current source is official.',
			last_outcome='Completed decision 15.',
			strategy_checkpoint=checkpoint,
			strategy_review=review,
		)
	)

	execution = next(section for section in document.sections if section['id'] == 'execution_state')
	assert document.metrics['estimated_tokens'] <= 20_000
	assert len(execution['fields']['required_strategy_review']['trajectory']) == 15
	assert document.metrics['sources']['checkpoint_trajectory']['reason'] == 'checkpoint_trajectory_compacted'


def test_strategy_checkpoint_is_never_silently_dropped_under_a_low_prompt_budget() -> None:
	"""A checkpoint review either retains prior strategy state or fails explicitly."""

	composer = PromptComposer(
		_task(),
		PromptTarget(model_id='gpt-5.4', step_text_token_budget=1_500),
		max_steps=100,
		thought_language='简体中文',
	)
	checkpoint = StrategyCheckpoint(
		completed_decisions=15,
		strategy_catalog='- ' + 'catalog ' * 100,
		active_strategy='- ' + 'active ' * 25,
		confirmed_infeasible='- ' + 'blocked ' * 50,
		next_strategies='- ' + 'next ' * 45,
	)
	review = StrategyReviewRequest(
		completed_decisions=15,
		trajectory=tuple(
			{
				'decision': index + 1,
				'step': index + 1,
				'url': f'https://example.com/{index}/' + 'u' * 5_000,
				'title': 'title ' * 2_000,
				'page_observation': 'browser observation ' * 5_000,
				'action': {'action': 'navigate', 'url': f'https://example.com/{index}/' + 'a' * 5_000},
				'outcome': 'action outcome ' * 5_000,
			}
			for index in range(15)
		),
		trigger='page_entry',
	)

	with pytest.raises(PromptBudgetExceeded) as error:
		composer.compose_step(
			StepContext(
				step_index=15,
				observation=_observation(page_text='current evidence ' * 20_000),
				history=(),
				memory='Verified: current source is official.' * 100,
				last_outcome='Completed decision 15.' * 100,
				strategy_checkpoint=checkpoint,
				strategy_review=review,
			)
		)

	assert 'exploration_checkpoint' in error.value.mandatory_sections


def test_strategy_checkpoint_never_silently_drops_the_existing_fact_ledger() -> None:
	"""A review cannot trade the fact ledger away merely to fit a small budget."""

	composer = PromptComposer(
		_task(),
		PromptTarget(model_id='gpt-5.4', step_text_token_budget=1_750),
		max_steps=100,
		thought_language='简体中文',
	)
	checkpoint = StrategyCheckpoint(
		completed_decisions=15,
		strategy_catalog='- Tried table route.',
		active_strategy='- Try export route.',
		confirmed_infeasible='- None confirmed.',
		next_strategies='- Export then chart traffic.',
	)
	review = StrategyReviewRequest(
		completed_decisions=15,
		trajectory=tuple(
			{
				'decision': index + 1,
				'step': index + 1,
				'url': f'https://example.com/{index}',
				'title': 'Results',
				'page_observation': 'Observed route.',
				'action': {'action': 'navigate', 'url': f'https://example.com/{index}'},
				'outcome': 'Completed.',
			}
			for index in range(15)
		),
		trigger='page_entry',
	)
	context = StepContext(
		step_index=15,
		observation=_observation(),
		history=(),
		memory='FACT-LEDGER ' * 100,
		last_outcome='Completed decision 15.',
		strategy_checkpoint=checkpoint,
		strategy_review=review,
	)

	with pytest.raises(PromptBudgetExceeded) as error:
		composer.compose_step(context)

	assert 'exploration_checkpoint' in error.value.mandatory_sections
