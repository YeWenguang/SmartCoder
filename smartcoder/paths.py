from __future__ import annotations

import os
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_ROOT.parent
WORKSPACE_ROOT = PROJECT_ROOT.parent


def bundled_long_memory_path() -> Path:
    return PACKAGE_ROOT / "data" / "repoexec_long_term_memory.jsonl"


def resolve_repoexec_root(explicit: str = "") -> Path:
    candidates = []
    if explicit:
        candidates.append(Path(explicit))
    env_root = os.environ.get("SMARTCODER_REPOEXEC_ROOT", "").strip()
    if env_root:
        candidates.append(Path(env_root))
    candidates.append(PROJECT_ROOT / "datasets" / "RepoExec_complete")
    candidates.append(PROJECT_ROOT / "datasets" / "RepoExec")
    candidates.append(WORKSPACE_ROOT / "datasets" / "RepoExec")

    for candidate in candidates:
        if candidate.exists():
            return candidate.resolve()
    return candidates[0].resolve() if candidates else (PROJECT_ROOT / "datasets" / "RepoExec").resolve()


def resolve_repoexec_parquet(repo_root: Path, explicit: str = "") -> Path:
    if explicit:
        return Path(explicit).resolve()
    return (repo_root / "hf_dataset" / "data" / "full_context-00000-of-00001.parquet").resolve()
