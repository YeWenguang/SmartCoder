from __future__ import annotations

import os

from smartcoder.repoexec.runner import build_parser, run_repoexec


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if getattr(args, "command", None) == "repoexec" and getattr(args, "repoexec_command", None) == "run":
        if not args.base_url:
            args.base_url = os.environ.get("OPENAI_BASE_URL", "")
        return run_repoexec(args)

    parser.print_help()
    return 1
