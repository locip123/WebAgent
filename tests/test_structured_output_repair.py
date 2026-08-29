from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from browser_use.llm.exceptions import ModelStructuredOutputError
from browser_use.llm.views import ChatInvokeCompletion
from browser_use.webretriever.agent import ProtocolIIIAgent
from browser_use.webretriever.browser import BrowserObservation
from browser_use.webretriever.model_services import (
    ModelServiceConfig,
    ModelServiceRouter,
    model_service_group_key,
)
from browser_use.webretriever.model_retry import is_retryable_model_error
from browser_use.webretriever.models import (
    CompetitionTask,
    InitialPageAgentDecisionEnvelope,
)


class _FakeModel:
    def __init__(
        self, name: str, outcomes: Sequence[ChatInvokeCompletion[Any] | Exception]
    ) -> None:
        self.name = name
        self._outcomes = iter(outcomes)
        self.calls = 0

    async def ainvoke(self, *_args: Any, **_kwargs: Any) -> ChatInvokeCompletion[Any]:
        self.calls += 1
        outcome = next(self._outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class _Runtime:
    async def observe(self, step: int) -> BrowserObservation:
        assert step == 0
        return BrowserObservation(
            screenshot=b"",
            url="https://example.test/start",
            title="测试页面",
            tabs=[
                {
                    "index": 0,
                    "url": "https://example.test/start",
                    "title": "测试页面",
                    "active": True,
                }
            ],
            viewport_width=1280,
            viewport_height=720,
            elements=[],
            page_text="页面中已显示可作为答案的事实。",
            recent_network=[],
            downloads=[],
        )


def _successful_finish() -> ChatInvokeCompletion[Any]:
    return ChatInvokeCompletion(
        completion=InitialPageAgentDecisionEnvelope.model_validate(
            {
                "decision": {
                    "action": "finish",
                    "thought": "页面中已经有可验证的答案。",
                    "success": True,
                    "answer": "测试答案",
                    "evidence": ["测试页面中的可见事实。"],
                }
            }
        ),
        raw_completion='{"decision":{"action":"finish"}}',
        usage=None,
    )


def _initial_path_semantic_error() -> ChatInvokeCompletion[Any]:
    return ChatInvokeCompletion(
        completion=InitialPageAgentDecisionEnvelope.model_validate(
            {
                "decision": {
                    "action": "click",
                    "thought": "尝试点击一个入口。",
                    "current_path_id": "1",
                    "decision_summary": "需要先创建探索路径。",
                    "path_json_action": {"operations": []},
                    "element_id": 0,
                }
            }
        ),
        raw_completion='{"decision":{"action":"click"}}',
        usage=None,
    )


def _router(*models: _FakeModel) -> ModelServiceRouter:
    return ModelServiceRouter(
        model="test-model",
        services=(
            ModelServiceConfig(
                name="primary",
                api_base="https://gateway-a.example/v1",
                api_key="test-key",
            ),
            ModelServiceConfig(
                name="alias",
                api_base="https://gateway-a.example/v1/",
                api_key="test-key",
            ),
            ModelServiceConfig(
                name="fallback",
                api_base="https://gateway-b.example/v1",
                api_key="test-key",
            ),
        ),
        clients=models,  # type: ignore[arg-type]
        model_timeout_seconds=1.0,
    )


def _agent(router: ModelServiceRouter, task_dir: Path) -> ProtocolIIIAgent:
    return ProtocolIIIAgent(
        task=CompetitionTask(
            task_idx=0,
            task_id="structured-output-repair",
            website="https://example.test/start",
            task="根据当前页面回答测试问题。",
        ),
        llm=router,
        runtime=_Runtime(),
        task_dir=task_dir,
        max_steps=1,
        model_timeout_seconds=1.0,
        structured_prompt_log=True,
        chart_network_inspector=object(),
    )


def test_agent_repairs_invalid_structured_output_through_another_gateway_group(
    tmp_path: Path,
) -> None:
    async def scenario() -> tuple[
        Any, _FakeModel, _FakeModel, _FakeModel, dict[str, Any]
    ]:
        primary = _FakeModel(
            "primary",
            [
                ModelStructuredOutputError(
                    "invalid JSON in response", raw_completion="**Planning**"
                )
            ],
        )
        alias = _FakeModel(
            "alias", [AssertionError("same gateway alias must be excluded")]
        )
        fallback = _FakeModel("fallback", [_successful_finish()])
        task_dir = tmp_path / "cross-gateway-repair"
        outcome = await _agent(_router(primary, alias, fallback), task_dir).run()
        prompt_log = json.loads(
            (task_dir / "model_prompts.json").read_text(encoding="utf-8")
        )
        return outcome, primary, alias, fallback, prompt_log

    outcome, primary, alias, fallback, prompt_log = asyncio.run(scenario())

    assert outcome.status == "SUCCESS"
    assert (primary.calls, alias.calls, fallback.calls) == (1, 0, 1)
    assert len(prompt_log["steps"]) == 2
    assert prompt_log["steps"][1]["excluded_model_service_groups"] == [
        "https://gateway-a.example/v1"
    ]
    assert (
        "STRUCTURED DECISION REPAIR FEEDBACK"
        in prompt_log["steps"][1]["prompt"]["rendered_text"]
    )


def test_path_semantic_repair_does_not_exclude_the_current_gateway_group(
    tmp_path: Path,
) -> None:
    async def scenario() -> tuple[
        Any, _FakeModel, _FakeModel, _FakeModel, dict[str, Any]
    ]:
        primary = _FakeModel(
            "primary", [_initial_path_semantic_error(), _successful_finish()]
        )
        alias = _FakeModel(
            "alias", [AssertionError("task affinity should retain primary")]
        )
        fallback = _FakeModel(
            "fallback", [AssertionError("semantic error must not switch groups")]
        )
        task_dir = tmp_path / "semantic-repair"
        outcome = await _agent(_router(primary, alias, fallback), task_dir).run()
        prompt_log = json.loads(
            (task_dir / "model_prompts.json").read_text(encoding="utf-8")
        )
        return outcome, primary, alias, fallback, prompt_log

    outcome, primary, alias, fallback, prompt_log = asyncio.run(scenario())

    assert outcome.status == "SUCCESS"
    assert (primary.calls, alias.calls, fallback.calls) == (2, 0, 0)
    assert len(prompt_log["steps"]) == 2
    assert "excluded_model_service_groups" not in prompt_log["steps"][1]
    assert (
        "STRUCTURED DECISION REPAIR FEEDBACK"
        in prompt_log["steps"][1]["prompt"]["rendered_text"]
    )


def test_bare_json_decode_error_in_structured_call_returns_to_agent_without_router_fallback(
    tmp_path: Path,
) -> None:
    async def scenario() -> tuple[Any, _FakeModel, _FakeModel, _FakeModel, dict[str, Any]]:
        primary = _FakeModel(
            "primary",
            [json.JSONDecodeError("invalid response", "not-json", 0)],
        )
        alias = _FakeModel("alias", [AssertionError("same gateway alias must be excluded")])
        fallback = _FakeModel("fallback", [_successful_finish()])
        task_dir = tmp_path / "bare-json-error"
        outcome = await _agent(_router(primary, alias, fallback), task_dir).run()
        prompt_log = json.loads((task_dir / "model_prompts.json").read_text(encoding="utf-8"))
        return outcome, primary, alias, fallback, prompt_log

    outcome, primary, alias, fallback, prompt_log = asyncio.run(scenario())

    assert outcome.status == "SUCCESS"
    assert (primary.calls, alias.calls, fallback.calls) == (1, 0, 1)
    assert prompt_log["steps"][1]["excluded_model_service_groups"] == [
        model_service_group_key("https://gateway-a.example/v1")
    ]
    assert is_retryable_model_error(
        json.JSONDecodeError("invalid response", "not-json", 0)
    )
    assert not is_retryable_model_error(
        json.JSONDecodeError("invalid response", "not-json", 0),
        structured_output=True,
    )
