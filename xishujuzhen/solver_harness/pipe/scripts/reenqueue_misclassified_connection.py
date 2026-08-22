#!/usr/bin/env python3
"""reenqueue_misclassified_connection.py — 把1,111题误分类的连接错误重新入队

审计确认：1,111题被误分类为failed_token_limit（实际是Connection error），
从未被retry重试，没有thinking数据，必须重新跑。

操作：
1. 从硬盘读pane_snapshot确认是连接错误（不信任DB的status）
2. 把这些题的problem_id入队到math:pending
3. 更新ArangoDB中extraction_status为queued

用法:
  python reenqueue_misclassified_connection.py --dry-run   # 只统计，不入队
  python reenqueue_misclassified_connection.py              # 执行入队
"""
import sys
import os
import json
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from arango import ArangoClient
import redis

DB_HOST = "http://localhost:8529"
DB_NAME = "xishujuzhen_math_glm52"
DB_USER = "root"
DB_PASS = "moira123"
TRAJ_DIR = "/Volumes/data/math-agent-glm5.2-tmux-agents-trajectory"
PENDING_KEY = "math:pending"

CONNECTION_MARKERS = ["connection error", "econnrefused", "etimedout", "socket hang up", "fetch failed"]


def read_pane(exp_id):
    path = os.path.join(TRAJ_DIR, exp_id, "collector", "pane_snapshot.txt")
    if os.path.exists(path):
        try:
            with open(path, errors="replace") as f:
                return f.read()
        except Exception:
            return ""
    return ""


def main():
    dry_run = "--dry-run" in sys.argv

    client = ArangoClient(hosts=DB_HOST, request_timeout=300)
    db = client.db(DB_NAME, username=DB_USER, password=DB_PASS)
    r = redis.Redis(host="localhost", port=6379, db=0, decode_responses=True)

    # 1. 查所有failed_token_limit的最后run
    print("查询所有failed_token_limit的最后run...")
    aql = """
FOR p IN problem_extraction_progress
  FILTER p.difficulty_tier == 1
  LET last_run = (
    FOR r IN devin_problem_runs
      FILTER r.problem_id == p._key
      FILTER r.batch_id == "pipe-runner"
      FILTER r.status != "running"
      SORT r.ended_at DESC
      LIMIT 1
      RETURN {exp_id: r.exp_id, status: r.status}
  )
  FILTER LENGTH(last_run) > 0
  FILTER last_run[0].status == "failed_token_limit"
  RETURN {
    problem_id: p._key,
    exp_id: last_run[0].exp_id,
    priority: p.priority || 0
  }
"""
    cursor = db.aql.execute(aql, ttl=300, batch_size=2000)
    runs = list(cursor)
    print(f"  failed_token_limit最后run: {len(runs)}")

    # 2. 读pane_snapshot确认是连接错误
    print("读pane_snapshot确认是连接错误...")
    misclassified = []
    for i, run in enumerate(runs):
        if i % 500 == 0:
            print(f"  进度: {i}/{len(runs)}...", flush=True)
        pane = read_pane(run["exp_id"])
        if pane:
            pane_lower = pane.lower()
            if any(m in pane_lower for m in CONNECTION_MARKERS):
                misclassified.append(run)

    print(f"  确认误分类（连接错误）: {len(misclassified)}")
    print()

    if len(misclassified) == 0:
        print("没有误分类的题，无需入队。")
        return

    # 3. 检查这些题是否已经在pending队列中
    already_pending = 0
    to_enqueue = []
    for run in misclassified:
        score = r.zscore(PENDING_KEY, run["problem_id"])
        if score is not None:
            already_pending += 1
        else:
            to_enqueue.append(run)

    print(f"--- 入队计划 ---")
    print(f"  误分类总数: {len(misclassified)}")
    print(f"  已在pending队列: {already_pending}")
    print(f"  需要入队: {len(to_enqueue)}")
    print()

    if dry_run:
        print("DRY RUN模式，不执行入队。")
        print(f"  将入队 {len(to_enqueue)} 题到 math:pending")
        # 抽样显示
        for run in to_enqueue[:5]:
            print(f"    {run['problem_id']} (exp_id={run['exp_id']})")
        return

    if len(to_enqueue) == 0:
        print("所有题已在pending队列中，无需入队。")
        return

    # 4. 入队
    print(f"入队 {len(to_enqueue)} 题到 math:pending...")
    pipe = r.pipeline()
    enqueued = 0
    for run in to_enqueue:
        # priority越小越优先（sorted set score升序）
        # 用priority作为score，保证按优先级排序
        score = float(run["priority"])
        pipe.zadd(PENDING_KEY, {run["problem_id"]: score})
        enqueued += 1
        if enqueued % 500 == 0:
            pipe.execute()
            print(f"  已入队: {enqueued}/{len(to_enqueue)}...")
            pipe = r.pipeline()
    pipe.execute()
    print(f"  入队完成: {enqueued}")
    print()

    # 5. 更新ArangoDB extraction_status为queued
    print(f"更新ArangoDB extraction_status为queued...")
    updated = 0
    for run in to_enqueue:
        try:
            db.collection("problem_extraction_progress").update(
                {"_key": run["problem_id"], "extraction_status": "queued"}
            )
            updated += 1
        except Exception as e:
            print(f"  ⚠️ 更新失败: {run['problem_id']} error={e}")
    print(f"  更新完成: {updated}")
    print()

    # 6. 验证
    pending_count = r.zcard(PENDING_KEY)
    print(f"=== 完成 ===")
    print(f"  入队: {enqueued} 题")
    print(f"  ArangoDB更新: {updated} 题")
    print(f"  math:pending当前总数: {pending_count}")


if __name__ == "__main__":
    main()
