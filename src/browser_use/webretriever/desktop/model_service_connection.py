"""Safe, minimal connection probes for OpenAI-compatible model services."""

from __future__ import annotations

import httpx

from browser_use.webretriever.desktop.contracts import ModelServiceInput


class ModelServiceConnectionError(Exception):
	"""A connection-test failure with a stable, credential-safe error code."""

	def __init__(self, error_code: str) -> None:
		super().__init__(error_code)
		self.error_code = error_code


async def test_model_service_connection(service: ModelServiceInput) -> None:
	"""Send one minimal request using the service's selected API protocol."""

	if service.response_mode == "responses":
		path = "/responses"
		payload = {"model": service.model, "input": "Reply only with OK.", "max_output_tokens": 1}
	else:
		path = "/chat/completions"
		payload = {
			"model": service.model,
			"messages": [{"role": "user", "content": "Reply only with OK."}],
			"max_tokens": 1,
		}
	try:
		async with httpx.AsyncClient(timeout=20.0) as client:
			response = await client.post(
				service.api_base.rstrip("/") + path,
				headers={"Authorization": f"Bearer {service.api_key}"},
				json=payload,
			)
	except httpx.TimeoutException as exc:
		raise ModelServiceConnectionError("connection_timed_out") from exc
	except httpx.HTTPError as exc:
		raise ModelServiceConnectionError("connection_failed") from exc
	if response.is_success:
		return
	if response.status_code in {401, 403}:
		raise ModelServiceConnectionError("authentication_failed")
	if 400 <= response.status_code < 500:
		raise ModelServiceConnectionError("request_rejected")
	raise ModelServiceConnectionError("provider_unavailable")


__all__ = ["ModelServiceConnectionError", "test_model_service_connection"]
