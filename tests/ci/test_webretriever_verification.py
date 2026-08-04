from __future__ import annotations

from browser_use.webretriever.browser import BrowserObservation, ElementRef
from browser_use.webretriever.verification import VerificationAction, VerificationController, VerificationState


def _observation(
	*,
	title: str = 'Just a moment...',
	page_text: str = 'Verify you are human',
	url: str = 'https://example.com/search',
	elements: list[ElementRef] | None = None,
) -> BrowserObservation:
	return BrowserObservation(
		screenshot=b'png',
		url=url,
		title=title,
		tabs=[{'index': 0, 'title': title, 'url': url, 'active': True}],
		viewport_width=1440,
		viewport_height=900,
		elements=elements or [],
		page_text=page_text,
		recent_network=[],
		downloads=[],
	)


def test_visible_verification_control_is_clicked_once_then_processing_is_waited() -> None:
	controller = VerificationController()
	challenge = _observation(
		elements=[
			ElementRef(index=9, tag='iframe', name='Verify you are human', x=20, y=300, width=300, height=80),
		]
	)

	first = controller.decide(challenge)
	second = controller.decide(_observation(title='Just a moment...', page_text='Verifying you are human'))

	assert first.action is VerificationAction.CLICK
	assert first.element_id == 9
	assert first.state is VerificationState.ACTION_REQUIRED
	assert second.action is VerificationAction.WAIT
	assert second.wait_seconds == 3.0
	assert second.state is VerificationState.PROCESSING


def test_verification_episode_passes_only_after_a_stable_target_page_recovery() -> None:
	controller = VerificationController()
	controller.decide(
		_observation(elements=[ElementRef(index=1, tag='iframe', name='Verify you are human', width=300, height=80)])
	)

	confirming = controller.decide(_observation(title='Search', page_text='Results for civil war'))
	passed = controller.decide(_observation(title='Search', page_text='Results for civil war'))

	assert confirming.action is VerificationAction.WAIT
	assert passed.action is VerificationAction.NONE
	assert passed.state is VerificationState.PASSED
	assert controller.challenge_episodes == 1


def test_transient_blank_during_recovery_remains_part_of_the_same_episode() -> None:
	controller = VerificationController()
	challenge = _observation(elements=[ElementRef(index=1, tag='iframe', name='Verify you are human', width=300, height=80)])

	controller.decide(challenge)
	first_recovery = controller.decide(_observation(title='Search', page_text='Results'))
	resumed = controller.decide(challenge)
	second_recovery = controller.decide(_observation(title='Search', page_text='Results'))

	assert first_recovery.action is VerificationAction.WAIT
	assert resumed.action is VerificationAction.WAIT
	assert second_recovery.action is VerificationAction.WAIT
	assert controller.challenge_episodes == 1


def test_recovery_requires_the_target_site_not_an_unrelated_document() -> None:
	controller = VerificationController(target_url='https://example.com/start')
	controller.decide(
		_observation(elements=[ElementRef(index=1, tag='iframe', name='Verify you are human', width=300, height=80)])
	)

	first_foreign = controller.decide(_observation(title='Elsewhere', page_text='Other results', url='https://elsewhere.example/'))
	second_foreign = controller.decide(_observation(title='Elsewhere', page_text='Other results', url='https://elsewhere.example/'))

	assert first_foreign.action is VerificationAction.WAIT
	assert second_foreign.action is VerificationAction.WAIT
	assert controller.challenge_episodes == 1


def test_verification_processing_stops_after_its_bounded_wait_budget() -> None:
	controller = VerificationController(max_wait_observations=2)
	controller.decide(
		_observation(elements=[ElementRef(index=1, tag='iframe', name='Verify you are human', width=300, height=80)])
	)

	first_wait = controller.decide(_observation(title='Just a moment...', page_text='Verifying you are human'))
	second_wait = controller.decide(_observation(title='Just a moment...', page_text='Verifying you are human'))
	blocked = controller.decide(_observation(title='Just a moment...', page_text='Verifying you are human'))

	assert first_wait.action is VerificationAction.WAIT
	assert second_wait.action is VerificationAction.WAIT
	assert blocked.action is VerificationAction.BLOCKED
	assert blocked.state is VerificationState.BLOCKED


def test_challenge_url_without_visible_challenge_state_is_not_counted() -> None:
	controller = VerificationController()

	decision = controller.decide(
		_observation(
			title='Library of Congress',
			page_text='Search the Library of Congress',
			url='https://www.loc.gov/search/?__cf_chl_rt_tk=temporary',
		)
	)

	assert decision.action is VerificationAction.NONE
	assert decision.state is VerificationState.NONE
	assert controller.challenge_episodes == 0


def test_bare_verifying_text_is_a_visible_challenge_episode() -> None:
	controller = VerificationController()

	decision = controller.decide(
		_observation(
			title='Checking your browser',
			page_text='Verifying… This may take a few seconds.',
			elements=[ElementRef(index=1, tag='iframe', width=300, height=80)],
		)
	)

	assert decision.action is VerificationAction.CLICK
	assert controller.challenge_episodes == 1
