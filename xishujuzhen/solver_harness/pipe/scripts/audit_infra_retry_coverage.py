#!/usr/bin/env python3
"""Audit tier infrastructure failures against retry code and Redis coverage.

This is the persisted version of the read-only AQL/Redis analysis used by the
409 investigation report.  It reproduces the 155-problem infrastructure pool,
the retry-count distribution, and the missing/covered Redis partition.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path

from tier1_audit_common import (
    INFRA_STATUSES,
    connect_db,
    connect_redis,
    extract_constant_set,
    json_dump,
    last_run_by_problem,
    load_pipe_terminal_runs,
    load_tier_problems,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Audit tier infrastructure failures and retry coverage"
    )
    parser.add_argument("--tier", type=int, default=1)
    parser.add_argument("--export", help="Optional JSON output path")
    args = parser.parse_args()

    db = connect_db()
    redis_client = connect_redis()
    problems = load_tier_problems(db, args.tier)
    all_runs = load_pipe_terminal_runs(db)
    last_runs = last_run_by_problem(all_runs)

    pipe_dir = Path(__file__).resolve().parents[1]
    retry_statuses = extract_constant_set(
        pipe_dir / "retry_infrastructure.py", "INFRA_FAILURES"
    )
    collector_statuses = extract_constant_set(pipe_dir / "collector.py", "INFRA_FAILURES")

    runs_by_problem: dict[str, list[dict]] = defaultdict(list)
    for run in all_runs:
        runs_by_problem[str(run.get("problem_id"))].append(run)

    failed_raw = redis_client.lrange("math:failed", 0, -1)
    failed_items: list[dict] = []
    malformed_failed_items = 0
    for item in failed_raw:
        try:
            value = json.loads(item)
            if isinstance(value, dict):
                failed_items.append(value)
        except (json.JSONDecodeError, TypeError):
            malformed_failed_items += 1

    failed_by_problem: dict[str, dict] = {}
    failed_occurrences = Counter()
    for item in failed_items:
        key = item.get("problem_key")
        if key:
            key = str(key)
            failed_by_problem[key] = item
            failed_occurrences[key] += 1

    details: list[dict] = []
    for key, problem in problems.items():
        last = last_runs.get(key)
        if not last or last.get("status") not in INFRA_STATUSES:
            continue
        runs = runs_by_problem.get(key, [])
        retry_count_matching_code = sum(
            1 for run in runs if run.get("verdict") in retry_statuses
        )
        infra_status_count = sum(1 for run in runs if run.get("status") in INFRA_STATUSES)
        redis_item = failed_by_problem.get(key)
        details.append(
            {
                "problem_key": key,
                "source_dataset": problem.get("source_dataset"),
                "last_status": last.get("status"),
                "last_ended_at": last.get("ended_at"),
                "last_exp_id": last.get("exp_id"),
                "terminal_run_count": len(runs),
                "infra_status_count": infra_status_count,
                "retry_count_matching_code": retry_count_matching_code,
                "redis_failed_present": redis_item is not None,
                "redis_failed_verdict": redis_item.get("verdict") if redis_item else None,
                "redis_failed_occurrences": failed_occurrences.get(key, 0),
            }
        )

    present = [row for row in details if row["redis_failed_present"]]
    absent = [row for row in details if not row["redis_failed_present"]]
    exhausted = [row for row in details if row["retry_count_matching_code"] >= 6]
    crash_omitted = [
        row
        for row in details
        if row["last_status"] == "crash_recovered"
        and "crash_recovered" not in retry_statuses
    ]
    below_limit_absent = [
        row
        for row in absent
        if row["retry_count_matching_code"] < 6
        and row["last_status"] in retry_statuses
    ]

    result = {
        "audit": "tier infrastructure retry coverage",
        "tier": args.tier,
        "database_problem_count": len(problems),
        "code_status_sets": {
            "collector_infra_statuses": sorted(collector_statuses),
            "retry_infra_statuses": sorted(retry_statuses),
            "collector_minus_retry": sorted(collector_statuses - retry_statuses),
            "report_expected_infra_statuses": sorted(INFRA_STATUSES),
        },
        "summary": {
            "infra_failure_problems": len(details),
            "reached_retry_count_6_or_more": len(exhausted),
            "crash_recovered_omitted_by_retry_code": len(crash_omitted),
            "below_limit_but_absent_from_redis_failed": len(below_limit_absent),
            "redis_failed_present_unique": len(present),
            "redis_failed_absent_unique": len(absent),
            "redis_failed_list_length": len(failed_raw),
            "redis_failed_malformed_items": malformed_failed_items,
        },
        "breakdowns": {
            "last_status": dict(Counter(row["last_status"] for row in details)),
            "source_dataset": dict(
                Counter(row["source_dataset"] for row in details)
            ),
            "retry_count_matching_code": dict(
                sorted(
                    Counter(
                        str(row["retry_count_matching_code"]) for row in details
                    ).items(),
                    key=lambda item: int(item[0]),
                )
            ),
            "redis_presence_by_status": [
                {
                    "redis": redis_state,
                    "retry_count": retry_count,
                    "status": status,
                    "count": count,
                }
                for (redis_state, retry_count, status), count in sorted(
                    Counter(
                        (
                            "present" if row["redis_failed_present"] else "absent",
                            row["retry_count_matching_code"],
                            row["last_status"],
                        )
                        for row in details
                    ).items()
                )
            ],
            "utc_hour": [
                {"utc_hour": hour, "status": status, "count": count}
                for (hour, status), count in sorted(
                    Counter(
                        ((row["last_ended_at"] or "")[:13], row["last_status"])
                        for row in details
                    ).items()
                )
            ],
        },
        "problems": sorted(details, key=lambda row: row["problem_key"]),
    }
    json_dump(result, args.export)


if __name__ == "__main__":
    main()
