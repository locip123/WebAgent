import asyncio
import json
from pathlib import Path

from browser_use.webretriever.completion import (
	AnswerCandidate,
	CompletionGate,
	CompletionVerification,
	RequirementAudit,
	RequirementSpec,
	RequirementVerdict,
	RequirementsDraft,
)
from browser_use.webretriever.models import AnswerClaim, AnswerItem, CompetitionTask


class FreeFormatReviewer:
	async def compile_requirements(
		self,
		task: CompetitionTask,
		*,
		feedback=(),
	) -> RequirementsDraft:
		return RequirementsDraft(
			requirements=[
				RequirementSpec(
					requirement_id="R1",
					kind="entity",
					description="Find today's articles.",
				)
			]
		)

	async def audit_requirements(self, task: CompetitionTask, draft: RequirementsDraft) -> RequirementAudit:
		return RequirementAudit(approved=True)

	async def verify_candidate(self, task, ledger, candidate, evidence) -> CompletionVerification:
		return CompletionVerification(
			verdicts=[
				RequirementVerdict(
					requirement_id="R1",
					verdict="entailed",
					reason="The cited page text lists both articles.",
					evidence_ids=["ev-000001"],
				)
			]
		)


def test_prepare_freezes_requirements_without_an_answer_format(tmp_path: Path) -> None:
	task = CompetitionTask(
		task_idx=0,
		task_id="today-articles",
		website="https://example.com",
		task="Find today's articles.",
	)
	gate = CompletionGate(task=task, task_dir=tmp_path, reviewer=FreeFormatReviewer())

	ledger = asyncio.run(gate.prepare())

	assert [requirement.requirement_id for requirement in ledger.requirements] == ["R1"]
	assert "answer_contract" not in ledger.model_dump(mode="json")


def test_submit_accepts_a_free_format_answer_without_item_constraints(tmp_path: Path) -> None:
	task = CompetitionTask(
		task_idx=0,
		task_id="today-articles",
		website="https://example.com",
		task="Find today's articles.",
	)
	gate = CompletionGate(task=task, task_dir=tmp_path, reviewer=FreeFormatReviewer())
	asyncio.run(gate.prepare())
	evidence = gate.register_action(
		action="find_text",
		step=1,
		source_url="https://example.com",
		output=json.dumps(
			{"results": [{"text": "Article A and Article B were published today.", "url": "https://example.com"}]}
		),
		parameters={},
	)
	candidate = AnswerCandidate(
		answer="Today:\n- Article A\n- Article B",
		answer_items=[AnswerItem(value="Article A"), AnswerItem(value="Article B")],
		claims=[
			AnswerClaim(
				requirement_id="R1",
				statement="Article A and Article B are today's articles.",
				evidence_ids=[evidence.records[0].evidence_id],
			)
		],
	)

	result = asyncio.run(gate.submit(candidate))

	assert result.accepted is True
