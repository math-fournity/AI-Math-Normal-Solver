#!/usr/bin/env python3
"""audit_trace.py — 双向追溯验证

验证DB记录和物理文件之间的双向追溯性。
依据: db-file-traceability规则——DB记录的paths字段必须完整指向所有物理文件，
      从物理文件能回查DB记录。

用法:
  python audit_trace.py --db-to-file [--limit N]
  python audit_trace.py --file-to-db [--dir <trajectory_base>]
  python audit_trace.py --problem-to-runs [--problem-key <key>]
  python audit_trace.py --run-to-problem [--exp-id <id>]
  python audit_trace.py --all
"""
import sys
import os
import json
import argparse
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))

from arango import ArangoClient

DB_HOST = "http://localhost:8529"
DB_NAME = "xishujuzhen_math_glm52"
DB_USER = "root"
DB_PASS = "moira123"
ATTEMPT_COLLECTION = "devin_problem_runs"
PROBLEM_COLLECTION = "problem_extraction_progress"

TRAJECTORY_BASE = Path("/Volumes/data/math-agent-glm5.2-tmux-agents-trajectory")

# expected missing: failed题没有proof.md是正常的
EXPECTED_MISSING_PATTERNS = ["proof_path", "proof.md"]


def trace_db_to_file(db, limit: int = 0) -> dict:
    """DB→文件：从DB记录的paths字段查文件存在性"""
    aql = f"FOR a IN {ATTEMPT_COLLECTION} FILTER a.batch_id == 'pipe-runner'"
    if limit > 0:
        aql += f" LIMIT {limit}"
    aql += " RETURN a"
    cursor = db.aql.execute(aql, ttl=300)

    total_records = 0
    files_checked = 0
    expected_missing = 0
    unexpected_missing = 0
    missing_details = []

    for attempt in cursor:
        total_records += 1
        paths = attempt.get("paths", {})
        for name, path_str in paths.items():
            if not path_str or not isinstance(path_str, str):
                continue
            files_checked += 1
            path = Path(path_str)
            if path.exists():
                continue
            # 检查是否是expected missing
            is_expected = any(p in name or p in str(path) for p in EXPECTED_MISSING_PATTERNS)
            if is_expected:
                expected_missing += 1
            else:
                unexpected_missing += 1
                missing_details.append({
                    "attempt_key": attempt["_key"],
                    "path_name": name,
                    "path": str(path),
                })

    verdict = "PASS" if unexpected_missing == 0 else "FAIL"
    return {
        "total_records": total_records,
        "files_checked": files_checked,
        "expected_missing": expected_missing,
        "unexpected_missing": unexpected_missing,
        "missing_details": missing_details,
        "verdict": verdict,
    }


def trace_file_to_db(db, trajectory_dir: Path) -> dict:
    """文件→DB：从物理目录名(exp_id)反查DB记录"""
    # 扫描trajectory_base下的目录
    # pipe-runner的目录在trajectory_base下直接以exp_id命名
    all_dirs = []
    for d in trajectory_dir.iterdir():
        if d.is_dir() and d.name.startswith("p") and len(d.name) >= 10:
            # 排除_pipe, _batches等
            if d.name.startswith("_"):
                continue
            all_dirs.append(d)

    total_dirs = len(all_dirs)
    db_found = 0
    orphan_dirs = []
    for d in all_dirs:
        exp_id = d.name
        # 查DB
        attempt_key = f"pipe_{exp_id[:40]}"
        doc = db.collection(ATTEMPT_COLLECTION).get(attempt_key)
        if doc:
            db_found += 1
        else:
            # 也可能是旧系统的attempt
            aql = f"FOR a IN {ATTEMPT_COLLECTION} FILTER a.exp_id == '{exp_id}' LIMIT 1 RETURN a._key"
            cursor = db.aql.execute(aql, ttl=60)
            keys = list(cursor)
            if keys:
                db_found += 1
            else:
                orphan_dirs.append(exp_id)

    verdict = "PASS" if len(orphan_dirs) == 0 else "PARTIAL"
    return {
        "total_dirs": total_dirs,
        "db_found": db_found,
        "orphan_dirs": orphan_dirs[:20],
        "verdict": verdict,
    }


def trace_problem_to_runs(db, problem_key: str = None) -> dict:
    """题目→运行：从problem查关联的runs"""
    if problem_key:
        aql = (
            f"FOR a IN {ATTEMPT_COLLECTION} "
            f"FILTER a.problem_id == '{problem_key}' "
            f"SORT a.started_at DESC "
            f"RETURN {{_key: a._key, exp_id: a.exp_id, status: a.status, verdict: a.verdict}}"
        )
        cursor = db.aql.execute(aql, ttl=300)
        runs = list(cursor)
        return {"problem_key": problem_key, "run_count": len(runs), "runs": runs}
    else:
        # 统计：有多少已跑题目有运行记录
        aql = (
            f"FOR p IN {PROBLEM_COLLECTION} "
            f"FILTER p.extraction_status IN ['completed', 'failed', 'queued', 'running'] "
            f"LET runs = (FOR a IN {ATTEMPT_COLLECTION} FILTER a.problem_id == p._key COLLECT WITH COUNT INTO c RETURN c) "
            f"FILTER LENGTH(runs) == 0 OR runs[0] == 0 "
            f"COLLECT WITH COUNT INTO c RETURN c"
        )
        cursor = db.aql.execute(aql, ttl=300)
        orphan_count = cursor.next()
        return {"orphan_problems_no_runs": orphan_count, "verdict": "PASS" if orphan_count == 0 else "PARTIAL"}


def trace_run_to_problem(db, exp_id: str = None) -> dict:
    """运行→题目：从run查关联的problem"""
    if exp_id:
        aql = f"FOR a IN {ATTEMPT_COLLECTION} FILTER a.exp_id == '{exp_id}' RETURN a"
        cursor = db.aql.execute(aql, ttl=300)
        attempt = cursor.next() if cursor.batch else None
        if not attempt:
            return {"exp_id": exp_id, "error": "attempt not found"}
        problem_id = attempt.get("problem_id")
        if not problem_id:
            return {"exp_id": exp_id, "error": "no problem_id in attempt"}
        problem = db.collection(PROBLEM_COLLECTION).get(problem_id)
        if not problem:
            return {"exp_id": exp_id, "problem_id": problem_id, "error": "problem not found"}
        return {
            "exp_id": exp_id,
            "attempt_key": attempt["_key"],
            "problem_id": problem_id,
            "problem_text_preview": problem.get("problem_text", "")[:200],
            "source_dataset": problem.get("source_dataset", ""),
            "difficulty_tier": problem.get("difficulty_tier", ""),
        }
    else:
        # 统计：有多少运行记录没有对应的题目
        aql = (
            f"FOR a IN {ATTEMPT_COLLECTION} "
            f"FILTER a.batch_id == 'pipe-runner' "
            f"LET p = (FOR pp IN {PROBLEM_COLLECTION} FILTER pp._key == a.problem_id LIMIT 1 RETURN pp._key) "
            f"FILTER LENGTH(p) == 0 "
            f"COLLECT WITH COUNT INTO c RETURN c"
        )
        cursor = db.aql.execute(aql, ttl=300)
        orphan_count = cursor.next()
        return {"orphan_runs_no_problem": orphan_count, "verdict": "PASS" if orphan_count == 0 else "PARTIAL"}


def main():
    parser = argparse.ArgumentParser(description="双向追溯验证")
    parser.add_argument("--db-to-file", action="store_true")
    parser.add_argument("--file-to-db", action="store_true")
    parser.add_argument("--problem-to-runs", action="store_true")
    parser.add_argument("--run-to-problem", action="store_true")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--dir", type=str, default=str(TRAJECTORY_BASE))
    parser.add_argument("--problem-key", type=str)
    parser.add_argument("--exp-id", type=str)
    args = parser.parse_args()

    client = ArangoClient(hosts=DB_HOST, request_timeout=300)
    db = client.db(DB_NAME, username=DB_USER, password=DB_PASS)

    if args.all or args.db_to_file:
        print("=== DB→File Trace ===")
        r = trace_db_to_file(db, args.limit)
        print(f"Total records: {r['total_records']}")
        print(f"Files checked: {r['files_checked']}")
        print(f"Expected missing: {r['expected_missing']}")
        print(f"Unexpected missing: {r['unexpected_missing']}")
        if r["missing_details"]:
            for d in r["missing_details"][:10]:
                print(f"  ❌ {d['attempt_key']}: {d['path_name']} → {d['path']}")
        print(f"VERDICT: {r['verdict']}")
        print()

    if args.all or args.file_to_db:
        print("=== File→DB Trace ===")
        r = trace_file_to_db(db, Path(args.dir))
        print(f"Total dirs: {r['total_dirs']}")
        print(f"DB records found: {r['db_found']}")
        print(f"Orphan dirs: {len(r['orphan_dirs'])}")
        if r["orphan_dirs"]:
            for d in r["orphan_dirs"][:10]:
                print(f"  ❌ {d}")
        print(f"VERDICT: {r['verdict']}")
        print()

    if args.all or args.problem_to_runs:
        print("=== Problem→Runs Trace ===")
        r = trace_problem_to_runs(db, args.problem_key)
        if args.problem_key:
            print(f"Problem: {r['problem_key']}")
            print(f"Run count: {r['run_count']}")
            for run in r["runs"]:
                print(f"  {run['_key']:45s} {run.get('status',''):15s} {run.get('verdict','')}")
        else:
            print(f"Orphan problems (no runs): {r['orphan_problems_no_runs']}")
            print(f"VERDICT: {r['verdict']}")
        print()

    if args.all or args.run_to_problem:
        print("=== Run→Problem Trace ===")
        r = trace_run_to_problem(db, args.exp_id)
        if args.exp_id:
            if r.get("error"):
                print(f"❌ {r['error']}: {args.exp_id}")
            else:
                print(f"Exp ID: {r['exp_id']}")
                print(f"Attempt: {r['attempt_key']}")
                print(f"Problem: {r['problem_id']}")
                print(f"Dataset: {r['source_dataset']}")
                print(f"Tier: {r['difficulty_tier']}")
                print(f"Text preview: {r['problem_text_preview'][:100]}...")
        else:
            print(f"Orphan runs (no problem): {r['orphan_runs_no_problem']}")
            print(f"VERDICT: {r['verdict']}")
        print()


if __name__ == "__main__":
    main()
