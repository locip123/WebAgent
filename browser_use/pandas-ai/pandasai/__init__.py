"""Minimal MIT-licensed PandasAI core embedded for WebRetriever.

Only the per-call dataframe/code-generation surface is exported.  Dataset
connectors, global configuration, telemetry, CLI, charting, and code execution
are intentionally outside this embedded runtime.
"""

from pandasai.agent import Agent
from pandasai.config import Config
from pandasai.dataframe import DataFrame
from pandasai.llm import LLM

from .__version__ import __version__

__all__ = ['Agent', 'Config', 'DataFrame', 'LLM', '__version__']
