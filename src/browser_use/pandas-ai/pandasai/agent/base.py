"""Generate-only PandasAI Agent retained from the MIT core."""

from __future__ import annotations

from typing import Any

import pandas as pd

from pandasai.config import Config
from pandasai.core.code_generation.base import CodeGenerator
from pandasai.core.prompts import get_chat_prompt_for_sql
from pandasai.dataframe.base import DataFrame

from .state import AgentState


class Agent:
    """A fresh, self-contained PandasAI code-generation agent.

    WebRetriever deliberately exposes only ``generate_code``.  Generated code
    is validated and executed by WebRetriever's isolated DuckDB boundary, never
    by PandasAI's general-purpose executor.
    """

    def __init__(
        self,
        dfs: DataFrame | pd.DataFrame | list[DataFrame | pd.DataFrame],
        config: Config | dict[str, Any] | None = None,
        memory_size: int = 1,
        description: str | None = None,
        **_unused: Any,
    ) -> None:
        frames = dfs if isinstance(dfs, list) else [dfs]
        if not frames:
            raise ValueError('At least one dataframe is required')
        normalized = [frame if isinstance(frame, DataFrame) else DataFrame(frame) for frame in frames]
        self.description = description
        self._state = AgentState()
        self._state.initialize(normalized, config, memory_size, description=description)
        self._code_generator = CodeGenerator(self._state)

    def generate_code(self, query: str) -> str:
        if not isinstance(query, str) or not query.strip():
            raise ValueError('query must be a non-empty string')
        self._state.memory.add(query, is_user=True)
        prompt = get_chat_prompt_for_sql(self._state)
        code = self._code_generator.generate_code(prompt)
        self._state.last_prompt_used = prompt
        return code

    def clear_memory(self) -> None:
        self._state.memory.clear()
