#!/usr/bin/env python3
"""reenqueue_failed_no_proof.py — 把64题failed_no_proof无文件的题重新入队

这些题的最后run是failed_no_proof，但没有thinking数据（无export）：
- 10题无目录：devin cli启动1-5秒就崩溃
- 54题无export：devin cli卡在thinking阶段

failed_no_proof被归类为MODEL_FAILURES，retry不会自动重试，需要手动入队。

用法:
  python reenqueue_failed_no_proof.py --dry-run   # 只统计
  python reenqueue_failed_no_proof.py              # 执行入队
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
TRAJ_DIR = "/Volumes/data/math-agent-glm5.2-tmux-agents-trajectory"
PENDING_KEY = "math:pending"


def main():
    dry_run = "--dry-run" in sys.argv

    client = ArangoClient(hosts=DB_HOST, request_timeout=300)
    db = client.db(DB_NAME, username=DB_USER, password=DB_PASS)
    r = redis.Redis(host="localhost", port=6379, db=0, decode_responses=True)

    # 查所有failed_no_proof的最后run
    print("查询所有failed_no_proof的最后run...")
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
  FILTER last_run[0].status == "failed_no_proof"
  RETURN {
    problem_id: p._key,
    exp_id: last_run[0].exp_id,
    priority: p.priority || 0
  }
"""
    cursor = db.aql.execute(aql, ttl=300, batch_size=2000)
    runs = list(cursor)
    print(f"  failed_no_proof最后run: {len(runs)}")

    # 确认无export（从硬盘检查）
    print("检查硬盘确认无export...")
    no_export = []
    for run in runs:
        export_path = os.path.join(TRAJ_DIR, run["exp_id"], "exports", "conversation.json")
        if not os.path.exists(export_path):
            no_export.append(run)

    print(f"  确认无export: {len(no_export)}")
    print()

    if len(no_export) == 0:
        print("没有无export的题，无需入队。")
        return

    # 检查是否已在pending中
    already_pending = 0
    to_enqueue = []
    for run in no_export:
        score = r.zscore(PENDING_KEY, run["problem_id"])
        if score is not None:
            already_pending += 1
        else:
            to_enqueue.append(run)

    print(f"--- 入队计划 ---")
    print(f"  无export总数: {len(no_export)}")
    print(f"  已在pending队列: {already_pending}")
    print(f"  需要入队: {len(to_enqueue)}")
    print()

    if dry_run:
        print("DRY RUN模式，不执行入队。")
        for run in to_enqueue[:5]:
            print(f"    {run['problem_id']} (exp_id={run['exp_id']})")
        return

    if len(to_enqueue) == 0:
        print("所有题已在pending队列中，无需入队。")
        return

    # 入队
    print(f"入队 {len(to_enqueue)} 题到 math:pending...")
    pipe = r.pipeline()
    enqueued = 0
    for run in to_enqueue:
        score = float(run["priority"])
        pipe.zadd(PENDING_KEY, {run["problem_id"]: score})
        enqueued += 1
        if enqueued % 100 == 0:
            pipe.execute()
            print(f"  已入队: {enqueued}/{len(to_enqueue)}...")
            pipe = r.pipeline()
    pipe.execute()
    print(f"  入队完成: {enqueued}")

    # 更新ArangoDB
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

    pending_count = r.zcard(PENDING_KEY)
    print(f"=== 完成 ===")
    print(f"  入队: {enqueued} 题")
    print(f"  ArangoDB更新: {updated} 题")
    print(f"  math:pending当前总数: {pending_count}")


if __name__ == "__main__":
    main()
