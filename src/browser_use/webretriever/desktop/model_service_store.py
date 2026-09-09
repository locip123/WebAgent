"""Durable model-service configuration without exposing credentials in reads."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from browser_use.webretriever.desktop.contracts import ModelServiceInput, ModelServiceSummary


class ModelServiceStore:
	"""Read and append services in the runner's JSON configuration file."""

	def __init__(self, path: Path | str | None = None) -> None:
		self._path = Path(path) if path is not None else None
		self._payload = self._read_payload() if self._path is not None else {"model_services": []}

	def list(self) -> list[ModelServiceSummary]:
		return [self._summary(raw_service) for raw_service in self._raw_services()]

	def add(self, service: ModelServiceInput) -> ModelServiceSummary:
		raw_services = self._raw_services()
		if any(raw_service.get("name") == service.name for raw_service in raw_services):
			raise ValueError("a model service with this name already exists")
		raw_service = service.model_dump()
		raw_services.append(raw_service)
		self._write_payload()
		return self._summary(raw_service)

	def get(self, name: str) -> ModelServiceInput:
		for raw_service in self._raw_services():
			if raw_service.get("name") == name:
				return ModelServiceInput.model_validate(raw_service)
		raise KeyError(name)

	def load_raw_services(self) -> list[dict[str, Any]]:
		"""Return a copy for runner-side use; this method is never an HTTP response."""

		return [dict(service) for service in self._raw_services()]

	def _raw_services(self) -> list[dict[str, Any]]:
		raw_services = self._payload.get("model_services")
		if not isinstance(raw_services, list):
			raise ValueError("model_services must be a list")
		if any(not isinstance(service, dict) for service in raw_services):
			raise ValueError("each model service must be an object")
		return raw_services

	@staticmethod
	def _summary(raw_service: dict[str, Any]) -> ModelServiceSummary:
		without_credential = dict(raw_service)
		without_credential.pop("api_key", None)
		return ModelServiceSummary.model_validate(without_credential)

	def _read_payload(self) -> dict[str, Any]:
		assert self._path is not None
		if not self._path.exists():
			return {"api_model": "gpt-5.4", "api_mode": "responses", "model_services": []}
		try:
			payload = json.loads(self._path.read_text(encoding="utf-8"))
		except (OSError, json.JSONDecodeError) as exc:
			raise ValueError("model service configuration is unreadable") from exc
		if not isinstance(payload, dict):
			raise ValueError("model service configuration must be a JSON object")
		return payload

	def _write_payload(self) -> None:
		if self._path is None:
			return
		self._path.parent.mkdir(parents=True, exist_ok=True)
		temporary_path = self._path.with_name(f".{self._path.name}.tmp")
		temporary_path.write_text(json.dumps(self._payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
		os.replace(temporary_path, self._path)


__all__ = ["ModelServiceStore"]
