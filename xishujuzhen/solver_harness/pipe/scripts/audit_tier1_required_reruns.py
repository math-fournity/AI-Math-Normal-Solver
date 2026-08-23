#!/usr/bin/env python3
"""Authoritative reproducible audit of tier content that must run again.

This script extends ``check_tier1_completion.py`` in three material ways:

1. it parses retained content and rejects candidate_solved runs with missing
   reasoning or any tool call;
2. it reuses valid historical non-pipe assets instead of treating every absent
   pipe run as never processed;
3. it keeps documented model failures as Profile data rather than rerun work.

The output is a complete 39,831-problem partition.  No database, Redis, or
trajectory data is modified.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from typing import Any

from tier1_audit_common import (
    DATA_STATUSES,
    INFRA_STATUSES,
    MODEL_STATUSES,
    NONTERMINAL_STATUSES,
    connect_db,
    inspect_candidate_asset,
    json_dump,
    last_run_by_problem,
    load_nonpipe_runs_for_problems,
    load_pipe_terminal_runs,
    load_tier_problems,
)


def compact_asset(asset: dict[str, Any], include_full: bool) -> dict[str, Any]:
    if include_full:
        return asset
    return {
        "exp_id": asset["exp_id"],
        "valid_reasoning_proof_no_tools": asset[
            "valid_reasoning_proof_no_tools"
        ],
        "reasoning_sources": asset["reasoning_sources"],
        "proof_sources": asset["proof_sources"],
        "tool_names": asset["tool_names"],
        "export_structure": asset["export_structure"],
    }


def invalid_candidate_classification(scope: str, asset: dict[str, Any]) -> str:
    if asset["tool_names"]:
        return f"must_run_{scope}_candidate_tool_use"
    if not asset["has_reasoning"]:
        return f"must_run_{scope}_candidate_missing_reasoning"
    if not asset["has_proof"]:
        return f"must_run_{scope}_candidate_missing_proof"
    return f"must_run_{scope}_candidate_invalid_other"


def find_valid_historical_candidates(
    runs_by_problem: dict[str, list[dict[str, Any]]],
    inspect,
) -> dict[str, dict[str, Any]]:
    reusable: dict[str, dict[str, Any]] = {}
    for key, runs in runs_by_problem.items():
        for run in reversed(runs):
            if run.get("status") != "candidate_solved" or not run.get("exp_id"):
                continue
            asset = inspect(str(run["exp_id"]))
            if asset["valid_reasoning_proof_no_tools"]:
                reusable[key] = {"run": run, "asset": asset}
                break
    return reusable


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Full tier rerun requirement audit with historical asset reuse"
    )
    parser.add_argument("--tier", type=int, default=1)
    parser.add_argument("--export", help="Optional JSON output path")
    parser.add_argument(
        "--full-valid-assets",
        action="store_true",
        help="Include full file-size metadata for valid candidates (large JSON)",
    )
    args = parser.parse_args()

    db = connect_db()
    problems = load_tier_problems(db, args.tier)
    pipe_runs = load_pipe_terminal_runs(db)
    pipe_last = last_run_by_problem(pipe_runs)
    asset_cache: dict[str, dict[str, Any]] = {}

    def inspect(exp_id: str) -> dict[str, Any]:
        if exp_id not in asset_cache:
            asset_cache[exp_id] = inspect_candidate_asset(exp_id)
        return asset_cache[exp_id]

    classifications: dict[str, dict[str, Any]] = {}
    provisional_pipe_must_run: dict[str, dict[str, Any]] = {}
    candidate_index = 0
    for key, problem in problems.items():
        run = pipe_last.get(key)
        if run is None:
            continue
        status = run.get("status")
        base = {
            "problem_key": key,
            "problem_id": problem.get("problem_id"),
            "source_dataset": problem.get("source_dataset"),
            "pipe_last_status": status,
            "pipe_last_exp_id": run.get("exp_id"),
            "pipe_last_ended_at": run.get("ended_at"),
        }
        if status == "candidate_solved":
            candidate_index += 1
            if candidate_index % 1000 == 0:
                print(
                    f"pipe candidate progress: {candidate_index}",
                    file=sys.stderr,
                )
            asset = inspect(str(run.get("exp_id")))
            if asset["valid_reasoning_proof_no_tools"]:
                classifications[key] = {
                    **base,
                    "group": "valid_completed",
                    "classification": "valid_completed_pipe",
                    "asset": compact_asset(asset, args.full_valid_assets),
                }
            else:
                row = {
                    **base,
                    "group": "must_run",
                    "classification": invalid_candidate_classification(
                        "pipe", asset
                    ),
                    "asset": asset,
                }
                classifications[key] = row
                provisional_pipe_must_run[key] = problem
        elif status in INFRA_STATUSES:
            row = {
                **base,
                "group": "must_run",
                "classification": "must_run_pipe_infrastructure_failure",
            }
            classifications[key] = row
            provisional_pipe_must_run[key] = problem
        elif status in MODEL_STATUSES:
            classifications[key] = {
                **base,
                "group": "model_profile",
                "classification": "model_profile_pipe",
            }
        elif status in DATA_STATUSES:
            classifications[key] = {
                **base,
                "group": "data_issue",
                "classification": "data_issue_pipe",
            }
        else:
            row = {
                **base,
                "group": "must_run",
                "classification": "must_run_pipe_unknown_status",
            }
            classifications[key] = row
            provisional_pipe_must_run[key] = problem

    # A pipe failure/invalid candidate is not rerun if an older valid candidate
    # already provides the same reasoning+proof+no-tools evidence.
    historical_for_pipe = load_nonpipe_runs_for_problems(
        db, provisional_pipe_must_run
    )
    reusable_for_pipe = find_valid_historical_candidates(historical_for_pipe, inspect)
    for key, reusable in reusable_for_pipe.items():
        previous = classifications[key]
        classifications[key] = {
            **previous,
            "group": "valid_completed",
            "classification": "valid_completed_historical_reuse_for_pipe_issue",
            "superseded_classification": previous["classification"],
            "historical_run": reusable["run"],
            "historical_asset": compact_asset(
                reusable["asset"], args.full_valid_assets
            ),
        }

    no_pipe_problems = {
        key: problem for key, problem in problems.items() if key not in pipe_last
    }
    historical_no_pipe = load_nonpipe_runs_for_problems(db, no_pipe_problems)
    old_candidate_index = 0
    for key, problem in no_pipe_problems.items():
        runs = historical_no_pipe.get(key, [])
        solved_runs = [run for run in runs if run.get("status") == "candidate_solved"]
        base = {
            "problem_key": key,
            "problem_id": problem.get("problem_id"),
            "source_dataset": problem.get("source_dataset"),
            "db_problem_text_empty": not str(problem.get("problem_text") or "").strip(),
            "historical_run_count": len(runs),
        }
        if solved_runs:
            old_candidate_index += 1
            if old_candidate_index % 50 == 0:
                print(
                    f"historical candidate progress: {old_candidate_index}/{len(no_pipe_problems)}",
                    file=sys.stderr,
                )
            run = solved_runs[-1]
            asset = inspect(str(run.get("exp_id")))
            solved_base = {
                **base,
                "historical_last_solved_status": run.get("status"),
                "historical_last_solved_exp_id": run.get("exp_id"),
                "historical_last_solved_batch_id": run.get("batch_id"),
                "historical_last_solved_ended_at": run.get("ended_at"),
            }
            if asset["valid_reasoning_proof_no_tools"]:
                classifications[key] = {
                    **solved_base,
                    "group": "valid_completed",
                    "classification": "valid_completed_historical_nonpipe",
                    "asset": compact_asset(asset, args.full_valid_assets),
                }
            else:
                classifications[key] = {
                    **solved_base,
                    "group": "must_run",
                    "classification": invalid_candidate_classification(
                        "historical", asset
                    ),
                    "asset": asset,
                }
            continue

        terminal = [
            run for run in runs if run.get("status") not in NONTERMINAL_STATUSES
        ]
        if not terminal:
            classifications[key] = {
                **base,
                "group": "must_run",
                "classification": "must_run_historical_no_terminal",
                "historical_statuses": [run.get("status") for run in runs],
            }
            continue
        run = terminal[-1]
        status = run.get("status")
        terminal_base = {
            **base,
            "historical_last_status": status,
            "historical_last_exp_id": run.get("exp_id"),
            "historical_last_batch_id": run.get("batch_id"),
            "historical_last_ended_at": run.get("ended_at"),
        }
        if status in INFRA_STATUSES:
            classifications[key] = {
                **terminal_base,
                "group": "must_run",
                "classification": "must_run_historical_infrastructure_failure",
            }
        elif status in MODEL_STATUSES:
            classifications[key] = {
                **terminal_base,
                "group": "model_profile",
                "classification": "model_profile_historical_nonpipe",
            }
        elif status in DATA_STATUSES:
            classifications[key] = {
                **terminal_base,
                "group": "data_issue",
                "classification": "data_issue_historical_nonpipe",
            }
        else:
            classifications[key] = {
                **terminal_base,
                "group": "must_run",
                "classification": "must_run_historical_unknown_status",
            }

    missing_classifications = sorted(set(problems) - set(classifications))
    if missing_classifications:
        raise RuntimeError(
            f"classification incomplete: {len(missing_classifications)} keys missing"
        )

    group_counts = Counter(row["group"] for row in classifications.values())
    classification_counts = Counter(
        row["classification"] for row in classifications.values()
    )
    must_run_rows = [
        row for row in classifications.values() if row["group"] == "must_run"
    ]
    result = {
        "audit": "full tier rerun requirement with historical asset reuse",
        "tier": args.tier,
        "definition": {
            "valid_completed": "retained reasoning + retained proof + zero tool calls",
            "must_run": "infrastructure/nonterminal or invalid candidate asset",
            "model_profile": "documented model failure; no same-budget rerun",
            "data_issue": "documented data issue; no solver rerun",
            "historical_reuse": "valid non-pipe assets may satisfy completion",
        },
        "summary": {
            "tier_problem_count": len(problems),
            "valid_completed": group_counts["valid_completed"],
            "must_run": group_counts["must_run"],
            "model_profile": group_counts["model_profile"],
            "data_issue": group_counts["data_issue"],
            "conservation_total": sum(group_counts.values()),
            "pipe_problems_with_valid_historical_reuse": len(reusable_for_pipe),
            "no_pipe_problem_count": len(no_pipe_problems),
        },
        "classification_counts": dict(classification_counts),
        "must_run_breakdowns": {
            "source_dataset": dict(
                Counter(row["source_dataset"] for row in must_run_rows)
            ),
            "classification": dict(
                Counter(row["classification"] for row in must_run_rows)
            ),
        },
        "problems": {
            key: classifications[key] for key in sorted(classifications)
        },
    }
    if result["summary"]["conservation_total"] != len(problems):
        raise RuntimeError("classification conservation check failed")
    json_dump(result, args.export)


if __name__ == "__main__":
    main()
