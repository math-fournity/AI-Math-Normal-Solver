#!/usr/bin/env python3
"""Audit candidate_solved runs that miss one of the standard retention files.

The script distinguishes a cosmetic/replaceable omission (for example a pane
snapshot when export and thinking are complete) from loss of the core Profile
asset (reasoning).  It reproduces the 21-versus-2 split in report 409.
"""

from __future__ import annotations

import argparse
from collections import Counter

from tier1_audit_common import (
    TRAJECTORY_BASE,
    connect_db,
    inspect_candidate_asset,
    json_dump,
    last_run_by_problem,
    load_pipe_terminal_runs,
    load_tier_problems,
)


def standard_missing_files(exp_id: str) -> list[str]:
    trajectory_dir = TRAJECTORY_BASE / exp_id
    missing: list[str] = []
    if not (trajectory_dir / "session_info.json").is_file():
        missing.append("session_info")
    export_dir = trajectory_dir / "exports"
    if not export_dir.is_dir() or not any(path.is_file() for path in export_dir.iterdir()):
        missing.append("export")
    collector_dir = trajectory_dir / "collector"
    if not collector_dir.is_dir() or not any(
        path.is_file() for path in collector_dir.iterdir()
    ):
        missing.append("pane")
    tmux_dir = trajectory_dir / "tmux"
    if not tmux_dir.is_dir() or not any(path.is_file() for path in tmux_dir.iterdir()):
        missing.append("tmux")
    return missing


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Audit standard-file omissions in tier candidate_solved runs"
    )
    parser.add_argument("--tier", type=int, default=1)
    parser.add_argument("--export", help="Optional JSON output path")
    args = parser.parse_args()

    db = connect_db()
    problems = load_tier_problems(db, args.tier)
    last_runs = last_run_by_problem(load_pipe_terminal_runs(db))
    details: list[dict] = []
    candidate_count = 0
    for key, problem in problems.items():
        run = last_runs.get(key)
        if not run or run.get("status") != "candidate_solved":
            continue
        candidate_count += 1
        exp_id = str(run.get("exp_id"))
        missing = standard_missing_files(exp_id)
        if not missing:
            continue
        asset = inspect_candidate_asset(exp_id)
        details.append(
            {
                "problem_key": key,
                "source_dataset": problem.get("source_dataset"),
                "exp_id": run.get("exp_id"),
                "ended_at": run.get("ended_at"),
                "standard_missing_files": missing,
                "core_asset_valid": asset["valid_reasoning_proof_no_tools"],
                "requires_rerun": not asset["valid_reasoning_proof_no_tools"],
                "asset": asset,
            }
        )

    result = {
        "audit": "candidate_solved standard missing assets",
        "tier": args.tier,
        "summary": {
            "candidate_solved": candidate_count,
            "standard_missing_total": len(details),
            "core_asset_valid_no_rerun": sum(
                row["core_asset_valid"] for row in details
            ),
            "core_asset_invalid_requires_rerun": sum(
                row["requires_rerun"] for row in details
            ),
        },
        "missing_file_breakdown": dict(
            Counter(name for row in details for name in row["standard_missing_files"])
        ),
        "problems": sorted(details, key=lambda row: row["problem_key"]),
    }
    json_dump(result, args.export)


if __name__ == "__main__":
    main()
