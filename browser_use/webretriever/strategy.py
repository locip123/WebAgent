"""Adaptive exploration-checkpoint state for long Protocol III trajectories.

The tracker is deliberately independent of the factual ``memory`` ledger and
of browser-action execution.  It owns the small amount of bookkeeping needed
to decide when a normal agent call must also refresh its exploration plan.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlsplit

STRATEGY_CHECKPOINT_INTERVAL = 10
StrategyReviewTrigger = Literal['initial_page', 'page_entry', 'periodic']

# The regular runner allows 4,096 completion tokens.  These caps leave room
# for the normal memory ledger, thought, and one browser action while still
# allowing a compact but useful catalogue of routes at a checkpoint.
CHECKPOINT_DECISION_FIELD_LIMITS: dict[str, int] = {
	'checkpoint_strategy_catalog': 900,
	'checkpoint_active_strategy': 250,
	'checkpoint_confirmed_infeasible': 450,
	'checkpoint_next_strategies': 350,
}
CHECKPOINT_DECISION_FIELDS = tuple(CHECKPOINT_DECISION_FIELD_LIMITS)


class StrategyCheckpointError(ValueError):
	"""Raised when a checkpoint is incomplete or violates its lifecycle."""


def _bounded_non_empty(value: object, *, field_name: str, max_characters: int) -> str:
	if not isinstance(value, str):
		raise StrategyCheckpointError(f'{field_name} must be a string')
	normalized = value.strip()
	if not normalized:
		raise StrategyCheckpointError(f'{field_name} must not be empty at a strategy checkpoint')
	if len(normalized) > max_characters:
		raise StrategyCheckpointError(f'{field_name} exceeds its {max_characters}-character limit')
	return normalized


def _bounded_markdown_list(value: object, *, field_name: str, max_characters: int) -> str:
	"""Validate the one-item-per-line checkpoint representation.

	All four checkpoint fields are rendered beneath a numbered heading.  Keeping
	their stored representation identical means the renderer can apply one
	indentation rule and callers never have to infer whether a value is prose or
	a list of strategies.
	"""

	normalized = _bounded_non_empty(value, field_name=field_name, max_characters=max_characters)
	invalid_lines = [line for line in normalized.splitlines() if not line.startswith('- ') or not line[2:].strip()]
	if invalid_lines:
		raise StrategyCheckpointError(
			f'{field_name} must be a Markdown list with one non-empty item per line beginning "- "'
		)
	return normalized


def _page_identity(value: object) -> str | None:
	"""Return the observed page URL without a fragment, or no valid page identity.

	The browser's ``':'`` download placeholder is deliberately excluded.  Fragment
	changes do not enter a different page for exploration planning, while a
	redirect is naturally represented by its observed destination URL.
	"""

	url = str(value or '').strip()
	if not url or url == ':':
		return None
	parts = urlsplit(url)
	if not (parts.scheme or parts.netloc or parts.path):
		return None
	return parts._replace(fragment='').geturl()


@dataclass(frozen=True, slots=True)
class StrategyCheckpoint:
	"""The durable four-part exploration state shown on all later turns."""

	completed_decisions: int
	strategy_catalog: str
	active_strategy: str
	confirmed_infeasible: str
	next_strategies: str

	def __post_init__(self) -> None:
		if self.completed_decisions < 0:
			raise StrategyCheckpointError('completed_decisions must not be negative')
		for attribute, field_name in (
			('strategy_catalog', 'checkpoint_strategy_catalog'),
			('active_strategy', 'checkpoint_active_strategy'),
			('confirmed_infeasible', 'checkpoint_confirmed_infeasible'),
			('next_strategies', 'checkpoint_next_strategies'),
		):
			object.__setattr__(
				self,
				attribute,
				_bounded_markdown_list(
					getattr(self, attribute),
					field_name=field_name,
					max_characters=CHECKPOINT_DECISION_FIELD_LIMITS[field_name],
				),
			)


@dataclass(frozen=True, slots=True)
class StrategyReviewRequest:
	"""The exact decision window that an adaptive checkpoint call must review."""

	completed_decisions: int
	trajectory: tuple[Mapping[str, Any], ...]
	trigger: StrategyReviewTrigger

	def __post_init__(self) -> None:
		if self.completed_decisions < 0:
			raise StrategyCheckpointError('completed_decisions must not be negative')
		if self.trigger not in ('initial_page', 'page_entry', 'periodic'):
			raise StrategyCheckpointError(f'unsupported strategy review trigger: {self.trigger!r}')
		if len(self.trajectory) > STRATEGY_CHECKPOINT_INTERVAL:
			raise StrategyCheckpointError(
				f'a strategy review cannot contain more than {STRATEGY_CHECKPOINT_INTERVAL} completed decisions'
			)
		if self.trigger == 'initial_page':
			if self.completed_decisions != 0 or self.trajectory:
				raise StrategyCheckpointError('the initial-page review must have no completed decision trajectory')
		elif self.trigger == 'periodic' and len(self.trajectory) != STRATEGY_CHECKPOINT_INTERVAL:
			raise StrategyCheckpointError(
				f'the periodic strategy review requires exactly {STRATEGY_CHECKPOINT_INTERVAL} completed decisions'
			)
		elif not self.trajectory:
			raise StrategyCheckpointError(f'{self.trigger} strategy review requires at least one completed decision')
		if any(not isinstance(item, Mapping) for item in self.trajectory):
			raise StrategyCheckpointError('strategy review trajectory entries must be mappings')


class ExplorationCheckpointTracker:
	"""Hide checkpoint cadence, durable state, and the since-review window.

	The agent only records a completed valid decision and, when ``review_request``
	returns a value, accepts the four flat model fields before recording another
	decision.  This small interface keeps adaptive cadence rules out of the
	browser loop.
	"""

	def __init__(self) -> None:
		self._completed_decisions = 0
		self._since_review: deque[dict[str, Any]] = deque(maxlen=STRATEGY_CHECKPOINT_INTERVAL)
		self._checkpoint: StrategyCheckpoint | None = None
		self._last_recorded_page_identity: str | None = None
		self._pending_review: StrategyReviewRequest | None = None

	@property
	def checkpoint(self) -> StrategyCheckpoint | None:
		return self._checkpoint

	@property
	def completed_decisions(self) -> int:
		return self._completed_decisions

	def review_request(self, *, current_page_url: object) -> StrategyReviewRequest | None:
		"""Return the next required initial, page-entry, or periodic review.

		A request stays pending until its four model fields are accepted.  This is
		what makes an invalid structured response retry the same review rather than
		losing its trajectory or triggering a different one on the retry.
		"""

		if self._pending_review is not None:
			return self._pending_review

		current_page_identity = _page_identity(current_page_url)
		trigger: StrategyReviewTrigger | None = None
		if self._checkpoint is None and self._completed_decisions == 0 and current_page_identity is not None:
			trigger = 'initial_page'
		elif (
			current_page_identity is not None
			and self._last_recorded_page_identity is not None
			and current_page_identity != self._last_recorded_page_identity
		):
			trigger = 'page_entry'
		elif len(self._since_review) >= STRATEGY_CHECKPOINT_INTERVAL:
			trigger = 'periodic'

		if trigger is None:
			return None
		self._pending_review = StrategyReviewRequest(
			completed_decisions=self._completed_decisions,
			trajectory=tuple(deepcopy(item) for item in self._since_review),
			trigger=trigger,
		)
		return self._pending_review

	def accept_review(
		self,
		*,
		strategy_catalog: str | None,
		active_strategy: str | None,
		confirmed_infeasible: str | None,
		next_strategies: str | None,
	) -> StrategyCheckpoint:
		"""Persist one complete model-produced strategy review for the pending window."""

		request = self._pending_review
		if request is None:
			raise StrategyCheckpointError('no strategy checkpoint is currently due')
		checkpoint = StrategyCheckpoint(
			completed_decisions=request.completed_decisions,
			strategy_catalog=_bounded_markdown_list(
				strategy_catalog,
				field_name='checkpoint_strategy_catalog',
				max_characters=CHECKPOINT_DECISION_FIELD_LIMITS['checkpoint_strategy_catalog'],
			),
			active_strategy=_bounded_markdown_list(
				active_strategy,
				field_name='checkpoint_active_strategy',
				max_characters=CHECKPOINT_DECISION_FIELD_LIMITS['checkpoint_active_strategy'],
			),
			confirmed_infeasible=_bounded_markdown_list(
				confirmed_infeasible,
				field_name='checkpoint_confirmed_infeasible',
				max_characters=CHECKPOINT_DECISION_FIELD_LIMITS['checkpoint_confirmed_infeasible'],
			),
			next_strategies=_bounded_markdown_list(
				next_strategies,
				field_name='checkpoint_next_strategies',
				max_characters=CHECKPOINT_DECISION_FIELD_LIMITS['checkpoint_next_strategies'],
			),
		)
		self._checkpoint = checkpoint
		self._since_review.clear()
		self._pending_review = None
		return checkpoint

	def record_decision(self, record: Mapping[str, Any]) -> None:
		"""Commit one completed valid AgentDecision to the rolling trajectory."""

		if not isinstance(record, Mapping):
			raise StrategyCheckpointError('completed decision record must be a mapping')
		if self._pending_review is not None:
			raise StrategyCheckpointError('a due strategy review must be accepted before another decision is recorded')
		self._completed_decisions += 1
		snapshot = deepcopy(dict(record))
		snapshot['decision'] = self._completed_decisions
		self._since_review.append(snapshot)
		page_identity = _page_identity(snapshot.get('url'))
		if page_identity is not None:
			# A download placeholder is not a page transition and must not erase the
			# most recent valid page used for the next comparison.
			self._last_recorded_page_identity = page_identity


__all__ = [
	'CHECKPOINT_DECISION_FIELDS',
	'CHECKPOINT_DECISION_FIELD_LIMITS',
	'ExplorationCheckpointTracker',
	'STRATEGY_CHECKPOINT_INTERVAL',
	'StrategyCheckpoint',
	'StrategyCheckpointError',
	'StrategyReviewRequest',
	'StrategyReviewTrigger',
]
