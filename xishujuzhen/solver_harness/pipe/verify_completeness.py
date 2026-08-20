#!/usr/bin/env python3
"""verify_completeness.py — 数据完备性验证

验证数据管理的完备性：ID完整性、Redis与DB一致性、tmux session一致性。

用法:
  python verify_completeness.py --check-ids
  python verify_completeness.py --check-attempts
  python verify_completeness.py --check-redis-sync
  python verify_completeness.py --check-tmux
  python verify_completeness.py --all
"""
import sys
import os
import subprocess
import argparse

sys.path.insert(0, os.path.dirname(__file__))

from arango import ArangoClient
from redis_queue import get_redis, pending_count, running_count, completed_count, failed_count, get_all_running, ping

DB_HOST = "http://localhost:8529"
DB_NAME = "xishujuzhen_math_glm52"
DB_USER = "root"
DB_PASS = "moira123"
ATTEMPT_COLLECTION = "devin_problem_runs"
PROBLEM_COLLECTION = "problem_extraction_progress"


def check_ids(db) -> dict:
    """DM-01: 题目唯一ID完整性"""
    # 检查_key为空的题
    aql = f"FOR p IN {PROBLEM_COLLECTION} FILTER p._key == null OR p._key == '' COLLECT WITH COUNT INTO c RETURN c"
    cursor = db.aql.execute(aql, ttl=300)
    empty_key = cursor.next()

    # 检查重复_key（ArangoDB的_key本身就是主键，理论上不可能重复）
    aql = f"FOR p IN {PROBLEM_COLLECTION} COLLECT key = p._key WITH COUNT INTO c FILTER c > 1 SORT c DESC RETURN {{key, count: c}}"
    cursor = db.aql.execute(aql, ttl=300)
    duplicates = list(cursor)

    # 检查problem_hash为空
    aql = f"FOR p IN {PROBLEM_COLLECTION} FILTER p.problem_hash == null OR p.problem_hash == '' COLLECT WITH COUNT INTO c RETURN c"
    cursor = db.aql.execute(aql, ttl=300)
    empty_hash = cursor.next()

    total = db.collection(PROBLEM_COLLECTION).count()
    return {
        "total": total,
        "empty_key": empty_key,
        "duplicate_keys": len(duplicates),
        "empty_hash": empty_hash,
        "pass": empty_key == 0 and len(duplicates) == 0,
    }


def check_attempts(db) -> dict:
    """DM-02: 运行记录唯一ID完整性"""
    aql = f"FOR a IN {ATTEMPT_COLLECTION} FILTER a.batch_id == 'pipe-runner' FILTER a.exp_id == null OR a.exp_id == '' COLLECT WITH COUNT INTO c RETURN c"
    cursor = db.aql.execute(aql, ttl=300)
    empty_exp_id = cursor.next()

    aql = f"FOR a IN {ATTEMPT_COLLECTION} FILTER a.batch_id == 'pipe-runner' COLLECT exp = a.exp_id WITH COUNT INTO c FILTER c > 1 RETURN {{exp, count: c}}"
    cursor = db.aql.execute(aql, ttl=300)
    duplicates = list(cursor)

    aql = f"FOR a IN {ATTEMPT_COLLECTION} FILTER a.batch_id == 'pipe-runner' COLLECT WITH COUNT INTO c RETURN c"
    cursor = db.aql.execute(aql, ttl=300)
    total = cursor.next()

    return {
        "total": total,
        "empty_exp_id": empty_exp_id,
        "duplicate_exp_ids": len(duplicates),
        "pass": empty_exp_id == 0 and len(duplicates) == 0,
    }


def check_redis_sync(db) -> dict:
    """DM-07: Redis与DB一致性"""
    if not ping():
        return {"error": "Redis连接失败", "pass": False}

    r = get_redis()
    redis_completed = completed_count(r)
    redis_failed = failed_count(r)
    redis_running = running_count(r)
    redis_pending = pending_count(r)

    # DB中pipe-runner的终态记录数
    aql = f"FOR a IN {ATTEMPT_COLLECTION} FILTER a.batch_id == 'pipe-runner' FILTER a.status IN ['candidate_solved','answer_leak'] COLLECT WITH COUNT INTO c RETURN c"
    cursor = db.aql.execute(aql, ttl=300)
    db_completed = cursor.next()

    aql = f"FOR a IN {ATTEMPT_COLLECTION} FILTER a.batch_id == 'pipe-runner' FILTER a.status IN ['failed_timeout','failed_stall','dead_session','failed_no_proof','failed_connection','failed_token_limit','rate_limited','launch_error','answer_leak_in_input'] COLLECT WITH COUNT INTO c RETURN c"
    cursor = db.aql.execute(aql, ttl=300)
    db_failed = cursor.next()

    aql = f"FOR a IN {ATTEMPT_COLLECTION} FILTER a.batch_id == 'pipe-runner' FILTER a.status == 'running' COLLECT WITH COUNT INTO c RETURN c"
    cursor = db.aql.execute(aql, ttl=300)
    db_running = cursor.next()

    return {
        "redis": {"pending": redis_pending, "running": redis_running, "completed": redis_completed, "failed": redis_failed},
        "db": {"running": db_running, "completed": db_completed, "failed": db_failed},
        "completed_match": redis_completed == db_completed,
        "failed_match": redis_failed == db_failed,
        "running_match": redis_running == db_running,
        "pass": redis_completed == db_completed and redis_failed == db_failed and redis_running == db_running,
    }


def check_tmux(db) -> dict:
    """CF-07: tmux session一致性——Redis running中的都有对应tmux session"""
    if not ping():
        return {"error": "Redis连接失败", "pass": False}

    r = get_redis()
    running = get_all_running(r)
    if not running:
        return {"total_running": 0, "missing_tmux": 0, "pass": True}

    # 获取所有tmux sessions
    result = subprocess.run(["tmux", "list-sessions"], capture_output=True, text=True, timeout=5)
    tmux_sessions = result.stdout if result.returncode == 0 else ""

    missing = []
    for exp_id, meta in running.items():
        tmux_session = meta.get("tmux_session", "")
        if tmux_session and tmux_session not in tmux_sessions:
            missing.append({"exp_id": exp_id, "tmux_session": tmux_session})

    return {
        "total_running": len(running),
        "missing_tmux": len(missing),
        "missing_details": missing[:10] if missing else [],
        "pass": len(missing) == 0,
    }


def main():
    parser = argparse.ArgumentParser(description="数据完备性验证")
    parser.add_argument("--check-ids", action="store_true")
    parser.add_argument("--check-attempts", action="store_true")
    parser.add_argument("--check-redis-sync", action="store_true")
    parser.add_argument("--check-tmux", action="store_true")
    parser.add_argument("--all", action="store_true")
    args = parser.parse_args()

    client = ArangoClient(hosts=DB_HOST, request_timeout=300)
    db = client.db(DB_NAME, username=DB_USER, password=DB_PASS)

    all_pass = True

    if args.all or args.check_ids:
        print("=== DM-01: 题目ID完整性 ===")
        r = check_ids(db)
        print(f"  总题数:       {r['total']}")
        print(f"  空key:        {r['empty_key']}")
        print(f"  重复key:      {r['duplicate_keys']}")
        print(f"  空hash:       {r['empty_hash']}")
        print(f"  VERDICT: {'PASS' if r['pass'] else 'FAIL'}")
        all_pass = all_pass and r["pass"]
        print()

    if args.all or args.check_attempts:
        print("=== DM-02: 运行记录ID完整性 ===")
        r = check_attempts(db)
        print(f"  总attempt数:  {r['total']}")
        print(f"  空exp_id:     {r['empty_exp_id']}")
        print(f"  重复exp_id:   {r['duplicate_exp_ids']}")
        print(f"  VERDICT: {'PASS' if r['pass'] else 'FAIL'}")
        all_pass = all_pass and r["pass"]
        print()

    if args.all or args.check_redis_sync:
        print("=== DM-07: Redis与DB一致性 ===")
        r = check_redis_sync(db)
        if r.get("error"):
            print(f"  ❌ {r['error']}")
            all_pass = False
        else:
            print(f"  Redis:  pending={r['redis']['pending']} running={r['redis']['running']} completed={r['redis']['completed']} failed={r['redis']['failed']}")
            print(f"  DB:     running={r['db']['running']} completed={r['db']['completed']} failed={r['db']['failed']}")
            print(f"  completed匹配: {r['completed_match']}")
            print(f"  failed匹配:    {r['failed_match']}")
            print(f"  running匹配:   {r['running_match']}")
            print(f"  VERDICT: {'PASS' if r['pass'] else 'FAIL'}")
            all_pass = all_pass and r["pass"]
        print()

    if args.all or args.check_tmux:
        print("=== CF-07: tmux session一致性 ===")
        r = check_tmux(db)
        if r.get("error"):
            print(f"  ❌ {r['error']}")
            all_pass = False
        else:
            print(f"  Running总数:   {r['total_running']}")
            print(f"  缺失tmux:      {r['missing_tmux']}")
            if r["missing_details"]:
                for d in r["missing_details"]:
                    print(f"    ❌ {d['exp_id']}: {d['tmux_session']}")
            print(f"  VERDICT: {'PASS' if r['pass'] else 'FAIL'}")
            all_pass = all_pass and r["pass"]
        print()

    if args.all:
        print(f"=== 总体验收: {'PASS' if all_pass else 'FAIL'} ===")


if __name__ == "__main__":
    main()
