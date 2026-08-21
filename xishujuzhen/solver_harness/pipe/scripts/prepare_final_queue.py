#!/usr/bin/env python3
"""prepare_final_queue.py — 平凡解题系统最终队列准备

一次性把3,448题（2,565从未处理 + 639检测误判 + 244基础设施失败）入队到Redis math:pending。
之后不启动feeder，队列自包含。

用法:
  python prepare_final_queue.py --dry-run   # 只查数量，不入队
  python prepare_final_queue.py              # 实际入队（清空pending/failed/completed + 入队3,448题）

详见 dev-docs/403-v0-2026-08-21-平凡解题系统队列准备方案.md
"""
import sys
import os
import time

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
RUNNING_KEY = "math:running"
COMPLETED_KEY = "math:completed"
FAILED_KEY = "math:failed"

# 应入队的最后run status集合
# 检测误判（已取消）+ 基础设施失败（重试）
ENQUEUE_LAST_STATUS = {
    "answer_leak", "answer_leak_in_input",               # 检测误判（已取消）
    "rate_limited", "dead_session", "crash_recovered",   # 基础设施失败
    "failed_connection", "launch_error",
}


def main():
    dry_run = "--dry-run" in sys.argv

    client = ArangoClient(hosts=DB_HOST, request_timeout=300)
    db = client.db(DB_NAME, username=DB_USER, password=DB_PASS)
    r = redis.Redis(host="localhost", port=6379, db=0, decode_responses=True)

    # === 1. 查从未处理过的题 ===
    print("查询从未处理过的题（无pipe-runner run记录）...")
    aql_never = """
FOR p IN problem_extraction_progress
  FILTER p.difficulty_tier == 1
  LET has_run = (
    FOR r IN devin_problem_runs
      FILTER r.problem_id == p._key
      FILTER r.batch_id == "pipe-runner"
      LIMIT 1
      RETURN 1
  )
  FILTER LENGTH(has_run) == 0
  RETURN {key: p._key, priority: p.priority || 1}
"""
    cursor = db.aql.execute(aql_never, ttl=300, batch_size=1000)
    never_processed = list(cursor)
    print(f"  从未处理过的题: {len(never_processed)}")

    # === 2. 查检测误判题 + 基础设施失败题（按每题最后run的status） ===
    print("查询检测误判题 + 基础设施失败题（按最后run的status）...")
    aql_retry = """
FOR p IN problem_extraction_progress
  FILTER p.difficulty_tier == 1
  LET last_run = (
    FOR r IN devin_problem_runs
      FILTER r.problem_id == p._key
      FILTER r.batch_id == "pipe-runner"
      FILTER r.status != "running"
      SORT r.ended_at DESC
      LIMIT 1
      RETURN r.status
  )
  FILTER LENGTH(last_run) > 0
  FILTER last_run[0] IN [
    "answer_leak", "answer_leak_in_input",
    "rate_limited", "dead_session", "crash_recovered",
    "failed_connection", "launch_error"
  ]
  RETURN {key: p._key, priority: p.priority || 1, last_status: last_run[0]}
"""
    cursor = db.aql.execute(aql_retry, ttl=300, batch_size=1000)
    retry_problems = list(cursor)
    print(f"  检测误判 + 基础设施失败题: {len(retry_problems)}")

    # 按last_status分类统计
    from collections import Counter
    status_counts = Counter(p["last_status"] for p in retry_problems)
    for status, count in sorted(status_counts.items()):
        print(f"    {status}: {count}")

    # === 3. 合并去重 ===
    all_keys = {}
    for p in never_processed:
        all_keys[p["key"]] = p["priority"]
    for p in retry_problems:
        if p["key"] not in all_keys:
            all_keys[p["key"]] = p["priority"]

    total = len(all_keys)
    print(f"\n合计（去重后）: {total}")
    print(f"  从未处理: {len(never_processed)}")
    print(f"  检测误判+infra失败: {len(retry_problems)}")
    print(f"  去重减少: {len(never_processed) + len(retry_problems) - total}")

    if total != 3448:
        print(f"\n⚠️  数量不是3,448！预期3,448，实际{total}。请检查。")
        if not dry_run:
            print("数量不符，中止入队。")
            return

    if dry_run:
        print("\nDRY-RUN模式，不入队。去掉--dry-run参数实际入队。")
        return

    # === 4. 检查running队列是否为空 ===
    running_count = r.hlen(RUNNING_KEY)
    if running_count > 0:
        print(f"\n⚠️  math:running有{running_count}个正在运行的题！")
        print("请先停止pipe系统并等running=0后再执行入队。")
        return

    # === 5. 清空Redis队列 ===
    print("\n清空Redis队列...")
    pending_count = r.zcard(PENDING_KEY)
    completed_count = r.llen(COMPLETED_KEY)
    failed_count = r.llen(FAILED_KEY)
    print(f"  清空前: pending={pending_count} completed={completed_count} failed={failed_count}")

    r.delete(PENDING_KEY)
    r.delete(COMPLETED_KEY)
    r.delete(FAILED_KEY)
    print(f"  清空后: pending={r.zcard(PENDING_KEY)} completed={r.llen(COMPLETED_KEY)} failed={r.llen(FAILED_KEY)}")

    # === 6. 入队 ===
    print(f"\n入队 {total} 题到 math:pending ...")
    batch_size = 500
    keys_list = list(all_keys.items())
    enqueued = 0
    for i in range(0, len(keys_list), batch_size):
        batch = keys_list[i:i + batch_size]
        # Redis pipeline批量zadd
        pipe = r.pipeline()
        for key, priority in batch:
            pipe.zadd(PENDING_KEY, {key: priority})
        pipe.execute()
        enqueued += len(batch)
        print(f"  入队进度: {enqueued}/{total}")

    # === 7. 更新ArangoDB extraction_status ===
    print(f"\n更新ArangoDB extraction_status为'queued' ...")
    updated = 0
    for i in range(0, len(keys_list), batch_size):
        batch = keys_list[i:i + batch_size]
        for key, _ in batch:
            db.collection(COLLECTION).update({
                "_key": key,
                "extraction_status": "queued",
            })
        updated += len(batch)
        print(f"  更新进度: {updated}/{total}")

    # === 8. 验证 ===
    final_pending = r.zcard(PENDING_KEY)
    final_completed = r.llen(COMPLETED_KEY)
    final_failed = r.llen(FAILED_KEY)
    final_running = r.hlen(RUNNING_KEY)

    print(f"\n=== 验证结果 ===")
    print(f"  math:pending = {final_pending} (预期 {total})")
    print(f"  math:running = {final_running} (预期 0)")
    print(f"  math:completed = {final_completed} (预期 0)")
    print(f"  math:failed = {final_failed} (预期 0)")
    print(f"  ArangoDB extraction_status更新 = {updated} 题")

    if final_pending == total:
        print(f"\n✅ 队列准备完成！math:pending = {total}")
    else:
        print(f"\n⚠️  数量不符！pending={final_pending} 预期={total}")


if __name__ == "__main__":
    main()
