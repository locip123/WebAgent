#!/usr/bin/env python3
"""汇总 WebRetriever 任务的模型服务调用情况。

默认分析 ``outputs/local-smoke-3`` 下每个任务目录的 ``model_call.json``，并
结合相邻 ``result.json`` 给出两类成功率：

* 调用成功率：该服务的 successful 调用数 / 该服务的全部调用数；
* 任务成功率：使用过该服务的任务中，result.json 为 SUCCESS 的比例。

后者只说明服务参与的任务结果，多个服务可参与同一任务，因此不代表某个服务
对任务成功具有因果贡献。

``service_routing.attempts`` 是共享路由器的事件流，可能包含并发任务的调用；本
脚本刻意只统计 ``steps[].attempts``，以保证每条记录属于当前任务。

使用示例（先执行 ``conda activate Browser-Use``）：

    python tests/analyze_model_calls.py
    python tests/analyze_model_calls.py --format json
    python tests/analyze_model_calls.py --output-dir outputs/another-run

服务配置只显示服务名和 API Base，绝不输出 API key。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "outputs" / "local-smoke-3"
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.json"
SUCCESS_STATUS = "SUCCESS"
CALL_SUCCESS_STATUS = "successful"


def read_json_object(path: Path) -> dict[str, Any]:
    """读取 JSON 对象，并在格式错误时给出带路径的错误。"""

    try:
        with path.open(encoding="utf-8") as input_file:
            payload = json.load(input_file)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"无法读取 JSON 文件 {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"JSON 根节点必须是对象: {path}")
    return payload


def load_service_bases(config_path: Path | None) -> dict[str, str | None]:
    """从配置提取服务名到 API Base 的映射，不读取或返回密钥。"""

    if config_path is None or not config_path.is_file():
        return {}

    config = read_json_object(config_path)
    configured_services = config.get("model_services")
    if not isinstance(configured_services, list):
        return {}

    bases: dict[str, str | None] = {}
    for service in configured_services:
        if not isinstance(service, Mapping):
            continue
        name = service.get("name")
        api_base = service.get("api_base")
        if isinstance(name, str) and name:
            bases[name] = api_base if isinstance(api_base, str) and api_base else None
    return bases


def task_attempts(model_call: Mapping[str, Any]) -> Iterable[Mapping[str, Any]]:
    """产出当前任务 steps 中记录的模型调用尝试。"""

    steps = model_call.get("steps")
    if not isinstance(steps, list):
        return
    for step in steps:
        if not isinstance(step, Mapping):
            continue
        attempts = step.get("attempts")
        if not isinstance(attempts, list):
            continue
        for attempt in attempts:
            if isinstance(attempt, Mapping):
                yield attempt


def _as_nonempty_string(value: object, fallback: str) -> str:
    return value if isinstance(value, str) and value else fallback


def analyse(output_dir: Path, service_bases: Mapping[str, str | None]) -> dict[str, Any]:
    """按服务配置聚合调用频率、调用成功率和关联任务成功率。"""

    if not output_dir.is_dir():
        raise ValueError(f"输出目录不存在或不是目录: {output_dir}")

    model_call_paths = sorted(output_dir.glob("*/model_call.json"))
    if not model_call_paths:
        raise ValueError(f"未在 {output_dir} 下找到任务 model_call.json")

    service_stats: dict[str, Counter[str]] = defaultdict(Counter)
    tasks: list[dict[str, Any]] = []
    warnings: list[str] = []

    for model_call_path in model_call_paths:
        task_dir = model_call_path.parent
        result_path = task_dir / "result.json"
        try:
            model_call = read_json_object(model_call_path)
        except ValueError as exc:
            warnings.append(str(exc))
            continue

        result_status = "MISSING_RESULT"
        if result_path.is_file():
            try:
                result_status = _as_nonempty_string(read_json_object(result_path).get("status"), "MISSING_STATUS")
            except ValueError as exc:
                warnings.append(str(exc))
                result_status = "INVALID_RESULT"
        else:
            warnings.append(f"缺少 result.json: {result_path}")

        task_services: set[str] = set()
        attempt_count = 0
        for attempt in task_attempts(model_call):
            service = _as_nonempty_string(attempt.get("service"), "<missing-service>")
            status = _as_nonempty_string(attempt.get("status"), "<missing-status>")
            service_stats[service]["attempts"] += 1
            service_stats[service][status] += 1
            task_services.add(service)
            attempt_count += 1

        tasks.append(
            {
                "task_dir": task_dir.name,
                "result_status": result_status,
                "attempt_count": attempt_count,
                "services": sorted(task_services),
            }
        )

    total_attempts = sum(stats["attempts"] for stats in service_stats.values())
    total_tasks = len(tasks)
    rows: list[dict[str, Any]] = []
    for service, stats in service_stats.items():
        attempt_count = stats["attempts"]
        successful_calls = stats[CALL_SUCCESS_STATUS]
        task_rows = [task for task in tasks if service in task["services"]]
        successful_tasks = sum(task["result_status"] == SUCCESS_STATUS for task in task_rows)
        status_counts = {
            status: count
            for status, count in sorted(stats.items())
            if status != "attempts"
        }
        rows.append(
            {
                "service": service,
                "api_base": service_bases.get(service),
                "attempt_count": attempt_count,
                "call_share": attempt_count / total_attempts if total_attempts else 0.0,
                "successful_calls": successful_calls,
                "failed_calls": stats["failed"],
                "timed_out_calls": stats["timed_out"],
                "cancelled_calls": stats["cancelled"],
                "call_success_rate": successful_calls / attempt_count if attempt_count else 0.0,
                "task_count": len(task_rows),
                "successful_tasks": successful_tasks,
                "task_success_rate": successful_tasks / len(task_rows) if task_rows else 0.0,
                "status_counts": status_counts,
            }
        )
    rows.sort(key=lambda row: (-row["attempt_count"], -row["call_success_rate"], row["service"]))

    return {
        "output_dir": str(output_dir),
        "task_count": total_tasks,
        "successful_task_count": sum(task["result_status"] == SUCCESS_STATUS for task in tasks),
        "task_success_rate": (
            sum(task["result_status"] == SUCCESS_STATUS for task in tasks) / total_tasks if total_tasks else 0.0
        ),
        "total_attempt_count": total_attempts,
        "service_statistics": rows,
        "tasks": tasks,
        "warnings": warnings,
        "method": "仅统计每个任务 model_call.json 的 steps[].attempts；不统计 service_routing.attempts。",
    }


def _percentage(value: float) -> str:
    return f"{value:.1%}"


def format_markdown(report: Mapping[str, Any]) -> str:
    """将统计报告格式化为可直接粘贴到终端或 Markdown 的表格。"""

    lines = [
        f"分析目录：{report['output_dir']}",
        (
            f"任务：{report['successful_task_count']}/{report['task_count']} 成功"
            f"（{_percentage(report['task_success_rate'])}）；"
            f"模型调用：{report['total_attempt_count']} 次。"
        ),
        "",
        "| 服务配置 | API Base | 调用次数 | 占比 | 调用成功率 | 成功/失败/超时/取消 | 覆盖任务 | 任务成功率 |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in report["service_statistics"]:
        api_base = row["api_base"] or "未在配置中找到"
        call_outcomes = (
            f"{row['successful_calls']}/{row['failed_calls']}/"
            f"{row['timed_out_calls']}/{row['cancelled_calls']}"
        )
        lines.append(
            "| {service} | {api_base} | {attempt_count} | {call_share} | {call_success_rate} | "
            "{call_outcomes} | {successful_tasks}/{task_count} | {task_success_rate} |".format(
                service=row["service"],
                api_base=api_base,
                attempt_count=row["attempt_count"],
                call_share=_percentage(row["call_share"]),
                call_success_rate=_percentage(row["call_success_rate"]),
                call_outcomes=call_outcomes,
                successful_tasks=row["successful_tasks"],
                task_count=row["task_count"],
                task_success_rate=_percentage(row["task_success_rate"]),
            )
        )

    warnings = report["warnings"]
    if warnings:
        lines.extend(["", "警告："])
        lines.extend(f"- {warning}" for warning in warnings)
    lines.extend(["", f"口径：{report['method']}"])
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="统计 WebRetriever 模型服务调用频率与成功率")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR, help="任务输出目录")
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help="可选的 config.json；仅显示 API Base，不读取或输出 API key",
    )
    parser.add_argument("--format", choices=("markdown", "json"), default="markdown", help="输出格式")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        report = analyse(args.output_dir, load_service_bases(args.config))
    except ValueError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2

    if args.format == "json":
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(format_markdown(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
