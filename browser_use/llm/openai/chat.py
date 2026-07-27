import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, TypeVar, overload

import httpx
from openai import APIConnectionError, APIStatusError, AsyncOpenAI, RateLimitError
from openai.types.chat import ChatCompletionContentPartTextParam
from openai.types.chat.chat_completion import ChatCompletion
from openai.types.responses import Response
from openai.types.shared.chat_model import ChatModel
from openai.types.shared_params.reasoning_effort import ReasoningEffort
from openai.types.shared_params.response_format_json_schema import JSONSchema, ResponseFormatJSONSchema
from pydantic import BaseModel, ValidationError

from browser_use.llm.base import BaseChatModel
from browser_use.llm.exceptions import ModelOutputTruncatedError, ModelProviderError, ModelRateLimitError
from browser_use.llm.messages import BaseMessage
from browser_use.llm.openai.responses_serializer import ResponsesAPIMessageSerializer
from browser_use.llm.openai.serializer import OpenAIMessageSerializer
from browser_use.llm.schema import SchemaOptimizer
from browser_use.llm.views import ChatInvokeCompletion, ChatInvokeUsage

T = TypeVar('T', bound=BaseModel)


@dataclass
class ChatOpenAI(BaseChatModel):
	"""
	A wrapper around AsyncOpenAI that implements the BaseLLM protocol.

	This class accepts all AsyncOpenAI parameters while adding model
	and temperature parameters for the LLM interface (if temperature it not `None`).
	"""

	# Model configuration
	model: ChatModel | str

	# Model params
	temperature: float | None = 0.2
	frequency_penalty: float | None = 0.3  # this avoids infinite generation of \t for models like 4.1-mini
	reasoning_effort: ReasoningEffort = 'low'
	seed: int | None = None
	service_tier: Literal['auto', 'default', 'flex', 'priority', 'scale'] | None = None
	top_p: float | None = None
	add_schema_to_system_prompt: bool = False  # Add JSON schema to system prompt instead of using response_format
	dont_force_structured_output: bool = False  # If True, the model will not be forced to output a structured output
	remove_min_items_from_schema: bool = (
		False  # If True, remove minItems from JSON schema (for compatibility with some providers)
	)
	remove_defaults_from_schema: bool = (
		False  # If True, remove default values from JSON schema (for compatibility with some providers)
	)
	use_responses_api: bool = False
	"""Use the OpenAI Responses API instead of Chat Completions."""
	stream_responses_api: bool = False
	"""Consume Responses API calls as SSE and stop as soon as a terminal event arrives."""

	# Client initialization parameters
	api_key: str | None = None
	organization: str | None = None
	project: str | None = None
	base_url: str | httpx.URL | None = None
	websocket_base_url: str | httpx.URL | None = None
	timeout: float | httpx.Timeout | None = None
	max_retries: int = 5  # Increase default retries for automation reliability
	default_headers: Mapping[str, str] | None = None
	default_query: Mapping[str, object] | None = None
	http_client: httpx.AsyncClient | None = None
	_strict_response_validation: bool = False
	max_completion_tokens: int | None = 4096
	reasoning_models: list[ChatModel | str] | None = field(
		default_factory=lambda: [
			'o4-mini',
			'o3',
			'o3-mini',
			'o1',
			'o1-pro',
			'o3-pro',
			'gpt-5',
			'gpt-5-mini',
			'gpt-5-nano',
		]
	)

	# Static
	@property
	def provider(self) -> str:
		return 'openai'

	def _get_client_params(self) -> dict[str, Any]:
		"""Prepare client parameters dictionary."""
		# Define base client params
		base_params = {
			'api_key': self.api_key,
			'organization': self.organization,
			'project': self.project,
			'base_url': self.base_url,
			'websocket_base_url': self.websocket_base_url,
			'timeout': self.timeout,
			'max_retries': self.max_retries,
			'default_headers': self.default_headers,
			'default_query': self.default_query,
			'_strict_response_validation': self._strict_response_validation,
		}

		# Create client_params dict with non-None values
		client_params = {k: v for k, v in base_params.items() if v is not None}

		# Add http_client if provided
		if self.http_client is not None:
			client_params['http_client'] = self.http_client

		return client_params

	def get_client(self) -> AsyncOpenAI:
		"""
		Returns an AsyncOpenAI client.

		Returns:
			AsyncOpenAI: An instance of the AsyncOpenAI client.
		"""
		client_params = self._get_client_params()
		return AsyncOpenAI(**client_params)

	@property
	def name(self) -> str:
		return str(self.model)

	def _get_usage(self, response: ChatCompletion) -> ChatInvokeUsage | None:
		if response.usage is not None:
			# Note: completion_tokens already includes reasoning_tokens per OpenAI API docs.
			# Unlike Google Gemini where thinking_tokens are reported separately,
			# OpenAI's reasoning_tokens are a subset of completion_tokens.
			usage = ChatInvokeUsage(
				prompt_tokens=response.usage.prompt_tokens,
				prompt_cached_tokens=response.usage.prompt_tokens_details.cached_tokens
				if response.usage.prompt_tokens_details is not None
				else None,
				prompt_cache_creation_tokens=None,
				prompt_image_tokens=None,
				# Completion
				completion_tokens=response.usage.completion_tokens,
				total_tokens=response.usage.total_tokens,
			)
		else:
			usage = None

		return usage

	def _get_responses_usage(self, response: Response | str) -> ChatInvokeUsage | None:
		"""Extract usage from a Responses API response, including proxy text responses."""
		if isinstance(response, str) or response.usage is None:
			return None

		cached_tokens = None
		if response.usage.input_tokens_details is not None:
			cached_tokens = response.usage.input_tokens_details.cached_tokens

		return ChatInvokeUsage(
			prompt_tokens=response.usage.input_tokens,
			prompt_cached_tokens=cached_tokens,
			prompt_cache_creation_tokens=None,
			prompt_image_tokens=None,
			completion_tokens=response.usage.output_tokens,
			total_tokens=response.usage.total_tokens,
		)

	@staticmethod
	def _get_responses_text(response: Response | str) -> str:
		"""Return generated text from the official response schema or a proxy text response."""
		if isinstance(response, str):
			return ChatOpenAI._extract_responses_sse_text(response)
		return response.output_text or ''

	@staticmethod
	def _extract_responses_sse_text(response_text: str) -> str:
		"""Extract output text when a proxy returns Responses API SSE events as a string."""
		if not response_text.lstrip().startswith('data:'):
			return response_text

		deltas: list[str] = []
		completed_text: str | None = None

		for line in response_text.splitlines():
			if not line.startswith('data:'):
				continue

			payload = line.removeprefix('data:').strip()
			if not payload or payload == '[DONE]':
				continue

			try:
				event = json.loads(payload)
			except json.JSONDecodeError:
				continue

			if event.get('type') == 'response.output_text.delta' and isinstance(event.get('delta'), str):
				deltas.append(event['delta'])
			elif event.get('type') == 'response.output_text.done' and isinstance(event.get('text'), str):
				completed_text = event['text']
			elif event.get('type') == 'response.completed':
				completed_text = ChatOpenAI._extract_completed_response_text(event) or completed_text

		return completed_text or ''.join(deltas) or response_text

	@staticmethod
	def _extract_completed_response_text(event: dict[str, Any]) -> str:
		"""Extract output text from a response.completed event payload."""
		response = event.get('response')
		if not isinstance(response, dict):
			return ''

		output = response.get('output')
		if not isinstance(output, list):
			return ''

		text_parts: list[str] = []
		for item in output:
			if not isinstance(item, dict) or item.get('type') != 'message':
				continue
			content = item.get('content')
			if not isinstance(content, list):
				continue
			for part in content:
				if isinstance(part, dict) and part.get('type') == 'output_text' and isinstance(part.get('text'), str):
					text_parts.append(part['text'])

		return ''.join(text_parts)

	@staticmethod
	def _get_responses_stop_reason(response: Response | str) -> str | None:
		"""Return the Responses API status when the provider returned a standard response object."""
		if isinstance(response, str):
			return None
		return response.status

	@staticmethod
	def _parse_responses_structured_text(response_text: str, output_format: type[T]) -> T:
		"""Validate structured text, tolerating proxy-appended duplicate fragments.

		Some OpenAI-compatible gateways append a repeated parameter object after
		the complete schema-constrained object, sometimes truncating that repeated
		fragment. Prefer strict validation, then accept only the first complete JSON
		value and validate it against the requested schema. The ignored suffix can
		never become a second browser action.
		"""
		try:
			return output_format.model_validate_json(response_text)
		except ValidationError as validation_error:
			decoder = json.JSONDecoder()
			try:
				first_value, end_position = decoder.raw_decode(response_text.lstrip())
			except json.JSONDecodeError:
				raise validation_error

			if not response_text.lstrip()[end_position:].strip():
				raise validation_error
			return output_format.model_validate(first_value)

	async def _create_streaming_response(self, model_params: dict[str, Any]) -> Response | str:
		"""Consume a Responses SSE stream without waiting for the server to close it.

		LiteLLM's ChatGPT-subscription adapter always talks SSE to its upstream,
		even when its caller requests a non-streaming response.  Asking the proxy
		for SSE as well avoids its non-streaming EOF aggregation path.  Explicitly
		break on the terminal event because a proxy/upstream may keep the HTTP
		connection open after ``response.completed``.
		"""
		client = self.get_client()
		stream = None
		text_deltas: list[str] = []
		completed_text: str | None = None
		try:
			stream = await client.responses.create(**model_params, stream=True)
			async for event in stream:
				event_type = getattr(event, 'type', '')
				if event_type == 'response.output_text.delta':
					delta = getattr(event, 'delta', None)
					if isinstance(delta, str):
						text_deltas.append(delta)
					continue
				if event_type == 'response.output_text.done':
					text = getattr(event, 'text', None)
					if isinstance(text, str):
						completed_text = text
					continue
				if event_type == 'response.completed':
					response = getattr(event, 'response', None)
					if response is None:
						raise ModelProviderError(message='Responses API completion omitted its response', model=self.name)
					return response if response.output_text else completed_text or ''.join(text_deltas)
				if event_type == 'response.incomplete':
					response = getattr(event, 'response', None)
					if response is None:
						raise ModelProviderError(message='Responses API incomplete event omitted its response', model=self.name)
					return response if response.output_text else completed_text or ''.join(text_deltas)
				if event_type == 'response.failed':
					response = getattr(event, 'response', None)
					error = getattr(response, 'error', None)
					message = getattr(error, 'message', None) or str(error or 'Responses API request failed')
					raise ModelProviderError(message=message, model=self.name)
				if event_type == 'error':
					raise ModelProviderError(
						message=getattr(event, 'message', None) or 'Responses API stream failed',
						model=self.name,
					)

			raise ModelProviderError(
				message='Responses API stream ended without a terminal response event',
				model=self.name,
			)
		finally:
			if stream is not None:
				await stream.close()
			await client.close()

	async def _ainvoke_responses_api(
		self, messages: list[BaseMessage], output_format: type[T] | None = None, **kwargs: Any
	) -> ChatInvokeCompletion[T] | ChatInvokeCompletion[str]:
		"""Invoke the model through the OpenAI Responses API."""
		input_messages = ResponsesAPIMessageSerializer.serialize_messages(messages)

		try:
			model_params: dict[str, Any] = {
				'model': self.model,
				'input': input_messages,
			}

			if self.temperature is not None:
				model_params['temperature'] = self.temperature

			if self.max_completion_tokens is not None:
				model_params['max_output_tokens'] = self.max_completion_tokens

			if self.top_p is not None:
				model_params['top_p'] = self.top_p

			if self.service_tier is not None:
				model_params['service_tier'] = self.service_tier

			if self.reasoning_models and any(str(m).lower() in str(self.model).lower() for m in self.reasoning_models):
				model_params['reasoning'] = {'effort': self.reasoning_effort}
				model_params.pop('temperature', None)

			if output_format is not None:
				json_schema = SchemaOptimizer.create_optimized_json_schema(
					output_format,
					remove_min_items=self.remove_min_items_from_schema,
					remove_defaults=self.remove_defaults_from_schema,
				)
				model_params['text'] = {
					'format': {
						'type': 'json_schema',
						'name': 'agent_output',
						'strict': True,
						'schema': json_schema,
					}
				}

				if self.add_schema_to_system_prompt and input_messages and input_messages[0]['role'] == 'system':
					schema_text = f'\n<json_schema>\n{json_schema}\n</json_schema>'
					content = input_messages[0]['content']
					if isinstance(content, str):
						input_messages[0]['content'] = content + schema_text
					else:
						input_messages[0]['content'] = list(content) + [{'type': 'input_text', 'text': schema_text}]

				if self.dont_force_structured_output:
					model_params.pop('text', None)

			if self.stream_responses_api:
				response = await self._create_streaming_response(model_params)
			else:
				response = await self.get_client().responses.create(**model_params)
			response_text = self._get_responses_text(response)
			usage = self._get_responses_usage(response)
			stop_reason = self._get_responses_stop_reason(response)

			if output_format is None:
				return ChatInvokeCompletion(completion=response_text, usage=usage, stop_reason=stop_reason)

			if not response_text:
				raise ModelProviderError(
					message='Failed to parse structured output from Responses API response',
					status_code=500,
					model=self.name,
				)

			return ChatInvokeCompletion(
				completion=self._parse_responses_structured_text(response_text, output_format),
				usage=usage,
				stop_reason=stop_reason,
			)

		except ModelProviderError:
			raise
		except RateLimitError as e:
			raise ModelRateLimitError(message=e.message, model=self.name) from e
		except APIConnectionError as e:
			raise ModelProviderError(message=str(e), model=self.name) from e
		except APIStatusError as e:
			raise ModelProviderError(message=e.message, status_code=e.status_code, model=self.name) from e
		except Exception as e:
			raise ModelProviderError(message=str(e), model=self.name) from e

	@overload
	async def ainvoke(
		self, messages: list[BaseMessage], output_format: None = None, **kwargs: Any
	) -> ChatInvokeCompletion[str]: ...

	@overload
	async def ainvoke(self, messages: list[BaseMessage], output_format: type[T], **kwargs: Any) -> ChatInvokeCompletion[T]: ...

	async def ainvoke(
		self, messages: list[BaseMessage], output_format: type[T] | None = None, **kwargs: Any
	) -> ChatInvokeCompletion[T] | ChatInvokeCompletion[str]:
		"""
		Invoke the model with the given messages.

		Args:
			messages: List of chat messages
			output_format: Optional Pydantic model class for structured output

		Returns:
			Either a string response or an instance of output_format
		"""
		if self.use_responses_api:
			return await self._ainvoke_responses_api(messages, output_format, **kwargs)

		openai_messages = OpenAIMessageSerializer.serialize_messages(messages)

		try:
			model_params: dict[str, Any] = {}

			if self.temperature is not None:
				model_params['temperature'] = self.temperature

			if self.frequency_penalty is not None:
				model_params['frequency_penalty'] = self.frequency_penalty

			if self.max_completion_tokens is not None:
				model_params['max_completion_tokens'] = self.max_completion_tokens

			if self.top_p is not None:
				model_params['top_p'] = self.top_p

			if self.seed is not None:
				model_params['seed'] = self.seed

			if self.service_tier is not None:
				model_params['service_tier'] = self.service_tier

			if self.reasoning_models and any(str(m).lower() in str(self.model).lower() for m in self.reasoning_models):
				model_params['reasoning_effort'] = self.reasoning_effort
				model_params.pop('temperature', None)
				model_params.pop('frequency_penalty', None)

			if output_format is None:
				# Return string response
				response = await self.get_client().chat.completions.create(
					model=self.model,
					messages=openai_messages,
					**model_params,
				)

				choice = response.choices[0] if response.choices else None
				if choice is None:
					base_url = str(self.base_url) if self.base_url is not None else None
					hint = f' (base_url={base_url})' if base_url is not None else ''
					raise ModelProviderError(
						message=(
							'Invalid OpenAI chat completion response: missing or empty `choices`.'
							' If you are using a proxy via `base_url`, ensure it implements the OpenAI'
							' `/v1/chat/completions` schema and returns `choices` as a non-empty list.'
							f'{hint}'
						),
						status_code=502,
						model=self.name,
					)

				usage = self._get_usage(response)
				return ChatInvokeCompletion(
					completion=choice.message.content or '',
					usage=usage,
					stop_reason=choice.finish_reason,
				)

			else:
				response_format: JSONSchema = {
					'name': 'agent_output',
					'strict': True,
					'schema': SchemaOptimizer.create_optimized_json_schema(
						output_format,
						remove_min_items=self.remove_min_items_from_schema,
						remove_defaults=self.remove_defaults_from_schema,
					),
				}

				# Add JSON schema to system prompt if requested
				if self.add_schema_to_system_prompt and openai_messages and openai_messages[0]['role'] == 'system':
					schema_text = f'\n<json_schema>\n{response_format}\n</json_schema>'
					if isinstance(openai_messages[0]['content'], str):
						openai_messages[0]['content'] += schema_text
					elif isinstance(openai_messages[0]['content'], Iterable):
						openai_messages[0]['content'] = list(openai_messages[0]['content']) + [
							ChatCompletionContentPartTextParam(text=schema_text, type='text')
						]

				if self.dont_force_structured_output:
					response = await self.get_client().chat.completions.create(
						model=self.model,
						messages=openai_messages,
						**model_params,
					)
				else:
					# Return structured response
					response = await self.get_client().chat.completions.create(
						model=self.model,
						messages=openai_messages,
						response_format=ResponseFormatJSONSchema(json_schema=response_format, type='json_schema'),
						**model_params,
					)

				choice = response.choices[0] if response.choices else None
				if choice is None:
					base_url = str(self.base_url) if self.base_url is not None else None
					hint = f' (base_url={base_url})' if base_url is not None else ''
					raise ModelProviderError(
						message=(
							'Invalid OpenAI chat completion response: missing or empty `choices`.'
							' If you are using a proxy via `base_url`, ensure it implements the OpenAI'
							' `/v1/chat/completions` schema and returns `choices` as a non-empty list.'
							f'{hint}'
						),
						status_code=502,
						model=self.name,
					)

				# before the content-None guard: reasoning models can burn the whole budget
				# on hidden reasoning, leaving finish_reason='length' with content=None
				if choice.finish_reason == 'length':
					cap = (
						f'max_completion_tokens={self.max_completion_tokens}'
						if self.max_completion_tokens is not None
						else "the model's output token limit"
					)
					raise ModelOutputTruncatedError(
						message=(
							f'Model output was truncated at {cap};'
							' the structured output is incomplete. Increase max_completion_tokens or request'
							' shorter output.'
						),
						model=self.name,
					)

				if choice.message.content is None:
					raise ModelProviderError(
						message='Failed to parse structured output from model response',
						status_code=500,
						model=self.name,
					)

				usage = self._get_usage(response)

				parsed = output_format.model_validate_json(choice.message.content)

				return ChatInvokeCompletion(
					completion=parsed,
					usage=usage,
					stop_reason=choice.finish_reason,
				)

		except ModelProviderError:
			# Preserve status_code and message from validation errors
			raise

		except RateLimitError as e:
			raise ModelRateLimitError(message=e.message, model=self.name) from e

		except APIConnectionError as e:
			raise ModelProviderError(message=str(e), model=self.name) from e

		except APIStatusError as e:
			raise ModelProviderError(message=e.message, status_code=e.status_code, model=self.name) from e

		except Exception as e:
			raise ModelProviderError(message=str(e), model=self.name) from e
