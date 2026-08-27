"""Append-only exploration-path state for WebRetriever tasks.

The path tree is deliberately independent of browser-action execution. It owns
stable identifiers, observed start URLs, node-local progress, and lifecycle
state. Model-supplied path JSON is treated as a permissive wire format; this
module canonicalizes the subset that can be applied safely.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, replace
from typing import Annotated, Any, Literal, TypeAlias
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter
from pydantic.json_schema import SkipJsonSchema

EXPLORATION_PATH_SCHEMA_VERSION = 2
EXPLORATION_PATH_FILENAME = 'path.json'
EXPLORATION_REVIEW_INTERVAL = 10
EXPLORATION_REVIEW_RECENT_DECISION_WINDOW = 10
EXPLORATION_REVIEW_MAX_IN_WINDOW = 3
EXPLORATION_PATH_STATUSES = frozenset({'pending', 'in_progress', 'failed', 'succeeded'})
SYSTEM_INITIAL_PATH_ID = '1'
SYSTEM_INITIAL_PATH_LOCATION = '任务起始页面'
SYSTEM_INITIAL_PATH_STRATEGY = '任务起始，准备分析任务起始页面寻找所有可到达任务目的地或正确页面的路线,并将其添加到子路径中'
SYSTEM_INITIAL_PATH_PROGRESS = '准备分析任务起始页面寻找所有可到达任务目的地或正确页面的路线,并将其添加到子路径中'

ExplorationPathStatus = Literal['pending', 'in_progress', 'failed', 'succeeded']
ExplorationReviewTrigger = Literal['initial_page', 'unseen_page', 'periodic']
PathJsonOperationName = Literal['add', 'update']
_RETIRED_PATH_OPERATION_FIELDS = frozenset({'objective', 'action', 'target_name', 'description'})
_ADD_EXECUTOR_FIELDS = frozenset({'path_id', 'start_url', 'status', 'progress', 'children'})
_MAX_FIELD_LENGTHS = {
    'path_id': 128,
    'parent_path_id': 128,
    'location': 1_000,
    'strategy_description': 4_000,
    'progress': 4_000,
}

# The compatibility parser intentionally remains permissive at the
# deserialization boundary: the executor reports malformed model values per
# operation instead of rejecting the entire browser decision.  This fallback is
# wrapped in ``SkipJsonSchema`` below, so it never widens the model-facing
# output contract.
PathJsonWireValue = Annotated[
    Any,
    Field(json_schema_extra={'anyOf': [{'type': 'string'}, {'type': 'null'}]}),
]


class ExplorationPathError(ValueError):
    """Raised when required path-tree lifecycle state cannot be established."""


class PathJsonAddOperation(BaseModel):
    """The model-facing schema for creating one exploration path."""

    model_config = ConfigDict(extra='allow', strict=True)

    op: Literal['add']
    parent_path_id: str | None = None
    location: str
    strategy_description: str


class PathJsonUpdateOperation(BaseModel):
    """The model-facing schema for mutating an existing exploration path."""

    model_config = ConfigDict(extra='allow', strict=True)

    op: Literal['update']
    path_id: str
    status: ExplorationPathStatus | None = None
    progress: str | None = None


class PathJsonLegacyOperation(BaseModel):
    """Permissive compatibility parser for pre-split path-operation responses.

    The class is intentionally excluded from the advertised JSON Schema.  It
    keeps old or malformed responses available to the executor so they receive
    operation-level diagnostics instead of becoming opaque decision parse errors.
    """

    model_config = ConfigDict(extra='allow', strict=True)

    op: PathJsonWireValue = ''
    path_id: PathJsonWireValue = None
    parent_path_id: PathJsonWireValue = None
    location: PathJsonWireValue = None
    strategy_description: PathJsonWireValue = None
    status: PathJsonWireValue = None
    progress: PathJsonWireValue = None


PathJsonOperationWire: TypeAlias = (
    PathJsonAddOperation | PathJsonUpdateOperation | SkipJsonSchema[PathJsonLegacyOperation]
)


class PathJsonOperation:
    """Compatibility facade for callers that construct one path operation.

    The model-facing ``PathJsonAction`` uses ``PathJsonOperationWire`` directly
    so its advertised schema contains only the typed add/update branches.  This
    facade preserves the old ``PathJsonOperation(...)`` and
    ``PathJsonOperation.model_validate(...)`` conveniences for local callers;
    both return one of the permissive runtime operation models above.
    """

    _adapter = TypeAdapter(PathJsonOperationWire)

    def __new__(cls, **values: Any) -> PathJsonOperationWire:
        return cls._adapter.validate_python(values)

    @classmethod
    def model_validate(cls, value: Any) -> PathJsonOperationWire:
        return cls._adapter.validate_python(value)


class PathJsonAction(BaseModel):
    """All model-supplied path-tree mutations for one browser decision.

    The protocol deliberately has no operation-count cap; only semantic path
    validation decides whether a requested incremental mutation is accepted.
    """

    model_config = ConfigDict(extra='allow', strict=True)

    operations: list[PathJsonOperationWire] = Field(default_factory=list)


@dataclass(frozen=True, slots=True)
class CanonicalPathJsonOperation:
    """The only mutation representation accepted by the append-only tree."""

    op: PathJsonOperationName
    path_id: str | None = None
    parent_path_id: str | None = None
    location: str | None = None
    strategy_description: str | None = None
    status: ExplorationPathStatus | None = None
    progress: str | None = None
    mutable_fields: frozenset[str] = frozenset()

    def payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {'op': self.op}
        for field_name in (
            'path_id',
            'parent_path_id',
            'location',
            'strategy_description',
            'status',
            'progress',
        ):
            value = getattr(self, field_name)
            if value is not None:
                payload[field_name] = value
        return payload


@dataclass(frozen=True, slots=True)
class PathJsonOperationResult:
    """Auditable result of one requested path operation."""

    index: int
    requested_op: str
    applied: bool
    canonical_operation: dict[str, Any] | None = None
    ignored_fields: tuple[str, ...] = ()
    retired_fields: tuple[str, ...] = ()
    normalized_fields: tuple[str, ...] = ()
    reason: str | None = None

    def payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            'index': self.index,
            'requested_op': self.requested_op,
            'applied': self.applied,
        }
        if self.canonical_operation is not None:
            payload['canonical_operation'] = self.canonical_operation
        if self.ignored_fields:
            payload['ignored_fields'] = list(self.ignored_fields)
        if self.retired_fields:
            payload['retired_fields'] = list(self.retired_fields)
        if self.normalized_fields:
            payload['normalized_fields'] = list(self.normalized_fields)
        if self.reason:
            payload['reason'] = self.reason
        return payload


@dataclass(frozen=True, slots=True)
class PathJsonActionResult:
    """Result of applying a batch and selecting its active current path."""

    operations: tuple[PathJsonOperationResult, ...]
    blocked_reason: str | None = None
    answer_priority_mode: bool = False

    @property
    def blocked(self) -> bool:
        return self.blocked_reason is not None

    @property
    def has_unapplied_operations(self) -> bool:
        """Whether at least one requested mutation was rejected."""

        return any(not operation.applied for operation in self.operations)

    def payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {'operations': [operation.payload() for operation in self.operations]}
        if self.blocked_reason:
            payload['blocked_reason'] = self.blocked_reason
        if self.answer_priority_mode:
            payload['answer_priority_mode'] = True
        return payload

    def feedback(self, *, limit: int = 1_000) -> str:
        """Return a compact, model-facing summary without retaining extra values."""

        messages: list[str] = []
        if self.blocked_reason:
            messages.append(f'path tree update blocked: {self.blocked_reason}')
        for operation in self.operations:
            details: list[str] = []
            if operation.ignored_fields:
                details.append('ignored ' + ', '.join(operation.ignored_fields))
            if operation.retired_fields:
                details.append('retired fields ' + ', '.join(operation.retired_fields))
            if operation.normalized_fields:
                details.append('normalized ' + ', '.join(operation.normalized_fields))
            if operation.reason:
                details.append('not applied: ' + operation.reason)
            if details:
                messages.append(f'path operation {operation.index} ({operation.requested_op}): ' + '; '.join(details))
        rendered = ' | '.join(messages)
        return rendered[:limit]


@dataclass(frozen=True, slots=True)
class ExplorationReviewRequest:
    """A required full tree review at one browser observation."""

    trigger: ExplorationReviewTrigger
    completed_decisions: int


@dataclass(frozen=True, slots=True)
class ExplorationDecisionRecord:
    """The lifecycle effects of recording one model decision."""

    completed_decisions: int
    consecutive_no_progress: int
    consider_switch: bool
    path_failed: bool


def page_identity(value: object) -> str | None:
    """Return a canonical observed logical-page identity.

    Hash-based single-page applications use fragments as their router state, while
    ordinary documents use them as in-page anchors. Preserve route-like fragments
    and discard ordinary anchors so both cases receive useful exploration reviews.
    """

    url = str(value or '').strip()
    if not url or url == ':':
        return None
    parts = urlsplit(url)
    if not (parts.scheme or parts.netloc or parts.path):
        return None
    fragment = parts.fragment
    if fragment.startswith(('/', '!')) or '?' in fragment or '=' in fragment:
        return parts.geturl()
    return parts._replace(fragment='').geturl()


class ExplorationPathTracker:
    """Maintain one task's complete append-only exploration path tree."""

    def __init__(
        self,
        *,
        task_id: str,
        on_change: Callable[[Mapping[str, Any]], None] | None = None,
    ) -> None:
        normalized_task_id = task_id.strip()
        if not normalized_task_id:
            raise ExplorationPathError('task_id must not be empty')
        self._tree: dict[str, Any] = {
            'schema_version': EXPLORATION_PATH_SCHEMA_VERSION,
            'task_id': normalized_task_id,
            'paths': [],
        }
        self._on_change = on_change
        self._completed_decisions = 0
        self._last_review_decision = 0
        self._review_decision_numbers: list[int] = []
        self._seen_pages: set[str] = set()
        self._initial_review_requested = False
        self._pending_review: ExplorationReviewRequest | None = None
        self._current_observation_is_unseen_page = False
        self._active_path_id: str | None = None
        self._stagnation_path_id: str | None = None
        self._consecutive_no_progress = 0
        self._answer_priority_mode = False

    @property
    def completed_decisions(self) -> int:
        return self._completed_decisions

    @property
    def active_path_id(self) -> str | None:
        return self._active_path_id

    @property
    def pending_review(self) -> ExplorationReviewRequest | None:
        return self._pending_review

    @property
    def current_observation_is_unseen_page(self) -> bool:
        return self._current_observation_is_unseen_page

    @property
    def consecutive_no_progress(self) -> int:
        return self._consecutive_no_progress

    @property
    def answer_priority_mode(self) -> bool:
        """Whether a succeeded route has retired path-tree protocol for this task."""

        return self._answer_priority_mode

    def payload(self) -> dict[str, Any]:
        """Return the full tree without summarisation or mutation aliases."""

        return deepcopy(self._tree)

    def ensure_system_initial_path(self, *, start_url: object) -> bool:
        """Create the executor-owned first root after an observed initial page.

        The first root removes an otherwise model-dependent bootstrap condition:
        the first AgentDecision always receives an existing path ``"1"`` to select
        or update. It is a permanent in-progress task anchor; concrete route
        progress belongs to its child paths. Calling this method again is a no-op
        so callers may safely invoke it for every observation.

        Returns ``True`` only when this call created the root.
        """

        if self._tree['paths']:
            return False
        observed_start_url = page_identity(start_url)
        if observed_start_url is None:
            raise ExplorationPathError('system initial path requires an observed non-placeholder start_url')
        self._tree['paths'].append(
            {
                'path_id': SYSTEM_INITIAL_PATH_ID,
                'start_url': observed_start_url,
                'location': SYSTEM_INITIAL_PATH_LOCATION,
                'strategy_description': SYSTEM_INITIAL_PATH_STRATEGY,
                'status': 'in_progress',
                'progress': SYSTEM_INITIAL_PATH_PROGRESS,
                'children': [],
            }
        )
        self._active_path_id = SYSTEM_INITIAL_PATH_ID
        self._changed()
        return True

    def review_request(self, *, current_page_url: object) -> ExplorationReviewRequest | None:
        """Return a pending initial, unseen-page, or periodic full review."""

        if self._answer_priority_mode:
            return None
        if self._pending_review is not None:
            return self._pending_review

        identity = page_identity(current_page_url)
        if identity is None:
            self._current_observation_is_unseen_page = False
            return None

        if not self._initial_review_requested:
            self._initial_review_requested = True
            self._seen_pages.add(identity)
            self._current_observation_is_unseen_page = True
            self._pending_review = ExplorationReviewRequest('initial_page', self._completed_decisions)
            return self._pending_review

        if identity not in self._seen_pages:
            self._seen_pages.add(identity)
            self._current_observation_is_unseen_page = True
            self._reset_stagnation()
            if self._review_is_allowed():
                self._pending_review = ExplorationReviewRequest('unseen_page', self._completed_decisions)
                return self._pending_review
            return None

        self._current_observation_is_unseen_page = False
        if self._completed_decisions - self._last_review_decision >= EXPLORATION_REVIEW_INTERVAL and self._review_is_allowed():
            self._pending_review = ExplorationReviewRequest('periodic', self._completed_decisions)
            return self._pending_review
        return None

    @staticmethod
    def validate_initial_page_review(
        review: ExplorationReviewRequest | None,
        action: PathJsonAction,
    ) -> PathJsonActionResult | None:
        """Require one concrete route when completing the first-page review.

        This deliberately adds only the agreed minimum invariant.  Other path
        mutations remain subject to the ordinary per-operation validation below.
        """

        if review is None or review.trigger != 'initial_page':
            return None
        if any(operation.op == 'add' for operation in action.operations):
            return None
        return PathJsonActionResult((), 'initial page exploration review requires at least one add operation')

    def apply_path_json_action_and_activate(
        self,
        action: PathJsonAction,
        *,
        start_url: object,
        current_path_id: str,
        progress: str,
    ) -> PathJsonActionResult:
        """Apply safe operations once, then activate a trustworthy current path.

        A malformed individual operation is reported and skipped. Conditions that
        make the active path or reported progress untrustworthy block the browser
        action and leave the durable tree unchanged.
        """

        if not isinstance(action, PathJsonAction):
            return PathJsonActionResult((), 'path_json_action must be a PathJsonAction value')

        observed_start_url = page_identity(start_url)
        if observed_start_url is None:
            return self._blocked_result(action, 'path JSON actions require an observed non-placeholder start_url')

        candidate = deepcopy(self._tree)
        tree_was_empty = not self._tree['paths']
        results: list[PathJsonOperationResult] = []
        entered_answer_priority = False
        for index, operation in enumerate(action.operations):
            if entered_answer_priority:
                results.append(
                    PathJsonOperationResult(
                        index=index,
                        requested_op=self._requested_op(operation),
                        applied=False,
                        reason='not processed after a path entered answer-priority mode',
                    )
                )
                continue
            canonical, result = self._canonicalize_operation(index, operation)
            if canonical is None:
                results.append(result)
                continue
            if tree_was_empty and canonical.op == 'update':
                results.append(
                    PathJsonOperationResult(
                        index=index,
                        requested_op=result.requested_op,
                        applied=False,
                        canonical_operation=canonical.payload(),
                        ignored_fields=result.ignored_fields,
                        normalized_fields=result.normalized_fields,
                        reason='empty exploration tree accepts add operations only; update is forbidden until a later decision',
                    )
                )
                continue
            try:
                if canonical.op == 'add':
                    self._apply_add(candidate, canonical, observed_start_url)
                else:
                    self._apply_update(candidate, canonical)
            except ExplorationPathError as exc:
                results.append(
                    PathJsonOperationResult(
                        index=index,
                        requested_op=result.requested_op,
                        applied=False,
                        canonical_operation=canonical.payload(),
                        ignored_fields=result.ignored_fields,
                        normalized_fields=result.normalized_fields,
                        reason=str(exc),
                    )
                )
                continue
            results.append(
                PathJsonOperationResult(
                    index=index,
                    requested_op=result.requested_op,
                    applied=True,
                    canonical_operation=canonical.payload(),
                    ignored_fields=result.ignored_fields,
                    normalized_fields=result.normalized_fields,
                )
            )

            if canonical.op == 'update' and canonical.status == 'succeeded':
                entered_answer_priority = True

        # A root-protection violation is a planning-contract error, not a
        # partially applicable route update. Keep the durable tree untouched so
        # the retry is based on exactly the state the model last saw.
        if any(
            not result.applied and 'system initial root "1" is immutable' in (result.reason or '')
            for result in results
        ):
            return PathJsonActionResult(
                tuple(
                    replace(
                        result,
                        applied=False,
                        reason='not applied because this batch attempted to change immutable system initial root "1"',
                    )
                    if result.applied
                    else result
                    for result in results
                )
            )

        if entered_answer_priority:
            # A successful route confirms that the agent has reached the answer
            # location.  Persist all preceding valid mutations, then retire path
            # protocol without trying to activate the now-terminal current route.
            self._tree = candidate
            self._active_path_id = None
            self._pending_review = None
            self._current_observation_is_unseen_page = False
            self._answer_priority_mode = True
            self._reset_stagnation()
            self._changed()
            return PathJsonActionResult(tuple(results), answer_priority_mode=True)

        if not isinstance(progress, str) or not progress.strip():
            return self._blocked_result(action, 'progress must be a non-empty string')
        if self._current_observation_is_unseen_page and progress.strip() == '无':
            return self._blocked_result(action, 'a previously unseen page must be recorded as progress, not "无"')
        if not isinstance(current_path_id, str) or not current_path_id.strip():
            return self._blocked_result(action, 'current_path_id must be a non-empty string')

        normalized_current_path_id = current_path_id.strip()
        node = self._find_path_in(candidate, normalized_current_path_id)
        if node is None:
            return PathJsonActionResult(tuple(results), f'current_path_id does not exist: {normalized_current_path_id!r}')
        status = node['status']
        if status in {'failed', 'succeeded'}:
            return PathJsonActionResult(
                tuple(results), f'current_path_id is terminal: {normalized_current_path_id!r} ({status})'
            )
        self._deactivate_previous_path(
            candidate,
            previous_path_id=self._active_path_id,
            next_path_id=normalized_current_path_id,
        )
        self._activate_path_chain(candidate, normalized_current_path_id)
        self._tree = candidate
        self._active_path_id = normalized_current_path_id
        self._changed()
        return PathJsonActionResult(tuple(results))

    def accept_review(self, request: ExplorationReviewRequest | None) -> None:
        """Mark a required full review satisfied after its updates were applied."""

        if request is None:
            return
        if request != self._pending_review:
            raise ExplorationPathError('review request is not pending')
        self._pending_review = None
        self._last_review_decision = self._completed_decisions
        if request.trigger != 'initial_page':
            self._review_decision_numbers.append(self._completed_decisions)

    def activate(self, path_id: str) -> None:
        """Associate the next action with one non-terminal tree node."""

        node = self._find_path(path_id)
        if node is None:
            raise ExplorationPathError(f'current_path_id does not exist: {path_id!r}')
        status = node['status']
        if status in {'failed', 'succeeded'}:
            raise ExplorationPathError(f'current_path_id is terminal: {path_id!r} ({status})')
        self._deactivate_previous_path(
            self._tree,
            previous_path_id=self._active_path_id,
            next_path_id=path_id,
        )
        self._activate_path_chain(self._tree, path_id)
        self._active_path_id = path_id
        self._changed()

    def record_decision(self, *, current_path_id: str, progress: str) -> ExplorationDecisionRecord:
        """Commit one completed model decision and update node-local progress."""

        if current_path_id != self._active_path_id:
            raise ExplorationPathError('recorded current_path_id is not active')
        node = self._find_path(current_path_id)
        if node is None:
            raise ExplorationPathError(f'current_path_id does not exist: {current_path_id!r}')
        if not isinstance(progress, str) or not progress.strip():
            raise ExplorationPathError('progress must be a non-empty string')
        normalized_progress = progress.strip()

        if self._stagnation_path_id != current_path_id:
            self._stagnation_path_id = current_path_id
            self._consecutive_no_progress = 0
        if normalized_progress == '无':
            self._consecutive_no_progress += 1
        else:
            if not self._is_system_initial_path(node):
                node['progress'] = normalized_progress
            self._consecutive_no_progress = 0

        self._completed_decisions += 1
        path_failed = False
        if self._consecutive_no_progress >= 10 and not self._is_system_initial_path(node):
            node['status'] = 'failed'
            node['progress'] = '连续十次决策没有有效进展，判定该探索路径无法继续。'
            path_failed = True
        self._changed()
        return ExplorationDecisionRecord(
            completed_decisions=self._completed_decisions,
            consecutive_no_progress=self._consecutive_no_progress,
            consider_switch=self._consecutive_no_progress >= 5,
            path_failed=path_failed,
        )

    def _blocked_result(self, action: PathJsonAction, reason: str) -> PathJsonActionResult:
        results: list[PathJsonOperationResult] = []
        for index, operation in enumerate(action.operations):
            retired_fields = self._retired_fields(operation)
            if retired_fields:
                extra_fields = set(operation.model_extra or {})
                results.append(
                    PathJsonOperationResult(
                        index=index,
                        requested_op=self._requested_op(operation),
                        applied=False,
                        ignored_fields=tuple(sorted(extra_fields - set(retired_fields))),
                        retired_fields=retired_fields,
                        reason=self._retired_field_reason(retired_fields),
                    )
                )
                continue
            results.append(
                PathJsonOperationResult(
                    index=index,
                    requested_op=self._requested_op(operation),
                    applied=False,
                    reason='not processed',
                )
            )
        return PathJsonActionResult(tuple(results), reason)

    def _canonicalize_operation(
        self, index: int, operation: PathJsonOperationWire
    ) -> tuple[CanonicalPathJsonOperation | None, PathJsonOperationResult]:
        requested_op = self._requested_op(operation)
        extra_fields = set(operation.model_extra or {})
        retired_fields = self._retired_fields(operation)
        ignored = extra_fields - set(retired_fields)
        normalized: set[str] = set()

        # These fields existed in schema v1.  Preserve their names, but never
        # their values, in the diagnostic so a stale model response is repaired
        # in the same decision instead of being silently accepted.
        if retired_fields:
            return None, PathJsonOperationResult(
                index=index,
                requested_op=requested_op,
                applied=False,
                ignored_fields=tuple(sorted(ignored)),
                retired_fields=retired_fields,
                reason=self._retired_field_reason(retired_fields),
            )

        op = self._normalized_string(operation.op, 'op', normalized, max_length=32)
        if op is not None:
            normalized_op = op.casefold()
            if normalized_op != op:
                normalized.add('op')
            op = normalized_op
        if op not in {'add', 'update'}:
            return None, PathJsonOperationResult(
                index=index,
                requested_op=requested_op,
                applied=False,
                ignored_fields=tuple(sorted(ignored)),
                normalized_fields=tuple(sorted(normalized)),
                reason='op must be "add" or "update"',
            )

        if op == 'add':
            ignored.update(field_name for field_name in _ADD_EXECUTOR_FIELDS if field_name in operation.model_fields_set)
            parent_path_id = self._normalized_optional_string(operation.parent_path_id, 'parent_path_id', normalized)
            if operation.parent_path_id is not None and parent_path_id is None:
                return None, self._invalid_operation_result(
                    index, requested_op, ignored, normalized, 'parent_path_id must be a non-empty string or null'
                )
            values: dict[str, str] = {}
            for field_name in ('location', 'strategy_description'):
                value = self._normalized_required_string(getattr(operation, field_name), field_name, normalized)
                if value is None:
                    return None, self._invalid_operation_result(
                        index, requested_op, ignored, normalized, f'add requires a non-empty string {field_name}'
                    )
                values[field_name] = value
            if self._is_url_like_location(values['location']):
                return None, self._invalid_operation_result(
                    index, requested_op, ignored, normalized, 'location must be a semantic page-position label, not a URL'
                )
            canonical = CanonicalPathJsonOperation(
                op='add',
                parent_path_id=parent_path_id,
                location=values['location'],
                strategy_description=values['strategy_description'],
            )
        else:
            if 'parent_path_id' in operation.model_fields_set or 'parent_path_id' in extra_fields:
                ignored.add('parent_path_id')
            location_value = getattr(operation, 'location', None)
            if ('location' in operation.model_fields_set or 'location' in extra_fields) and location_value is not None:
                return None, self._invalid_operation_result(
                    index,
                    requested_op,
                    ignored,
                    normalized,
                    'location is immutable after add and may not be updated',
                )
            if 'strategy_description' in operation.model_fields_set or 'strategy_description' in extra_fields:
                return None, self._invalid_operation_result(
                    index,
                    requested_op,
                    ignored,
                    normalized,
                    'strategy_description is immutable after add; create a new path with add for another route',
                )
            path_id = self._normalized_required_string(operation.path_id, 'path_id', normalized)
            if path_id is None:
                return None, self._invalid_operation_result(index, requested_op, ignored, normalized, 'update requires path_id')
            changes: dict[str, Any] = {}
            for field_name in ('progress',):
                if field_name not in operation.model_fields_set:
                    continue
                raw_value = getattr(operation, field_name)
                # Strict structured-output schemas may return null for every
                # field not being changed.  Null is an omitted placeholder,
                # not an instruction to erase a path property.
                if raw_value is None:
                    continue
                value = self._normalized_optional_string(raw_value, field_name, normalized)
                if value is None:
                    return None, self._invalid_operation_result(
                        index, requested_op, ignored, normalized, f'{field_name} must be a non-empty string or omitted'
                    )
                changes[field_name] = value
            if 'status' in operation.model_fields_set:
                if operation.status is None:
                    # See the null-placeholder rule above.  A status change
                    # must use one of the explicit lifecycle values.
                    status = None
                else:
                    status = self._normalized_string(operation.status, 'status', normalized, max_length=32)
                if status is None:
                    if operation.status is None:
                        pass
                    else:
                        return None, self._invalid_operation_result(
                            index,
                            requested_op,
                            ignored,
                            normalized,
                            'status must be pending, in_progress, failed, or succeeded, or omitted',
                        )
                else:
                    if status not in EXPLORATION_PATH_STATUSES:
                        return None, self._invalid_operation_result(
                            index, requested_op, ignored, normalized, 'status must be pending, in_progress, failed, or succeeded'
                        )
                    changes['status'] = status
            if not changes:
                return None, self._invalid_operation_result(
                    index, requested_op, ignored, normalized, 'update requires at least one mutable path property'
                )
            existing_node = self._find_path(path_id)
            if changes.get('status') == 'failed' and existing_node is not None and self._is_system_initial_path(existing_node):
                return None, self._invalid_operation_result(
                    index,
                    requested_op,
                    ignored,
                    normalized,
                    f'system initial root "1" is immutable: status must remain in_progress and progress must remain "{SYSTEM_INITIAL_PATH_PROGRESS}"; find another exploration path instead',
                )
            if changes.get('status') == 'failed':
                failure_progress = changes.get('progress')
                if not isinstance(failure_progress, str) or not failure_progress.strip() or failure_progress.strip() == '无':
                    return None, self._invalid_operation_result(
                        index,
                        requested_op,
                        ignored,
                        normalized,
                        'marking a path failed requires a non-empty progress reason other than "无"',
                    )
            canonical = CanonicalPathJsonOperation(
                op='update',
                path_id=path_id,
                status=changes.get('status'),
                progress=changes.get('progress'),
                mutable_fields=frozenset(changes),
            )

        return canonical, PathJsonOperationResult(
            index=index,
            requested_op=requested_op,
            applied=False,
            ignored_fields=tuple(sorted(ignored)),
            normalized_fields=tuple(sorted(normalized)),
        )

    @staticmethod
    def _requested_op(operation: PathJsonOperationWire) -> str:
        return operation.op[:128] if isinstance(operation.op, str) else f'<{type(operation.op).__name__}>'

    @staticmethod
    def _retired_fields(operation: PathJsonOperationWire) -> tuple[str, ...]:
        return tuple(sorted(set(operation.model_extra or {}) & _RETIRED_PATH_OPERATION_FIELDS))

    @staticmethod
    def _retired_field_reason(retired_fields: Sequence[str]) -> str:
        return (
            'retired path operation fields: '
            + ', '.join(retired_fields)
            + '; remove them and use strategy_description for the exploration strategy'
        )

    @staticmethod
    def _normalized_string(value: Any, field_name: str, normalized: set[str], *, max_length: int) -> str | None:
        if not isinstance(value, str):
            return None
        stripped = value.strip()
        if stripped != value:
            normalized.add(field_name)
        if not stripped or len(stripped) > max_length:
            return None
        return stripped

    @staticmethod
    def _is_url_like_location(value: str) -> bool:
        """Keep model-owned location labels distinct from executor-owned URLs."""

        parts = urlsplit(value)
        return bool(
            parts.scheme
            or parts.netloc
            or value.startswith('//')
            or value.casefold().startswith('www.')
        )

    @classmethod
    def _normalized_optional_string(cls, value: Any, field_name: str, normalized: set[str]) -> str | None:
        if value is None:
            return None
        return cls._normalized_string(value, field_name, normalized, max_length=_MAX_FIELD_LENGTHS[field_name])

    @classmethod
    def _normalized_required_string(cls, value: Any, field_name: str, normalized: set[str]) -> str | None:
        return cls._normalized_string(value, field_name, normalized, max_length=_MAX_FIELD_LENGTHS[field_name])

    @staticmethod
    def _invalid_operation_result(
        index: int,
        requested_op: str,
        ignored: set[str],
        normalized: set[str],
        reason: str,
    ) -> PathJsonOperationResult:
        return PathJsonOperationResult(
            index=index,
            requested_op=requested_op,
            applied=False,
            ignored_fields=tuple(sorted(ignored)),
            normalized_fields=tuple(sorted(normalized)),
            reason=reason,
        )

    def _review_is_allowed(self) -> bool:
        first_recent_decision = max(1, self._completed_decisions - EXPLORATION_REVIEW_RECENT_DECISION_WINDOW + 1)
        recent_count = sum(decision >= first_recent_decision for decision in self._review_decision_numbers)
        return recent_count < EXPLORATION_REVIEW_MAX_IN_WINDOW

    def _reset_stagnation(self) -> None:
        self._stagnation_path_id = None
        self._consecutive_no_progress = 0

    def _changed(self) -> None:
        if self._on_change is not None:
            self._on_change(self.payload())

    @staticmethod
    def _new_node(operation: CanonicalPathJsonOperation, *, path_id: str, start_url: str) -> dict[str, Any]:
        return {
            'path_id': path_id,
            'start_url': start_url,
            'location': operation.location,
            'strategy_description': operation.strategy_description,
            'status': 'pending',
            'progress': None,
            'children': [],
        }

    def _apply_add(self, tree: dict[str, Any], operation: CanonicalPathJsonOperation, start_url: str) -> None:
        if operation.parent_path_id is None:
            children = tree['paths']
            path_id = str(len(children) + 1)
        else:
            parent = self._find_path_in(tree, operation.parent_path_id)
            if parent is None:
                raise ExplorationPathError(f'parent_path_id does not exist: {operation.parent_path_id!r}')
            children = parent['children']
            path_id = f"{parent['path_id']}->{len(children) + 1}"
        children.append(self._new_node(operation, path_id=path_id, start_url=start_url))

    def _apply_update(self, tree: dict[str, Any], operation: CanonicalPathJsonOperation) -> None:
        node = self._find_path_in(tree, operation.path_id or '')
        if node is None:
            raise ExplorationPathError(f'path_id does not exist: {operation.path_id!r}')
        if self._is_system_initial_path(node):
            if 'status' in operation.mutable_fields and operation.status != 'in_progress':
                raise ExplorationPathError(
                    f'system initial root "1" is immutable: status must remain in_progress and progress must remain "{SYSTEM_INITIAL_PATH_PROGRESS}"; find another exploration path instead'
                )
            if 'progress' in operation.mutable_fields and operation.progress != SYSTEM_INITIAL_PATH_PROGRESS:
                raise ExplorationPathError(
                    f'system initial root "1" is immutable: status must remain in_progress and progress must remain "{SYSTEM_INITIAL_PATH_PROGRESS}"; find another exploration path instead'
                )
            return
        for field_name in operation.mutable_fields:
            value = getattr(operation, field_name)
            if field_name == 'progress' and value in {None, '无'}:
                continue
            if field_name == 'status':
                self._validate_status_transition(node, value)
            node[field_name] = value

    @classmethod
    def _deactivate_previous_path(
        cls,
        tree: dict[str, Any],
        *,
        previous_path_id: str | None,
        next_path_id: str,
    ) -> None:
        """Retire only the old active branch that is outside the new branch.

        A parent remains ``in_progress`` while one of its children is active.
        A sibling switch returns the old unfinished leaf (and any abandoned
        descendants) to ``pending``; an explicit ``failed`` update remains
        terminal and is never reopened here. The system root is always kept
        ``in_progress``.
        """

        if previous_path_id is None or previous_path_id == next_path_id:
            return
        previous_chain = cls._path_chain_in(tree, previous_path_id)
        next_chain = set(cls._path_chain_in(tree, next_path_id))
        for path_id in previous_chain:
            if path_id in next_chain:
                continue
            node = cls._find_path_in(tree, path_id)
            if node is not None and node['status'] == 'in_progress' and not cls._is_system_initial_path(node):
                node['status'] = 'pending'

    @classmethod
    def _activate_path_chain(cls, tree: dict[str, Any], path_id: str) -> None:
        """Mark the selected path and all of its ancestors as in progress."""

        for ancestor_id in cls._path_chain_in(tree, path_id):
            node = cls._find_path_in(tree, ancestor_id)
            if node is not None and node['status'] == 'pending':
                node['status'] = 'in_progress'

    @classmethod
    def _path_chain_in(cls, tree: Mapping[str, Any], path_id: str) -> tuple[str, ...]:
        """Return root-to-node IDs for one path, or an empty tuple if absent."""

        def visit(nodes: Sequence[dict[str, Any]], ancestors: tuple[str, ...]) -> tuple[str, ...] | None:
            for node in nodes:
                chain = ancestors + (str(node['path_id']),)
                if node['path_id'] == path_id:
                    return chain
                found = visit(node['children'], chain)
                if found is not None:
                    return found
            return None

        return visit(tree['paths'], ()) or ()

    @staticmethod
    def _is_system_initial_path(node: Mapping[str, Any]) -> bool:
        return (
            node.get('path_id') == SYSTEM_INITIAL_PATH_ID
            and node.get('location') == SYSTEM_INITIAL_PATH_LOCATION
            and node.get('strategy_description') == SYSTEM_INITIAL_PATH_STRATEGY
        )

    @staticmethod
    def _validate_status_transition(node: Mapping[str, Any], next_status: str | None) -> None:
        if next_status is None:
            raise ExplorationPathError('status may not be null')
        current_status = str(node['status'])
        if current_status in {'failed', 'succeeded'} and next_status != current_status:
            raise ExplorationPathError(f'cannot reopen terminal path status {current_status!r}')
        if current_status == 'pending' and next_status == 'in_progress':
            return
        if current_status == 'in_progress' and next_status == 'pending':
            return
        if current_status == next_status or next_status in {'failed', 'succeeded'}:
            return
        raise ExplorationPathError(f'invalid path status transition {current_status!r} -> {next_status!r}')

    def _find_path(self, path_id: str) -> dict[str, Any] | None:
        return self._find_path_in(self._tree, path_id)

    @classmethod
    def _find_path_in(cls, tree: Mapping[str, Any], path_id: str) -> dict[str, Any] | None:
        def visit(nodes: Sequence[dict[str, Any]]) -> dict[str, Any] | None:
            for node in nodes:
                if node['path_id'] == path_id:
                    return node
                found = visit(node['children'])
                if found is not None:
                    return found
            return None

        return visit(tree['paths'])


__all__ = [
    'CanonicalPathJsonOperation',
    'EXPLORATION_PATH_FILENAME',
    'EXPLORATION_PATH_SCHEMA_VERSION',
    'EXPLORATION_PATH_STATUSES',
    'SYSTEM_INITIAL_PATH_ID',
    'SYSTEM_INITIAL_PATH_LOCATION',
    'SYSTEM_INITIAL_PATH_PROGRESS',
    'SYSTEM_INITIAL_PATH_STRATEGY',
    'EXPLORATION_REVIEW_INTERVAL',
    'EXPLORATION_REVIEW_MAX_IN_WINDOW',
    'EXPLORATION_REVIEW_RECENT_DECISION_WINDOW',
    'ExplorationDecisionRecord',
    'ExplorationPathError',
    'ExplorationPathStatus',
    'ExplorationPathTracker',
    'ExplorationReviewRequest',
    'ExplorationReviewTrigger',
    'PathJsonAction',
    'PathJsonActionResult',
    'PathJsonAddOperation',
    'PathJsonLegacyOperation',
    'PathJsonOperation',
    'PathJsonOperationName',
    'PathJsonOperationResult',
    'PathJsonUpdateOperation',
    'page_identity',
]
