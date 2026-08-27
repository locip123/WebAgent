"""Bounded, browser-visible verification handling for WebRetriever tasks.

The controller decides only from the observation produced by the already-
connected Playwright browser.  It does not create a second HTTP client, alter
browser identity, or inject challenge tokens.  Its state machine keeps the
general-purpose agent from clicking a challenge that is already processing, and
it solves visible puzzle sliders from the same screenshot the runtime captured.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from browser_use.webretriever.verification_vision import plan_checkbox_click, plan_slider_drag

if TYPE_CHECKING:
	from browser_use.webretriever.browser import BrowserObservation, ElementRef
	from browser_use.webretriever.verification_vision import SliderPlan


__all__ = ['VerificationAction', 'VerificationController', 'VerificationDecision', 'VerificationState']


class VerificationState(str, Enum):
	"""Externally observable state of one visible verification episode."""

	NONE = 'none'
	ACTION_REQUIRED = 'action_required'
	PROCESSING = 'processing'
	RATE_LIMITED = 'rate_limited'
	PASSED = 'passed'
	BLOCKED = 'blocked'


class VerificationAction(str, Enum):
	"""One bounded browser action the agent may execute without an LLM turn."""

	NONE = 'none'
	CLICK = 'click'
	CLICK_XY = 'click_xy'
	DRAG = 'drag'
	WAIT = 'wait'
	BLOCKED = 'blocked'


@dataclass(frozen=True, slots=True)
class VerificationDecision:
	"""A controller result that is recorded in the normal task trajectory."""

	state: VerificationState
	action: VerificationAction
	reason: str
	element_id: int | None = None
	x: float | None = None
	y: float | None = None
	end_x: float | None = None
	end_y: float | None = None
	wait_seconds: float | None = None
	profile: str | None = None


_CHALLENGE_MARKERS = (
	'just a moment',
	'verify you are human',
	'verifying you are human',
	'performing security verification',
	'checking your browser',
	'verifying',
	'cloudflare security verification',
	'safety check',
	'安全验证',
	'拖动下方滑块完成拼图',
	'请完成安全验证',
	'请完成验证',
	'complete the security check',
)
_PROCESSING_MARKERS = (
	'verifying you are human',
	'verifying…',
	'verifying...',
	'checking your browser',
)
_RATE_LIMIT_MARKERS = (
	'操作过于频繁',
	'请稍后再试',
	'too many attempts',
	'try again later',
	'access denied',
)
_SLIDER_MARKERS = (
	'拖动下方滑块',
	'完成拼图',
	'safety check',
	'slide to complete',
	'slide to verify',
	'drag the slider',
)
_CONTROL_MARKERS = ('verify', 'human', 'challenge', 'security', 'turnstile', 'captcha', 'checkbox')
_CLICKABLE_CONTROL_TAGS = frozenset({'button', 'input', 'label'})
_CLICKABLE_CONTROL_ROLES = frozenset({'button', 'checkbox'})
_PROGRESS_ONLY_CONTROL_MARKERS = ('privacy', 'help', 'terms', 'cookie')


class VerificationController:
	"""Detect and bound visible human-verification interactions.

	A continuous visible challenge is one episode.  Interactive challenges are
	clicked or dragged at most ``max_clicks`` / ``max_drags`` times.  A page
	that is already processing is only waited on.  ``BLOCKED`` is returned when
	the wait budget is exhausted.  A URL query parameter alone is intentionally
	not a challenge signal because successful redirects can retain Cloudflare
	query parameters.
	"""

	def __init__(
		self,
		*,
		max_clicks: int = 2,
		max_drags: int = 3,
		max_wait_observations: int = 16,
		wait_seconds: float = 3.0,
		processing_wait_seconds: float = 5.0,
		rate_limit_wait_seconds: float = 12.0,
		retry_after_wait_observations: int = 2,
		recovery_observations: int = 2,
		target_url: str | None = None,
	) -> None:
		if max_clicks < 1:
			raise ValueError('max_clicks must be at least 1')
		if max_drags < 1:
			raise ValueError('max_drags must be at least 1')
		if max_wait_observations < 1:
			raise ValueError('max_wait_observations must be at least 1')
		if wait_seconds <= 0:
			raise ValueError('wait_seconds must be greater than zero')
		if processing_wait_seconds <= 0:
			raise ValueError('processing_wait_seconds must be greater than zero')
		if rate_limit_wait_seconds <= 0:
			raise ValueError('rate_limit_wait_seconds must be greater than zero')
		if retry_after_wait_observations < 1:
			raise ValueError('retry_after_wait_observations must be at least 1')
		if recovery_observations < 2:
			raise ValueError('recovery_observations must be at least 2')
		self.max_clicks = max_clicks
		self.max_drags = max_drags
		self.max_wait_observations = max_wait_observations
		self.wait_seconds = wait_seconds
		self.processing_wait_seconds = processing_wait_seconds
		self.rate_limit_wait_seconds = rate_limit_wait_seconds
		self.retry_after_wait_observations = retry_after_wait_observations
		self.recovery_observations = recovery_observations
		self.target_origin = self._origin(target_url) if target_url else None
		self.state = VerificationState.NONE
		self.challenge_episodes = 0
		self._episode_active = False
		self._click_count = 0
		self._drag_count = 0
		self._wait_count = 0
		self._waits_since_action = 0
		self._recovery_observations = 0
		self._blank_wait_count = 0
		self.max_blank_waits = 3

	def decide(self, observation: BrowserObservation) -> VerificationDecision:
		"""Return the next bounded action for the current browser observation."""

		screenshot = self._raw_screenshot(observation)
		slider_plan = plan_slider_drag(screenshot)
		checkbox_plan = plan_checkbox_click(screenshot)
		visible_challenge = self._is_visible_challenge(observation, slider_plan=slider_plan, checkbox_plan=checkbox_plan)
		if not visible_challenge:
			if (
				not self._episode_active
				and self._is_blank_interstitial(observation)
				and self._blank_wait_count < self.max_blank_waits
			):
				self._blank_wait_count += 1
				self.state = VerificationState.PROCESSING
				return VerificationDecision(
					state=VerificationState.PROCESSING,
					action=VerificationAction.WAIT,
					reason='waiting for a possible verification overlay to finish rendering',
					wait_seconds=self.wait_seconds,
				)
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
			self._recovery_observations = 0

		if self.state is VerificationState.BLOCKED:
			return VerificationDecision(
				state=VerificationState.BLOCKED,
				action=VerificationAction.BLOCKED,
				reason='the visible verification episode already exhausted its bounded budget',
			)

		if self._is_rate_limited(observation):
			return self._wait(
				seconds=self.rate_limit_wait_seconds,
				state=VerificationState.RATE_LIMITED,
				reason='verification is rate-limited; waiting before another visible attempt',
			)

		if self._needs_slider(observation, slider_plan):
			if slider_plan is not None and self._should_drag_again():
				return self._drag(slider_plan)
			if self._should_refresh_slider(observation):
				refresh = self._refresh_control(observation.elements)
				if refresh is not None:
					return self._click(refresh, reason='refreshing the visible puzzle after a failed slider attempt')
			if slider_plan is None and self._should_click_again(observation):
				candidate = self._verification_control(observation.elements)
				if candidate is not None:
					return self._click(candidate)
			return self._wait(
				seconds=self.wait_seconds,
				state=VerificationState.ACTION_REQUIRED,
				reason='waiting before another visible slider attempt',
			)

		if checkbox_plan is not None and self._should_click_again(observation):
			return self._click_xy(
				checkbox_plan.x,
				checkbox_plan.y,
				reason='clicking the visible verification checkbox',
			)

		candidate = self._verification_control(observation.elements)
		if self._should_click_again(observation) and candidate is not None and not self._is_processing(observation):
			return self._click(candidate)

		if self._is_processing(observation):
			return self._wait(
				seconds=self.processing_wait_seconds,
				state=VerificationState.PROCESSING,
				reason='verification is processing',
			)

		return self._wait(
			seconds=self.wait_seconds,
			state=VerificationState.PROCESSING,
			reason='waiting before a bounded retry',
		)

	def _start_episode(self) -> None:
		self.challenge_episodes += 1
		self._episode_active = True
		self._click_count = 0
		self._drag_count = 0
		self._wait_count = 0
		self._waits_since_action = 0
		self._recovery_observations = 0
		self.state = VerificationState.ACTION_REQUIRED

	def _should_click_again(self, observation: BrowserObservation) -> bool:
		if self._click_count >= self.max_clicks:
			return False
		if self._click_count == 0:
			return True
		return not self._is_processing(observation) and self._waits_since_action >= self.retry_after_wait_observations

	def _should_drag_again(self) -> bool:
		if self._drag_count >= self.max_drags:
			return False
		if self._drag_count == 0:
			return True
		return self._waits_since_action >= self.retry_after_wait_observations

	def _should_refresh_slider(self, observation: BrowserObservation) -> bool:
		if self._drag_count == 0 or self._click_count >= self.max_clicks:
			return False
		if self._is_processing(observation):
			return False
		return self._waits_since_action >= self.retry_after_wait_observations

	def _click(self, candidate: ElementRef, *, reason: str = 'clicking the visible verification control') -> VerificationDecision:
		if candidate.width > 0 and candidate.height > 0:
			return self._click_xy(
				candidate.x + candidate.width / 2.0,
				candidate.y + candidate.height / 2.0,
				reason=reason,
			)
		self._click_count += 1
		self._waits_since_action = 0
		self.state = VerificationState.PROCESSING
		return VerificationDecision(
			state=VerificationState.ACTION_REQUIRED,
			action=VerificationAction.CLICK,
			reason=reason,
			element_id=candidate.index,
		)

	def _click_xy(
		self, x: float, y: float, *, reason: str = 'clicking the visible verification control by screenshot coordinates'
	) -> VerificationDecision:
		self._click_count += 1
		self._waits_since_action = 0
		self.state = VerificationState.PROCESSING
		return VerificationDecision(
			state=VerificationState.ACTION_REQUIRED,
			action=VerificationAction.CLICK_XY,
			reason=reason,
			x=x,
			y=y,
		)

	def _drag(self, plan: SliderPlan) -> VerificationDecision:
		self._drag_count += 1
		self._waits_since_action = 0
		self.state = VerificationState.PROCESSING
		return VerificationDecision(
			state=VerificationState.ACTION_REQUIRED,
			action=VerificationAction.DRAG,
			reason='dragging the visible puzzle slider to the detected gap',
			x=plan.start_x,
			y=plan.start_y,
			end_x=plan.end_x,
			end_y=plan.end_y,
			profile='human',
		)

	def _wait(self, *, seconds: float, state: VerificationState, reason: str) -> VerificationDecision:
		if self._wait_count >= self.max_wait_observations:
			self.state = VerificationState.BLOCKED
			return VerificationDecision(
				state=VerificationState.BLOCKED,
				action=VerificationAction.BLOCKED,
				reason=f'visible verification did not complete after {self._wait_count} bounded waits',
			)
		self.state = state
		self._wait_count += 1
		self._waits_since_action += 1
		return VerificationDecision(
			state=state,
			action=VerificationAction.WAIT,
			reason=reason,
			wait_seconds=seconds,
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
		self._waits_since_action += 1
		return VerificationDecision(
			state=VerificationState.PROCESSING,
			action=VerificationAction.WAIT,
			reason='waiting to confirm that the target page recovered after verification',
			wait_seconds=self.wait_seconds,
		)

	@staticmethod
	def _is_visible_challenge(
		observation: BrowserObservation,
		*,
		slider_plan: object | None = None,
		checkbox_plan: object | None = None,
	) -> bool:
		if slider_plan is not None:
			return True
		visible = VerificationController._visible_text(observation)
		has_challenge_text = any(marker in visible for marker in _CHALLENGE_MARKERS)
		if checkbox_plan is not None and has_challenge_text:
			return True
		if not has_challenge_text:
			return False
		return not VerificationController._looks_like_content_page(observation)

	@staticmethod
	def _is_processing(observation: BrowserObservation) -> bool:
		visible = VerificationController._visible_text(observation)
		return any(marker in visible for marker in _PROCESSING_MARKERS)

	@staticmethod
	def _is_rate_limited(observation: BrowserObservation) -> bool:
		visible = VerificationController._visible_text(observation)
		return any(marker in visible for marker in _RATE_LIMIT_MARKERS)

	@staticmethod
	def _needs_slider(observation: BrowserObservation, slider_plan: object | None = None) -> bool:
		if slider_plan is not None:
			return True
		visible = VerificationController._visible_text(observation)
		if not any(marker in visible for marker in _SLIDER_MARKERS):
			return False
		return not VerificationController._looks_like_content_page(observation)

	@staticmethod
	def _looks_like_content_page(observation: BrowserObservation) -> bool:
		if len(observation.page_text) >= 2_500:
			return True
		visible_controls = sum(1 for element in observation.elements if element.width > 0 and element.height > 0)
		return visible_controls >= 12

	@staticmethod
	def _raw_screenshot(observation: BrowserObservation) -> bytes:
		path = getattr(observation, 'screenshot_path', '') or ''
		if path:
			from pathlib import Path

			file_path = Path(path)
			if file_path.is_file():
				return file_path.read_bytes()
		return getattr(observation, 'screenshot', b'') or b''

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
	def _is_progress_only_control(element: ElementRef) -> bool:
		visible_text = VerificationController._element_text(element)
		return any(marker in visible_text for marker in _PROGRESS_ONLY_CONTROL_MARKERS)

	@staticmethod
	def _is_clickable_control(element: ElementRef) -> bool:
		tag = element.tag.casefold()
		role = element.role.casefold()
		return tag in _CLICKABLE_CONTROL_TAGS or role in _CLICKABLE_CONTROL_ROLES or tag == 'iframe'

	@staticmethod
	def _verification_control(elements: list[ElementRef]) -> ElementRef | None:
		def score(element: ElementRef) -> tuple[int, int, float]:
			visible_text = VerificationController._element_text(element)
			semantic_hits = sum(marker in visible_text for marker in _CONTROL_MARKERS)
			is_frame = int(element.tag.casefold() == 'iframe')
			is_control = int(VerificationController._is_clickable_control(element))
			area = max(0.0, element.width) * max(0.0, element.height)
			return semantic_hits + is_frame, is_control + is_frame, area

		candidates = [
			element
			for element in elements
			if element.width > 0
			and element.height > 0
			and not VerificationController._is_progress_only_control(element)
			and (
				any(marker in VerificationController._element_text(element) for marker in _CONTROL_MARKERS)
				or element.tag.casefold() == 'iframe'
			)
			and VerificationController._is_clickable_control(element)
		]
		return max(candidates, key=score) if candidates else None

	@staticmethod
	def _refresh_control(elements: list[ElementRef]) -> ElementRef | None:
		for element in elements:
			visible_text = VerificationController._element_text(element)
			if element.width <= 0 or element.height <= 0:
				continue
			if any(marker in visible_text for marker in ('refresh', 'reload', '换一张', '刷新')):
				return element
		return None

	@staticmethod
	def _is_blank_interstitial(observation: BrowserObservation) -> bool:
		visible = f'{observation.title}\n{observation.page_text}'.strip()
		return not visible and not observation.elements

	def summary(self) -> dict[str, object]:
		"""Return the bounded episode metrics persisted with a task result."""

		return {
			'challenge_episodes': self.challenge_episodes,
			'state': self.state.value,
			'click_count': self._click_count,
			'drag_count': self._drag_count,
			'wait_count': self._wait_count,
		}
