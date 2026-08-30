"""Stable browser-failure taxonomy for task results and worker diagnostics."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class BrowserFailureCategory(str, Enum):
	"""Root cause classes for terminal browser failures.

	The names intentionally describe the failed browser capability rather than a
	particular Playwright error message, which can vary by browser version.
	"""

	RUNTIME_NOT_STARTED = 'runtime_not_started'
	RUNTIME_CLOSED = 'runtime_closed'
	CONTEXT_UNAVAILABLE = 'context_unavailable'
	CONNECTION_UNAVAILABLE = 'connection_unavailable'
	TASK_PAGE_UNAVAILABLE = 'task_page_unavailable'
	NO_LIVE_PAGE = 'no_live_page'
	DOWNLOAD_PAGE_ORPHANED = 'download_page_orphaned'
	SESSION_LOST = 'session_lost'
	OBSERVATION = 'observation'
	STARTUP = 'startup'


class BrowserFailurePhase(str, Enum):
	"""Task phase in which a terminal browser failure surfaced."""

	STARTUP = 'startup'
	OBSERVATION = 'observation'
	ACTION = 'action'


@dataclass(frozen=True, slots=True)
class BrowserFailure:
	"""A serializable classification for one terminal browser failure."""

	category: BrowserFailureCategory
	phase: BrowserFailurePhase
	exception_type: str

	@property
	def status(self) -> str:
		return f'FAIL_BROWSER_{self.category.value.upper()}'

	def payload(self, *, recovery_attempted: bool) -> dict[str, Any]:
		"""Return the stable result.json diagnostic schema.

		The complete, redacted error text remains in ``result.error``. Keeping it
		out of this object makes category aggregation independent of provider- and
		browser-version-specific wording.
		"""

		return {
			'category': self.category.value,
			'phase': self.phase.value,
			'exception_type': self.exception_type,
			'recovery_attempted': recovery_attempted,
		}


def classify_browser_failure(
	error: BaseException | str,
	*,
	phase: BrowserFailurePhase,
	session_closed: bool = False,
) -> BrowserFailure:
	"""Classify a browser failure without coupling to Playwright exception types."""

	message = str(error)
	if isinstance(error, str):
		exception_type = 'BrowserActionError'
	else:
		exception_type = type(error).__name__
	lowered = message.casefold()

	if session_closed:
		category = BrowserFailureCategory.SESSION_LOST
	elif 'call browserruntime.start(website) first' in lowered:
		category = BrowserFailureCategory.RUNTIME_NOT_STARTED
	elif 'browserruntime is closed' in lowered:
		category = BrowserFailureCategory.RUNTIME_CLOSED
	elif 'has no browser context' in lowered or 'worker has no browser context' in lowered:
		category = BrowserFailureCategory.CONTEXT_UNAVAILABLE
	elif 'no cdp worker was available' in lowered:
		category = BrowserFailureCategory.CONNECTION_UNAVAILABLE
	elif 'browserruntime has no active task page' in lowered:
		category = BrowserFailureCategory.TASK_PAGE_UNAVAILABLE
	elif "download placeholder page ':' has no live safe opener or fallback page" in lowered:
		category = BrowserFailureCategory.DOWNLOAD_PAGE_ORPHANED
	elif 'browserruntime has no live safe page' in lowered or 'no live tabs' in lowered:
		category = BrowserFailureCategory.NO_LIVE_PAGE
	elif phase is BrowserFailurePhase.OBSERVATION:
		category = BrowserFailureCategory.OBSERVATION
	else:
		category = BrowserFailureCategory.STARTUP

	return BrowserFailure(category=category, phase=phase, exception_type=exception_type)
