#!/usr/bin/env python3
"""query_progress.py — 运行进度查询

查询当前运行进度、某题目的运行历史、某次运行的详情。

用法:
  python query_progress.py
  python query_progress.py --problem-key <key>
  python query_progress.py --exp-id <id>
  python query_progress.py --since "2026-08-12" --until "2026-08-13"
"""
import sys
import os
import argparse

sys.path.insert(0, os.path.dirname(__file__))

from arango import ArangoClient
from redis_queue import get_redis, pending_count, running_count, completed_count, failed_count, update_stats, ping

DB_HOST = "http://localhost:8529"
DB_NAME = "xishujuzhen_math_glm52"
DB_USER = "root"
DB_PASS = "moira123"
ATTEMPT_COLLECTION = "devin_problem_runs"
PROBLEM_COLLECTION = "problem_extraction_progress"


def query_current(db) -> dict:
    """当前状态"""
    # Redis状态
    r = get_redis()
    update_stats(r)
    redis_status = {
        "pending": pending_count(r),
        "running": running_count(r),
        "completed": completed_count(r),
        "failed": failed_count(r),
    }

    # DB状态
    aql = f"FOR a IN {ATTEMPT_COLLECTION} FILTER a.batch_id == 'pipe-runner' COLLECT status = a.status WITH COUNT INTO c RETURN {{status, count: c}}"
    cursor = db.aql.execute(aql, ttl=300)
    db_status = {row["status"]: row["count"] for row in cursor}

    # 总题数
    aql = f"FOR p IN {PROBLEM_COLLECTION} COLLECT status = p.extraction_status WITH COUNT INTO c RETURN {{status, count: c}}"
    cursor = db.aql.execute(aql, ttl=300)
    problem_status = {row["status"]: row["count"] for row in cursor}

    return {"redis": redis_status, "db_attempts": db_status, "db_problems": problem_status}


def query_problem_history(db, problem_key: str) -> list[dict]:
    """某题目的运行历史"""
    aql = (
        f"FOR a IN {ATTEMPT_COLLECTION} "
        f"FILTER a.problem_id == '{problem_key}' "
        f"SORT a.started_at DESC "
        f"RETURN {{_key: a._key, exp_id: a.exp_id, status: a.status, verdict: a.verdict, "
        f"started_at: a.started_at, ended_at: a.ended_at, runtime_seconds: a.runtime_seconds, "
        f"end_reason: a.end_reason}}"
    )
    cursor = db.aql.execute(aql, ttl=300)
    return list(cursor)


def query_run_detail(db, exp_id: str) -> dict:
    """某次运行的详情"""
    aql = f"FOR a IN {ATTEMPT_COLLECTION} FILTER a.exp_id == '{exp_id}' RETURN a"
    cursor = db.aql.execute(aql, ttl=300)
    attempts = list(cursor)
    if not attempts:
        return {"error": "not found", "exp_id": exp_id}
    attempt = attempts[0]
    # 关联题目
    problem_id = attempt.get("problem_id")
    problem = db.collection(PROBLEM_COLLECTION).get(problem_id) if problem_id else None
    return {
        "attempt": {
            "_key": attempt["_key"],
            "exp_id": attempt["exp_id"],
            "status": attempt.get("status"),
            "verdict": attempt.get("verdict"),
            "started_at": attempt.get("started_at"),
            "ended_at": attempt.get("ended_at"),
            "runtime_seconds": attempt.get("runtime_seconds"),
            "end_reason": attempt.get("end_reason"),
            "tmux_session": attempt.get("tmux_session"),
            "paths": attempt.get("paths", {}),
        },
        "problem": {
            "_key": problem.get("_key") if problem else None,
            "source_dataset": problem.get("source_dataset") if problem else None,
            "difficulty_tier": problem.get("difficulty_tier") if problem else None,
            "problem_text_preview": problem.get("problem_text", "")[:200] if problem else None,
        } if problem else None,
    }


def query_time_range(db, since: str, until: str) -> list[dict]:
    """某时间段内的运行"""
    aql = (
        f"FOR a IN {ATTEMPT_COLLECTION} "
        f"FILTER a.batch_id == 'pipe-runner' "
        f"FILTER a.started_at >= '{since}' "
        f"FILTER a.started_at <= '{until}' "
        f"SORT a.started_at DESC "
        f"RETURN {{_key: a._key, problem_id: a.problem_id, status: a.status, verdict: a.verdict, "
        f"started_at: a.started_at, runtime_seconds: a.runtime_seconds}}"
    )
    cursor = db.aql.execute(aql, ttl=300)
    return list(cursor)


def main():
    parser = argparse.ArgumentParser(description="运行进度查询")
    parser.add_argument("--problem-key", type=str)
    parser.add_argument("--exp-id", type=str)
    parser.add_argument("--since", type=str)
    parser.add_argument("--until", type=str)
    args = parser.parse_args()

    client = ArangoClient(hosts=DB_HOST, request_timeout=300)
    db = client.db(DB_NAME, username=DB_USER, password=DB_PASS)

    if args.problem_key:
        runs = query_problem_history(db, args.problem_key)
        print(f"=== {args.problem_key} 运行历史 ({len(runs)}次) ===")
        for r in runs:
            print(f"  {r['_key']:45s} {r.get('status',''):15s} {r.get('verdict',''):25s} {r.get('runtime_seconds',0):>6}s  {r.get('started_at','')}")
        return

    if args.exp_id:
        detail = query_run_detail(db, args.exp_id)
        if detail.get("error"):
            print(f"❌ {detail['error']}: {args.exp_id}")
            return
        a = detail["attempt"]
        print(f"=== 运行详情: {args.exp_id} ===")
        print(f"  attempt_key:    {a['_key']}")
        print(f"  status:         {a['status']}")
        print(f"  verdict:        {a.get('verdict','')}")
        print(f"  started_at:     {a.get('started_at','')}")
        print(f"  ended_at:       {a.get('ended_at','')}")
        print(f"  runtime:        {a.get('runtime_seconds',0)}s")
        print(f"  end_reason:     {a.get('end_reason','')}")
        print(f"  tmux_session:   {a.get('tmux_session','')}")
        if a.get("paths"):
            print(f"  paths:")
            for name, path in a["paths"].items():
                print(f"    {name}: {path}")
        if detail.get("problem"):
            p = detail["problem"]
            print(f"  problem:")
            print(f"    key:          {p['_key']}")
            print(f"    dataset:      {p['source_dataset']}")
            print(f"    tier:         {p['difficulty_tier']}")
            print(f"    text preview: {p['problem_text_preview'][:100]}...")
        return

    if args.since:
        until = args.until or "2099-01-01"
        runs = query_time_range(db, args.since, until)
        print(f"=== {args.since} ~ {until} 运行记录 ({len(runs)}条) ===")
        for r in runs:
            print(f"  {r['_key']:45s} {r.get('problem_id',''):30s} {r.get('status',''):15s} {r.get('verdict',''):25s} {r.get('runtime_seconds',0):>6}s")
        return

    # 默认：当前状态
    if not ping():
        print("❌ Redis连接失败")
    status = query_current(db)
    print("=== 当前运行状态 ===")
    print(f"\nRedis队列:")
    for k, v in status["redis"].items():
        print(f"  {k:12s} {v:>8}")
    print(f"\nDB attempts (pipe-runner):")
    for k, v in sorted(status["db_attempts"].items(), key=lambda x: -x[1]):
        print(f"  {k:25s} {v:>8}")
    print(f"\nDB problems:")
    for k, v in sorted(status["db_problems"].items(), key=lambda x: -(x[1] or 0)):
        print(f"  {k:25s} {v or 0:>8}")


if __name__ == "__main__":
    main()
