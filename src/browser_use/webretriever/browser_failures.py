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


class BrowserFailureSubtype(str, Enum):
	"""Stable, actionable cause inside a browser failure category.

	``BrowserFailureCategory`` is intentionally coarse so existing dashboards can
	continue grouping failures by capability.  The subtype is the information an
	operator should use when deciding what to inspect or recover.
	"""

	RUNTIME_NOT_STARTED = 'runtime_not_started'
	RUNTIME_CLOSED = 'runtime_closed'
	CONTEXT_UNAVAILABLE = 'context_unavailable'
	CONNECTION_UNAVAILABLE = 'connection_unavailable'
	TASK_PAGE_MISSING = 'task_page_missing'
	TASK_PAGE_CLOSED = 'task_page_closed'
	TASK_PAGE_RECOVERY_EXHAUSTED = 'task_page_recovery_exhausted'
	NO_LIVE_PAGE = 'no_live_page'
	DOWNLOAD_PAGE_ORPHANED = 'download_page_orphaned'
	TARGET_CRASHED = 'target_crashed'
	CDP_CONNECTION_TIMEOUT = 'cdp_connection_timeout'
	SCREENSHOT_TIMEOUT = 'screenshot_timeout'
	SCREENSHOT_CAPTURE_FAILED = 'screenshot_capture_failed'
	NAVIGATION_FAILED = 'navigation_failed'
	SESSION_CLOSED = 'session_closed'
	UNKNOWN = 'unknown'


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
	subtype: BrowserFailureSubtype = BrowserFailureSubtype.UNKNOWN

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
			'subtype': self.subtype.value,
			'phase': self.phase.value,
			'exception_type': self.exception_type,
			'recovery_attempted': recovery_attempted,
		}


def classify_browser_failure(
	error: BaseException | str,
	*,
	phase: BrowserFailurePhase,
	session_closed: bool = False,
	task_page_state: str | None = None,
	recovery_exhausted: bool = False,
) -> BrowserFailure:
	"""Classify a browser failure without coupling to Playwright exception types.

	``task_page_state`` accepts ``missing`` or ``closed`` when the caller can
	inspect the runtime object.  This lets the same legacy error text produce a
	useful subtype without making the classifier depend on Playwright classes.
	"""

	message = str(error)
	if isinstance(error, str):
		exception_type = 'BrowserActionError'
	else:
		exception_type = type(error).__name__
	lowered = message.casefold()

	if session_closed:
		category = BrowserFailureCategory.SESSION_LOST
		subtype = (
			BrowserFailureSubtype.TARGET_CRASHED
			if 'target crashed' in lowered
			else BrowserFailureSubtype.SESSION_CLOSED
		)
	elif 'call browserruntime.start(website) first' in lowered:
		category = BrowserFailureCategory.RUNTIME_NOT_STARTED
		subtype = BrowserFailureSubtype.RUNTIME_NOT_STARTED
	elif 'browserruntime is closed' in lowered:
		category = BrowserFailureCategory.RUNTIME_CLOSED
		subtype = BrowserFailureSubtype.RUNTIME_CLOSED
	elif 'has no browser context' in lowered or 'worker has no browser context' in lowered:
		category = BrowserFailureCategory.CONTEXT_UNAVAILABLE
		subtype = BrowserFailureSubtype.CONTEXT_UNAVAILABLE
	elif 'no cdp worker was available' in lowered:
		category = BrowserFailureCategory.CONNECTION_UNAVAILABLE
		subtype = BrowserFailureSubtype.CONNECTION_UNAVAILABLE
	elif 'connect_over_cdp' in lowered and ('timeout' in lowered or 'timed out' in lowered):
		category = BrowserFailureCategory.CONNECTION_UNAVAILABLE
		subtype = BrowserFailureSubtype.CDP_CONNECTION_TIMEOUT
	elif 'browserruntime active task page is closed' in lowered:
		category = BrowserFailureCategory.TASK_PAGE_UNAVAILABLE
		subtype = BrowserFailureSubtype.TASK_PAGE_CLOSED
	elif 'browserruntime has no active task page' in lowered:
		category = BrowserFailureCategory.TASK_PAGE_UNAVAILABLE
		if recovery_exhausted:
			subtype = BrowserFailureSubtype.TASK_PAGE_RECOVERY_EXHAUSTED
		elif task_page_state == 'closed':
			subtype = BrowserFailureSubtype.TASK_PAGE_CLOSED
		else:
			subtype = BrowserFailureSubtype.TASK_PAGE_MISSING
	elif 'target crashed' in lowered:
		# A target crash observed before the generic session-close predicate is
		# applied still means the task page cannot be trusted.  Keep it in the
		# legacy page-unavailable family during observation for compatibility.
		category = (
			BrowserFailureCategory.TASK_PAGE_UNAVAILABLE
			if phase is BrowserFailurePhase.OBSERVATION
			else BrowserFailureCategory.SESSION_LOST
		)
		subtype = BrowserFailureSubtype.TARGET_CRASHED
	elif "download placeholder page ':' has no live safe opener or fallback page" in lowered:
		category = BrowserFailureCategory.DOWNLOAD_PAGE_ORPHANED
		subtype = BrowserFailureSubtype.DOWNLOAD_PAGE_ORPHANED
	elif 'browserruntime has no live safe page' in lowered or 'no live tabs' in lowered:
		category = BrowserFailureCategory.NO_LIVE_PAGE
		subtype = BrowserFailureSubtype.NO_LIVE_PAGE
	elif 'screenshot' in lowered and ('timeout' in lowered or 'timed out' in lowered or 'exceeded' in lowered):
		category = BrowserFailureCategory.OBSERVATION
		subtype = BrowserFailureSubtype.SCREENSHOT_TIMEOUT
	elif 'screenshot' in lowered and ('capture' in lowered or 'protocol error' in lowered):
		category = BrowserFailureCategory.OBSERVATION
		subtype = BrowserFailureSubtype.SCREENSHOT_CAPTURE_FAILED
	elif phase is BrowserFailurePhase.STARTUP and any(
		marker in lowered for marker in ('navigation failed', 'goto', 'net::err_', 'initial navigation')
	):
		category = BrowserFailureCategory.STARTUP
		subtype = BrowserFailureSubtype.NAVIGATION_FAILED
	elif phase is BrowserFailurePhase.OBSERVATION:
		category = BrowserFailureCategory.OBSERVATION
		subtype = BrowserFailureSubtype.UNKNOWN
	else:
		category = BrowserFailureCategory.STARTUP
		subtype = BrowserFailureSubtype.UNKNOWN

	return BrowserFailure(category=category, subtype=subtype, phase=phase, exception_type=exception_type)
