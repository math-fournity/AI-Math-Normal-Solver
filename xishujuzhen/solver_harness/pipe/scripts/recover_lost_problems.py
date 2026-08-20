#!/usr/bin/env python3
"""recover_lost_problems.py — 恢复从队列中丢失的题

问题根因：runner launch失败时（如VENV_PYTHON路径错误），题从pending用ZPOPMIN取出后，
except块中的add_failed可能未成功执行，导致题既不在pending、不在running、不在failed、不在DB。

恢复逻辑：
1. 从runner.log中提取所有"启动失败 <problem_key>"的题
2. 检查每个problem_key是否在ArangoDB devin_problem_runs中有记录
3. 检查是否已在Redis pending/running/failed队列中
4. 真正丢失的题（无DB记录+不在任何队列）重新入pending队列

用法:
  set -a; source /Users/user/glm5.2-math-worktree/.env; set +a
  PYTHONPATH=xishujuzhen/solver_harness/pipe /Users/user/glm5.2-math-worktree/.venv/bin/python3 \
    xishujuzhen/solver_harness/pipe/scripts/recover_lost_problems.py [--dry-run] [--log-file <path>]

  --dry-run: 只打印不实际入队
  --log-file: runner.log路径（默认: /Volumes/data/math-agent-glm5.2-tmux-agents-trajectory/_pipe/logs/runner.log）
"""
import argparse
import json
import os
import re
import sys
from pathlib import Path

from arango import ArangoClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from redis_queue import (
    get_redis, enqueue_pending,
    pending_count, running_count, completed_count, failed_count,
)

DB_HOST = os.environ.get("ARANGO_HOST", "http://localhost:8529")
DB_NAME = os.environ.get("ARANGO_DB", "xishujuzhen_math_glm52")
DB_USER = os.environ.get("ARANGO_USER", "root")
DB_PASS = os.environ.get("ARANGO_PASS", "")
ATTEMPT_COLLECTION = "devin_problem_runs"
COLLECTION = "problem_extraction_progress"

DEFAULT_LOG = "/Volumes/data/math-agent-glm5.2-tmux-agents-trajectory/_pipe/logs/runner.log"


def extract_lost_keys(log_file: str) -> list[str]:
    """从runner.log中提取所有'启动失败 <problem_key>'的题"""
    pattern = re.compile(r"启动失败 (\S+):")
    keys = []
    seen = set()
    with open(log_file, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            m = pattern.search(line)
            if m:
                key = m.group(1)
                if key not in seen:
                    seen.add(key)
                    keys.append(key)
    return keys


def check_in_db(db, problem_keys: list[str]) -> set[str]:
    """返回在devin_problem_runs中有记录的problem_key集合"""
    in_db = set()
    for key in problem_keys:
        aql = (
            "FOR r IN devin_problem_runs "
            "FILTER r.problem_id == @key "
            "COLLECT WITH COUNT INTO c "
            "RETURN c"
        )
        count = list(db.aql.execute(aql, bind_vars={"key": key}))[0]
        if count > 0:
            in_db.add(key)
    return in_db


def check_in_redis(r, problem_keys: list[str]) -> set[str]:
    """返回在Redis任何队列中存在的problem_key集合"""
    in_redis = set()
    # pending (sorted set)
    pending_members = r.zrange("math:pending", 0, -1)
    in_redis.update(k for k in problem_keys if k in pending_members)
    # running (hash) — values是JSON，含problem_key
    running_vals = r.hvals("math:running")
    running_keys = set()
    for v in running_vals:
        try:
            running_keys.add(json.loads(v).get("problem_key", ""))
        except Exception:
            pass
    in_redis.update(k for k in problem_keys if k in running_keys)
    # failed (list) — 遍历所有元素
    failed_items = r.lrange("math:failed", 0, -1)
    failed_keys = set()
    for item in failed_items:
        try:
            failed_keys.add(json.loads(item).get("problem_key", ""))
        except Exception:
            pass
    in_redis.update(k for k in problem_keys if k in failed_keys)
    # completed (list) — 遍历所有元素
    completed_items = r.lrange("math:completed", 0, -1)
    completed_keys = set()
    for item in completed_items:
        try:
            completed_keys.add(json.loads(item).get("problem_key", ""))
        except Exception:
            pass
    in_redis.update(k for k in problem_keys if k in completed_keys)
    return in_redis


def get_priority_from_db(db, problem_key: str) -> int:
    """从problem_extraction_progress获取题目的priority"""
    doc = db.collection(COLLECTION).get(problem_key)
    if doc:
        return int(doc.get("priority", 0))
    return 0


def main():
    parser = argparse.ArgumentParser(description="恢复从队列中丢失的题")
    parser.add_argument("--dry-run", action="store_true", help="只打印不实际入队")
    parser.add_argument("--log-file", default=DEFAULT_LOG, help=f"runner.log路径（默认: {DEFAULT_LOG}）")
    args = parser.parse_args()

    print(f"=== 恢复丢失的题 ===")
    print(f"日志文件: {args.log_file}")
    print(f"Dry-run: {args.dry_run}")
    print()

    # 1. 提取丢失的题
    lost_keys = extract_lost_keys(args.log_file)
    print(f"[1] 从runner.log提取launch失败的题: {len(lost_keys)}个")

    if not lost_keys:
        print("没有丢失的题，退出")
        return

    # 2. 连接DB和Redis
    client = ArangoClient(hosts=DB_HOST, request_timeout=300)
    db = client.db(DB_NAME, username=DB_USER, password=DB_PASS)
    r = get_redis()
    print(f"[2] 连接ArangoDB({DB_NAME})和Redis成功")

    # 3. 检查哪些题在DB中有记录
    print(f"[3] 检查DB记录...")
    in_db = check_in_db(db, lost_keys)
    truly_lost = [k for k in lost_keys if k not in in_db]
    print(f"    在DB中有记录: {len(in_db)}个（不需要恢复）")
    print(f"    在DB中无记录: {len(truly_lost)}个（候选恢复）")

    # 4. 检查哪些题在Redis队列中
    print(f"[4] 检查Redis队列...")
    in_redis = check_in_redis(r, truly_lost)
    really_lost = [k for k in truly_lost if k not in in_redis]
    print(f"    在Redis队列中: {len(in_redis)}个（不需要恢复）")
    print(f"    真正丢失: {len(really_lost)}个（需要恢复）")

    if not really_lost:
        print("\n没有真正丢失的题，退出")
        return

    # 5. 重新入队
    print(f"\n[5] 重新入队 {len(really_lost)} 个题...")
    requeued = 0
    skipped = 0
    for key in really_lost:
        # 确认题目在题库中存在
        doc = db.collection(COLLECTION).get(key)
        if not doc:
            print(f"    ⚠️ {key} 不在题库中，跳过")
            skipped += 1
            continue
        priority = int(doc.get("priority", 0))
        if args.dry_run:
            print(f"    [DRY-RUN] 入队 {key} (priority={priority})")
            requeued += 1
        else:
            enqueue_pending(r, key, priority)
            requeued += 1

    print(f"\n=== 恢复完成 ===")
    print(f"  丢失总数: {len(lost_keys)}")
    print(f"  DB有记录: {len(in_db)}")
    print(f"  Redis有记录: {len(in_redis)}")
    print(f"  重新入队: {requeued}")
    print(f"  跳过(不在题库): {skipped}")
    if args.dry_run:
        print(f"  (Dry-run模式，未实际入队)")
    else:
        print(f"  当前pending: {pending_count(r)}")


if __name__ == "__main__":
    main()
