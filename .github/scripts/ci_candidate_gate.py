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
    if len(args) not in {2, 4} or args[0] not in {"draft", "candidate"}:
        print(
            "usage: ci_candidate_gate.py {draft|candidate} JOB_RESULT [PR_HEAD_SHA CHECKOUT_SHA]",
            file=sys.stderr,
        )
        return 2

    state, job_result = args[:2]
    pr_head_sha, checkout_sha = args[2:] if len(args) == 4 else ("unknown", "unknown")
    sha_summary = f"PR HEAD: {pr_head_sha}\nChecked-out SHA: {checkout_sha}"
    if state == "draft":
        _record(
            "## Validation state: NOT RUN (Draft)\n"
            "The full test job was intentionally skipped. This is not a test pass; "
            "mark the PR Ready for review to run candidate checks.\n"
            f"{sha_summary}"
        )
        return 0

    if job_result != "success":
        _record(
            "## Validation state: FAILED\n"
            f"Required test job result: {job_result}. Candidate verification requires success.\n"
            f"{sha_summary}"
        )
        return 1

    _record(
        "## Validation state: VERIFIED PASS\n"
        "Required candidate test job result: success.\n"
        f"{sha_summary}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
