class ModelError(Exception):
	pass


class ModelProviderError(ModelError):
	"""Exception raised when a model provider returns an error."""

	def __init__(
		self,
		message: str,
		status_code: int = 502,
		model: str | None = None,
		raw_completion: str | None = None,
	):
		super().__init__(message)
		self.message = message
		self.status_code = status_code
		self.model = model
		self.raw_completion = raw_completion


class ModelStructuredOutputError(ModelProviderError):
	"""A model response that cannot satisfy the requested structured output."""

	def __init__(
		self,
		message: str,
		status_code: int = 502,
		model: str | None = None,
		raw_completion: str | None = None,
	) -> None:
		super().__init__(message, status_code=status_code, model=model, raw_completion=raw_completion)
		# The router fills these in before returning the error to the agent.  Keep
		# them explicit rather than relying on dynamically-added attributes so
		# callers can safely inspect an error from any model adapter.
		self.service_name: str | None = None
		self.service_group: str | None = None


class ModelRateLimitError(ModelProviderError):
	"""Exception raised when a model provider returns a rate limit error."""

	def __init__(
		self,
		message: str,
		status_code: int = 429,
		model: str | None = None,
	):
		super().__init__(message, status_code, model)


class ModelOutputTruncatedError(ModelProviderError):
	"""Output was cut off at an output-token limit (finish_reason='length' / stop_reason='max_tokens').

	Status 400 keeps it out of same-provider retry loops; the agent's fallback switch treats it as recoverable.
	"""

	def __init__(
		self,
		message: str,
		model: str | None = None,
		raw_completion: str | None = None,
	):
		super().__init__(message, status_code=400, model=model, raw_completion=raw_completion)
