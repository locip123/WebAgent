"""Durable local storage for the desktop application's single account profile."""

from __future__ import annotations

import json
import os
from pathlib import Path

from browser_use.webretriever.desktop.contracts import AccountProfile


class AccountProfileStore:
	"""Read and replace one account profile without sharing the run journal schema."""

	def __init__(self, path: Path | str | None = None) -> None:
		self._path = Path(path) if path is not None else None
		self._profile = AccountProfile()
		if self._path is not None:
			self._profile = self._load()

	def get(self) -> AccountProfile:
		return self._profile

	def save(self, profile: AccountProfile) -> AccountProfile:
		if self._path is not None:
			self._path.parent.mkdir(parents=True, exist_ok=True)
			temporary_path = self._path.with_name(f".{self._path.name}.tmp")
			temporary_path.write_text(profile.model_dump_json(indent=2) + "\n", encoding="utf-8")
			os.replace(temporary_path, self._path)
		self._profile = profile
		return profile

	def _load(self) -> AccountProfile:
		assert self._path is not None
		try:
			payload = json.loads(self._path.read_text(encoding="utf-8"))
			return AccountProfile.model_validate(payload)
		except (OSError, json.JSONDecodeError, ValueError):
			return AccountProfile()


__all__ = ["AccountProfileStore"]
