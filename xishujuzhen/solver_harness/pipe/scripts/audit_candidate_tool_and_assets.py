#!/usr/bin/env python3
"""Audit current tier candidate_solved assets, tool use, and prior reuse.

The completion checker only tests file presence.  This script parses the ATIF
export (with historical fallbacks) and enforces the experiment contract:

* retained reasoning is non-empty;
* a proof marker or proof asset exists;
* no tool call was made.

It also checks whether an invalid current candidate can be replaced by a valid
historical non-pipe candidate for the same problem.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter

from tier1_audit_common import (
    inspect_candidate_asset,
    json_dump,
    last_run_by_problem,
    load_nonpipe_runs_for_problems,
    load_pipe_terminal_runs,
    load_tier_problems,
    connect_db,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Audit tier candidate_solved reasoning, proof, and tool calls"
    )
    parser.add_argument("--tier", type=int, default=1)
    parser.add_argument("--export", help="Optional JSON output path")
    parser.add_argument(
        "--include-valid",
        action="store_true",
        help="Include all valid candidate rows in JSON (large output)",
    )
    args = parser.parse_args()

    db = connect_db()
    problems = load_tier_problems(db, args.tier)
    last_runs = last_run_by_problem(load_pipe_terminal_runs(db))
    candidates = [
        (key, problem, last_runs[key])
        for key, problem in problems.items()
        if key in last_runs and last_runs[key].get("status") == "candidate_solved"
    ]

    audited: list[dict] = []
    for index, (key, problem, run) in enumerate(candidates, 1):
        if index % 1000 == 0:
            print(f"candidate asset progress: {index}/{len(candidates)}", file=sys.stderr)
        asset = inspect_candidate_asset(str(run.get("exp_id")))
        audited.append(
            {
                "problem_key": key,
                "source_dataset": problem.get("source_dataset"),
                "ended_at": run.get("ended_at"),
                "exp_id": run.get("exp_id"),
                "asset": asset,
            }
        )

    invalid = [
        row for row in audited if not row["asset"]["valid_reasoning_proof_no_tools"]
    ]
    tool_rows = [row for row in audited if row["asset"]["tool_names"]]
    missing_reasoning = [row for row in audited if not row["asset"]["has_reasoning"]]
    missing_proof = [row for row in audited if not row["asset"]["has_proof"]]

    invalid_problems = {row["problem_key"]: problems[row["problem_key"]] for row in invalid}
    historical = load_nonpipe_runs_for_problems(db, invalid_problems)
    valid_prior_by_problem: dict[str, list[str]] = {}
    for key, runs in historical.items():
        valid_exp_ids: list[str] = []
        for run in runs:
            if run.get("status") != "candidate_solved" or not run.get("exp_id"):
                continue
            if inspect_candidate_asset(str(run["exp_id"]))[
                "valid_reasoning_proof_no_tools"
            ]:
                valid_exp_ids.append(str(run["exp_id"]))
        if valid_exp_ids:
            valid_prior_by_problem[key] = valid_exp_ids

    only_read = [
        row for row in tool_rows if set(row["asset"]["tool_names"]) <= {"read"}
    ]
    non_read = [
        row for row in tool_rows if not set(row["asset"]["tool_names"]) <= {"read"}
    ]
    tool_names = Counter(
        call.get("name", "<unknown>")
        for row in tool_rows
        for call in row["asset"].get("tool_calls", [])
    )

    result = {
        "audit": "current candidate reasoning/proof/tool contract",
        "tier": args.tier,
        "summary": {
            "candidate_solved": len(audited),
            "valid_reasoning_proof_no_tools": len(audited) - len(invalid),
            "invalid_total": len(invalid),
            "tool_use_problems": len(tool_rows),
            "tool_use_only_read": len(only_read),
            "tool_use_non_read": len(non_read),
            "missing_reasoning": len(missing_reasoning),
            "missing_proof": len(missing_proof),
            "invalid_with_valid_historical_candidate": len(valid_prior_by_problem),
            "must_rerun_after_historical_reuse": len(invalid)
            - len(valid_prior_by_problem),
        },
        "breakdowns": {
            "tool_names": dict(tool_names),
            "tool_use_by_source": dict(
                Counter(row["source_dataset"] for row in tool_rows)
            ),
            "tool_use_by_ended_date": dict(
                Counter((row["ended_at"] or "")[:10] for row in tool_rows)
            ),
            "tool_call_count_per_problem": dict(
                sorted(
                    Counter(
                        str(len(row["asset"].get("tool_calls", [])))
                        for row in tool_rows
                    ).items(),
                    key=lambda item: int(item[0]),
                )
            ),
        },
        "valid_historical_reuse": valid_prior_by_problem,
        "invalid_candidates": sorted(invalid, key=lambda row: row["problem_key"]),
    }
    if args.include_valid:
        result["valid_candidates"] = sorted(
            (row for row in audited if row not in invalid),
            key=lambda row: row["problem_key"],
        )
    json_dump(result, args.export)


if __name__ == "__main__":
    main()
