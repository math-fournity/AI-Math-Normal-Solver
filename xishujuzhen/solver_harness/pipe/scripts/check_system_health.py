#!/usr/bin/env python3
"""check_system_health.py — 系统运行落盘一致性检查

检查pipe系统的硬盘落盘和数据库落盘的一致性：
1. Redis队列状态（pending/running/completed/failed）
2. ArangoDB中本次启动后的run记录
3. 硬盘trajectory目录
4. 交叉验证（ArangoDB run vs 硬盘目录）
5. 终态run文件完整性（按status分组抽样）
6. ArangoDB字段完整性

用法:
  python check_system_health.py                # 检查本次启动后的运行情况
  python check_system_health.py --start-ts N   # 指定启动时间戳（Unix秒）
"""
import sys
import os
import time
from datetime import datetime, timezone
from collections import Counter, defaultdict

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
    custom_start = None
    if "--start-ts" in sys.argv:
        idx = sys.argv.index("--start-ts")
        custom_start = float(sys.argv[idx + 1])

    client = ArangoClient(hosts=DB_HOST, request_timeout=300)
    db = client.db(DB_NAME, username=DB_USER, password=DB_PASS)
    r = redis.Redis(host="localhost", port=6379, db=0, decode_responses=True)

    # 系统启动时间
    start_ts = r.get("math:system:start_time")
    if start_ts:
        start_ts = float(start_ts)
    elif custom_start:
        start_ts = custom_start
    else:
        start_ts = time.time() - 3600  # fallback: 1小时前

    start_dt = datetime.fromtimestamp(start_ts, tz=timezone.utc)
    start_iso = start_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    start_str = start_dt.strftime("%Y-%m-%d %H:%M:%S UTC")

    print(f"=== 系统运行落盘一致性检查 ===")
    print(f"系统启动时间: {start_str}")
    print()

    # === 1. Redis队列状态 ===
    pending = r.zcard(PENDING_KEY)
    running = r.hlen("math:running")
    completed = r.llen("math:completed")
    failed = r.llen("math:failed")
    concurrency = r.get("math:config:concurrency")

    print(f"--- Redis队列状态 ---")
    print(f"  pending: {pending}")
    print(f"  running: {running}")
    print(f"  completed: {completed}")
    print(f"  failed: {failed}")
    print(f"  concurrency: {concurrency}")
    print(f"  已消费: {3448 - pending} / 3448")
    print()

    # === 2. ArangoDB run记录 ===
    aql = """
FOR r IN devin_problem_runs
  FILTER r.batch_id == "pipe-runner"
  FILTER r.started_at >= @start_iso
  RETURN {exp_id: r.exp_id, status: r.status, problem_id: r.problem_id}
"""
    cursor = db.aql.execute(aql, bind_vars={"start_iso": start_iso}, ttl=300, batch_size=2000)
    runs = list(cursor)

    print(f"--- ArangoDB run记录 ---")
    print(f"  本次启动后新增: {len(runs)}")
    sc = Counter(r["status"] for r in runs)
    for s, c in sorted(sc.items(), key=lambda x: -x[1]):
        print(f"    {s}: {c}")
    print()

    # === 3. 硬盘目录交叉验证 ===
    print(f"--- 交叉验证 ---")
    on_disk = 0
    not_on_disk = 0
    not_on_disk_list = []
    for r in runs:
        path = os.path.join(TRAJ_DIR, r["exp_id"])
        if os.path.exists(path):
            on_disk += 1
        else:
            not_on_disk += 1
            not_on_disk_list.append(r)

    print(f"  ArangoDB run: {len(runs)}, 硬盘有目录: {on_disk}, 硬盘无目录: {not_on_disk}")
    if not_on_disk_list:
        print(f"  ⚠️  硬盘无目录的run（前10个）:")
        for r in not_on_disk_list[:10]:
            print(f"    exp_id={r['exp_id']} status={r['status']}")
    else:
        print(f"  ✅ ArangoDB与硬盘完全一致")
    print()

    # === 4. 终态run文件完整性（按status分组抽样3个）===
    finished = [r for r in runs if r["status"] != "running"]
    by_status = defaultdict(list)
    for r in finished:
        by_status[r["status"]].append(r["exp_id"])

    print(f"--- 终态run文件完整性（每组抽样3个）---")
    for status, exp_ids in sorted(by_status.items()):
        print(f"  {status} ({len(exp_ids)}题）:")
        for exp_id in exp_ids[:3]:
            path = os.path.join(TRAJ_DIR, exp_id)
            files = []
            if os.path.exists(path):
                for root, dirs, fnames in os.walk(path):
                    for f in fnames:
                        rel = os.path.relpath(os.path.join(root, f), path)
                        size = os.path.getsize(os.path.join(root, f))
                        files.append((rel, size))
            has_si = any(f[0] == "session_info.json" for f in files)
            has_export = any(f[0].startswith("exports/") for f in files)
            has_pane = any(f[0].startswith("collector/") for f in files)
            has_tmux = any(f[0].startswith("tmux/") for f in files)
            export_size = next((f[1] for f in files if f[0].startswith("exports/")), 0)
            print(f"    {exp_id}: si={'✅' if has_si else '❌'} "
                  f"export={'✅' if has_export else '❌'}({export_size}B) "
                  f"pane={'✅' if has_pane else '❌'} "
                  f"tmux={'✅' if has_tmux else '❌'}")
    print()

    # === 5. ArangoDB字段完整性（抽样5条终态run）===
    print(f"--- ArangoDB字段完整性（抽样5条终态run）---")
    aql2 = """
FOR r IN devin_problem_runs
  FILTER r.batch_id == "pipe-runner"
  FILTER r.started_at >= @start_iso
  FILTER r.status != "running"
  SORT r.started_at DESC
  LIMIT 5
  RETURN {
    key: r._key, problem_id: r.problem_id, status: r.status,
    ended_at: r.ended_at, runtime: r.runtime_seconds,
    end_reason: r.end_reason, verdict: r.verdict,
    has_pane: r.pane_snapshot != null
  }
"""
    for r in db.aql.execute(aql2, bind_vars={"start_iso": start_iso}, ttl=300):
        reason = (r["end_reason"] or "")[:50]
        print(f"  {r['key']}: status={r['status']} ended={r['ended_at']} "
              f"runtime={r['runtime']}s verdict={r['verdict']} "
              f"pane={'✅' if r['has_pane'] else '❌'} reason={reason}")
    print()

    # === 6. 总结 ===
    print(f"=== 总结 ===")
    print(f"  已消费: {3448 - pending} / 3448 题")
    print(f"  正在运行: {running}")
    print(f"  已终态: {len(finished)}")
    print(f"  ArangoDB与硬盘一致性: {'✅' if not_on_disk == 0 else '⚠️ 不一致'}")


if __name__ == "__main__":
    main()
