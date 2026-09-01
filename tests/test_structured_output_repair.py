from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from types import SimpleNamespace

from browser_use.llm.exceptions import ModelStructuredOutputError
from browser_use.llm.views import ChatInvokeCompletion
from browser_use.llm.schema import SchemaOptimizer
from browser_use.webretriever.agent import ProtocolIIIAgent
from browser_use.webretriever.browser import BrowserObservation
from browser_use.webretriever.model_services import (
    ModelServiceConfig,
    ModelServiceRouter,
    model_service_group_key,
)
from browser_use.webretriever.model_retry import is_retryable_model_error
from browser_use.webretriever.models import (
    ACTION_PARAMETER_CONTRACTS,
    AgentDecisionEnvelope,
    CompetitionTask,
    InitialPageAgentDecisionEnvelope,
    WebRetrieverActionResult,
)


class _FakeModel:
    def __init__(
        self, name: str, outcomes: Sequence[ChatInvokeCompletion[Any] | Exception]
    ) -> None:
        self.name = name
        self._outcomes = iter(outcomes)
        self.calls = 0
        self.output_formats: list[Any] = []
        self.system_prompts: list[str] = []
        self.user_prompts: list[str] = []

    async def ainvoke(self, *_args: Any, **_kwargs: Any) -> ChatInvokeCompletion[Any]:
        self.calls += 1
        output_format = _kwargs.get('output_format')
        if output_format is not None:
            self.output_formats.append(output_format)
        if _args and isinstance(_args[0], list) and _args[0]:
            self.system_prompts.append(str(getattr(_args[0][0], 'content', '')))
            if len(_args[0]) > 1:
                content = getattr(_args[0][1], 'content', '')
                if isinstance(content, list) and content:
                    self.user_prompts.append(str(getattr(content[0], 'text', '')))
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


class _RecoveryRuntime:
    def __init__(self, ready_data_dir: Path) -> None:
        self._ready_data_dir = ready_data_dir

    async def observe(self, step: int) -> BrowserObservation:
        downloads: list[dict[str, Any]] = []
        if step == 1:
            downloads.append(
                {
                    'data_artifact': {
                        'status': 'ready',
                        'data_dir': str(self._ready_data_dir),
                        'manifest_sha256': 'a' * 64,
                        'artifact_id': 'download-ready-artifact',
                    }
                }
            )
        return BrowserObservation(
            screenshot=b'',
            url='https://example.test/start',
            title='测试页面',
            tabs=[{'index': 0, 'url': 'https://example.test/start', 'title': '测试页面', 'active': True}],
            viewport_width=1280,
            viewport_height=720,
            elements=[],
            page_text='页面中已显示可作为答案的事实。',
            recent_network=[],
            downloads=downloads,
        )

    async def execute(self, _decision: Any) -> WebRetrieverActionResult:
        return WebRetrieverActionResult(action='wait', status='ok', executed=True, state_changed=False, summary='waited')


class _ExplorationRuntime:
    async def observe(self, step: int) -> BrowserObservation:
        assert step in {0, 1}
        return BrowserObservation(
            screenshot=b'',
            url='https://example.test/start',
            title='测试页面',
            tabs=[{'index': 0, 'url': 'https://example.test/start', 'title': '测试页面', 'active': True}],
            viewport_width=1280,
            viewport_height=720,
            elements=[],
            page_text='页面中已显示可作为答案的事实。',
            recent_network=[],
            downloads=[],
        )

    async def execute(self, _decision: Any) -> WebRetrieverActionResult:
        return WebRetrieverActionResult(action='wait', status='ok', executed=True, state_changed=False, summary='waited')


class _ReadyRuntime:
    def __init__(self, ready_data_dir: Path) -> None:
        self._ready_data_dir = ready_data_dir

    async def observe(self, step: int) -> BrowserObservation:
        assert step == 0
        return BrowserObservation(
            screenshot=b'',
            url='https://example.test/start',
            title='测试页面',
            tabs=[{'index': 0, 'url': 'https://example.test/start', 'title': '测试页面', 'active': True}],
            viewport_width=1280,
            viewport_height=720,
            elements=[],
            page_text='页面中已显示可作为答案的事实。',
            recent_network=[],
            downloads=[
                {
                    'data_artifact': {
                        'status': 'ready',
                        'data_dir': str(self._ready_data_dir),
                        'manifest_sha256': 'b' * 64,
                        'artifact_id': 'initial-ready-artifact',
                    }
                }
            ],
        )


def _successful_finish() -> ChatInvokeCompletion[Any]:
    return ChatInvokeCompletion(
        completion=InitialPageAgentDecisionEnvelope.model_validate(
            {
                "decision": {
                    "action": "finish",
                    "thought": "页面中已经有可验证的答案。",
                    "decision_summary": "起始页尚未执行动作；下一步提交当前页已确认的答案，以完成任务。",
                    "success": True,
                    "answer": "测试答案",
                    "evidence": ["测试页面中的可见事实。"],
                }
            }
        ),
        raw_completion='{"decision":{"action":"finish"}}',
        usage=None,
    )


def _unavailable_analysis_attempt() -> ChatInvokeCompletion[Any]:
    return ChatInvokeCompletion(
        completion=InitialPageAgentDecisionEnvelope.model_validate(
            {
                "decision": {
                    "action": "call_data_analysis_assistant",
                    "thought": "下载文档后尝试交给数据分析助手。",
                    "current_path_id": "1->1",
                    "decision_summary": "文档已下载，准备分析。",
                    "path_json_action": {
                        "operations": [
                            {
                                "op": "add",
                                "parent_path_id": "1",
                                "location": "下载文档",
                                "strategy_description": "下载文档并交给数据分析助手。",
                            }
                        ]
                    },
                    "analysis_query": "提取答案。",
                    "data_dir": "/downloads/report.pdf",
                }
            }
        ),
        raw_completion='{"decision":{"action":"call_data_analysis_assistant"}}',
        usage=None,
    )


def _initial_wait() -> ChatInvokeCompletion[Any]:
    return ChatInvokeCompletion(
        completion=InitialPageAgentDecisionEnvelope.model_validate(
            {
                'decision': {
                    'action': 'wait',
                    'thought': '等待下载工件被浏览器运行时登记。',
                    'current_path_id': '1->1',
                    'decision_summary': '等待已观察的下载完成。',
                    'path_json_action': {
                        'operations': [
                            {
                                'op': 'add',
                                'parent_path_id': '1',
                                'location': '等待下载',
                                'strategy_description': '等待当前下载生成结构化工件。',
                            }
                        ]
                    },
                    'seconds': 1,
                }
            }
        ),
        raw_completion='{"decision":{"action":"wait"}}',
        usage=None,
    )


def _root_update_wait() -> ChatInvokeCompletion[Any]:
    return ChatInvokeCompletion(
        completion=AgentDecisionEnvelope.model_validate(
            {
                'decision': {
                    'action': 'wait',
                    'thought': '错误地把系统锚点当成可更新路径。',
                    'current_path_id': '1->1',
                    'decision_summary': '错误地更新根节点。',
                    'path_json_action': {
                        'operations': [
                            {
                                'op': 'update',
                                'path_id': '1',
                                'status': 'in_progress',
                                'progress': '错误地记录起始页进展。',
                            }
                        ]
                    },
                    'seconds': 1,
                }
            }
        ),
        raw_completion='{"decision":{"action":"wait"}}',
        usage=None,
    )


def _ineligible_data_dir_attempt() -> ChatInvokeCompletion[Any]:
    return ChatInvokeCompletion(
        completion=AgentDecisionEnvelope.model_validate(
            {
                'decision': {
                    'action': 'call_data_analysis_assistant',
                    'thought': '尝试使用下载目录而不是已登记工件。',
                    'current_path_id': '1->1',
                    'decision_summary': '错误地把下载路径作为数据目录。',
                    'path_json_action': {
                        'operations': [
                            {
                                'op': 'update',
                                'path_id': '1->1',
                                'status': 'in_progress',
                                'progress': '错误地宣称文档可以分析。',
                            }
                        ]
                    },
                    'analysis_query': '提取答案。',
                    'data_dir': '/downloads/report.pdf',
                }
            }
        ),
        raw_completion='{"decision":{"action":"call_data_analysis_assistant"}}',
        usage=None,
    )


def _eligible_data_dir_attempt(data_dir: str) -> ChatInvokeCompletion[Any]:
    return ChatInvokeCompletion(
        completion=AgentDecisionEnvelope.model_validate(
            {
                'decision': {
                    'action': 'call_data_analysis_assistant',
                    'thought': '使用运行时登记的数据工件。',
                    'current_path_id': '1->1',
                    'decision_summary': '对已登记工件进行分析。',
                    'analysis_query': '提取答案。',
                    'data_dir': data_dir,
                }
            }
        ),
        raw_completion='{"decision":{"action":"call_data_analysis_assistant"}}',
        usage=None,
    )


class _UnavailableAnalysisAssistant:
    async def execute(self, *, analysis_query: str, data_dir: str) -> Any:
        return SimpleNamespace(
            output=json.dumps(
                {
                    'action': 'call_data_analysis_assistant',
                    'status': 'analysis_unavailable',
                    'analysis_query': analysis_query,
                    'error': 'no tabular input is available',
                }
            ),
            usage={},
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


def test_agent_request_hides_unavailable_analysis_capability_from_model(tmp_path: Path) -> None:
    async def scenario() -> tuple[Any, _FakeModel]:
        model = _FakeModel('direct', [_successful_finish()])
        agent = ProtocolIIIAgent(
            task=CompetitionTask(
                task_idx=0,
                task_id='analysis-capability-gate',
                website='https://example.test/start',
                task='根据当前页面回答测试问题。',
            ),
            llm=model,  # type: ignore[arg-type]
            runtime=_Runtime(),
            task_dir=tmp_path / 'analysis-capability-gate',
            max_steps=1,
            model_timeout_seconds=1.0,
            structured_prompt_log=True,
            chart_network_inspector=object(),
        )
        return await agent.run(), model

    outcome, model = asyncio.run(scenario())

    assert outcome.status == 'SUCCESS'
    assert len(model.output_formats) == 1
    assert 'call_data_analysis_assistant' not in json.dumps(
        SchemaOptimizer.create_optimized_json_schema(model.output_formats[0]), ensure_ascii=False
    )
    assert len(model.system_prompts) == 1
    assert 'call_data_analysis_assistant' not in model.system_prompts[0]
    prompt_log = json.loads((tmp_path / 'analysis-capability-gate' / 'model_prompts.json').read_text(encoding='utf-8'))
    assert prompt_log['steps'][0]['analysis_capability'] == {
        'status': 'unavailable',
        'eligible_data_dirs': [],
    }


def test_unavailable_analysis_attempt_is_rejected_before_path_updates_and_recovers_same_step(
    tmp_path: Path,
) -> None:
    async def scenario() -> tuple[Any, _FakeModel]:
        model = _FakeModel('direct', [_unavailable_analysis_attempt(), _successful_finish()])
        agent = ProtocolIIIAgent(
            task=CompetitionTask(
                task_idx=0,
                task_id='analysis-not-ready-recovery',
                website='https://example.test/start',
                task='根据当前页面回答测试问题。',
            ),
            llm=model,  # type: ignore[arg-type]
            runtime=_Runtime(),
            task_dir=tmp_path / 'analysis-not-ready-recovery',
            max_steps=1,
            model_timeout_seconds=1.0,
            structured_prompt_log=True,
            chart_network_inspector=object(),
        )
        return await agent.run(), model

    outcome, model = asyncio.run(scenario())

    assert outcome.status == 'SUCCESS'
    assert len(model.output_formats) == 2
    assert len(model.system_prompts) == 2
    assert '数据分析助手当前不可用' in model.user_prompts[1]
    rejected_step = outcome.steps[0]
    assert rejected_step['gate_rejected'] is True
    assert json.loads(rejected_step['outcome'])['status'] == 'analysis_not_ready'
    assert rejected_step['path_json_action_result']['operations'] == []


def test_new_ready_data_dir_clears_recovery_and_restores_analysis_capability(tmp_path: Path) -> None:
    async def scenario() -> tuple[Any, _FakeModel, str]:
        task_dir = tmp_path / 'new-ready-data-dir'
        ready_data_dir = task_dir / 'data_artifacts' / 'download-ready-artifact'
        ready_data_dir.mkdir(parents=True)
        model = _FakeModel('direct', [_unavailable_analysis_attempt(), _initial_wait(), _successful_finish()])
        agent = ProtocolIIIAgent(
            task=CompetitionTask(
                task_idx=0,
                task_id='new-ready-data-dir',
                website='https://example.test/start',
                task='根据当前页面回答测试问题。',
            ),
            llm=model,  # type: ignore[arg-type]
            runtime=_RecoveryRuntime(ready_data_dir),
            task_dir=task_dir,
            max_steps=2,
            model_timeout_seconds=1.0,
            structured_prompt_log=True,
            chart_network_inspector=object(),
        )
        return await agent.run(), model, str(ready_data_dir)

    outcome, model, ready_data_dir = asyncio.run(scenario())

    assert outcome.status == 'SUCCESS'
    assert len(model.output_formats) == 3
    third_schema = json.dumps(SchemaOptimizer.create_optimized_json_schema(model.output_formats[2]), ensure_ascii=False)
    assert 'call_data_analysis_assistant' in third_schema
    assert ready_data_dir in third_schema
    assert '===== DATA ANALYSIS RECOVERY STATUS =====' not in model.user_prompts[2]


def test_ineligible_data_dir_repairs_same_step_without_path_or_action_effect(tmp_path: Path) -> None:
    async def scenario() -> tuple[Any, _FakeModel]:
        task_dir = tmp_path / 'ineligible-data-dir'
        ready_data_dir = task_dir / 'data_artifacts' / 'download-ready-artifact'
        ready_data_dir.mkdir(parents=True)
        model = _FakeModel('direct', [_initial_wait(), _ineligible_data_dir_attempt(), _successful_finish()])
        agent = ProtocolIIIAgent(
            task=CompetitionTask(
                task_idx=0,
                task_id='ineligible-data-dir',
                website='https://example.test/start',
                task='根据当前页面回答测试问题。',
            ),
            llm=model,  # type: ignore[arg-type]
            runtime=_RecoveryRuntime(ready_data_dir),
            task_dir=task_dir,
            max_steps=2,
            model_timeout_seconds=1.0,
            structured_prompt_log=True,
            chart_network_inspector=object(),
        )
        return await agent.run(), model

    outcome, model = asyncio.run(scenario())

    assert outcome.status == 'SUCCESS'
    assert len(model.output_formats) == 3
    assert len(outcome.steps) == 2
    assert outcome.steps[0]['action']['action'] == 'wait'
    assert 'STRUCTURED DECISION REPAIR FEEDBACK' in model.user_prompts[2]
    assert '错误地宣称文档可以分析' not in json.dumps(outcome.steps, ensure_ascii=False)


def test_static_analysis_rejection_preserves_diagnostic_then_activates_capability_gate(tmp_path: Path) -> None:
    async def scenario() -> tuple[Any, _FakeModel]:
        task_dir = tmp_path / 'static-analysis-rejection'
        ready_data_dir = task_dir / 'data_artifacts' / 'download-ready-artifact'
        ready_data_dir.mkdir(parents=True)
        model = _FakeModel('direct', [_initial_wait(), _eligible_data_dir_attempt(str(ready_data_dir)), _successful_finish()])
        agent = ProtocolIIIAgent(
            task=CompetitionTask(
                task_idx=0,
                task_id='static-analysis-rejection',
                website='https://example.test/start',
                task='根据当前页面回答测试问题。',
            ),
            llm=model,  # type: ignore[arg-type]
            runtime=_RecoveryRuntime(ready_data_dir),
            task_dir=task_dir,
            max_steps=3,
            model_timeout_seconds=1.0,
            structured_prompt_log=True,
            chart_network_inspector=object(),
            data_analysis_assistant=_UnavailableAnalysisAssistant(),
        )
        return await agent.run(), model

    outcome, model = asyncio.run(scenario())

    assert outcome.status == 'SUCCESS'
    assert json.loads(outcome.steps[1]['outcome'])['status'] == 'analysis_unavailable'
    assert 'call_data_analysis_assistant' not in json.dumps(
        SchemaOptimizer.create_optimized_json_schema(model.output_formats[2]), ensure_ascii=False
    )
    assert '===== DATA ANALYSIS RECOVERY STATUS =====' in model.user_prompts[2]


def test_concurrent_agents_keep_data_analysis_capabilities_isolated(tmp_path: Path) -> None:
    async def scenario() -> tuple[Any, _FakeModel, Any, _FakeModel, str]:
        unavailable_model = _FakeModel('unavailable', [_successful_finish()])
        unavailable_agent = ProtocolIIIAgent(
            task=CompetitionTask(
                task_idx=0,
                task_id='concurrent-unavailable',
                website='https://example.test/start',
                task='根据当前页面回答测试问题。',
            ),
            llm=unavailable_model,  # type: ignore[arg-type]
            runtime=_Runtime(),
            task_dir=tmp_path / 'concurrent-unavailable',
            max_steps=1,
            model_timeout_seconds=1.0,
            chart_network_inspector=object(),
        )
        ready_task_dir = tmp_path / 'concurrent-ready'
        ready_data_dir = ready_task_dir / 'data_artifacts' / 'initial-ready-artifact'
        ready_data_dir.mkdir(parents=True)
        ready_model = _FakeModel('ready', [_successful_finish()])
        ready_agent = ProtocolIIIAgent(
            task=CompetitionTask(
                task_idx=1,
                task_id='concurrent-ready',
                website='https://example.test/start',
                task='根据当前页面回答测试问题。',
            ),
            llm=ready_model,  # type: ignore[arg-type]
            runtime=_ReadyRuntime(ready_data_dir),
            task_dir=ready_task_dir,
            max_steps=1,
            model_timeout_seconds=1.0,
            chart_network_inspector=object(),
        )
        unavailable_outcome, ready_outcome = await asyncio.gather(unavailable_agent.run(), ready_agent.run())
        return unavailable_outcome, unavailable_model, ready_outcome, ready_model, str(ready_data_dir)

    unavailable_outcome, unavailable_model, ready_outcome, ready_model, ready_data_dir = asyncio.run(scenario())

    assert unavailable_outcome.status == ready_outcome.status == 'SUCCESS'
    unavailable_schema = json.dumps(SchemaOptimizer.create_optimized_json_schema(unavailable_model.output_formats[0]))
    ready_schema = json.dumps(SchemaOptimizer.create_optimized_json_schema(ready_model.output_formats[0]))
    assert 'call_data_analysis_assistant' not in unavailable_schema
    assert 'call_data_analysis_assistant' in ready_schema
    assert ready_data_dir in ready_schema
    assert 'call_data_analysis_assistant' in ACTION_PARAMETER_CONTRACTS


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


def test_root_update_repair_hides_update_from_the_same_step_retry(tmp_path: Path) -> None:
    async def scenario() -> tuple[Any, _FakeModel, dict[str, Any]]:
        model = _FakeModel('direct', [_initial_wait(), _root_update_wait(), _successful_finish()])
        task_dir = tmp_path / 'root-update-repair'
        agent = ProtocolIIIAgent(
            task=CompetitionTask(
                task_idx=0,
                task_id='root-update-repair',
                website='https://example.test/start',
                task='根据当前页面回答测试问题。',
            ),
            llm=model,  # type: ignore[arg-type]
            runtime=_ExplorationRuntime(),
            task_dir=task_dir,
            max_steps=2,
            model_timeout_seconds=1.0,
            structured_prompt_log=True,
            chart_network_inspector=object(),
        )
        outcome = await agent.run()
        prompt_log = json.loads((task_dir / 'model_prompts.json').read_text(encoding='utf-8'))
        return outcome, model, prompt_log

    outcome, model, prompt_log = asyncio.run(scenario())

    assert outcome.status == 'SUCCESS'
    assert len(model.output_formats) == 3
    repair_schema = json.dumps(
        SchemaOptimizer.create_optimized_json_schema(model.output_formats[2]), ensure_ascii=False
    )
    assert 'PathJsonUpdateOperation' not in repair_schema
    assert [entry['output_protocol_variant'] for entry in prompt_log['steps']] == [
        'initial_page_add_only',
        'standard',
        'root_update_repair_add_only',
    ]


def test_repeated_action_contract_error_hides_action_only_for_the_current_step(tmp_path: Path) -> None:
    invalid_inspect_network = ModelStructuredOutputError(
        'inspect_network does not accept: analysis_query',
        raw_completion='{"decision":{"action":"inspect_network","analysis_query":"THEFT"}}',
    )

    async def scenario() -> tuple[Any, _FakeModel, dict[str, Any]]:
        model = _FakeModel(
            'direct',
            [invalid_inspect_network, invalid_inspect_network, _initial_wait(), _successful_finish()],
        )
        task_dir = tmp_path / 'repeated-action-contract-error'
        agent = ProtocolIIIAgent(
            task=CompetitionTask(
                task_idx=0,
                task_id='repeated-action-contract-error',
                website='https://example.test/start',
                task='根据当前页面回答测试问题。',
            ),
            llm=model,  # type: ignore[arg-type]
            runtime=_ExplorationRuntime(),
            task_dir=task_dir,
            max_steps=2,
            model_timeout_seconds=1.0,
            structured_prompt_log=True,
            chart_network_inspector=object(),
        )
        outcome = await agent.run()
        prompt_log = json.loads((task_dir / 'model_prompts.json').read_text(encoding='utf-8'))
        return outcome, model, prompt_log

    outcome, model, prompt_log = asyncio.run(scenario())

    assert outcome.status == 'SUCCESS'
    assert len(model.output_formats) == 4
    hidden_schema = json.dumps(SchemaOptimizer.create_optimized_json_schema(model.output_formats[2]), ensure_ascii=False)
    restored_schema = json.dumps(SchemaOptimizer.create_optimized_json_schema(model.output_formats[3]), ensure_ascii=False)
    assert 'inspect_network' not in hidden_schema
    assert 'finish' in hidden_schema
    assert 'inspect_network' in restored_schema
    assert 'inspect_network' not in model.system_prompts[2]
    assert 'inspect_network' in model.system_prompts[3]
    assert prompt_log['steps'][2]['temporarily_hidden_actions'] == ['inspect_network']
    assert '已从当前步骤剩余的修复请求中暂时移除' in model.user_prompts[2]


def test_action_contract_hide_survives_intervening_generic_structured_error(tmp_path: Path) -> None:
    invalid_inspect_network = ModelStructuredOutputError(
        'inspect_network does not accept: analysis_query',
        raw_completion='{"decision":{"action":"inspect_network","analysis_query":"THEFT"}}',
    )
    intervening_generic_error = ModelStructuredOutputError(
        'Extra data: line 2 column 1 (char 80018)',
    )

    async def scenario() -> tuple[Any, _FakeModel, dict[str, Any]]:
        model = _FakeModel(
            'direct',
            [invalid_inspect_network, intervening_generic_error, invalid_inspect_network, _initial_wait(), _successful_finish()],
        )
        task_dir = tmp_path / 'action-contract-hide-after-generic-error'
        agent = ProtocolIIIAgent(
            task=CompetitionTask(
                task_idx=0,
                task_id='action-contract-hide-after-generic-error',
                website='https://example.test/start',
                task='根据当前页面回答测试问题。',
            ),
            llm=model,  # type: ignore[arg-type]
            runtime=_ExplorationRuntime(),
            task_dir=task_dir,
            max_steps=2,
            model_timeout_seconds=1.0,
            structured_prompt_log=True,
            chart_network_inspector=object(),
        )
        outcome = await agent.run()
        prompt_log = json.loads((task_dir / 'model_prompts.json').read_text(encoding='utf-8'))
        return outcome, model, prompt_log

    outcome, model, prompt_log = asyncio.run(scenario())

    assert outcome.status == 'SUCCESS'
    assert len(model.output_formats) == 5
    hidden_schema = json.dumps(SchemaOptimizer.create_optimized_json_schema(model.output_formats[3]), ensure_ascii=False)
    restored_schema = json.dumps(SchemaOptimizer.create_optimized_json_schema(model.output_formats[4]), ensure_ascii=False)
    assert 'inspect_network' not in hidden_schema
    assert 'finish' in hidden_schema
    assert 'inspect_network' in restored_schema
    assert prompt_log['steps'][3]['temporarily_hidden_actions'] == ['inspect_network']
    assert '已从当前步骤剩余的修复请求中暂时移除' in model.user_prompts[3]


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
