#!/usr/bin/env python3
"""analyze_rerun_vs_remaining.py — 分析某次重跑后tier=1"应重试"池的变化

回答的核心问题："我们重跑了N道题，为什么还有M道题应重试？"

分析维度：
1. tier=1总体情况（总题数/有run/无run/按最后status分类）
2. 重跑名单（指定时间后的run涉及的unique题目）
3. 重跑前后的status变化（重跑前是什么status，重跑后变成什么）
4. "应重试"池的构成（从未处理 + 基础设施失败）
5. 重跑名单与"应重试"池的交叉分析（两个集合是否重叠）
6. 从未处理题的empty_problem_text检测（题目文本为空的题不会跑出run记录）
7. Redis队列verdict分布（empty_problem_text在Redis中可见）

用法:
  python analyze_rerun_vs_remaining.py --tier 1 --rerun-since 2026-08-22T09:00:00Z
  python analyze_rerun_vs_remaining.py --tier 1 --rerun-since 2026-08-22T09:00:00Z --verbose
  python analyze_rerun_vs_remaining.py --tier 1  # 不指定rerun-since，只看当前应重试池

输出：
  - 终端打印完整分析报告
  --export FILE: 导出完整分析结果到JSON文件
"""
import sys
import os
import json
import argparse
from collections import Counter

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from arango import ArangoClient
import redis

DB_HOST = "http://localhost:8529"
DB_NAME = "xishujuzhen_math_glm52"
DB_USER = "root"
DB_PASS = "moira123"
TRAJ_DIR = "/Volumes/data/math-agent-glm5.2-tmux-agents-trajectory"
BATCH_ID = "pipe-runner"

INFRA_FAILURE_STATUSES = {
    "rate_limited", "dead_session", "crash_recovered",
    "failed_connection", "launch_error",
}
MODEL_FAILURE_STATUSES = {
    "failed_token_limit", "ai_gave_up", "failed_thinking_spin",
    "failed_tool_stall", "failed_no_proof", "failed_stall",
}


def load_tier1_problems(db, tier):
    """加载tier=N的全部题目，返回 {key: {extraction_status, source_dataset}}"""
    aql = (
        f"FOR p IN problem_extraction_progress "
        f"FILTER p.difficulty_tier == {tier} "
        f"RETURN {{key: p._key, extraction_status: p.extraction_status, source_dataset: p.source_dataset}}"
    )
    problems = list(db.aql.execute(aql, ttl=300, batch_size=5000))
    return {p["key"]: p for p in problems}


def load_all_runs(db):
    """加载全部pipe-runner终态run，按problem_id分组取最后一条"""
    aql = (
        "FOR r IN devin_problem_runs "
        'FILTER r.batch_id == "pipe-runner" '
        'FILTER r.status != "running" '
        "SORT r.problem_id, r.ended_at DESC "
        "RETURN {problem_id: r.problem_id, status: r.status, ended_at: r.ended_at, started_at: r.started_at, exp_id: r.exp_id}"
    )
    runs = list(db.aql.execute(aql, ttl=300, batch_size=5000))
    # 按problem_id取最后一条（已按ended_at DESC排序）
    last_run = {}
    for r in runs:
        if r["problem_id"] not in last_run:
            last_run[r["problem_id"]] = r
    return runs, last_run


def load_rerun_pids(db, rerun_since):
    """加载指定时间后重跑涉及的unique题目ID"""
    aql = (
        "FOR r IN devin_problem_runs "
        'FILTER r.batch_id == "pipe-runner" '
        f"FILTER r.started_at >= '{rerun_since}' "
        "RETURN DISTINCT r.problem_id"
    )
    return set(db.aql.execute(aql, ttl=300, batch_size=2000))


def load_rerun_last_status(db, rerun_pids, last_run_by_pid):
    """获取重跑名单中每题的最后status"""
    result = {}
    for pid in rerun_pids:
        if pid in last_run_by_pid:
            result[pid] = last_run_by_pid[pid]["status"]
        else:
            result[pid] = "<无终态run>"
    return result


def load_pre_rerun_status(db, rerun_pids, rerun_since):
    """获取重跑名单中每题在重跑前的最后status"""
    aql = (
        "FOR r IN devin_problem_runs "
        'FILTER r.batch_id == "pipe-runner" '
        f"FILTER r.started_at < '{rerun_since}' "
        'FILTER r.status != "running" '
        "SORT r.problem_id, r.ended_at DESC "
        "RETURN {problem_id: r.problem_id, status: r.status}"
    )
    pre_runs = list(db.aql.execute(aql, ttl=300, batch_size=5000))
    pre_last = {}
    for r in pre_runs:
        if r["problem_id"] not in pre_last:
            pre_last[r["problem_id"]] = r["status"]
    result = {}
    for pid in rerun_pids:
        if pid in pre_last:
            result[pid] = pre_last[pid]
        else:
            result[pid] = "<无run>"
    return result


def get_redis_verdicts():
    """获取Redis completed+failed队列中的verdict分布"""
    r = redis.Redis(host="localhost", port=6379, db=0, decode_responses=True)
    completed = r.lrange("math:completed", 0, -1)
    failed = r.lrange("math:failed", 0, -1)
    verdicts = Counter()
    redis_pids = set()
    for item in completed + failed:
        try:
            d = json.loads(item)
            verdicts[d.get("verdict", "<unknown>")] += 1
            if d.get("problem_key"):
                redis_pids.add(d["problem_key"])
        except (json.JSONDecodeError, TypeError):
            pass
    return verdicts, redis_pids, len(completed), len(failed)


def main():
    parser = argparse.ArgumentParser(description="分析某次重跑后tier=1'应重试'池的变化")
    parser.add_argument("--tier", type=int, default=1, help="难度档位（默认1）")
    parser.add_argument("--rerun-since", type=str, default=None,
                        help="重跑开始时间（ISO格式，如2026-08-22T09:00:00Z）")
    parser.add_argument("--verbose", action="store_true", help="列出详细题目ID")
    parser.add_argument("--export", type=str, default=None, help="导出完整分析结果到JSON文件")
    args = parser.parse_args()

    client = ArangoClient(hosts=DB_HOST, request_timeout=300)
    db = client.db(DB_NAME, username=DB_USER, password=DB_PASS)

    tier = args.tier
    rerun_since = args.rerun_since

    # === 1. 加载tier=N题目 ===
    print(f"加载tier={tier}题目...")
    tier1_problems = load_tier1_problems(db, tier)
    tier1_keys = set(tier1_problems.keys())
    print(f"  tier={tier}总数: {len(tier1_keys)}")

    # === 2. 加载所有pipe-runner终态run ===
    print("加载所有pipe-runner终态run...")
    all_runs, last_run_by_pid = load_all_runs(db)
    print(f"  总run数: {len(all_runs)}")
    print(f"  有终态run的unique题数: {len(last_run_by_pid)}")

    # === 3. tier=N总体分类 ===
    never_processed = tier1_keys - set(last_run_by_pid.keys())
    has_run = tier1_keys & set(last_run_by_pid.keys())
    last_status_counts = Counter(last_run_by_pid[p]["status"] for p in has_run)
    infra_fail_pids = set(p for p in has_run if last_run_by_pid[p]["status"] in INFRA_FAILURE_STATUSES)
    model_fail_pids = set(p for p in has_run if last_run_by_pid[p]["status"] in MODEL_FAILURE_STATUSES)
    solved_pids = set(p for p in has_run if last_run_by_pid[p]["status"] == "candidate_solved")

    print()
    print(f"=== tier={tier} 总体分类 ===")
    print(f"  从未处理（无pipe-runner终态run）: {len(never_processed)}")
    print(f"  有run记录: {len(has_run)}")
    print(f"    candidate_solved: {len(solved_pids)}")
    print(f"    基础设施失败: {len(infra_fail_pids)}")
    for s in sorted(INFRA_FAILURE_STATUSES):
        cnt = last_status_counts.get(s, 0)
        if cnt:
            print(f"      {s}: {cnt}")
    print(f"    AI能力失败: {len(model_fail_pids)}")
    for s in sorted(MODEL_FAILURE_STATUSES):
        cnt = last_status_counts.get(s, 0)
        if cnt:
            print(f"      {s}: {cnt}")

    should_retry = len(never_processed) + len(infra_fail_pids)
    print(f"\n  '应重试'池 = {len(never_processed)}从未处理 + {len(infra_fail_pids)}基础设施失败 = {should_retry}")

    # === 4. 从未处理题的详细分析 ===
    print()
    print(f"=== 从未处理题（{len(never_processed)}题）详细分析 ===")
    never_info = [tier1_problems[k] for k in never_processed]
    print(f"  extraction_status分布:")
    for s, cnt in sorted(Counter(p["extraction_status"] for p in never_info).items(), key=lambda x: -x[1]):
        print(f"    {s}: {cnt}")
    print(f"  source_dataset分布:")
    for s, cnt in sorted(Counter(p["source_dataset"] for p in never_info).items(), key=lambda x: -x[1]):
        print(f"    {s}: {cnt}")

    # Redis队列verdict分布
    redis_verdicts, redis_pids, redis_completed_len, redis_failed_len = get_redis_verdicts()
    print(f"\n  Redis队列状态:")
    print(f"    completed: {redis_completed_len}, failed: {redis_failed_len}")
    print(f"    Redis中unique题数: {len(redis_pids)}")
    print(f"  Redis队列verdict分布:")
    for v, cnt in sorted(redis_verdicts.items(), key=lambda x: -x[1]):
        print(f"    {v}: {cnt}")

    # 从未处理题在Redis中的情况
    never_in_redis = never_processed & redis_pids
    never_not_in_redis = never_processed - redis_pids
    print(f"\n  从未处理题在Redis队列中: {len(never_in_redis)}")
    print(f"  从未处理题不在Redis队列中: {len(never_not_in_redis)}")

    # 检测empty_problem_text
    empty_text_count = redis_verdicts.get("empty_problem_text", 0)
    if empty_text_count > 0:
        print(f"\n  ⚠️  Redis中有 {empty_text_count} 题verdict=empty_problem_text（题目文本为空，runner跳过）")
        if len(never_in_redis) == empty_text_count:
            print(f"  → 从未处理的{len(never_processed)}题全部是empty_problem_text，不是运行问题，是数据问题")

    # === 5. 重跑分析（如果指定了--rerun-since）===
    rerun_data = {}
    if rerun_since:
        print()
        print(f"=== 重跑分析（rerun-since={rerun_since}）===")
        rerun_pids = load_rerun_pids(db, rerun_since)
        print(f"  重跑名单: {len(rerun_pids)}题")

        # 重跑后的最后status
        rerun_post_status = load_rerun_last_status(db, rerun_pids, last_run_by_pid)
        post_sc = Counter(rerun_post_status.values())
        print(f"\n  重跑后最后status分布:")
        for s, cnt in sorted(post_sc.items(), key=lambda x: -x[1]):
            print(f"    {s}: {cnt}")

        # 重跑前的最后status
        rerun_pre_status = load_pre_rerun_status(db, rerun_pids, rerun_since)
        pre_sc = Counter(rerun_pre_status.values())
        print(f"\n  重跑前最后status分布:")
        for s, cnt in sorted(pre_sc.items(), key=lambda x: -x[1]):
            print(f"    {s}: {cnt}")

        # 交叉分析
        print(f"\n  === 重跑名单 vs '应重试'池 交叉分析 ===")
        never_in_rerun = never_processed & rerun_pids
        infra_in_rerun = infra_fail_pids & rerun_pids
        infra_not_in_rerun = infra_fail_pids - rerun_pids
        print(f"  从未处理({len(never_processed)})中在重跑名单内: {len(never_in_rerun)}")
        print(f"  基础设施失败({len(infra_fail_pids)})中在重跑名单内: {len(infra_in_rerun)}（重跑后仍失败的）")
        print(f"  基础设施失败({len(infra_fail_pids)})中不在重跑名单内: {len(infra_not_in_rerun)}（之前批次遗留的）")

        # 重跑前后的status变化矩阵
        print(f"\n  === 重跑前后status变化矩阵 ===")
        transition = Counter()
        for pid in rerun_pids:
            pre = rerun_pre_status.get(pid, "<无run>")
            post = rerun_post_status.get(pid, "<无终态run>")
            transition[(pre, post)] += 1
        print(f"  {'重跑前':<25} → {'重跑后':<25} {'数量':>6}")
        print(f"  {'-' * 65}")
        for (pre, post), cnt in sorted(transition.items(), key=lambda x: -x[1]):
            print(f"  {pre:<25} → {post:<25} {cnt:>6}")

        # 结论
        print(f"\n  === 结论 ===")
        rerun_solved = sum(1 for pid in rerun_pids if rerun_post_status.get(pid) == "candidate_solved")
        print(f"  重跑{len(rerun_pids)}题中，{rerun_solved}题成功解决")
        print(f"  重跑前是AI能力失败({len(rerun_pids) - len(infra_in_rerun)}题不在'应重试'池)")
        print(f"  重跑后{len(infra_in_rerun)}题变成基础设施失败（加入了'应重试'池）")
        print(f"  '应重试'池{should_retry}题与重跑{len(rerun_pids)}题几乎不相交:")
        print(f"    从未处理{len(never_processed)}题{'全部是empty_problem_text' if len(never_in_redis) == empty_text_count and empty_text_count > 0 else '不在重跑名单'}")
        print(f"    基础设施失败{len(infra_fail_pids)}题中{len(infra_not_in_rerun)}题是之前批次遗留的")

        rerun_data = {
            "rerun_since": rerun_since,
            "rerun_count": len(rerun_pids),
            "pre_status": dict(pre_sc),
            "post_status": dict(post_sc),
            "transition_matrix": {f"{pre}→{post}": cnt for (pre, post), cnt in transition.items()},
            "never_in_rerun": len(never_in_rerun),
            "infra_in_rerun": len(infra_in_rerun),
            "infra_not_in_rerun": len(infra_not_in_rerun),
        }

    # === 6. verbose输出 ===
    if args.verbose:
        print()
        print(f"=== 详细题目ID ===")
        if never_processed:
            print(f"\n从未处理题（前50个）:")
            for k in sorted(never_processed)[:50]:
                p = tier1_problems[k]
                print(f"  {k} | extraction_status={p['extraction_status']} | dataset={p['source_dataset']}")
            if len(never_processed) > 50:
                print(f"  ... 共{len(never_processed)}题")
        if infra_fail_pids:
            print(f"\n基础设施失败题（前50个）:")
            for k in sorted(infra_fail_pids)[:50]:
                print(f"  {k} | status={last_run_by_pid[k]['status']}")

    # === 7. 导出 ===
    if args.export:
        export_data = {
            "tier": tier,
            "total": len(tier1_keys),
            "never_processed": len(never_processed),
            "has_run": len(has_run),
            "last_status_counts": dict(last_status_counts),
            "should_retry": should_retry,
            "infra_fail_count": len(infra_fail_pids),
            "never_processed_by_dataset": dict(Counter(p["source_dataset"] for p in never_info)),
            "never_processed_by_extraction_status": dict(Counter(p["extraction_status"] for p in never_info)),
            "redis_verdicts": dict(redis_verdicts),
            "empty_text_count": empty_text_count,
            "rerun": rerun_data,
        }
        with open(args.export, "w") as f:
            json.dump(export_data, f, ensure_ascii=False, indent=2)
        print(f"\n分析结果已导出到: {args.export}")


if __name__ == "__main__":
    main()
