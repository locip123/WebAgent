from __future__ import annotations

from typing import TYPE_CHECKING

from .base import BasePrompt
from .generate_python_code_with_sql import GeneratePythonCodeWithSQLPrompt

if TYPE_CHECKING:
    from pandasai.agent.state import AgentState


def get_chat_prompt_for_sql(context: AgentState) -> BasePrompt:
    return GeneratePythonCodeWithSQLPrompt(
        context=context,
        last_code_generated=context.last_code_generated,
        output_type=context.output_type,
    )


__all__ = ['BasePrompt', 'GeneratePythonCodeWithSQLPrompt', 'get_chat_prompt_for_sql']
