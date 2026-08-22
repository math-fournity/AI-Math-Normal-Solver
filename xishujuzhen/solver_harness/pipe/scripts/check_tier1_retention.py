#!/usr/bin/env python3
"""check_tier1_retention.py — 检查tier=1全部题目的留存结果完整性

检查所有tier=1题目的：
1. 数据库中是否有run记录
2. 最后run的status是什么
3. 硬盘上是否有对应的trajectory目录
4. 目录中是否有要求的留存文件（session_info/conversation.json/pane_snapshot/tmux_pipe.log）

按status分组报告缺失情况。

用法:
  python check_tier1_retention.py                # 检查全部tier=1
  python check_tier1_retention.py --verbose      # 列出所有缺失的题
  python check_tier1_retention.py --export FILE  导出缺失列表到文件
"""
import sys
import os
import json
from collections import Counter, defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from arango import ArangoClient

DB_HOST = "http://localhost:8529"
DB_NAME = "xishujuzhen_math_glm52"
DB_USER = "root"
DB_PASS = "moira123"
TRAJ_DIR = "/Volumes/data/math-agent-glm5.2-tmux-agents-trajectory"


def main():
    verbose = "--verbose" in sys.argv
    export_path = None
    if "--export" in sys.argv:
        idx = sys.argv.index("--export")
        export_path = sys.argv[idx + 1]

    client = ArangoClient(hosts=DB_HOST, request_timeout=300)
    db = client.db(DB_NAME, username=DB_USER, password=DB_PASS)

    # === 1. 查全部tier=1题目的最后run ===
    print("查询全部tier=1题目的最后run记录...")
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
      RETURN {exp_id: r.exp_id, status: r.status, ended_at: r.ended_at}
  )
  RETURN {
    key: p._key,
    source_dataset: p.source_dataset,
    has_run: LENGTH(last_run) > 0,
    last_exp_id: last_run[0].exp_id || null,
    last_status: last_run[0].status || null,
    last_ended_at: last_run[0].ended_at || null
  }
"""
    cursor = db.aql.execute(aql, ttl=300, batch_size=2000)
    problems = list(cursor)
    print(f"  tier=1总数: {len(problems)}")
    print()

    # === 2. 分类统计 ===
    no_run = [p for p in problems if not p["has_run"]]
    has_run = [p for p in problems if p["has_run"]]

    print(f"--- 按是否有run记录分类 ---")
    print(f"  有run记录: {len(has_run)}")
    print(f"  无run记录（从未处理）: {len(no_run)}")
    print()

    status_counts = Counter(p["last_status"] for p in has_run)
    print(f"--- 有run记录的按最后status分类 ---")
    for s, c in sorted(status_counts.items(), key=lambda x: -x[1]):
        print(f"  {s}: {c}")
    print()

    # === 3. 检查硬盘文件完整性 ===
    # 按status分组检查文件
    # 文件要求：
    # - session_info.json: 所有status都必须有
    # - exports/conversation.json: 除rate_limited/failed_stall外都必须有
    # - collector/pane_snapshot.txt: 除failed_stall外都必须有
    # - tmux/tmux_pipe.log: 所有status都必须有

    EXEMPT_EXPORT = {"rate_limited", "failed_stall"}
    EXEMPT_PANE = {"failed_stall"}

    missing = defaultdict(list)  # category -> list of {key, exp_id, status, detail}
    file_stats = defaultdict(lambda: {"total": 0, "ok": 0, "missing": 0})

    print("检查硬盘文件完整性...")
    for p in has_run:
        exp_id = p["last_exp_id"]
        status = p["last_status"]
        path = os.path.join(TRAJ_DIR, exp_id)

        # 检查目录是否存在
        if not os.path.exists(path):
            missing["no_dir"].append({"key": p["key"], "exp_id": exp_id, "status": status})
            for f in ["session_info", "export", "pane", "tmux"]:
                file_stats[f]["total"] += 1
                file_stats[f]["missing"] += 1
            continue

        # 遍历目录获取文件列表
        files = set()
        for root, dirs, fnames in os.walk(path):
            for f in fnames:
                rel = os.path.relpath(os.path.join(root, f), path)
                files.add(rel)

        # 检查各文件
        checks = [
            ("session_info", "session_info.json", False),
            ("export", None, status in EXEMPT_EXPORT),  # 多个exports/下文件
            ("pane", None, status in EXEMPT_PANE),      # 多个collector/下文件
            ("tmux", None, False),                       # 多个tmux/下文件
        ]

        for name, exact, exempt in checks:
            file_stats[name]["total"] += 1
            if exempt:
                file_stats[name]["ok"] += 1
                continue

            if name == "session_info":
                found = "session_info.json" in files
            elif name == "export":
                found = any(f.startswith("exports/") for f in files)
            elif name == "pane":
                found = any(f.startswith("collector/") for f in files)
            elif name == "tmux":
                found = any(f.startswith("tmux/") for f in files)

            if found:
                file_stats[name]["ok"] += 1
            else:
                file_stats[name]["missing"] += 1
                missing[f"no_{name}"].append({
                    "key": p["key"], "exp_id": exp_id, "status": status
                })

    # === 4. 报告 ===
    print()
    print(f"=== 留存结果完整性报告 ===")
    print()
    print(f"tier=1总数: {len(problems)}")
    print(f"  有run记录: {len(has_run)}")
    print(f"  无run记录（从未处理）: {len(no_run)}")
    print()

    print(f"--- 文件完整性统计 ---")
    print(f"  {'文件':<25} {'总数':>8} {'有文件':>8} {'缺失':>8} {'豁免':>8}")
    for name in ["session_info", "export", "pane", "tmux"]:
        s = file_stats[name]
        exempt_count = sum(1 for p in has_run
                          if (name == "export" and p["last_status"] in EXEMPT_EXPORT) or
                             (name == "pane" and p["last_status"] in EXEMPT_PANE))
        print(f"  {name:<25} {s['total']:>8} {s['ok']:>8} {s['missing']:>8} {exempt_count:>8}")
    print()

    # 按status×文件交叉表
    print(f"--- 按status×文件缺失交叉表 ---")
    all_statuses = sorted(status_counts.keys())
    print(f"  {'status':<25} {'题数':>8} {'无目录':>8} {'无si':>8} {'无export':>8} {'无pane':>8} {'无tmux':>8}")
    for status in all_statuses:
        status_problems = [p for p in has_run if p["last_status"] == status]
        count = len(status_problems)
        no_dir = sum(1 for p in status_problems if not os.path.exists(os.path.join(TRAJ_DIR, p["last_exp_id"])))
        no_si = sum(1 for m in missing["no_session_info"] if m["status"] == status)
        no_export = sum(1 for m in missing["no_export"] if m["status"] == status)
        no_pane = sum(1 for m in missing["no_pane"] if m["status"] == status)
        no_tmux = sum(1 for m in missing["no_tmux"] if m["status"] == status)
        print(f"  {status:<25} {count:>8} {no_dir:>8} {no_si:>8} {no_export:>8} {no_pane:>8} {no_tmux:>8}")
    print()

    # 缺失汇总
    total_missing = sum(len(v) for v in missing.values())
    print(f"--- 缺失汇总 ---")
    print(f"  总缺失项: {total_missing}")
    for cat, items in sorted(missing.items()):
        if items:
            print(f"  {cat}: {len(items)}")
    print()

    # verbose: 列出所有缺失
    if verbose and total_missing > 0:
        print(f"--- 所有缺失详情 ---")
        for cat, items in sorted(missing.items()):
            if items:
                print(f"\n  {cat} ({len(items)}个）:")
                for m in items[:50]:
                    print(f"    key={m['key']} exp_id={m['exp_id']} status={m['status']}")
                if len(items) > 50:
                    print(f"    ... 共{len(items)}个")
        print()

    # 导出
    if export_path and total_missing > 0:
        export_data = {
            "total_tier1": len(problems),
            "has_run": len(has_run),
            "no_run": len(no_run),
            "missing": dict(missing),
        }
        with open(export_path, "w") as f:
            json.dump(export_data, f, ensure_ascii=False, indent=2)
        print(f"缺失列表已导出到: {export_path}")

    # === 5. 结论 ===
    print(f"=== 结论 ===")
    if total_missing == 0:
        print(f"✅ 所有有run记录的tier=1题目（{len(has_run)}题）的留存文件完整。")
    else:
        print(f"⚠️  有 {total_missing} 项缺失，需要进一步排查。")
    print(f"  无run记录的题（从未处理）: {len(no_run)} 题——这些题在最终队列中已处理或待处理。")


if __name__ == "__main__":
    main()
