"""WebRetriever Challenge Protocol III adapter.

The adapter deliberately keeps its browser runtime separate from Browser Use's
native CDP driver: the competition requires every browser operation to go
through Playwright.
"""

from browser_use.webretriever.agent import AgentRunOutcome, ProtocolIIIAgent
from browser_use.webretriever.browser import BrowserObservation, BrowserRuntime, ElementRef
from browser_use.webretriever.models import AgentDecision, CompetitionTask, WebRetrieverActionResult, load_tasks

__all__ = [
	'AgentDecision',
	'AgentRunOutcome',
	'BrowserObservation',
	'BrowserRuntime',
	'CompetitionTask',
	'ElementRef',
	'ProtocolIIIAgent',
	'WebRetrieverActionResult',
	'load_tasks',
]
