"""Versioned local desktop-control-plane boundaries.

Public contracts remain FastAPI-free; implementation modules are loaded only
by the local sidecar entry point and its tests.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
	from browser_use.webretriever.desktop.contracts import RunSpec


def __getattr__(name: str):
	if name != "RunSpec":
		raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
	from browser_use.webretriever.desktop.contracts import RunSpec

	globals()[name] = RunSpec
	return RunSpec


__all__ = ["RunSpec"]
