"""Per-agent configuration for the embedded PandasAI generation core."""

from typing import Optional

from pydantic import BaseModel, ConfigDict

from pandasai.llm.base import LLM


class Config(BaseModel):
    """Configuration supplied directly to each independent Agent instance."""

    save_logs: bool = False
    verbose: bool = False
    max_retries: int = 0
    llm: Optional[LLM] = None
    model_config = ConfigDict(arbitrary_types_allowed=True)
