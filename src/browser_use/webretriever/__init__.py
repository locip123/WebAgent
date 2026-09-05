"""WebRetriever Challenge Protocol III adapter.

The adapter deliberately keeps its browser runtime separate from Browser Use's
native CDP driver: the competition requires every browser operation to go
through Playwright.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
	from browser_use.webretriever.agent import AgentRunOutcome, ProtocolIIIAgent
	from browser_use.webretriever.browser import BrowserObservation, BrowserRuntime, ElementRef
	from browser_use.webretriever.models import AgentDecision, CompetitionTask, WebRetrieverActionResult, load_tasks


_LAZY_IMPORTS = {
	"AgentDecision": ("browser_use.webretriever.models", "AgentDecision"),
	"AgentRunOutcome": ("browser_use.webretriever.agent", "AgentRunOutcome"),
	"BrowserObservation": ("browser_use.webretriever.browser", "BrowserObservation"),
	"BrowserRuntime": ("browser_use.webretriever.browser", "BrowserRuntime"),
	"CompetitionTask": ("browser_use.webretriever.models", "CompetitionTask"),
	"ElementRef": ("browser_use.webretriever.browser", "ElementRef"),
	"ProtocolIIIAgent": ("browser_use.webretriever.agent", "ProtocolIIIAgent"),
	"WebRetrieverActionResult": ("browser_use.webretriever.models", "WebRetrieverActionResult"),
	"load_tasks": ("browser_use.webretriever.models", "load_tasks"),
}


def __getattr__(name: str):
	try:
		module_path, attribute = _LAZY_IMPORTS[name]
	except KeyError as exc:
		raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from exc
	from importlib import import_module

	value = getattr(import_module(module_path), attribute)
	globals()[name] = value
	return value

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
