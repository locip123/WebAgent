"""Bounded, browser-visible verification handling for WebRetriever tasks.

The controller deliberately decides only from the observation produced by the
already-connected Playwright browser.  It does not create a second HTTP client,
alter browser identity, or inject challenge tokens.  Its small state machine
prevents the general-purpose agent loop from repeatedly clicking a challenge
while it is already processing.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

if TYPE_CHECKING:
	from browser_use.webretriever.browser import BrowserObservation, ElementRef


__all__ = ['VerificationAction', 'VerificationController', 'VerificationDecision', 'VerificationState']


class VerificationState(str, Enum):
	"""Externally observable state of one visible verification episode."""

	NONE = 'none'
	ACTION_REQUIRED = 'action_required'
	PROCESSING = 'processing'
	PASSED = 'passed'
	BLOCKED = 'blocked'


class VerificationAction(str, Enum):
	"""One bounded browser action the agent may execute without an LLM turn."""

	NONE = 'none'
	CLICK = 'click'
	WAIT = 'wait'
	BLOCKED = 'blocked'


@dataclass(frozen=True, slots=True)
class VerificationDecision:
	"""A controller result that is recorded in the normal task trajectory."""

	state: VerificationState
	action: VerificationAction
	reason: str
	element_id: int | None = None
	wait_seconds: float | None = None


_CHALLENGE_MARKERS = (
	'just a moment',
	'verify you are human',
	'verifying you are human',
	'performing security verification',
	'checking your browser',
	'verifying',
	'cloudflare security verification',
)
_PROCESSING_MARKERS = ('verifying', 'performing security verification', 'checking your browser')
_CONTROL_MARKERS = ('verify', 'human', 'challenge', 'security', 'turnstile', 'captcha')


class VerificationController:
	"""Detect and bound visible human-verification interactions.

	A continuous visible challenge is one episode.  The controller clicks a
	semantic or visible challenge frame at most ``max_clicks`` times, waits in
	between attempts, and returns ``BLOCKED`` when the bounded wait budget is
	exhausted.  A URL query parameter alone is intentionally not a challenge
	signal because successful redirects can retain Cloudflare query parameters.
	"""

	def __init__(
		self,
		*,
		max_clicks: int = 2,
		max_wait_observations: int = 10,
		wait_seconds: float = 3.0,
		retry_after_wait_observations: int = 2,
		recovery_observations: int = 2,
		target_url: str | None = None,
	) -> None:
		if max_clicks < 1:
			raise ValueError('max_clicks must be at least 1')
		if max_wait_observations < 1:
			raise ValueError('max_wait_observations must be at least 1')
		if wait_seconds <= 0:
			raise ValueError('wait_seconds must be greater than zero')
		if retry_after_wait_observations < 1:
			raise ValueError('retry_after_wait_observations must be at least 1')
		if recovery_observations < 2:
			raise ValueError('recovery_observations must be at least 2')
		self.max_clicks = max_clicks
		self.max_wait_observations = max_wait_observations
		self.wait_seconds = wait_seconds
		self.retry_after_wait_observations = retry_after_wait_observations
		self.recovery_observations = recovery_observations
		self.target_origin = self._origin(target_url) if target_url else None
		self.state = VerificationState.NONE
		self.challenge_episodes = 0
		self._episode_active = False
		self._click_count = 0
		self._wait_count = 0
		self._waits_since_click = 0
		self._recovery_observations = 0

	def decide(self, observation: BrowserObservation) -> VerificationDecision:
		"""Return the next bounded action for the current browser observation."""

		if not self._is_visible_challenge(observation):
			if self._episode_active:
				if self._is_recovery_observation(observation):
					self._recovery_observations += 1
				else:
					self._recovery_observations = 0
				if self._recovery_observations < self.recovery_observations:
					return self._wait_for_recovery()
				self._episode_active = False
				self.state = VerificationState.PASSED
				return VerificationDecision(
					state=VerificationState.PASSED,
					action=VerificationAction.NONE,
					reason='the visible verification page disappeared',
				)
			self.state = VerificationState.NONE
			return VerificationDecision(
				state=VerificationState.NONE,
				action=VerificationAction.NONE,
				reason='no visible verification page',
			)

		if not self._episode_active:
			self._start_episode()
		else:
			# A visible challenge after a candidate recovery means the document was
			# only loading.  Start the confirmation window again for this episode.
			self._recovery_observations = 0

		if self.state is VerificationState.BLOCKED:
			return VerificationDecision(
				state=VerificationState.BLOCKED,
				action=VerificationAction.BLOCKED,
				reason='the visible verification episode already exhausted its bounded budget',
			)

		candidate = self._verification_control(observation.elements)
		if self._should_click_again(observation) and candidate is not None:
			return self._click(candidate)

		if self._wait_count >= self.max_wait_observations:
			self.state = VerificationState.BLOCKED
			return VerificationDecision(
				state=VerificationState.BLOCKED,
				action=VerificationAction.BLOCKED,
				reason=f'visible verification did not complete after {self._wait_count} bounded waits',
			)

		self.state = VerificationState.PROCESSING
		self._wait_count += 1
		self._waits_since_click += 1
		reason = 'verification is processing' if self._is_processing(observation) else 'waiting before a bounded retry'
		return VerificationDecision(
			state=VerificationState.PROCESSING,
			action=VerificationAction.WAIT,
			reason=reason,
			wait_seconds=self.wait_seconds,
		)

	def _start_episode(self) -> None:
		self.challenge_episodes += 1
		self._episode_active = True
		self._click_count = 0
		self._wait_count = 0
		self._waits_since_click = 0
		self._recovery_observations = 0
		self.state = VerificationState.ACTION_REQUIRED

	def _should_click_again(self, observation: BrowserObservation) -> bool:
		if self._click_count == 0:
			return True
		if self._click_count >= self.max_clicks:
			return False
		return not self._is_processing(observation) and self._waits_since_click >= self.retry_after_wait_observations

	def _click(self, candidate: ElementRef) -> VerificationDecision:
		self._click_count += 1
		self._waits_since_click = 0
		self.state = VerificationState.PROCESSING
		return VerificationDecision(
			state=VerificationState.ACTION_REQUIRED,
			action=VerificationAction.CLICK,
			reason='clicking the visible verification control',
			element_id=candidate.index,
		)

	def _wait_for_recovery(self) -> VerificationDecision:
		if self._wait_count >= self.max_wait_observations:
			self.state = VerificationState.BLOCKED
			return VerificationDecision(
				state=VerificationState.BLOCKED,
				action=VerificationAction.BLOCKED,
				reason=f'visible verification recovery was not confirmed after {self._wait_count} bounded waits',
			)
		self.state = VerificationState.PROCESSING
		self._wait_count += 1
		self._waits_since_click += 1
		return VerificationDecision(
			state=VerificationState.PROCESSING,
			action=VerificationAction.WAIT,
			reason='waiting to confirm that the target page recovered after verification',
			wait_seconds=self.wait_seconds,
		)

	@staticmethod
	def _is_visible_challenge(observation: BrowserObservation) -> bool:
		visible = VerificationController._visible_text(observation)
		return any(marker in visible for marker in _CHALLENGE_MARKERS)

	@staticmethod
	def _is_processing(observation: BrowserObservation) -> bool:
		visible = VerificationController._visible_text(observation)
		return any(marker in visible for marker in _PROCESSING_MARKERS)

	def _is_recovery_observation(self, observation: BrowserObservation) -> bool:
		"""Distinguish a restored target document from a transient blank load."""

		if not observation.url.strip() or not (observation.title.strip() or observation.page_text.strip()):
			return False
		return self.target_origin is None or self._origin(observation.url) == self.target_origin

	@staticmethod
	def _origin(url: str) -> str | None:
		parsed = urlsplit(url)
		if parsed.scheme not in {'http', 'https'} or not parsed.netloc:
			return None
		return f'{parsed.scheme}://{parsed.netloc}'.casefold()

	@staticmethod
	def _visible_text(observation: BrowserObservation) -> str:
		return f'{observation.title}\n{observation.page_text}'.casefold()

	@staticmethod
	def _element_text(element: ElementRef) -> str:
		return ' '.join(
			value for value in (element.name, element.text, element.role, element.placeholder, element.frame_url) if value
		).casefold()

	@staticmethod
	def _verification_control(elements: list[ElementRef]) -> ElementRef | None:
		def score(element: ElementRef) -> tuple[int, int, float]:
			visible_text = VerificationController._element_text(element)
			semantic_hits = sum(marker in visible_text for marker in _CONTROL_MARKERS)
			is_frame = int(element.tag.casefold() == 'iframe')
			is_control = int(element.tag.casefold() in {'button', 'input', 'label'} or element.role.casefold() in {'button', 'checkbox'})
			area = max(0.0, element.width) * max(0.0, element.height)
			return semantic_hits + is_frame, is_control + is_frame, area

		candidates = [
			element
			for element in elements
			if element.width > 0
			and element.height > 0
			and (
				any(marker in VerificationController._element_text(element) for marker in _CONTROL_MARKERS)
				or element.tag.casefold() == 'iframe'
			)
		]
		return max(candidates, key=score) if candidates else None

	def summary(self) -> dict[str, object]:
		"""Return the bounded episode metrics persisted with a task result."""

		return {
			'challenge_episodes': self.challenge_episodes,
			'state': self.state.value,
			'click_count': self._click_count,
			'wait_count': self._wait_count,
		}
