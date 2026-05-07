from __future__ import annotations

import re
from typing import Any, Dict, List


NAME_ERROR_RE = re.compile(r"name '([^']+)' is not defined")


def infer_reason_and_avoid(message: str) -> Dict[str, str]:
    text = (message or "").strip()
    lowered = text.lower()
    if "is not defined" in lowered:
        return {
            "reason": "used a symbol that is not visible in the current repository context",
            "avoid": "do not invent helpers or constants; only use symbols visible in the prompt or repository memory",
        }
    if "typeerror" in lowered:
        return {
            "reason": "called a helper or API with an incompatible signature or type contract",
            "avoid": "preserve repository-visible call signatures and container/value types",
        }
    if "attributeerror" in lowered:
        return {
            "reason": "assumed an object attribute or method that does not exist",
            "avoid": "reuse visible methods exactly and avoid fabricating object capabilities",
        }
    if "assertionerror" in lowered:
        return {
            "reason": "behavior still violates test expectations or edge-case contracts",
            "avoid": "preserve already passing behavior and focus on the failing contract with minimal edits",
        }
    return {
        "reason": "the repair did not satisfy the execution contract",
        "avoid": "modify the current best candidate locally instead of rewriting the function broadly",
    }


def initial_memory(row: Dict[str, Any], task_index: int, total_tests: int) -> Dict[str, Any]:
    return {
        "task_id": "sample_{:03d}".format(task_index),
        "dataset_id": int(row.get("id", task_index)),
        "project": row.get("project"),
        "module": row.get("module"),
        "entry_point": row.get("entry_point"),
        "round": 0,
        "total_tests": total_tests,
        "failed_repairs": [],
        "best_so_far": None,
        "confirmed_facts": [],
        "next_instruction": "从 best_so_far 局部修复",
    }


def _append_confirmed_fact(memory: Dict[str, Any], fact: str) -> None:
    if fact and fact not in memory["confirmed_facts"]:
        memory["confirmed_facts"].append(fact)


def update_memory(
    memory: Dict[str, Any],
    round_index: int,
    prediction: str,
    solution_fn: str,
    eval_result: Dict[str, Any],
    max_memory_repairs: int,
) -> None:
    memory["round"] = round_index
    passed_tests = int(eval_result.get("passed_tests", 0))
    failed_tests = int(eval_result.get("failed_tests", 0))
    message = str(eval_result.get("message", ""))
    best = memory.get("best_so_far")

    if best is None or passed_tests > int(best.get("passed_tests", -1)):
        memory["best_so_far"] = {
            "round": round_index,
            "passed_tests": passed_tests,
            "failed_tests": failed_tests,
            "prediction": prediction,
            "solution_fn": solution_fn,
            "why_best": "highest passed test count observed so far",
        }

    if not bool(eval_result.get("passed", False)):
        diagnosis = infer_reason_and_avoid(message)
        memory["failed_repairs"].append(
            {
                "round": round_index,
                "passed_tests": passed_tests,
                "failed_tests": failed_tests,
                "message": message[:300],
                "reason": diagnosis["reason"],
                "avoid": diagnosis["avoid"],
                "prediction_preview": prediction[:300],
            }
        )
        if len(memory["failed_repairs"]) > max_memory_repairs:
            memory["failed_repairs"] = memory["failed_repairs"][-max_memory_repairs:]

    for symbol in NAME_ERROR_RE.findall(message):
        _append_confirmed_fact(memory, "symbol `{}` is not resolvable in the current repository context".format(symbol))

    if eval_result.get("passed"):
        memory["next_instruction"] = "当前候选已通过，保持最小修改策略，避免破坏现有正确行为。"
    elif memory.get("best_so_far"):
        memory["next_instruction"] = "从 best_so_far 局部修复，不要重复 failed_repairs 中已失败的方案。"
    else:
        memory["next_instruction"] = "先获得一个可运行候选，再逐步做最小修复。"


def format_memory_for_prompt(memory: Dict[str, Any]) -> str:
    failed_lines = []
    for item in memory.get("failed_repairs", [])[-5:]:
        failed_lines.extend(
            [
                "- round={round} passed={passed_tests} failed={failed_tests}".format(**item),
                "  - error: {}".format(item.get("message", "")),
                "  - reason: {}".format(item.get("reason", "")),
                "  - avoid: {}".format(item.get("avoid", "")),
            ]
        )

    best = memory.get("best_so_far")
    best_lines: List[str] = []
    if best:
        best_lines.extend(
            [
                "- best_round={}".format(best.get("round")),
                "- passed_tests={} failed_tests={}".format(best.get("passed_tests", 0), best.get("failed_tests", 0)),
                "- best_so_far_code:",
                "```python",
                str(best.get("solution_fn", "")).rstrip(),
                "```",
            ]
        )
    else:
        best_lines.append("- best_so_far: 无")

    fact_lines = ["- {}".format(item) for item in memory.get("confirmed_facts", [])] or ["- 无"]
    return "\n".join(
        [
            "短期工作记忆：",
            "- task_id={}".format(memory.get("task_id")),
            "- round={}".format(memory.get("round", 0)),
            "- total_tests={}".format(memory.get("total_tests", 0)),
            "最近失败修复：",
            *(failed_lines or ["- 无"]),
            "当前 best_so_far：",
            *best_lines,
            "已确认事实：",
            *fact_lines,
            "下一步指令：",
            "- {}".format(memory.get("next_instruction", "")),
        ]
    )
