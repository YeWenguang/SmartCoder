from __future__ import annotations

import json
import os
import re
import subprocess
import time
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List

from smartcoder.repoexec.prompting import build_test_program


COUNT_RESULT_MARKER = "__SMARTCODER_COUNT_RESULT__"
TRACE_RESULT_MARKER = "__SMARTCODER_TRACE_RESULT__"


def env_flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def instrument_count_test_program(test_program: str, max_failed_tests: int) -> str:
    lines = [
        "import json as __count_json",
        "__count_passed_tests = 0",
        "__count_failed_count = 0",
        "__count_failed_tests = []",
        "__count_limit = {}".format(max_failed_tests),
    ]
    test_call_re = re.compile(r"^(test_[A-Za-z0-9_]+)\(\)\s*$")
    replaced = 0
    for line in test_program.splitlines():
        match = test_call_re.match(line)
        if not match:
            lines.append(line)
            continue
        test_name = match.group(1)
        replaced += 1
        lines.extend(
            [
                "try:",
                "    {}()".format(test_name),
                "    __count_passed_tests += 1",
                "except BaseException as __count_exc:",
                "    __count_failed_count += 1",
                "    if len(__count_failed_tests) < __count_limit:",
                "        __count_failed_tests.append({",
                "            'name': {!r},".format(test_name),
                "            'error_type': type(__count_exc).__name__,",
                "            'error': str(__count_exc),",
                "        })",
            ]
        )
    lines.extend(
        [
            "print({!r} + __count_json.dumps({{".format(COUNT_RESULT_MARKER),
            "    'passed_tests': __count_passed_tests,",
            "    'failed_tests': __count_failed_count,",
            "    'total_tests': __count_passed_tests + __count_failed_count,",
            "    'failed_test_details': __count_failed_tests,",
            "    'instrumented_calls': {}, ".format(replaced),
            "}, ensure_ascii=False))",
        ]
    )
    return "\n".join(lines) + "\n"


def summarize_count_failure(eval_result: Dict[str, Any]) -> str:
    details = eval_result.get("failed_test_details") or []
    if not details:
        return "failed"
    first = details[0]
    return "{}: {}: {}".format(first.get("name", "unknown"), first.get("error_type", "Error"), first.get("error", ""))


def _docker_command(
    repo_root: Path,
    package_dir: Path,
    round_dir: Path,
    program_name: str,
    image: str,
    timeout: int,
) -> List[str]:
    pip_cache_volume = os.environ.get("REPOEXEC_PIP_CACHE_VOLUME", "repoexec_pip_cache_vol")
    install_package = env_flag("REPOEXEC_INSTALL_PACKAGE", default=False)
    command_prefix = ""
    if install_package:
        command_prefix = (
            "if [ -f /package/package.txt ]; then "
            "pip install -r /package/package.txt >/tmp/pip_install.log 2>&1; "
            "fi; "
        )
    return [
        "docker",
        "run",
        "--rm",
        "-v",
        "{}:/work:ro".format(round_dir),
        "-v",
        "{}:/input:ro".format(repo_root),
        "-v",
        "{}:/output:ro".format(repo_root / "data_with_test_case"),
        "-v",
        "{}:/package:ro".format(package_dir),
        "-v",
        "{}:/tmp/pip_cache".format(pip_cache_volume),
        "-e",
        "PIP_CACHE_DIR=/tmp/pip_cache",
        "--entrypoint",
        "/bin/bash",
        image,
        "-lc",
        "cd /package && {}python /work/{}".format(command_prefix, program_name),
    ]


def run_counting_eval(
    row: Dict[str, Any],
    solution_fn: str,
    round_dir: Path,
    repo_root: Path,
    image: str,
    timeout: int,
    max_failed_tests: int = 5,
) -> Dict[str, Any]:
    round_dir.mkdir(parents=True, exist_ok=True)
    program_path = round_dir / "count_check.py"
    try:
        test_program = build_test_program(row, solution_fn)
        program_path.write_text(instrument_count_test_program(test_program, max_failed_tests), encoding="utf-8")
    except Exception as exc:
        total_tests = len(row.get("test_list", [])) or 1
        return {
            "passed": False,
            "passed_tests": 0,
            "failed_tests": total_tests,
            "total_tests": total_tests,
            "message": "{}: {}".format(type(exc).__name__, exc),
            "failed_test_details": [],
        }

    package_dir = repo_root / str(row["project"])
    started = time.time()
    try:
        completed = subprocess.run(
            _docker_command(repo_root, package_dir, round_dir, program_path.name, image, timeout),
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout + 5,
        )
    except subprocess.TimeoutExpired:
        elapsed = time.time() - started
        total_tests = len(row.get("test_list", [])) or 1
        return {
            "passed": False,
            "passed_tests": 0,
            "failed_tests": total_tests,
            "total_tests": total_tests,
            "message": "TimeoutExpired: {}s while running counting eval".format(timeout + 5),
            "failed_test_details": [],
            "elapsed_seconds": elapsed,
        }

    elapsed = time.time() - started
    output = (completed.stdout or "") + (completed.stderr or "")
    marker_line = None
    for line in output.splitlines():
        if line.startswith(COUNT_RESULT_MARKER):
            marker_line = line[len(COUNT_RESULT_MARKER) :]
    if marker_line is None:
        total_tests = len(row.get("test_list", [])) or 1
        return {
            "passed": False,
            "passed_tests": 0,
            "failed_tests": total_tests,
            "total_tests": total_tests,
            "message": output.strip() or "docker rc={}".format(completed.returncode),
            "failed_test_details": [],
            "raw_output": output[-4000:],
            "elapsed_seconds": elapsed,
        }

    data = json.loads(marker_line)
    data["passed"] = data["failed_tests"] == 0 and data["total_tests"] > 0
    data["message"] = "passed" if data["passed"] else summarize_count_failure(data)
    data["elapsed_seconds"] = elapsed
    return data


def instrument_trace_test_program(test_program: str, trace_limit: int) -> str:
    lines = [
        "import json as __trace_json",
        "import linecache as __trace_linecache",
        "import traceback as __traceback",
        "__trace_passed_tests = 0",
        "__trace_failed_count = 0",
        "__trace_failed_tests = []",
        "__trace_limit = {}".format(trace_limit),
    ]
    test_call_re = re.compile(r"^(test_[A-Za-z0-9_]+)\(\)\s*$")
    replaced = 0
    for line in test_program.splitlines():
        match = test_call_re.match(line)
        if not match:
            lines.append(line)
            continue
        test_name = match.group(1)
        replaced += 1
        lines.extend(
            [
                "try:",
                "    {}()".format(test_name),
                "    __trace_passed_tests += 1",
                "except BaseException as __trace_exc:",
                "    __trace_failed_count += 1",
                "    if len(__trace_failed_tests) < __trace_limit:",
                "        __trace_frames = []",
                "        __trace_tb = __trace_exc.__traceback__",
                "        while __trace_tb is not None:",
                "            __trace_frame = __trace_tb.tb_frame",
                "            __trace_file = __trace_frame.f_code.co_filename",
                "            __trace_line = __trace_tb.tb_lineno",
                "            __trace_frames.append({",
                "                'file': __trace_file,",
                "                'line': __trace_line,",
                "                'function': __trace_frame.f_code.co_name,",
                "                'source': __trace_linecache.getline(__trace_file, __trace_line).strip(),",
                "            })",
                "            __trace_tb = __trace_tb.tb_next",
                "        __trace_failed_tests.append({",
                "            'name': {!r},".format(test_name),
                "            'error_type': type(__trace_exc).__name__,",
                "            'error': str(__trace_exc),",
                "            'exception_only': ''.join(__traceback.format_exception_only(type(__trace_exc), __trace_exc)).strip(),",
                "            'frames': __trace_frames[-10:],",
                "        })",
            ]
        )
    lines.extend(
        [
            "print({!r} + __trace_json.dumps({{".format(TRACE_RESULT_MARKER),
            "    'passed_tests': __trace_passed_tests,",
            "    'failed_tests': __trace_failed_count,",
            "    'total_tests': __trace_passed_tests + __trace_failed_count,",
            "    'failed_test_details': __trace_failed_tests,",
            "    'instrumented_calls': {}, ".format(replaced),
            "}, ensure_ascii=False))",
        ]
    )
    return "\n".join(lines) + "\n"


def summarize_trace_failure(eval_result: Dict[str, Any]) -> str:
    details = eval_result.get("failed_test_details") or []
    if not details:
        return "failed"
    first = details[0]
    return "{}: {}: {}".format(
        first.get("name", "unknown"),
        first.get("exception_only") or first.get("error_type", "Error"),
        first.get("error", ""),
    )


def run_trace_eval(
    row: Dict[str, Any],
    solution_fn: str,
    round_dir: Path,
    repo_root: Path,
    image: str,
    timeout: int,
    trace_limit: int,
) -> Dict[str, Any]:
    round_dir.mkdir(parents=True, exist_ok=True)
    program_path = round_dir / "trace_check.py"
    try:
        test_program = build_test_program(row, solution_fn)
        program_path.write_text(instrument_trace_test_program(test_program, trace_limit), encoding="utf-8")
    except Exception as exc:
        total_tests = len(row.get("test_list", [])) or 1
        return {
            "passed": False,
            "passed_tests": 0,
            "failed_tests": total_tests,
            "total_tests": total_tests,
            "message": "{}: {}".format(type(exc).__name__, exc),
            "failed_test_details": [],
        }

    package_dir = repo_root / str(row["project"])
    started = time.time()
    try:
        completed = subprocess.run(
            _docker_command(repo_root, package_dir, round_dir, program_path.name, image, timeout),
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout + 5,
        )
    except subprocess.TimeoutExpired:
        elapsed = time.time() - started
        total_tests = len(row.get("test_list", [])) or 1
        return {
            "passed": False,
            "passed_tests": 0,
            "failed_tests": total_tests,
            "total_tests": total_tests,
            "message": "TimeoutExpired: {}s while running trace eval".format(timeout + 5),
            "failed_test_details": [],
            "elapsed_seconds": elapsed,
            "timed_out": True,
        }

    elapsed = time.time() - started
    output = (completed.stdout or "") + (completed.stderr or "")
    marker_line = None
    for line in output.splitlines():
        if line.startswith(TRACE_RESULT_MARKER):
            marker_line = line[len(TRACE_RESULT_MARKER) :]
    if marker_line is None:
        total_tests = len(row.get("test_list", [])) or 1
        return {
            "passed": False,
            "passed_tests": 0,
            "failed_tests": total_tests,
            "total_tests": total_tests,
            "message": output.strip() or "docker rc={}".format(completed.returncode),
            "failed_test_details": [],
            "raw_output": output[-4000:],
            "elapsed_seconds": elapsed,
        }

    data = json.loads(marker_line)
    data["passed"] = data["failed_tests"] == 0 and data["total_tests"] > 0
    data["message"] = "passed" if data["passed"] else summarize_trace_failure(data)
    data["elapsed_seconds"] = elapsed
    return data


def shorten(text: Any, max_chars: int = 220) -> str:
    compact = " ".join(str(text or "").split())
    if len(compact) <= max_chars:
        return compact
    return compact[: max_chars - 3] + "..."


def compact_execution_trace(
    eval_result: Dict[str, Any],
    max_failed_tests: int = 3,
    max_frames_per_test: int = 6,
    max_chars: int = 3600,
) -> str:
    details = eval_result.get("failed_test_details") or []
    signature_counter = Counter((item.get("error_type", "Error"), shorten(item.get("error", ""), 120)) for item in details)
    lines = [
        "压缩执行轨迹：",
        "- 测试统计：passed={}，failed={}，total={}".format(
            eval_result.get("passed_tests", 0),
            eval_result.get("failed_tests", 0),
            eval_result.get("total_tests", 0),
        ),
    ]
    if signature_counter:
        lines.append("- 失败签名统计：")
        for (error_type, message), count in signature_counter.most_common(5):
            lines.append("  - {}: {}；样例数={}".format(error_type, message, count))
    for idx, item in enumerate(details[:max_failed_tests], start=1):
        lines.append("- 失败样例 {}：{}".format(idx, item.get("name", "unknown")))
        lines.append("  - 异常：{}".format(item.get("exception_only") or item.get("error_type", "Error")))
        if item.get("error"):
            lines.append("  - 消息：{}".format(shorten(item.get("error"), 260)))
        frames = item.get("frames") or []
        if frames:
            lines.append("  - 相关栈帧：")
            for frame in frames[-max_frames_per_test:]:
                file_name = str(frame.get("file", ""))
                short_file = file_name.replace("/work/", "")
                source = shorten(frame.get("source", ""), 180)
                lines.append(
                    "    - {}@{}:{}: {}".format(frame.get("function", "?"), short_file, frame.get("line", "?"), source)
                )
    text = "\n".join(lines)
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 80] + "\n...（trace 已截断以减少 token）"


def cleanup_sample_intermediates(sample_dir: Path) -> None:
    for path in sample_dir.rglob("count_check.py"):
        path.unlink(missing_ok=True)
    for path in sample_dir.rglob("trace_check.py"):
        path.unlink(missing_ok=True)


def cleanup_output_intermediates(output_dir: Path) -> None:
    return None
