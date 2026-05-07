from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from smartcoder.paths import bundled_long_memory_path, resolve_repoexec_parquet, resolve_repoexec_root
from smartcoder.repoexec.eval import (
    cleanup_output_intermediates,
    cleanup_sample_intermediates,
    compact_execution_trace,
    run_counting_eval,
    run_trace_eval,
)
from smartcoder.repoexec.memory import format_memory_for_prompt, initial_memory, update_memory
from smartcoder.repoexec.prompting import (
    build_baseprompt,
    build_long_memory_card_v2,
    build_repo_memory_card_v2,
    materialize_solution,
    request_with_retry_detailed,
    solution_to_api_prediction,
    stop_at_stop_token,
)
from smartcoder.utils.io import append_jsonl, save_json, write_jsonl


def compact_error_feedback(eval_result: Dict[str, Any], max_items: int = 5) -> str:
    if eval_result.get("passed"):
        return "Latest execution feedback: passed"
    details = eval_result.get("failed_test_details") or []
    lines = [
        "Latest execution feedback:",
        "- passed_tests: {}".format(eval_result.get("passed_tests", 0)),
        "- failed_tests: {}".format(eval_result.get("failed_tests", 0)),
        "- total_tests: {}".format(eval_result.get("total_tests", 0)),
        "- message: {}".format(eval_result.get("message", "")),
    ]
    if details:
        lines.append("- Failed test summary:")
        for item in details[:max_items]:
            lines.append("  - {}: {}: {}".format(item.get("name", "unknown"), item.get("error_type", "Error"), item.get("error", "")))
    return "\n".join(lines)


def build_long_memory_for_round(row: Dict[str, Any], long_memory_path: Path, eval_result: Optional[Dict[str, Any]]) -> str:
    row_for_retrieval = dict(row)
    if eval_result is not None:
        row_for_retrieval["docstring"] = "\n".join([str(row.get("docstring") or ""), compact_error_feedback(eval_result)])
    return build_long_memory_card_v2(row_for_retrieval, long_memory_path)


def build_integrated_prompt(
    row: Dict[str, Any],
    round_index: int,
    memory: Dict[str, Any],
    long_memory_path: Path,
    previous_eval: Optional[Dict[str, Any]],
) -> str:
    repo_card = build_repo_memory_card_v2(row)
    long_card = build_long_memory_for_round(row, long_memory_path, previous_eval)
    work_card = format_memory_for_prompt(memory)
    cards = [
        "Three-level memory priority: disproved facts in short-term working memory > true symbols and contracts in structured repository memory > generic strategies in long-term experience memory.",
        repo_card,
    ]
    if long_card:
        cards.append(long_card)
    if round_index > 0:
        cards.extend(
            [
                work_card,
                compact_error_feedback(previous_eval or {}),
                "Repair requirements:",
                "- Continue with a local edit starting from best_so_far in working memory.",
                "- Do not repeat approaches already recorded in failed_repairs.",
                "- Use long-term experience as strategy only; if it refers to symbols not visible in structured repository memory or the original prompt, do not use them.",
                "- Preserve already validated behavior and avoid broad rewrites.",
                "- Output only the completion for the target function and do not include explanations.",
            ]
        )
    else:
        cards.extend(
            [
                "Initial generation requirements:",
                "- Prioritize visible symbols, exception contracts, and docstring examples from structured repository memory.",
                "- Use long-term experience as strategy only, never as a replacement for current repository facts.",
                "- Output only the completion for the target function and do not include explanations.",
            ]
        )
    cards.append(build_baseprompt(row))
    return "\n\n".join(cards)


def error_kind(message: str) -> str:
    for name in ["SyntaxError", "NameError", "TypeError", "AssertionError", "AttributeError", "IndexError", "ValueError", "KeyError"]:
        if name in message:
            return name
    if message == "passed":
        return "passed"
    return "Other"


def candidate_score(candidate: Dict[str, Any]) -> Tuple[int, int, int, int]:
    eval_result = candidate["eval_result"]
    passed_tests = int(eval_result.get("passed_tests", 0))
    failed_tests = int(eval_result.get("failed_tests", 0))
    passed_flag = 1 if eval_result.get("passed") else 0
    generation_index = int(candidate.get("generation_index", 0))
    return passed_tests, -failed_tests, passed_flag, -generation_index


def best_candidate(candidate_pool: List[Dict[str, Any]]) -> Dict[str, Any]:
    return max(candidate_pool, key=candidate_score)


def should_run_trace(candidate: Dict[str, Any], best_before: Optional[Dict[str, Any]], stagnation_count: int, args: argparse.Namespace) -> bool:
    eval_result = candidate["eval_result"]
    if eval_result.get("passed"):
        return False
    failed_tests = int(eval_result.get("failed_tests", 0))
    if 0 < failed_tests <= args.trace_failed_threshold:
        return True
    kind = error_kind(str(eval_result.get("message", "")))
    if kind in {"NameError", "TypeError", "AttributeError", "IndexError"}:
        return True
    message = str(eval_result.get("message", ""))
    if kind == "AssertionError" and message.rstrip().endswith("AssertionError:"):
        return True
    if best_before is not None and stagnation_count >= args.trace_stagnation_rounds:
        return True
    return False


def build_trace_integrated_prompt(
    row: Dict[str, Any],
    memory: Dict[str, Any],
    long_memory_path: Path,
    trace_eval: Dict[str, Any],
    traced_solution: str,
    trace_max_chars: int,
) -> str:
    repo_card = build_repo_memory_card_v2(row)
    long_card = build_long_memory_for_round(row, long_memory_path, trace_eval)
    work_card = format_memory_for_prompt(memory)
    trace_card = compact_execution_trace(trace_eval, max_chars=trace_max_chars)
    cards = [
        "Three-level memory plus runtime-trace repair:",
        "Priority: disproved facts in short-term working memory > true symbols and contracts in structured repository memory > concrete failures exposed by runtime traces > generic strategies in long-term experience memory.",
        repo_card,
    ]
    if long_card:
        cards.append(long_card)
    cards.extend(
        [
            work_card,
            "Candidate code selected for trace diagnosis:",
            "```python",
            traced_solution.rstrip(),
            "```",
            trace_card,
            "Trace-aware repair requirements:",
            "- Use the trace internally to localize the root cause, but output only the completion for the target function.",
            "- If the trace exposes an exact exception message or assertion source line, prioritize satisfying that concrete contract.",
            "- Do not repeat approaches that have already failed in short-term working memory.",
            "- Do not invent helpers, constants, or regex groups that are not visible in the current prompt or structured repository memory.",
            "- Prefer the smallest possible edit from the current best_so_far to avoid breaking already passing behavior.",
            build_baseprompt(row),
        ]
    )
    return "\n\n".join(cards)


def request_candidate(client: Any, row: Dict[str, Any], prompt: str, args: argparse.Namespace) -> Tuple[str, str, int, Optional[str], Dict[str, Any]]:
    request_meta = request_with_retry_detailed(
        client=client,
        model=args.model,
        prompt=prompt,
        max_tokens=args.max_tokens,
        temperature=args.temperature,
        retries=args.retries,
        sleep_base=args.sleep_base,
    )
    raw_prediction = str(request_meta["content"])
    attempts = int(request_meta["attempts"])
    api_error = request_meta["error"]
    prediction = stop_at_stop_token(raw_prediction)
    solution_fn = materialize_solution(row, prediction)
    return prediction, solution_fn, attempts, api_error, request_meta


def evaluate_solution(row: Dict[str, Any], solution_fn: str, round_dir: Path, repo_root: Path, args: argparse.Namespace, api_error: Optional[str]) -> Dict[str, Any]:
    total_tests = len(row.get("test_list", [])) or 1
    if api_error:
        return {
            "passed": False,
            "passed_tests": 0,
            "failed_tests": total_tests,
            "total_tests": total_tests,
            "message": api_error,
            "failed_test_details": [],
        }
    return run_counting_eval(
        row=row,
        solution_fn=solution_fn,
        round_dir=round_dir,
        repo_root=repo_root,
        image=args.image,
        timeout=args.timeout,
    )


def add_candidate(
    candidate_pool: List[Dict[str, Any]],
    memory: Dict[str, Any],
    row: Dict[str, Any],
    sample_dir: Path,
    output_dir: Path,
    phase: str,
    generation_index: int,
    prompt: str,
    prediction: str,
    solution_fn: str,
    eval_result: Dict[str, Any],
    attempts: int,
    api_error: Optional[str],
    max_memory_repairs: int,
    trace_card: str = "",
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    total_tokens: int = 0,
    generation_elapsed_seconds: float = 0.0,
) -> Dict[str, Any]:
    eval_result["api_attempts"] = attempts
    eval_result["api_error"] = api_error
    eval_result["prompt_tokens"] = prompt_tokens
    eval_result["completion_tokens"] = completion_tokens
    eval_result["total_tokens"] = total_tokens
    eval_result["generation_elapsed_seconds"] = generation_elapsed_seconds
    eval_result["eval_elapsed_seconds"] = float(eval_result.get("elapsed_seconds") or 0.0)
    eval_result["total_elapsed_seconds"] = generation_elapsed_seconds + float(eval_result.get("elapsed_seconds") or 0.0)
    update_memory(memory, generation_index, prediction, solution_fn, eval_result, max_memory_repairs)
    candidate = {
        "task_id": int(memory["task_id"].split("_")[-1]),
        "dataset_id": int(row["id"]),
        "entry_point": row["entry_point"],
        "phase": phase,
        "generation_index": generation_index,
        "prompt": prompt,
        "prediction": prediction,
        "solution_fn": solution_fn,
        "eval_result": eval_result,
        "trace_card": trace_card,
        "memory_after_candidate": memory,
        "prompt_chars": len(prompt),
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "generation_elapsed_seconds": generation_elapsed_seconds,
        "eval_elapsed_seconds": float(eval_result.get("elapsed_seconds") or 0.0),
        "total_elapsed_seconds": generation_elapsed_seconds + float(eval_result.get("elapsed_seconds") or 0.0),
    }
    candidate_pool.append(candidate)
    candidate_dir = sample_dir / "candidate_{:02d}_{}".format(generation_index, phase)
    save_json(candidate_dir / "candidate.json", candidate)
    save_json(sample_dir / "working_memory.json", memory)
    append_jsonl(
        output_dir / "rounds.jsonl",
        {
            "task_id": candidate["task_id"],
            "dataset_id": int(row["id"]),
            "generation_index": generation_index,
            "phase": phase,
            "entry_point": row["entry_point"],
            "passed": eval_result["passed"],
            "passed_tests": eval_result["passed_tests"],
            "failed_tests": eval_result["failed_tests"],
            "message": eval_result["message"],
            "api_attempts": attempts,
            "api_error": api_error,
            "prompt_chars": len(prompt),
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
            "generation_elapsed_seconds": generation_elapsed_seconds,
            "eval_elapsed_seconds": float(eval_result.get("elapsed_seconds") or 0.0),
            "total_elapsed_seconds": generation_elapsed_seconds + float(eval_result.get("elapsed_seconds") or 0.0),
            "trace_chars": len(trace_card),
        },
    )
    return candidate


def run_repoexec(args: argparse.Namespace) -> int:
    import httpx
    from openai import OpenAI
    from smartcoder.repoexec.dataset import load_repoexec_rows

    repo_root = resolve_repoexec_root(args.repo_root)
    parquet_path = resolve_repoexec_parquet(repo_root, args.parquet, args.context_level)
    long_memory_path = Path(args.long_memory_path).resolve() if args.long_memory_path else bundled_long_memory_path()

    if not parquet_path.exists():
        raise FileNotFoundError("RepoExec parquet not found: {}".format(parquet_path))
    if not repo_root.exists():
        raise FileNotFoundError("RepoExec root not found: {}".format(repo_root))

    api_key = args.api_key or ""
    if not api_key:
        import os
        api_key = os.environ.get(args.api_key_env, "")
    if not api_key:
        raise RuntimeError("Missing API key. Use --api-key or set {}.".format(args.api_key_env))
    if not args.base_url:
        raise RuntimeError("Missing --base-url or OPENAI_BASE_URL.")

    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    rows = load_repoexec_rows(parquet_path, args.start, args.limit)
    client = OpenAI(api_key=api_key, base_url=args.base_url, http_client=httpx.Client(timeout=120))

    final_generations = []
    final_records = []
    processed_records = []

    save_json(
        output_dir / "metadata.json",
        {
            "framework": "SMARTCoder",
            "dataset": "RepoExec",
            "context_level": args.context_level,
            "split": "{}_context".format(args.context_level),
            "parquet": str(parquet_path),
            "repo_root": str(repo_root),
            "model": args.model,
            "base_url": args.base_url,
            "rounds": args.rounds,
            "temperature": args.temperature,
            "max_tokens": args.max_tokens,
            "mechanism": "SMARTCoder",
            "implementation_name": "integrated_memory_plus_conditional_trace_repair",
            "long_memory_path": str(long_memory_path),
            "trace_failed_threshold": args.trace_failed_threshold,
            "trace_stagnation_rounds": args.trace_stagnation_rounds,
            "trace_limit": args.trace_limit,
            "trace_max_chars": args.trace_max_chars,
            "continue_after_baseline_pass": args.continue_after_baseline_pass,
            "final_selection": "candidate_pool_best",
            "image": args.image,
            "timeout": args.timeout,
            "retries": args.retries,
            "start": args.start,
            "limit": args.limit,
        },
    )

    for local_idx, row in enumerate(rows):
        global_idx = args.start + local_idx
        sample_dir = output_dir / "task_{:03d}_{}".format(global_idx, row["entry_point"])
        total_tests = len(row.get("test_list", [])) or 1
        memory = initial_memory(row, global_idx, total_tests)
        save_json(sample_dir / "working_memory.json", memory)
        candidate_pool = []
        generation_index = 0
        previous_eval = None
        stagnation_count = 0
        used_trace_for_signature = set()

        prompt = build_baseprompt(row)
        prediction, solution_fn, attempts, api_error, request_meta = request_candidate(client, row, prompt, args)
        eval_result = evaluate_solution(
            row=row,
            solution_fn=solution_fn,
            round_dir=sample_dir / "candidate_{:02d}_baseline".format(generation_index),
            repo_root=repo_root,
            args=args,
            api_error=api_error,
        )
        baseline_candidate = add_candidate(
            candidate_pool, memory, row, sample_dir, output_dir, "baseline", generation_index, prompt, prediction, solution_fn,
            eval_result, attempts, api_error, args.max_memory_repairs,
            prompt_tokens=int(request_meta["usage"].get("prompt_tokens", 0)),
            completion_tokens=int(request_meta["usage"].get("completion_tokens", 0)),
            total_tokens=int(request_meta["usage"].get("total_tokens", 0)),
            generation_elapsed_seconds=float(request_meta.get("generation_elapsed_seconds") or 0.0),
        )
        generation_index += 1
        previous_eval = eval_result
        selected = baseline_candidate if baseline_candidate["eval_result"]["passed"] and not args.continue_after_baseline_pass else None

        integrated_round = 0
        while selected is None and integrated_round < args.rounds:
            best_before = best_candidate(candidate_pool)
            prompt = build_integrated_prompt(row, memory.get("round", generation_index), memory, long_memory_path, previous_eval)
            prediction, solution_fn, attempts, api_error, request_meta = request_candidate(client, row, prompt, args)
            eval_result = evaluate_solution(
                row=row,
                solution_fn=solution_fn,
                round_dir=sample_dir / "candidate_{:02d}_integrated".format(generation_index),
                repo_root=repo_root,
                args=args,
                api_error=api_error,
            )
            current = add_candidate(
                candidate_pool, memory, row, sample_dir, output_dir, "integrated", generation_index, prompt, prediction, solution_fn,
                eval_result, attempts, api_error, args.max_memory_repairs,
                prompt_tokens=int(request_meta["usage"].get("prompt_tokens", 0)),
                completion_tokens=int(request_meta["usage"].get("completion_tokens", 0)),
                total_tokens=int(request_meta["usage"].get("total_tokens", 0)),
                generation_elapsed_seconds=float(request_meta.get("generation_elapsed_seconds") or 0.0),
            )
            generation_index += 1
            integrated_round += 1
            previous_eval = eval_result
            new_best = best_candidate(candidate_pool)
            if candidate_score(new_best) <= candidate_score(best_before):
                stagnation_count += 1
            else:
                stagnation_count = 0
            if new_best["eval_result"].get("passed"):
                selected = new_best
                break

            trace_signature = (current["phase"], str(current["eval_result"].get("message", "")), int(current["eval_result"].get("passed_tests", 0)))
            if trace_signature not in used_trace_for_signature and should_run_trace(current, best_before, stagnation_count, args):
                used_trace_for_signature.add(trace_signature)
                trace_target = best_candidate(candidate_pool)
                trace_eval = run_trace_eval(
                    row=row,
                    solution_fn=trace_target["solution_fn"],
                    round_dir=sample_dir / "trace_{:02d}_{}".format(generation_index, trace_target["phase"]),
                    repo_root=repo_root,
                    image=args.image,
                    timeout=args.timeout,
                    trace_limit=args.trace_limit,
                )
                trace_card = compact_execution_trace(trace_eval, max_chars=args.trace_max_chars)
                save_json(
                    sample_dir / "trace_{:02d}_{}".format(generation_index, trace_target["phase"]) / "trace_eval.json",
                    {
                        "trace_target_generation_index": trace_target["generation_index"],
                        "trace_target_phase": trace_target["phase"],
                        "trace_eval": trace_eval,
                        "trace_card": trace_card,
                    },
                )
                trace_prompt = build_trace_integrated_prompt(row, memory, long_memory_path, trace_eval, trace_target["solution_fn"], args.trace_max_chars)
                prediction, solution_fn, attempts, api_error, request_meta = request_candidate(client, row, trace_prompt, args)
                eval_result = evaluate_solution(
                    row=row,
                    solution_fn=solution_fn,
                    round_dir=sample_dir / "candidate_{:02d}_trace_repair".format(generation_index),
                    repo_root=repo_root,
                    args=args,
                    api_error=api_error,
                )
                trace_candidate = add_candidate(
                    candidate_pool, memory, row, sample_dir, output_dir, "trace_repair", generation_index, trace_prompt, prediction, solution_fn,
                    eval_result, attempts, api_error, args.max_memory_repairs, trace_card=trace_card,
                    prompt_tokens=int(request_meta["usage"].get("prompt_tokens", 0)),
                    completion_tokens=int(request_meta["usage"].get("completion_tokens", 0)),
                    total_tokens=int(request_meta["usage"].get("total_tokens", 0)),
                    generation_elapsed_seconds=float(request_meta.get("generation_elapsed_seconds") or 0.0),
                )
                generation_index += 1
                previous_eval = eval_result
                if trace_candidate["eval_result"].get("passed"):
                    selected = trace_candidate
                    break

        if selected is None:
            selected = best_candidate(candidate_pool)

        selected_solution = selected["solution_fn"]
        selected_eval = selected["eval_result"]
        prediction_body = solution_to_api_prediction(row, selected_solution)
        final_test = ""
        from smartcoder.repoexec.prompting import build_test_program
        try:
            final_test = build_test_program(row, selected_solution)
        except Exception:
            final_test = str(row["check"])

        save_json(sample_dir / "candidate_pool.json", candidate_pool)
        final_generations.append(
            [
                {
                    "task_id": global_idx,
                    "dataset_id": int(row["id"]),
                    "project": row["project"],
                    "module": row["module"],
                    "entry_point": row["entry_point"],
                    "prediction": prediction_body,
                    "raw_prediction": selected_solution,
                    "selected_generation_index": selected["generation_index"],
                    "selected_phase": selected["phase"],
                    "passed_tests": selected_eval.get("passed_tests", 0),
                    "failed_tests": selected_eval.get("failed_tests", total_tests),
                    "candidate_count": len(candidate_pool),
                }
            ]
        )
        final_records.append(
            {
                "task_id": global_idx,
                "dataset_id": int(row["id"]),
                "entry_point": row["entry_point"],
                "selected_generation_index": selected["generation_index"],
                "selected_phase": selected["phase"],
                "passed_tests": selected_eval.get("passed_tests", 0),
                "failed_tests": selected_eval.get("failed_tests", total_tests),
                "passed": bool(selected_eval.get("passed", False)),
                "candidate_count": len(candidate_pool),
                "phases": [item["phase"] for item in candidate_pool],
            }
        )
        processed_records.append(
            {
                "task_id": global_idx,
                "project": row["project"],
                "module": row["module"],
                "predictions": [selected_solution],
                "test": [final_test],
            }
        )
        save_json(output_dir / "generations.json", final_generations)
        save_json(output_dir / "final_records.json", final_records)
        write_jsonl(output_dir / "processed_generations.json", processed_records)
        cleanup_sample_intermediates(sample_dir)

    passed = sum(1 for item in final_records if item["passed"])
    save_json(
        output_dir / "summary.json",
        {
            "generated": len(final_records),
            "passed": passed,
            "pass_at_1": passed / len(final_records) if final_records else 0,
            "selection": "candidate_pool_best",
        },
    )
    cleanup_output_intermediates(output_dir)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="smartcoder")
    subparsers = parser.add_subparsers(dest="command")

    repoexec_parser = subparsers.add_parser("repoexec", help="RepoExec experiments")
    repoexec_subparsers = repoexec_parser.add_subparsers(dest="repoexec_command")
    run_parser = repoexec_subparsers.add_parser("run", help="Run SMARTCoder on RepoExec")
    run_parser.add_argument("--repo-root", default="", help="Path to RepoExec root")
    run_parser.add_argument("--parquet", default="", help="Path to RepoExec parquet split")
    run_parser.add_argument(
        "--context-level",
        choices=["full", "medium", "small"],
        default="full",
        help="RepoExec repository context level used for the run",
    )
    run_parser.add_argument("--output-dir", required=True)
    run_parser.add_argument("--start", type=int, default=0)
    run_parser.add_argument("--limit", type=int, default=50)
    run_parser.add_argument("--rounds", type=int, default=4, help="Integrated repair budget after the baseline candidate")
    run_parser.add_argument("--model", default="qwen-plus-2025-12-01")
    run_parser.add_argument("--base-url", default="")
    run_parser.add_argument("--api-key", default="")
    run_parser.add_argument("--api-key-env", default="OPENAI_API_KEY")
    run_parser.add_argument("--max-tokens", type=int, default=896, help="Maximum completion length for each generation call")
    run_parser.add_argument("--temperature", type=float, default=0.0, help="Sampling temperature; the paper setting uses 0.0")
    run_parser.add_argument("--retries", type=int, default=10)
    run_parser.add_argument("--sleep-base", type=float, default=2.0)
    run_parser.add_argument("--image", default="codeeval-runner-repoexec-first50")
    run_parser.add_argument("--timeout", type=int, default=120)
    run_parser.add_argument("--max-memory-repairs", type=int, default=6)
    run_parser.add_argument("--long-memory-path", default=str(bundled_long_memory_path()))
    run_parser.add_argument("--trace-limit", type=int, default=6)
    run_parser.add_argument("--trace-max-chars", type=int, default=3200)
    run_parser.add_argument("--trace-failed-threshold", type=int, default=5)
    run_parser.add_argument("--trace-stagnation-rounds", type=int, default=2)
    run_parser.add_argument("--continue-after-baseline-pass", action="store_true")
    return parser
