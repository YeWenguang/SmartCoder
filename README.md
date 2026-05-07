# SMARTCoder

This repository is a submission-oriented implementation of **SMARTCoder** (**S**tateful **M**emory **A**nd **R**easoning with **T**races), a framework for repository-level code generation and repair.

SMARTCoder treats repository-level coding as a **stateful test-time reasoning** problem rather than a pure context-scaling problem. Instead of relying only on larger context windows or stateless iterative prompting, it maintains explicit reasoning state across rounds and combines repository grounding, iterative repair memory, and selective runtime-trace diagnosis.

## Overview

SMARTCoder is designed for repository-level code generation settings where a model must:

- remain consistent with repository-visible symbols and contracts
- preserve useful behavior across multiple repair rounds
- avoid repeating already failed repair attempts
- use runtime traces only when coarse execution feedback is not enough

The implementation in this repository includes the main components described in the paper:

- **Structured repository memory**
  A compact repository-grounding representation built from visible imports, helper functions, classes, constants, regular expressions, docstring examples, and explicit contracts.
- **Working memory**
  A short-term repair state that tracks `best_so_far`, failed repairs, confirmed facts, and the next repair objective.
- **Long-term experience memory**
  Retrieved repair-strategy cards that provide reusable high-level guidance without copying task-specific code.
- **Conditional trace-aware reasoning**
  Runtime traces are introduced only when failures remain ambiguous or repair progress stagnates.
- **Candidate-pool final selection**
  SMARTCoder keeps multiple candidates across rounds and selects the best-performing one instead of assuming the last candidate is optimal.

## Repository Layout

- `smartcoder/`
  Main Python package.
- `smartcoder/repoexec/`
  RepoExec-facing implementation, including prompting, memory management, execution, and the main repair loop.
- `smartcoder/data/repoexec_long_term_memory.jsonl`
  Bundled long-term experience memory used by the current implementation.
- `scripts/run_repoexec_smartcoder.py`
  Thin executable wrapper for running the package without installation.
- `datasets/`
  Local dataset placement area used for experiments.

## Main Entry Points

- Package CLI: `smartcoder repoexec run`
- Script wrapper: `python scripts/run_repoexec_smartcoder.py repoexec run ...`

The current runnable experiment entrypoint is implemented in:

- `smartcoder/repoexec/runner.py`

## Installation

Use Python 3.10 or newer.

Install dependencies:

```bash
pip install -r requirements.txt
```

Or install as a package:

```bash
pip install -e .
```

## Experimental Scope

This package is organized around the **SMARTCoder implementation** and its **RepoExec experimental pipeline**.

The current public artifact supports:

- loading RepoExec executable repository-level tasks
- running SMARTCoder under `full`, `medium`, or `small` repository context levels
- materializing generated completions into executable repository code
- evaluating candidates with Docker-based execution
- recording per-round outputs needed to compute Pass@1, token usage, and runtime statistics

The current package intentionally focuses on the RepoExec portion of the paper experiments.

## Experimental Configuration

The packaged defaults are aligned with the paper's RepoExec setting:

- benchmark: RepoExec
- context level: `full` by default, with `medium` and `small` available through `--context-level`
- repair budget: `4` integrated repair rounds after the baseline candidate
- temperature: `0.0`
- generation budget: `896` max completion tokens per call
- final selection: best candidate in the candidate pool based on execution outcomes

The implementation is compatible with OpenAI-style chat-completions endpoints. The paper-facing models can be supplied through `--model`, for example:

- `qwen-plus-2025-12-01`
- `gemini-2.5-flash`
- `gpt-5.4-mini`

## Running SMARTCoder on RepoExec

### 1. Prepare the dataset

The runner looks for RepoExec in the following order:

1. `--repo-root`
2. `SMARTCODER_REPOEXEC_ROOT`
3. `smartcoder_project/datasets/RepoExec_complete`
4. `smartcoder_project/datasets/RepoExec`
5. `../datasets/RepoExec`

Expected RepoExec contents include:

- `hf_dataset/data/full_context-00000-of-00001.parquet`
- `hf_dataset/data/medium_context-00000-of-00001.parquet`
- `hf_dataset/data/small_context-00000-of-00001.parquet`
- `data_with_test_case/`
- repository project directories under `test-apps/` and related package roots

The dataset is expected to be placed locally for experiments and is not intended to be versioned inside the Git repository.

### 2. Prepare the API environment

Set an OpenAI-compatible API endpoint and key:

```bash
export OPENAI_BASE_URL="YOUR_OPENAI_COMPATIBLE_BASE_URL"
export OPENAI_API_KEY="YOUR_API_KEY"
```

You may also pass them directly with `--base-url` and `--api-key`.

### 3. Prepare Docker

RepoExec execution requires a Docker image compatible with the evaluation environment.

Default image:

```text
codeeval-runner-repoexec-first50
```

You can override it with `--image`.

### 4. Run a small smoke test

```bash
python scripts/run_repoexec_smartcoder.py repoexec run \
  --output-dir ./runs/repoexec_smoke \
  --limit 1 \
  --rounds 2 \
  --context-level full \
  --model qwen-plus \
  --base-url "$OPENAI_BASE_URL" \
  --api-key "$OPENAI_API_KEY"
```

### 5. Run the paper-style RepoExec setting

```bash
python scripts/run_repoexec_smartcoder.py repoexec run \
  --output-dir ./runs/repoexec_full_qwen \
  --start 0 \
  --limit 50 \
  --context-level full \
  --rounds 4 \
  --temperature 0 \
  --model qwen-plus-2025-12-01 \
  --base-url "$OPENAI_BASE_URL" \
  --api-key "$OPENAI_API_KEY" \
  --timeout 120 \
  --image codeeval-runner-repoexec-first50
```

### 6. Run the RepoExec robustness study across context levels

```bash
python scripts/run_repoexec_smartcoder.py repoexec run \
  --output-dir ./runs/repoexec_medium_qwen \
  --start 0 \
  --limit 50 \
  --context-level medium \
  --rounds 4 \
  --temperature 0 \
  --model qwen-plus-2025-12-01 \
  --base-url "$OPENAI_BASE_URL" \
  --api-key "$OPENAI_API_KEY" \
  --timeout 120 \
  --image codeeval-runner-repoexec-first50
```

Replace `medium` with `small` to run the small-context setting.

## Important CLI Arguments

- `--repo-root`
  Path to the RepoExec dataset root.
- `--parquet`
  Path to the specific RepoExec parquet split.
- `--context-level`
  RepoExec repository context granularity: `full`, `medium`, or `small`.
- `--output-dir`
  Output directory for run artifacts.
- `--start`, `--limit`
  Sample range to run.
- `--rounds`
  Maximum number of integrated repair rounds after the baseline candidate. The paper setting uses `4`.
- `--model`
  OpenAI-compatible model name, such as `qwen-plus-2025-12-01`.
- `--temperature`
  Sampling temperature. The paper setting uses `0.0`.
- `--max-tokens`
  Maximum completion length per generation call.
- `--base-url`, `--api-key`
  API endpoint and credential.
- `--trace-limit`, `--trace-max-chars`
  Controls trace-card collection and compression.
- `--continue-after-baseline-pass`
  Forces integrated repair to continue even when the baseline candidate already passes.

## Output Files

Each run writes:

- `metadata.json`
  Run configuration and mechanism metadata.
- `rounds.jsonl`
  Per-candidate execution summaries across baseline, integrated repair, and optional trace repair, including token and runtime fields.
- `generations.json`
  Selected predictions in RepoExec-style output format.
- `processed_generations.json`
  Executable prediction-and-test records.
- `final_records.json`
  Final selected candidate record for each sample.
- `summary.json`
  Aggregate pass statistics for the run.
- per-sample `candidate_pool.json`
  All candidates retained during the repair process.
- per-sample `working_memory.json`
  Final working-memory state for that sample.

## Current Status

The current packaged implementation has been validated locally for:

- CLI loading
- RepoExec dataset loading
- Docker-based execution
- SMARTCoder candidate generation and repair loop
- small-batch smoke experiments with `qwen-plus`

## Notes

- This repository is organized as a clean experimental package rather than a dump of historical scripts.
- Large historical experiment outputs from the original workspace are intentionally excluded from the submission-oriented structure.
- The bundled long-term memory is copied from the original experiment workspace and used as the default retrieval source in the current implementation.
