#!/usr/bin/env python3
"""Cross-audit math-manify's tier=1 business report against report 409.

This script persists the read-only DB/JSON comparisons used for
``math-manify/docs/同行讨论-002-来自normal-solver的审计回复.md``.  It checks:

* the narrow pipe-only 9,828 backlog;
* historical non-pipe token-limit handoff candidates;
* valid short questions dropped by ``MIN_TEXT=50``;
* panorama attempts omitted by the single-key JOIN;
* the resulting business/load gap.

It never updates ArangoDB, Redis, either repository, or the frozen panorama.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

from tier1_audit_common import connect_db, json_dump


DEFAULT_NORMAL_AUDIT = Path(
    "/tmp/tier1-required-rerun-audit-20260823-recheck/required_reruns.json"
)
DEFAULT_SOURCE_AUDIT = Path(
    "/tmp/tier1-required-rerun-audit-20260823-recheck/empty_problem_sources.json"
)
DEFAULT_PANORAMA = Path("/Volumes/data/math-manify-runs/panorama/panorama.jsonl")
DEFAULT_P27_COMPLETED = Path(
    "/Volumes/data/math-manify-runs/panorama/p27_completed.json"
)
DEFAULT_MATH_MANIFY_REPO = Path("/Users/user/math-manify")


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def load_panorama(path: Path) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    batch_counts: Counter = Counter()
    model_counts: Counter = Counter()
    rows_by_pid: dict[str, dict[str, Any]] = {}
    total_attempts = 0
    numeric_key_rows = 0
    numeric_key_attempts = 0
    numeric_key_p27_records = 0
    p27_records = 0
    p27_completed = 0
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            pid = str(row.get("pid"))
            rows_by_pid[pid] = row
            attempts = row.get("normal_solver_attempts") or []
            total_attempts += len(attempts)
            for attempt in attempts:
                batch_counts[attempt.get("b")] += 1
                model_counts[attempt.get("m")] += 1
            p27 = row.get("p27")
            if p27:
                p27_records += 1
                if p27.get("status") == "completed" and p27.get("final_status") == "COMPLETED":
                    p27_completed += 1
            if pid.isdigit():
                numeric_key_rows += 1
                numeric_key_attempts += len(attempts)
                numeric_key_p27_records += int(bool(p27))
    summary = {
        "rows": len(rows_by_pid),
        "normal_attempts": total_attempts,
        "normal_with_attempts": sum(
            bool(row.get("normal_solver_attempts")) for row in rows_by_pid.values()
        ),
        "p27_records": p27_records,
        "p27_completed": p27_completed,
        "numeric_key_rows": numeric_key_rows,
        "numeric_key_normal_attempts": numeric_key_attempts,
        "numeric_key_p27_records": numeric_key_p27_records,
        "batch_counts": {
            str(key): value for key, value in batch_counts.most_common()
        },
        "model_counts": {
            str(key): value for key, value in model_counts.most_common()
        },
    }
    return summary, rows_by_pid


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Cross-audit math-manify's tier=1 business report"
    )
    parser.add_argument(
        "--normal-audit-json", type=Path, default=DEFAULT_NORMAL_AUDIT
    )
    parser.add_argument(
        "--source-audit-json", type=Path, default=DEFAULT_SOURCE_AUDIT
    )
    parser.add_argument("--panorama", type=Path, default=DEFAULT_PANORAMA)
    parser.add_argument(
        "--p27-completed", type=Path, default=DEFAULT_P27_COMPLETED
    )
    parser.add_argument(
        "--math-manify-repo", type=Path, default=DEFAULT_MATH_MANIFY_REPO
    )
    parser.add_argument("--export", help="Optional JSON output path")
    args = parser.parse_args()

    normal = load_json(args.normal_audit_json)
    source = load_json(args.source_audit_json)
    panorama_summary, panorama_rows = load_panorama(args.panorama)
    p27_completed_rows = load_json(args.p27_completed)
    p27_completed_ids = {str(row.get("pid")) for row in p27_completed_rows}

    db = connect_db()
    query = """
FOR p IN problem_extraction_progress
  FILTER p.difficulty_tier == 1
  LET last = FIRST(
    FOR r IN devin_problem_runs
      FILTER r.problem_id == p._key
      FILTER r.batch_id == "pipe-runner"
      FILTER r.status != "running"
      SORT r.ended_at DESC
      LIMIT 1
      RETURN r.status
  )
  FILTER last IN ["failed_token_limit", "ai_gave_up"]
  RETURN {
    key: p._key,
    problem_id: p.problem_id,
    source_dataset: p.source_dataset,
    status: last,
    problem_text: p.problem_text,
    text_chars: p.problem_text != null ? LENGTH(p.problem_text) : 0
  }
"""
    narrow_rows = list(db.aql.execute(query, ttl=300, batch_size=2000))
    narrow_keys = {str(row["key"]) for row in narrow_rows}
    short_rows = [row for row in narrow_rows if row["text_chars"] < 50]

    problem_rows = normal["problems"]
    historical_token_rows = [
        row
        for row in problem_rows.values()
        if row.get("classification") == "model_profile_historical_nonpipe"
        and row.get("historical_last_status") == "failed_token_limit"
    ]
    historical_token_keys = {
        str(row["problem_key"]) for row in historical_token_rows
    }
    historical_token_text_ids = [
        str(row["problem_id"])
        for row in historical_token_rows
        if row.get("problem_id")
    ]

    p27_query = """
FOR d IN p27_continuation_runs
  FILTER d.problem_id IN @ids
  COLLECT status = d.status, final_status = d.final_status WITH COUNT INTO n
  RETURN {status, final_status, n}
"""
    p27_for_historical_token = list(
        db.aql.execute(
            p27_query,
            bind_vars={"ids": historical_token_text_ids},
            ttl=300,
        )
    )

    empty_cohort = [
        row for row in problem_rows.values() if row.get("db_problem_text_empty") is True
    ]
    empty_historical_runs = sum(
        int(row.get("historical_run_count") or 0) for row in empty_cohort
    )
    empty_classifications = Counter(
        row.get("classification") for row in empty_cohort
    )
    empty_panorama_attempts = sum(
        len((panorama_rows.get(str(row["problem_key"])) or {}).get("normal_solver_attempts") or [])
        for row in empty_cohort
    )

    loaded_problem_files = {
        path.stem for path in (args.math_manify_repo / "problems").glob("*.txt")
    }
    broad_keys = narrow_keys | historical_token_keys
    missing_from_loaded_narrow = sorted(narrow_keys - loaded_problem_files)
    missing_from_loaded_broad = sorted(broad_keys - loaded_problem_files)

    corrected_attempt_total = (
        panorama_summary["normal_attempts"]
        + empty_historical_runs
        - empty_panorama_attempts
    )
    nonnull_reported_models = 49073 + 1024

    result = {
        "audit": "math-manify tier1 peer-report cross-audit",
        "inputs": {
            "normal_audit_json": str(args.normal_audit_json),
            "source_audit_json": str(args.source_audit_json),
            "panorama": str(args.panorama),
            "p27_completed": str(args.p27_completed),
            "math_manify_repo": str(args.math_manify_repo),
        },
        "summary": {
            "pipe_only_backlog": len(narrow_rows),
            "historical_nonpipe_token_limit": len(historical_token_rows),
            "all_history_token_giveup_candidate_backlog": len(broad_keys),
            "currently_loaded_problem_files": len(loaded_problem_files),
            "missing_from_loaded_pipe_only": len(missing_from_loaded_narrow),
            "missing_from_loaded_all_history": len(missing_from_loaded_broad),
            "valid_short_questions_dropped_by_min_text_50": len(short_rows),
            "normal_solver_strict_must_run": normal["summary"]["must_run"],
            "normal_solver_valid_completed": normal["summary"]["valid_completed"],
            "empty_db_cohort": len(empty_cohort),
            "empty_db_cohort_historical_runs": empty_historical_runs,
            "empty_db_sources_recoverable": source["summary"]["recoverable"],
            "p27_records_for_86_historical_token_limit": sum(
                row["n"] for row in p27_for_historical_token
            ),
            "p27_completed_overlap_for_86_historical_token_limit": sum(
                row["problem_id"] in p27_completed_ids
                for row in historical_token_rows
            ),
        },
        "panorama": {
            **panorama_summary,
            "empty_cohort_attempts_recorded": empty_panorama_attempts,
            "empty_cohort_attempts_proven_by_three_key_audit": empty_historical_runs,
            "corrected_normal_attempt_total": corrected_attempt_total,
            "report_nonnull_model_count_49073_plus_1024": nonnull_reported_models,
            "implied_null_model_count_after_correction": corrected_attempt_total
            - nonnull_reported_models,
        },
        "empty_cohort_classifications": dict(empty_classifications),
        "p27_for_historical_token_limit": p27_for_historical_token,
        "short_questions": short_rows,
        "missing_from_loaded_pipe_only": missing_from_loaded_narrow,
        "missing_from_loaded_all_history": missing_from_loaded_broad,
        "historical_token_limit_problems": historical_token_rows,
    }
    json_dump(result, args.export)


if __name__ == "__main__":
    main()
