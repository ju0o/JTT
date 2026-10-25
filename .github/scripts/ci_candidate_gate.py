"""Report whether candidate CI ran and fail closed on any non-success result."""

import os
import sys
from pathlib import Path


def _record(message: str) -> None:
    print(message)
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with Path(summary_path).open("a", encoding="utf-8") as summary:
            summary.write(message + "\n")


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 2 or args[0] not in {"draft", "candidate"}:
        print("usage: ci_candidate_gate.py {draft|candidate} JOB_RESULT", file=sys.stderr)
        return 2

    state, job_result = args
    if state == "draft":
        _record(
            "## Validation state: NOT RUN (Draft)\n"
            "The full test job was intentionally skipped. This is not a test pass; "
            "mark the PR Ready for review to run candidate checks."
        )
        return 0

    if job_result != "success":
        _record(
            "## Validation state: FAILED\n"
            f"Required test job result: {job_result}. Candidate verification requires success."
        )
        return 1

    _record("## Validation state: VERIFIED PASS\nRequired candidate test job result: success.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
