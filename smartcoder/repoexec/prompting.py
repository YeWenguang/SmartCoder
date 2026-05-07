from __future__ import annotations

import ast
import json
import random
import re
import sys
import textwrap
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


STOP_WORDS = ["\nclass", "\ndef", "\n#", "\n@", "\nprint", "\nif", "\n```"]


def build_baseprompt(row: Dict[str, Any]) -> str:
    return (
        "Complete below code for function `{}`. "
        "Only reponse with the complete part not the whole code or function signature.\n\n{}"
    ).format(row["entry_point"], row["prompt"])


def should_keep_constant_name(name: str) -> bool:
    return (
        name.isupper()
        or name.startswith("_")
        or name.endswith("_RE")
        or "_RE" in name
        or name.endswith("_CHARS")
        or name.endswith("_MAP")
    )


def function_signature(node: ast.FunctionDef) -> str:
    try:
        args = ast.unparse(node.args)
        returns = " -> {}".format(ast.unparse(node.returns)) if node.returns else ""
        return "def {}({}){}:".format(node.name, args, returns)
    except Exception:
        return "def {}(...):".format(node.name)


def decorator_names(node: ast.FunctionDef) -> List[str]:
    names = []
    for decorator in node.decorator_list:
        try:
            names.append(ast.unparse(decorator))
        except Exception:
            continue
    return names


def first_arg_name(node: ast.FunctionDef) -> str:
    if not node.args.args:
        return ""
    return node.args.args[0].arg


def first_line(text: str, max_chars: int = 160) -> str:
    compact = " ".join((text or "").strip().split())
    if len(compact) <= max_chars:
        return compact
    return compact[: max_chars - 3] + "..."


def value_summary(node: ast.AST, max_chars: int = 180) -> str:
    try:
        text = ast.unparse(node)
    except Exception:
        text = type(node).__name__
    return first_line(text, max_chars=max_chars)


def extract_fallback_definitions(prompt: str) -> Dict[str, Any]:
    classes = []
    functions = []
    imports = []
    constants = []
    for line in prompt.splitlines():
        stripped = line.strip()
        if stripped.startswith(("import ", "from ")):
            imports.append(stripped)
        class_match = re.match(r"class\s+([A-Za-z_][A-Za-z0-9_]*)(?:\((.*?)\))?:", stripped)
        if class_match:
            classes.append(
                {"name": class_match.group(1), "base": class_match.group(2) or "", "summary": "", "methods": []}
            )
        fn_match = re.match(r"def\s+([A-Za-z_][A-Za-z0-9_]*)\s*(\(.*)", stripped)
        if fn_match:
            functions.append(
                {"name": fn_match.group(1), "signature": "def " + fn_match.group(1) + fn_match.group(2), "summary": ""}
            )
        const_match = re.match(r"([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.+)", stripped)
        if const_match and should_keep_constant_name(const_match.group(1)):
            constants.append({"name": const_match.group(1), "value": const_match.group(2), "summary": ""})
    return {"imports": imports, "classes": classes, "functions": functions, "constants": constants}


def extract_prompt_symbols(prompt: str) -> Dict[str, Any]:
    try:
        tree = ast.parse(prompt)
    except SyntaxError:
        return extract_fallback_definitions(prompt)

    imports = []
    classes = []
    functions = []
    constants = []

    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append("import {}{}".format(alias.name, " as " + alias.asname if alias.asname else ""))
        elif isinstance(node, ast.ImportFrom):
            module = "." * node.level + (node.module or "")
            names = ", ".join(
                alias.name + (" as " + alias.asname if alias.asname else "")
                for alias in node.names
            )
            imports.append("from {} import {}".format(module, names))
        elif isinstance(node, ast.ClassDef):
            base_names = []
            for base in node.bases:
                try:
                    base_names.append(ast.unparse(base))
                except Exception:
                    base_names.append("")
            classes.append(
                {
                    "name": node.name,
                    "base": ", ".join([name for name in base_names if name]),
                    "summary": ast.get_docstring(node) or "",
                    "methods": [
                        {
                            "name": item.name,
                            "signature": function_signature(item),
                            "summary": ast.get_docstring(item) or "",
                            "decorators": ", ".join(decorator_names(item)),
                            "first_arg": first_arg_name(item),
                        }
                        for item in node.body
                        if isinstance(item, ast.FunctionDef)
                    ],
                }
            )
        elif isinstance(node, ast.FunctionDef):
            functions.append({"name": node.name, "signature": function_signature(node), "summary": ast.get_docstring(node) or ""})
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and should_keep_constant_name(target.id):
                    constants.append({"name": target.id, "value": value_summary(node.value), "summary": ""})
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if should_keep_constant_name(node.target.id):
                constants.append(
                    {"name": node.target.id, "value": value_summary(node.value) if node.value else "", "summary": ""}
                )

    return {"imports": imports, "classes": classes, "functions": functions, "constants": constants}


def gpt_code_parser(response: str) -> str:
    if "```python" in response:
        parsed_code = response[response.index("```python") + len("```python") :]
        fence_index = parsed_code.rfind("```")
        return parsed_code[:fence_index] if fence_index != -1 else parsed_code
    if "```" in response:
        parsed_code = response[response.index("```") + len("```") :]
        fence_index = parsed_code.rfind("```")
        return parsed_code[:fence_index] if fence_index != -1 else parsed_code
    return response


def extract_function(source: str, entry_point: str) -> Optional[str]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == entry_point:
            segment = ast.get_source_segment(source, node)
            if segment:
                return segment
    return None


def materialize_solution(row: Dict[str, Any], prediction: str) -> str:
    parsed = gpt_code_parser(prediction).strip("\n")
    full_fn = extract_function(parsed, str(row["entry_point"]))
    if full_fn:
        return full_fn
    target_prompt = str(row["target_function_prompt"]).rstrip()
    if parsed.strip().startswith("return ") or not parsed.startswith("    "):
        parsed = textwrap.indent(parsed.strip(), prefix="    ")
    return target_prompt + "\n" + parsed.rstrip() + "\n"


def get_actual_solution(row: Dict[str, Any]) -> str:
    solution = str(row["solution"])
    check = str(row["check"])
    if solution in check:
        return solution
    parsed = extract_function(check, str(row["entry_point"]))
    if parsed:
        return parsed
    return solution


def build_test_program(row: Dict[str, Any], solution_fn: str) -> str:
    check = str(row["check"])
    actual_solution = get_actual_solution(row)
    if actual_solution not in check:
        raise ValueError("Cannot find actual solution for {} in check program".format(row["entry_point"]))
    return check.replace(actual_solution, solution_fn, 1)


def extract_doc_examples(docstring: str, max_examples: int = 8) -> List[str]:
    examples = []
    for line in (docstring or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith(">>>") or "# returns" in stripped or stripped.startswith("- "):
            examples.append(stripped)
        if len(examples) >= max_examples:
            break
    return examples


def extract_raise_contracts(prompt: str, max_items: int = 8) -> List[str]:
    contracts = []
    for line in prompt.splitlines():
        stripped = line.strip()
        if stripped.startswith("raise "):
            contracts.append(stripped)
        if len(contracts) >= max_items:
            break
    return contracts


def build_repo_memory_card_v2(row: Dict[str, Any]) -> str:
    symbols = extract_prompt_symbols(row["prompt"])
    target = row["entry_point"]
    dependency_functions = [fn for fn in symbols.get("functions", []) if isinstance(fn, dict) and fn.get("name") != target]
    classes = [cls for cls in symbols.get("classes", []) if isinstance(cls, dict)]
    imports = [item for item in symbols.get("imports", []) if isinstance(item, str)]
    constants = [item for item in symbols.get("constants", []) if isinstance(item, dict)]
    doc_examples = extract_doc_examples(str(row.get("docstring") or row.get("original_docstring") or ""))
    raise_contracts = extract_raise_contracts(str(row["prompt"]))

    function_lines = [
        "- {}；用途：{}".format(fn["signature"], first_line(fn.get("summary", "prompt 中可见函数"), 220))
        for fn in dependency_functions[:16]
    ]
    import_lines = ["- {}".format(item) for item in imports[:20]]
    constant_lines = ["- {} = {}".format(item["name"], first_line(item.get("value", ""), 220)) for item in constants[:16]]
    class_lines = []
    class_method_names = []
    for cls in classes[:10]:
        cls_head = "- {}{}；用途：{}".format(
            cls["name"],
            "({})".format(cls["base"]) if cls.get("base") else "",
            first_line(cls.get("summary", "prompt 中可见类"), 180),
        )
        class_lines.append(cls_head)
        methods = [item for item in cls.get("methods", []) if isinstance(item, dict)]
        for method in methods[:8]:
            decorators = method.get("decorators") or "无显式装饰器"
            first_arg = method.get("first_arg") or "无"
            class_method_names.append("{}.{}".format(cls["name"], method["name"]))
            class_lines.append(
                "  - 方法 {}.{}：{}；首参={}；装饰器={}；用途：{}".format(
                    cls["name"],
                    method["name"],
                    method["signature"],
                    first_arg,
                    decorators,
                    first_line(method.get("summary", ""), 160),
                )
            )

    candidate_names = (
        [fn["name"] for fn in dependency_functions[:8]]
        + [item["name"] for item in constants[:8]]
        + class_method_names[:8]
        + [cls["name"] for cls in classes[:4]]
    )
    candidates = ", ".join(candidate_names) if candidate_names else "无明显候选依赖"
    doc_lines = ["- {}".format(item) for item in doc_examples]
    raise_lines = ["- {}".format(item) for item in raise_contracts]

    return "\n".join(
        [
            "结构化仓库记忆 v2（仓库契约卡）：",
            "目标任务：",
            "- 项目：{}".format(row["project"]),
            "- 模块：{}".format(row["module"]),
            "- 函数：{}".format(row["entry_point"]),
            "- 签名：{}".format(row["function_signature"]),
            "- 需求摘要：{}".format(first_line(str(row["docstring"]), 320)),
            "docstring 示例/显式规则：",
            *(doc_lines or ["- 无显式示例"]),
            "可见 import：",
            *(import_lines or ["- 无"]),
            "可见常量/正则/表：",
            *(constant_lines or ["- 无"]),
            "可见类与类内方法：",
            *(class_lines or ["- 无"]),
            "可见依赖函数：",
            *(function_lines or ["- 无"]),
            "可见异常/边界契约：",
            *(raise_lines or ["- prompt 未显示显式 raise 语句；不要自行添加比 docstring 更严格的异常或校验。"]),
            "候选依赖：{}".format(candidates),
            "生成约束：",
            "- 保持目标函数签名，只输出目标函数需要补全的实现部分。",
            "- 优先复用可见 helper、类方法、常量和正则；不要发明 prompt 中不存在的符号、常量或正则 group。",
            "- 如果已有 helper/regex 能表达规则，优先相信它，不要再手写更严格的额外校验。",
            "- 不要添加 docstring、示例或可见正则没有明确要求的约束。",
            "- 如果抛出异常，复用可见异常类型和消息风格；不要随意改写异常消息文本。",
            "- 类内方法调用要尊重签名和首参；不要随意实例化 helper class，也不要凭空多传 self/cls。",
        ]
    )


def load_long_memory_cards(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    cards = []
    with path.open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                item = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise ValueError("Invalid JSON in {}:{}: {}".format(path, line_no, exc))
            if isinstance(item, dict):
                cards.append(item)
    return cards


def as_string_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value if item is not None]
    return [str(value)]


def contains_text(haystack: str, needle: str) -> bool:
    if not needle:
        return False
    return needle.lower() in haystack.lower()


def score_long_memory_card(row: Dict[str, Any], card: Dict[str, Any], query_text: str) -> Tuple[float, List[str]]:
    prompt_text = str(row.get("prompt") or "")
    required_symbols = as_string_list(card.get("requires_visible_symbols"))
    missing_required = [symbol for symbol in required_symbols if not contains_text(prompt_text, symbol)]
    if missing_required:
        return 0.0, []

    score = 0.0
    matched = []
    entry_points = as_string_list(card.get("entry_points"))
    if entry_points and str(row.get("entry_point")) not in entry_points:
        return 0.0, []
    if str(row.get("entry_point")) in entry_points:
        score += 8.0
        matched.append("entry_point={}".format(row.get("entry_point")))

    modules = as_string_list(card.get("modules"))
    row_module = str(row.get("module") or "")
    if modules and not any(module and module in row_module for module in modules):
        return 0.0, []
    for module in modules:
        if module and module in row_module:
            score += 2.0
            matched.append("module={}".format(module))

    projects = as_string_list(card.get("projects"))
    row_project = str(row.get("project") or "")
    if projects and not any(project and project in row_project for project in projects):
        return 0.0, []
    for project in projects:
        if project and project in row_project:
            score += 2.0
            matched.append("project={}".format(project))

    if required_symbols:
        score += len(required_symbols) * 1.5
        matched.extend(["visible={}".format(symbol) for symbol in required_symbols])

    trigger = card.get("trigger") if isinstance(card.get("trigger"), dict) else {}
    keywords = as_string_list(card.get("keywords")) + as_string_list(trigger.get("keywords")) + as_string_list(card.get("symbols"))
    for keyword in keywords:
        if contains_text(query_text, keyword):
            score += 2.0 if len(keyword) > 3 else 1.0
            matched.append(keyword)

    success_count = float(card.get("success_count") or 0)
    failure_count = float(card.get("failure_count") or 0)
    score += min(success_count, 10.0) * 0.2
    score -= min(failure_count, 10.0) * 0.6
    return max(score, 0.0), matched


def build_long_memory_card_v2(row: Dict[str, Any], memory_path: Path, max_cards: int = 3) -> str:
    cards = load_long_memory_cards(memory_path)
    if not cards:
        return ""
    query_text = "\n".join(
        [
            str(row.get("project") or ""),
            str(row.get("module") or ""),
            str(row.get("entry_point") or ""),
            str(row.get("function_signature") or ""),
            str(row.get("docstring") or ""),
            str(row.get("prompt") or ""),
        ]
    )
    scored = []
    for card in cards:
        score, matched = score_long_memory_card(row, card, query_text)
        if score > 0:
            scored.append((score, matched, card))
    scored.sort(
        key=lambda item: (item[0], float(item[2].get("success_count") or 0) - float(item[2].get("failure_count") or 0)),
        reverse=True,
    )
    selected = scored[:max_cards]
    if not selected:
        return ""
    lines = ["长期经验记忆 v2（按当前任务检索；只提供策略，不替代当前仓库事实）："]
    for score, matched, card in selected:
        card_id = card.get("id", "unknown")
        lesson = first_line(str(card.get("lesson") or ""), 260)
        bad_pattern = first_line(str(card.get("bad_pattern") or ""), 200)
        fix_pattern = first_line(str(card.get("fix_pattern") or ""), 220)
        matched_text = ", ".join(dict.fromkeys(matched[:8])) or "任务上下文相似"
        lines.append("- [{}] score={:.1f}；匹配：{}".format(card_id, score, matched_text))
        if lesson:
            lines.append("  经验：{}".format(lesson))
        if bad_pattern:
            lines.append("  避免：{}".format(bad_pattern))
        if fix_pattern:
            lines.append("  建议：{}".format(fix_pattern))
    lines.append("使用约束：如果经验提到的符号没有出现在当前 prompt 或结构化仓库记忆中，不要使用；以当前样本的可见代码、docstring 和测试反馈为准。")
    return "\n".join(lines)


def stop_at_stop_token(text: str) -> str:
    min_stop_index = len(text)
    for token in STOP_WORDS:
        index = text.find(token)
        if index != -1 and index < min_stop_index:
            min_stop_index = index
    return text[:min_stop_index]


def solution_to_api_prediction(row: Dict[str, Any], solution_fn: str) -> str:
    target_prompt = str(row.get("target_function_prompt") or "").rstrip()
    if target_prompt and solution_fn.startswith(target_prompt):
        body = solution_fn[len(target_prompt) :].strip("\n")
        return body
    return solution_fn


def extract_usage(response: Any) -> Dict[str, int]:
    usage = getattr(response, "usage", None)
    if usage is None and isinstance(response, dict):
        usage = response.get("usage")

    def coerce_int(value: Any) -> int:
        try:
            return int(value)
        except Exception:
            return 0

    if isinstance(usage, dict):
        return {
            "prompt_tokens": coerce_int(usage.get("prompt_tokens")),
            "completion_tokens": coerce_int(usage.get("completion_tokens")),
            "total_tokens": coerce_int(usage.get("total_tokens")),
        }
    return {
        "prompt_tokens": coerce_int(getattr(usage, "prompt_tokens", None) if usage is not None else None),
        "completion_tokens": coerce_int(getattr(usage, "completion_tokens", None) if usage is not None else None),
        "total_tokens": coerce_int(getattr(usage, "total_tokens", None) if usage is not None else None),
    }


def request_with_retry_detailed(
    client: Any,
    model: str,
    prompt: str,
    max_tokens: int,
    temperature: float,
    retries: int,
    sleep_base: float,
) -> Dict[str, Any]:
    last_error = None
    started = time.time()
    for attempt in range(1, retries + 1):
        try:
            response = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                temperature=temperature,
                max_tokens=max_tokens,
            )
            content = response.choices[0].message.content or ""
            return {
                "content": content,
                "attempts": attempt,
                "error": None,
                "usage": extract_usage(response),
                "generation_elapsed_seconds": time.time() - started,
            }
        except Exception as exc:
            last_error = "{}: {}".format(type(exc).__name__, exc)
            if attempt >= retries:
                break
            sleep_s = sleep_base * attempt + random.uniform(0, 0.5)
            print(
                "[retry] attempt {}/{} failed: {}; sleep {:.1f}s".format(attempt, retries, last_error, sleep_s),
                file=sys.stderr,
                flush=True,
            )
            time.sleep(sleep_s)
    return {
        "content": "",
        "attempts": retries,
        "error": last_error,
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        "generation_elapsed_seconds": time.time() - started,
    }
