"""Side-effect-free state used by the embedded PandasAI code generator."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List, Optional

from pandasai.config import Config
from pandasai.helpers.memory import Memory


class NullLogger:
    """Discard prompts, samples, and generated code instead of logging them."""

    def log(self, *_args: Any, **_kwargs: Any) -> None:
        return None


@dataclass
class AgentState:
    dfs: List[Any] = field(default_factory=list)
    config: Config | None = None
    memory: Memory = field(default_factory=Memory)
    vectorstore: None = None
    skills: list[Any] = field(default_factory=list)
    intermediate_values: dict[str, Any] = field(default_factory=dict)
    logger: NullLogger = field(default_factory=NullLogger)
    last_code_generated: Optional[str] = None
    last_code_executed: Optional[str] = None
    last_prompt_id: Optional[str] = None
    last_prompt_used: Any = None
    output_type: Optional[str] = None

    def initialize(
        self,
        dfs: Any,
        config: Config | dict[str, Any] | None,
        memory_size: int = 1,
        vectorstore: Any = None,
        description: str | None = None,
    ) -> None:
        if config is None:
            raise ValueError('The embedded PandasAI Agent requires an explicit per-call Config')
        self.dfs = dfs if isinstance(dfs, list) else [dfs]
        self.config = Config(**config) if isinstance(config, dict) else config
        self.memory = Memory(memory_size, agent_description=description)
        self.vectorstore = None
        self.skills = []
        self.logger = NullLogger()

    def reset_intermediate_values(self) -> None:
        self.intermediate_values.clear()
