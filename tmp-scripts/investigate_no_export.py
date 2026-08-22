#!/usr/bin/env python3
"""investigate_no_export.py — 排查failed_token_limit无export的原因

调查1,111题failed_token_limit无exports/conversation.json的原因：
1. 这些run的时间分布（是历史批次还是系统性问题）
2. 这些run的runtime分布（是否token_limit时devin cli来不及写export）
3. 这些run是否有其他文件（session_info/pane/tmux）
4. 对比有export的failed_token_limit run的特征
5. 检查pane_snapshot内容——是否有线索

用法:
  python investigate_no_export.py
"""
import sys
import os
from collections import Counter
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from arango import ArangoClient

DB_HOST = "http://localhost:8529"
DB_NAME = "xishujuzhen_math_glm52"
DB_USER = "root"
DB_PASS = "moira123"
TRAJ_DIR = "/Volumes/data/math-agent-glm5.2-tmux-agents-trajectory"


def main():
    client = ArangoClient(hosts=DB_HOST, request_timeout=300)
    db = client.db(DB_NAME, username=DB_USER, password=DB_PASS)

    # 1. 查所有failed_token_limit的最后run
    print("查询所有failed_token_limit的最后run记录...")
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
      RETURN {exp_id: r.exp_id, status: r.status, ended_at: r.ended_at, runtime: r.runtime_seconds, started_at: r.started_at}
  )
  FILTER LENGTH(last_run) > 0
  FILTER last_run[0].status == "failed_token_limit"
  RETURN {
    key: p._key,
    source_dataset: p.source_dataset,
    exp_id: last_run[0].exp_id,
    ended_at: last_run[0].ended_at,
    started_at: last_run[0].started_at,
    runtime: last_run[0].runtime
  }
"""
    cursor = db.aql.execute(aql, ttl=300, batch_size=2000)
    runs = list(cursor)
    print(f"  failed_token_limit总数: {len(runs)}")
    print()

    # 2. 分类：有export vs 无export
    has_export = []
    no_export = []
    for r in runs:
        path = os.path.join(TRAJ_DIR, r["exp_id"])
        has_export_file = False
        if os.path.exists(path):
            for root, dirs, fnames in os.walk(path):
                for f in fnames:
                    rel = os.path.relpath(os.path.join(root, f), path)
                    if rel.startswith("exports/"):
                        has_export_file = True
                        break
                if has_export_file:
                    break
        if has_export_file:
            has_export.append(r)
        else:
            no_export.append(r)

    print(f"--- 有export vs 无export ---")
    print(f"  有export: {len(has_export)} ({len(has_export)*100//len(runs)}%)")
    print(f"  无export: {len(no_export)} ({len(no_export)*100//len(runs)}%)")
    print()

    # 3. 时间分布分析
    print(f"--- 无export的run时间分布（按月）---")
    no_export_months = Counter()
    for r in no_export:
        ended = r.get("ended_at", "")
        if ended:
            month = ended[:7]  # YYYY-MM
            no_export_months[month] += 1
    for month, count in sorted(no_export_months.items()):
        print(f"  {month}: {count}")
    print()

    print(f"--- 有export的run时间分布（按月）---")
    has_export_months = Counter()
    for r in has_export:
        ended = r.get("ended_at", "")
        if ended:
            month = ended[:7]
            has_export_months[month] += 1
    for month, count in sorted(has_export_months.items()):
        print(f"  {month}: {count}")
    print()

    # 4. runtime分布对比
    print(f"--- runtime分布对比 ---")
    no_export_runtimes = [r["runtime"] for r in no_export if r["runtime"]]
    has_export_runtimes = [r["runtime"] for r in has_export if r["runtime"]]

    if no_export_runtimes:
        print(f"  无export runtime: min={min(no_export_runtimes):.0f}s max={max(no_export_runtimes):.0f}s "
              f"avg={sum(no_export_runtimes)/len(no_export_runtimes):.0f}s "
              f"median={sorted(no_export_runtimes)[len(no_export_runtimes)//2]:.0f}s")
    if has_export_runtimes:
        print(f"  有export runtime: min={min(has_export_runtimes):.0f}s max={max(has_export_runtimes):.0f}s "
              f"avg={sum(has_export_runtimes)/len(has_export_runtimes):.0f}s "
              f"median={sorted(has_export_runtimes)[len(has_export_runtimes)//2]:.0f}s")
    print()

    # 5. 无export的文件情况
    print(f"--- 无export的run其他文件情况 ---")
    file_counts = Counter()
    for r in no_export:
        path = os.path.join(TRAJ_DIR, r["exp_id"])
        if not os.path.exists(path):
            file_counts["no_dir"] += 1
            continue
        files = set()
        for root, dirs, fnames in os.walk(path):
            for f in fnames:
                rel = os.path.relpath(os.path.join(root, f), path)
                files.add(rel)
        has_si = "session_info.json" in files
        has_pane = any(f.startswith("collector/") for f in files)
        has_tmux = any(f.startswith("tmux/") for f in files)
        if has_si and has_pane and has_tmux:
            file_counts["si+pane+tmux（只缺export）"] += 1
        elif has_si and has_tmux and not has_pane:
            file_counts["si+tmux（缺export+pane）"] += 1
        elif has_si and not has_tmux and not has_pane:
            file_counts["只有si"] += 1
        elif not has_si:
            file_counts["无si"] += 1
        else:
            file_counts["其他组合"] += 1
    for combo, count in sorted(file_counts.items(), key=lambda x: -x[1]):
        print(f"  {combo}: {count}")
    print()

    # 6. 按数据集分布
    print(f"--- 无export按数据集分布 ---")
    ds_counts = Counter(r["source_dataset"] for r in no_export)
    for ds, count in sorted(ds_counts.items(), key=lambda x: -x[1]):
        total_ds = sum(1 for r in runs if r["source_dataset"] == ds)
        print(f"  {ds}: {count}/{total_ds} ({count*100//total_ds}%)")
    print()

    # 7. 抽样检查10个无export的pane_snapshot内容
    print(f"--- 抽样10个无export的pane_snapshot内容 ---")
    for r in no_export[:10]:
        path = os.path.join(TRAJ_DIR, r["exp_id"])
        pane_path = os.path.join(path, "collector", "pane_snapshot.txt")
        tmux_path = os.path.join(path, "tmux", "tmux_pipe.log")
        si_path = os.path.join(path, "session_info.json")

        print(f"\n  {r['exp_id']} (key={r['key']} runtime={r['runtime']}s ended={r['ended_at']})")

        # session_info
        if os.path.exists(si_path):
            import json
            with open(si_path) as f:
                si = json.load(f)
            print(f"    session_info: start={si.get('start_timestamp', '?')[:19]}")

        # pane_snapshot
        if os.path.exists(pane_path):
            size = os.path.getsize(pane_path)
            with open(pane_path) as f:
                content = f.read()
            # 显示最后500字符
            print(f"    pane_snapshot: {size}B")
            print(f"    pane内容最后500字符:")
            print(f"    {content[-500:]!r}")
        else:
            print(f"    pane_snapshot: ❌ 不存在")

        # tmux_pipe.log
        if os.path.exists(tmux_path):
            size = os.path.getsize(tmux_path)
            with open(tmux_path) as f:
                content = f.read()
            print(f"    tmux_pipe.log: {size}B")
            if size > 0:
                print(f"    tmux内容最后300字符:")
                print(f"    {content[-300:]!r}")
        else:
            print(f"    tmux_pipe.log: ❌ 不存在")

    # 8. 对比：抽样5个有export的failed_token_limit
    print(f"\n--- 对比：抽样5个有export的failed_token_limit ---")
    for r in has_export[:5]:
        path = os.path.join(TRAJ_DIR, r["exp_id"])
        export_path = os.path.join(path, "exports", "conversation.json")
        if os.path.exists(export_path):
            size = os.path.getsize(export_path)
            print(f"  {r['exp_id']} (key={r['key']} runtime={r['runtime']}s) export={size}B")


if __name__ == "__main__":
    main()
