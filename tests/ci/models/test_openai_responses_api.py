"""Tests for OpenAI Responses API support in the generic OpenAI chat model."""

import json

from pydantic import BaseModel

from browser_use.llm.messages import UserMessage
from browser_use.llm.openai.chat import ChatOpenAI


class Answer(BaseModel):
	answer: str


def _responses_payload(text: str) -> dict[str, object]:
	return {
		'id': 'resp_test',
		'object': 'response',
		'created_at': 0,
		'model': 'gpt-5.4',
		'status': 'completed',
		'output': [
			{
				'id': 'msg_test',
				'type': 'message',
				'status': 'completed',
				'role': 'assistant',
				'content': [{'type': 'output_text', 'text': text, 'annotations': []}],
			}
		],
		'usage': {
			'input_tokens': 5,
			'input_tokens_details': {'cached_tokens': 0},
			'output_tokens': 3,
			'output_tokens_details': {'reasoning_tokens': 0},
			'total_tokens': 8,
		},
	}


def _completed_sse(text: str) -> str:
	payload = {
		'type': 'response.completed',
		'sequence_number': 1,
		'response': _responses_payload(text),
	}
	return f'event: response.completed\ndata: {json.dumps(payload)}\n\ndata: not-valid-json\n\n'


def _delta_only_completed_sse(text: str) -> str:
	delta = {
		'type': 'response.output_text.delta',
		'sequence_number': 0,
		'item_id': 'msg_test',
		'output_index': 0,
		'content_index': 0,
		'delta': text,
		'logprobs': [],
	}
	completed = {
		'type': 'response.completed',
		'sequence_number': 1,
		'response': {**_responses_payload(''), 'output': []},
	}
	return (
		f'event: response.output_text.delta\ndata: {json.dumps(delta)}\n\n'
		f'event: response.completed\ndata: {json.dumps(completed)}\n\n'
	)


async def test_openai_responses_api_uses_responses_endpoint(httpserver):
	httpserver.expect_request('/v1/responses', method='POST').respond_with_json(_responses_payload('READY'))

	llm = ChatOpenAI(
		model='gpt-5.4',
		api_key='test-key',
		base_url=httpserver.url_for('/v1'),
		use_responses_api=True,
	)

	result = await llm.ainvoke([UserMessage(content='Reply with exactly: READY')])

	assert result.completion == 'READY'
	assert result.stop_reason == 'completed'
	assert result.usage is not None
	assert result.usage.total_tokens == 8


async def test_openai_responses_api_parses_structured_output(httpserver):
	httpserver.expect_request('/v1/responses', method='POST').respond_with_json(_responses_payload('{"answer":"READY"}'))

	llm = ChatOpenAI(
		model='gpt-5.4',
		api_key='test-key',
		base_url=httpserver.url_for('/v1'),
		use_responses_api=True,
	)

	result = await llm.ainvoke([UserMessage(content='Return JSON with an answer field.')], output_format=Answer)

	assert result.completion == Answer(answer='READY')


async def test_openai_responses_api_parses_proxy_concatenated_structured_output(httpserver):
	httpserver.expect_request('/v1/responses', method='POST').respond_with_json(
		_responses_payload('{"answer":"READY"}{"answer":"READY"}')
	)

	llm = ChatOpenAI(
		model='gpt-5.4',
		api_key='test-key',
		base_url=httpserver.url_for('/v1'),
		use_responses_api=True,
	)

	result = await llm.ainvoke([UserMessage(content='Return JSON with an answer field.')], output_format=Answer)

	assert result.completion == Answer(answer='READY')


async def test_openai_responses_api_ignores_proxy_truncated_duplicate_suffix(httpserver):
	httpserver.expect_request('/v1/responses', method='POST').respond_with_json(
		_responses_payload('{"answer":"READY"}"answer":"READY"}')
	)

	llm = ChatOpenAI(
		model='gpt-5.4',
		api_key='test-key',
		base_url=httpserver.url_for('/v1'),
		use_responses_api=True,
	)

	result = await llm.ainvoke([UserMessage(content='Return JSON with an answer field.')], output_format=Answer)

	assert result.completion == Answer(answer='READY')


async def test_openai_responses_api_accepts_proxy_text_response(httpserver):
	"""Some OpenAI-compatible proxies return a JSON string instead of a Response object."""
	httpserver.expect_request('/v1/responses', method='POST').respond_with_json('READY')

	llm = ChatOpenAI(
		model='gpt-5.4',
		api_key='test-key',
		base_url=httpserver.url_for('/v1'),
		use_responses_api=True,
	)

	result = await llm.ainvoke([UserMessage(content='Reply with exactly: READY')])

	assert result.completion == 'READY'


async def test_openai_responses_api_extracts_text_from_proxy_sse_response(httpserver):
	"""Some proxies return Responses API server-sent events as a plain string."""
	httpserver.expect_request('/v1/responses', method='POST').respond_with_data(
		'data: {"type":"response.output_text.delta","delta":"READY"}\n\ndata: [DONE]\n',
		content_type='text/event-stream',
	)

	llm = ChatOpenAI(
		model='gpt-5.4',
		api_key='test-key',
		base_url=httpserver.url_for('/v1'),
		use_responses_api=True,
	)

	result = await llm.ainvoke([UserMessage(content='Reply with exactly: READY')])

	assert result.completion == 'READY'


async def test_openai_responses_api_streaming_stops_at_completed_event(httpserver):
	"""A gateway need not close the SSE connection after its terminal event."""
	httpserver.expect_request('/v1/responses', method='POST').respond_with_data(
		_completed_sse('READY'),
		content_type='text/event-stream',
	)

	llm = ChatOpenAI(
		model='gpt-5.4',
		api_key='test-key',
		base_url=httpserver.url_for('/v1'),
		use_responses_api=True,
		stream_responses_api=True,
	)

	result = await llm.ainvoke([UserMessage(content='Reply with exactly: READY')])

	assert result.completion == 'READY'
	assert result.stop_reason == 'completed'
	assert result.usage is not None and result.usage.total_tokens == 8


async def test_openai_responses_api_streaming_uses_deltas_when_terminal_output_is_empty(httpserver):
	httpserver.expect_request('/v1/responses', method='POST').respond_with_data(
		_delta_only_completed_sse('{"answer":"READY"}'),
		content_type='text/event-stream',
	)

	llm = ChatOpenAI(
		model='gpt-5.4',
		api_key='test-key',
		base_url=httpserver.url_for('/v1'),
		use_responses_api=True,
		stream_responses_api=True,
	)

	result = await llm.ainvoke([UserMessage(content='Return JSON.')], output_format=Answer)

	assert result.completion == Answer(answer='READY')
