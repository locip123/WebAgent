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


def _task(*, task_idx: int = 1, website: str = 'https://example.com/start', task: str = 'Find the requested fact.') -> CompetitionTask:
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
				{'step': 0, 'url': task.website, 'thought': 'must disappear', 'action': {'action': 'click'}, 'outcome': 'Opened results.'},
				{'step': 1, 'url': observation.url, 'thought': 'must disappear too', 'action': {'action': 'wait'}, 'outcome': last_outcome},
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


def test_legacy_system_description_accepts_headings_with_commas() -> None:
	sections = describe_system_prompt('ROLE, SCOPE\nFirst body.\n\nNEXT ACTION\nSecond body.')

	assert [section['title'] for section in sections] == ['ROLE, SCOPE', 'NEXT ACTION']


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
	assert isinstance(history, list) and len(history) == 4
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
		composer.compose_step(
			StepContext(step_index=0, observation=_observation(), history=(), memory='', last_outcome='start')
		)


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
				{'step': index, 'url': 'https://example.com/' + 'u' * 1_000, 'action': {'action': 'click'}, 'outcome': 'o' * 2_000}
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
