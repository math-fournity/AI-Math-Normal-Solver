#!/usr/bin/env python3
"""fix_orphan_running.py — 修复DB中status=running但Redis中已不存在的孤儿记录

问题根因：collector.py旧代码先remove_running再update_db_status，
collector在两步之间退出时，Redis记录已移除但DB状态仍为running。

修复逻辑：
1. 找出DB中所有status=running且exp_id以p开头的记录
2. 检查每条记录是否在Redis running队列中
3. 不在Redis中的=孤儿记录，按export文件内容分类：
   - 有export且含PROOF COMPLETE → DB改为candidate_solved
   - 无export或无PROOF COMPLETE → DB改为dead_session，重新入pending队列

用法:
  cd /Users/user/glm5.2-math-worktree
  set -a; source .env; set +a
  PYTHONPATH=xishujuzhen/solver_harness/pipe .venv/bin/python3 \
    xishujuzhen/solver_harness/pipe/scripts/fix_orphan_running.py [--dry-run]
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

from arango import ArangoClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from redis_queue import get_redis, enqueue_pending

TRAJECTORY_BASE = Path("/Volumes/data/math-agent-glm5.2-tmux-agents-trajectory")
ATTEMPT_COLLECTION = "devin_problem_runs"


def main():
    parser = argparse.ArgumentParser(description="修复孤儿running记录")
    parser.add_argument("--dry-run", action="store_true", help="只打印不修改")
    args = parser.parse_args()

    # 连接DB
    c = ArangoClient(hosts=os.environ.get("ARANGO_HOST", "http://localhost:8529"))
    db = c.db(
        os.environ["ARANGO_DB"],
        username=os.environ.get("ARANGO_USER", "root"),
        password=os.environ.get("ARANGO_PASS", ""),
    )
    r = get_redis()

    # 找DB中所有running记录
    running_records = list(
        db.aql.execute(
            """
      FOR r IN devin_problem_runs
        FILTER r.exp_id LIKE "p%"
        FILTER r.status == "running"
        RETURN {exp_id: r.exp_id, problem_id: r.problem_id, _key: r._key, started_at: r.started_at}
    """
        )
    )

    redis_running_keys = set(r.hkeys("math:running"))
    orphans = [rec for rec in running_records if rec["exp_id"] not in redis_running_keys]

    print(f"DB running记录: {len(running_records)}")
    print(f"Redis running中: {len(redis_running_keys)}")
    print(f"孤儿记录(DB running但Redis不在): {len(orphans)}")
    print()

    if not orphans:
        print("无孤儿记录，无需修复")
        return

    fixed_solved = 0
    fixed_dead = 0
    requeued = 0

    for rec in orphans:
        exp_id = rec["exp_id"]
        problem_id = rec["problem_id"]
        attempt_key = rec["_key"]
        export_path = TRAJECTORY_BASE / exp_id / "exports" / "conversation.json"

        has_proof = False
        if export_path.exists() and export_path.stat().st_size > 100:
            try:
                content = export_path.read_text(encoding="utf-8", errors="replace")
                if "PROOF COMPLETE" in content:
                    has_proof = True
            except Exception:
                pass

        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        if has_proof:
            # 有PROOF COMPLETE → candidate_solved
            if not args.dry_run:
                db.collection(ATTEMPT_COLLECTION).update(
                    {
                        "_key": attempt_key,
                        "status": "candidate_solved",
                        "verdict": "candidate_solved",
                        "ended_at": now,
                        "end_reason": "fix_orphan: export含PROOF COMPLETE, 从running修正为candidate_solved",
                        "fixed_by": "fix_orphan_running.py",
                    }
                )
            fixed_solved += 1
            print(f"  SOLVED  {exp_id} problem={problem_id}")
        else:
            # 无export或无PROOF COMPLETE → dead_session + 重新入pending
            if not args.dry_run:
                db.collection(ATTEMPT_COLLECTION).update(
                    {
                        "_key": attempt_key,
                        "status": "dead_session",
                        "verdict": "dead_session",
                        "ended_at": now,
                        "end_reason": "fix_orphan: 无export或无PROOF COMPLETE, 从running修正为dead_session",
                        "fixed_by": "fix_orphan_running.py",
                    }
                )
                # 重新入pending队列
                enqueue_pending(r, problem_id, priority=0)
            fixed_dead += 1
            requeued += 1
            print(f"  DEAD    {exp_id} problem={problem_id} → 重新入pending")

    print()
    print(f"=== 修复完成 ===")
    print(f"  candidate_solved: {fixed_solved}")
    print(f"  dead_session:     {fixed_dead}")
    print(f"  重新入pending:    {requeued}")
    if args.dry_run:
        print(f"  (dry-run模式, 未实际修改)")


if __name__ == "__main__":
    main()
