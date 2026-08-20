#!/usr/bin/env python3
"""check_exports.py — 全量检查solved题的export文件存在性和大小合理性

对DB中所有candidate_solved的run，检查：
  1. export文件是否存在（/Volumes/data/.../<exp_id>/exports/conversation.json）
  2. 文件大小是否合理
     - <1KB：可疑不完整（正常export含完整thinking，至少几十KB）
     - 1KB-10KB：偏小，需关注
     - 10KB-1MB：正常范围
     - >1MB：偏大，需关注

用法:
  python check_exports.py                  # 全量检查
  python check_exports.py --sample 100     # 随机抽样100个
  python check_exports.py --recent 100     # 最近100个（按ended_at降序）
  python check_exports.py --batch-id pipe-runner  # 只查指定batch_id
"""
import sys
import os
import argparse
import json
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from arango import ArangoClient

TRAJECTORY_BASE = Path("/Volumes/data/math-agent-glm5.2-tmux-agents-trajectory")

# 大小阈值（字节）
SIZE_TOO_SMALL = 1024          # <1KB 可疑不完整
SIZE_SMALL_WARN = 10240        # <10KB 偏小警告
SIZE_LARGE_WARN = 1048576      # >1MB 偏大警告


def main():
    parser = argparse.ArgumentParser(description="全量检查solved题的export文件")
    parser.add_argument('--sample', type=int, default=0, help='随机抽样N个（0=全量）')
    parser.add_argument('--recent', type=int, default=0, help='最近N个（按ended_at降序）')
    parser.add_argument('--batch-id', type=str, default='pipe-runner', help='只查指定batch_id')
    parser.add_argument('--verbose', action='store_true', help='显示所有异常详情')
    args = parser.parse_args()

    client = ArangoClient(hosts=os.environ.get("ARANGO_HOST", "http://localhost:8529"), request_timeout=300)
    db = client.db(os.environ['ARANGO_DB'], username=os.environ.get('ARANGO_USER', 'root'),
                   password=os.environ.get('ARANGO_PASS', ''))

    # 构建AQL查询
    aql = (
        "FOR r IN devin_problem_runs "
        f"FILTER r.batch_id == '{args.batch_id}' "
        "FILTER r.status == 'candidate_solved' "
    )
    bind_vars = {}

    if args.recent > 0:
        aql += "SORT r.ended_at DESC LIMIT @limit "
        bind_vars['limit'] = args.recent
    elif args.sample > 0:
        aql += "SORT RAND() LIMIT @limit "
        bind_vars['limit'] = args.sample

    aql += "RETURN {exp_id: r.exp_id, problem_id: r.problem_id, ended_at: r.ended_at, " \
           "runtime_seconds: r.runtime_seconds, solve_time_seconds: r.solve_time_seconds}"

    print(f"=== Export文件全量检查 ===")
    mode = "全量" if args.sample == 0 and args.recent == 0 else \
           f"随机抽样{args.sample}" if args.sample > 0 else \
           f"最近{args.recent}个"
    print(f"模式: {mode}  batch_id={args.batch_id}")
    print()

    cursor = db.aql.execute(aql, bind_vars=bind_vars, ttl=600)
    records = list(cursor)
    total = len(records)
    print(f"待检查记录数: {total}")
    print()

    # 检查每个记录
    exists = 0
    missing = 0
    too_small = []    # <1KB
    small_warn = []   # 1-10KB
    large_warn = []   # >1MB
    sizes = []

    for i, rec in enumerate(records):
        if i > 0 and i % 1000 == 0:
            print(f"  进度: {i}/{total} ...", flush=True)

        exp_id = rec['exp_id']
        export_path = TRAJECTORY_BASE / exp_id / "exports" / "conversation.json"

        if not export_path.exists():
            missing += 1
            if args.verbose or missing <= 20:
                print(f"  ❌ 缺失: {exp_id} problem={rec['problem_id']} ended={rec.get('ended_at','?')}")
            continue

        exists += 1
        size = export_path.stat().st_size
        sizes.append(size)

        if size < SIZE_TOO_SMALL:
            too_small.append((exp_id, rec['problem_id'], size, rec.get('ended_at', '?')))
        elif size < SIZE_SMALL_WARN:
            small_warn.append((exp_id, rec['problem_id'], size, rec.get('ended_at', '?')))
        elif size > SIZE_LARGE_WARN:
            large_warn.append((exp_id, rec['problem_id'], size, rec.get('ended_at', '?')))

    # === 汇总报告 ===
    print()
    print(f"=== 检查结果 ===")
    print(f"  总数:          {total}")
    print(f"  export存在:    {exists} ({exists/total*100:.1f}%)" if total else "  export存在: 0")
    print(f"  export缺失:    {missing} ({missing/total*100:.1f}%)" if total else "  export缺失: 0")
    print()

    if sizes:
        sizes.sort()
        print(f"  文件大小统计（仅存在的文件）:")
        print(f"    最小: {sizes[0]:,} bytes ({sizes[0]/1024:.1f} KB)")
        print(f"    最大: {sizes[-1]:,} bytes ({sizes[-1]/1024:.1f} KB)")
        print(f"    中位数: {sizes[len(sizes)//2]:,} bytes ({sizes[len(sizes)//2]/1024:.1f} KB)")
        print(f"    平均: {sum(sizes)/len(sizes):,.0f} bytes ({sum(sizes)/len(sizes)/1024:.1f} KB)")
        print()

        # 大小分布
        buckets = {'<1KB(可疑不完整)': 0, '1-10KB(偏小)': 0, '10-100KB': 0,
                   '100KB-1MB(正常)': 0, '>1MB(偏大)': 0}
        for s in sizes:
            if s < 1024: buckets['<1KB(可疑不完整)'] += 1
            elif s < 10240: buckets['1-10KB(偏小)'] += 1
            elif s < 102400: buckets['10-100KB'] += 1
            elif s < 1048576: buckets['100KB-1MB(正常)'] += 1
            else: buckets['>1MB(偏大)'] += 1
        print(f"  大小分布:")
        for b, cnt in buckets.items():
            print(f"    {b}: {cnt}")
        print()

    # === 异常详情 ===
    if missing > 0:
        print(f"  ⚠️ 缺失export: {missing}个")
        if not args.verbose and missing > 20:
            print(f"    (用 --verbose 查看全部，仅显示前20个)")
        print()

    if too_small:
        print(f"  ❌ 可疑不完整（<1KB）: {len(too_small)}个")
        for eid, pid, size, ended in too_small[:20]:
            print(f"    {eid} problem={pid} size={size}B ended={ended}")
        if len(too_small) > 20:
            print(f"    ... 共{len(too_small)}个")
        print()

    if small_warn:
        print(f"  ⚠️ 偏小（1-10KB）: {len(small_warn)}个")
        for eid, pid, size, ended in small_warn[:20]:
            print(f"    {eid} problem={pid} size={size}B({size/1024:.1f}KB) ended={ended}")
        if len(small_warn) > 20:
            print(f"    ... 共{len(small_warn)}个")
        print()

    if large_warn:
        print(f"  ⚠️ 偏大（>1MB）: {len(large_warn)}个")
        for eid, pid, size, ended in large_warn[:20]:
            print(f"    {eid} problem={pid} size={size}B({size/1024/1024:.1f}MB) ended={ended}")
        if len(large_warn) > 20:
            print(f"    ... 共{len(large_warn)}个")
        print()

    # === 判定 ===
    print(f"=== 判定 ===")
    issues = []
    if missing > 0:
        issues.append(f"缺失export {missing}个")
    if too_small:
        issues.append(f"可疑不完整 {len(too_small)}个")
    if small_warn:
        issues.append(f"偏小 {len(small_warn)}个")
    if large_warn:
        issues.append(f"偏大 {len(large_warn)}个")

    if not issues:
        print(f"  ✅ 全部正常: {exists}个export文件存在且大小合理")
    else:
        print(f"  ⚠️ 有 {len(issues)} 类问题需要关注:")
        for issue in issues:
            print(f"    - {issue}")


if __name__ == "__main__":
    main()
