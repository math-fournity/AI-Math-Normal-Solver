#!/usr/bin/env python3
"""reenqueue_leak_problems.py — 把被误判为answer_leak的题重新入解题队列

取消泄漏检测后，把以下题重新入队：
- answer_leak_in_input（455题）：runner启动前误判
- answer_leak（188题）：collector运行中误判

这些题的problem_text本身包含答案是数据集特性，不是泄漏。
重新入队时更新extraction_status为pending，让feeder能重新选题。

用法:
  python reenqueue_leak_problems.py --dry-run   # 只查数量
  python reenqueue_leak_problems.py              # 实际入队
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from arango import ArangoClient
import redis

DB_HOST = "http://localhost:8529"
DB_NAME = "xishujuzhen_math_glm52"
DB_USER = "root"
DB_PASS = "moira123"
COLLECTION = "problem_extraction_progress"
RUNS_COLLECTION = "devin_problem_runs"

PENDING_KEY = "math:pending"


def main():
    dry_run = "--dry-run" in sys.argv

    client = ArangoClient(hosts=DB_HOST, request_timeout=300)
    db = client.db(DB_NAME, username=DB_USER, password=DB_PASS)

    r = redis.Redis(host="localhost", port=6379, db=0, decode_responses=True)

    # 找所有最后run为answer_leak或answer_leak_in_input的题
    aql = """
FOR p IN problem_extraction_progress
  FILTER p.difficulty_tier == 1
  LET has_leak_run = (
    FOR r IN devin_problem_runs
      FILTER r.problem_id == p._key
      FILTER r.batch_id == "pipe-runner"
      FILTER r.status != "running"
      SORT r.ended_at DESC
      LIMIT 1
      RETURN r.status
  )
  FILTER LENGTH(has_leak_run) > 0
  FILTER has_leak_run[0] IN ["answer_leak", "answer_leak_in_input"]
  RETURN {key: p._key, priority: p.priority || 1, last_status: has_leak_run[0]}
"""
    cursor = db.aql.execute(aql, ttl=300)
    problems = list(cursor)

    print(f"找到{len(problems)}道被误判为泄漏的题")

    # 按状态分类统计
    from collections import Counter
    status_counts = Counter(p["last_status"] for p in problems)
    for status, count in status_counts.items():
        print(f"  {status}: {count}")

    if dry_run:
        print("\nDRY-RUN模式，不入队。去掉--dry-run参数实际入队。")
        return

    # 重新入队
    enqueued = 0
    for p in problems:
        key = p["key"]
        priority = p["priority"]
        # 入Redis pending队列
        r.zadd(PENDING_KEY, {key: priority})
        # 更新extraction_status为pending（让feeder也能重新选题）
        db.collection(COLLECTION).update({
            "_key": key,
            "extraction_status": "pending",
        })
        enqueued += 1

    print(f"\n已重新入队 {enqueued} 题")
    print(f"Redis pending队列当前大小: {r.zcard(PENDING_KEY)}")


if __name__ == "__main__":
    main()
