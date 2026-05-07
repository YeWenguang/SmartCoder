# RepoExec Dataset Placement

Place the official RepoExec dataset directory here as:

```text
smartcoder_project/datasets/RepoExec/
```

At minimum, the SMARTCoder runner expects:

- `hf_dataset/data/full_context-00000-of-00001.parquet`
- `hf_dataset/data/medium_context-00000-of-00001.parquet`
- `hf_dataset/data/small_context-00000-of-00001.parquet`
- `data_with_test_case/`
- project source directories used by RepoExec execution

If you already have RepoExec elsewhere, you can pass `--repo-root` or set:

```bash
export SMARTCODER_REPOEXEC_ROOT=/path/to/RepoExec
```
