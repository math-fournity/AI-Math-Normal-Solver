#!/usr/bin/env python3
"""check_retry_effect.py — 验证retry效果：按每道题的最后一次run统计 vs 按所有run统计

调查rate_limited等基础设施失败的题是否被retry重新入队并最终解决。
按"每道题最后一次run的status"统计，才是真正的"AI没解决的问题"。

用法:
  python check_retry_effect.py --tier 1
  python check_retry_effect.py --tier 1 --batch-id pipe-runner
"""
import sys
import os
import argparse

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from arango import ArangoClient

DB_HOST = "http://localhost:8529"
DB_NAME = "xishujuzhen_math_glm52"
DB_USER = "root"
DB_PASS = "moira123"
ATTEMPT_COLLECTION = "devin_problem_runs"
PROBLEM_COLLECTION = "problem_extraction_progress"

MODEL_FAILURE_STATUSES = [
    "failed_token_limit", "ai_gave_up", "failed_thinking_spin",
    "failed_tool_stall", "failed_no_proof", "failed_stall",
]
INFRA_FAILURE_STATUSES = [
    "rate_limited", "dead_session", "crash_recovered",
    "failed_connection", "launch_error",
]
DATA_ISSUE_STATUSES = [
    "answer_leak_in_input", "answer_leak",
]


def query_all_runs_by_status(db, tier, batch_id):
    """按所有run统计（原来的方法）"""
    tier_filter = f"FILTER p.difficulty_tier == {tier}" if tier else ""
    aql = (
        f"FOR p IN {PROBLEM_COLLECTION} "
        f"{tier_filter} "
        f"FOR r IN {ATTEMPT_COLLECTION} "
        f"FILTER r.problem_id == p._key "
        f"FILTER r.batch_id == '{batch_id}' "
        f"FILTER r.status != 'running' "
        f"COLLECT status = r.status WITH COUNT INTO c "
        f"SORT c DESC "
        f"RETURN {{status, count: c}}"
    )
    return {r["status"]: r["count"] for r in db.aql.execute(aql, ttl=300)}


def query_last_run_by_status(db, tier, batch_id):
    """按每道题的最后一次run统计（正确方法）"""
    tier_filter = f"FILTER p.difficulty_tier == {tier}" if tier else ""
    # 先按problem_id分组取最后一次run的status
    aql = (
        f"FOR p IN {PROBLEM_COLLECTION} "
        f"{tier_filter} "
        f"FOR r IN {ATTEMPT_COLLECTION} "
        f"FILTER r.problem_id == p._key "
        f"FILTER r.batch_id == '{batch_id}' "
        f"FILTER r.status != 'running' "
        f"SORT r.problem_id, r.ended_at DESC "
        f"COLLECT problem_id = r.problem_id INTO runs = r.status "
        f"LET last_status = runs[0] "
        f"COLLECT status = last_status WITH COUNT INTO c "
        f"SORT c DESC "
        f"RETURN {{status, count: c}}"
    )
    return {r["status"]: r["count"] for r in db.aql.execute(aql, ttl=300)}


def query_retry_counts(db, tier, batch_id):
    """查每道题的run次数分布"""
    tier_filter = f"FILTER p.difficulty_tier == {tier}" if tier else ""
    aql = (
        f"FOR p IN {PROBLEM_COLLECTION} "
        f"{tier_filter} "
        f"FOR r IN {ATTEMPT_COLLECTION} "
        f"FILTER r.problem_id == p._key "
        f"FILTER r.batch_id == '{batch_id}' "
        f"FILTER r.status != 'running' "
        f"COLLECT problem_id = r.problem_id WITH COUNT INTO run_count "
        f"COLLECT rc = run_count WITH COUNT INTO num_problems "
        f"SORT rc ASC "
        f"RETURN {{run_count: rc, num_problems: num_problems}}"
    )
    return list(db.aql.execute(aql, ttl=300))


def query_infra_retry_outcome(db, tier, batch_id):
    """查基础设施失败的题重试后的最终结局"""
    tier_filter = f"FILTER p.difficulty_tier == {tier}" if tier else ""
    # 取每道题的第一次run是infra_failure的，看它最后一次run是什么status
    aql = (
        f"FOR p IN {PROBLEM_COLLECTION} "
        f"{tier_filter} "
        f"FOR r IN {ATTEMPT_COLLECTION} "
        f"FILTER r.problem_id == p._key "
        f"FILTER r.batch_id == '{batch_id}' "
        f"FILTER r.status != 'running' "
        f"SORT r.problem_id, r.ended_at ASC "
        f"COLLECT problem_id = r.problem_id INTO runs = r.status "
        f"LET first_status = runs[0] "
        f"LET last_status = runs[-1] "
        f"FILTER first_status IN ['rate_limited','dead_session','crash_recovered','failed_connection','launch_error'] "
        f"COLLECT outcome = last_status WITH COUNT INTO c "
        f"SORT c DESC "
        f"RETURN {{outcome, count: c}}"
    )
    return list(db.aql.execute(aql, ttl=300))


def print_comparison(all_runs, last_runs, tier_label):
    """打印两种统计的对比"""
    total_all = sum(all_runs.values())
    total_last = sum(last_runs.values())
    solved_all = all_runs.get("candidate_solved", 0)
    solved_last = last_runs.get("candidate_solved", 0)

    print(f"=== {tier_label} 对比统计 ===")
    print()
    print(f"{'指标':<30} {'按所有run':>12} {'按最后run':>12} {'差异':>10}")
    print("-" * 66)
    print(f"{'总run/题数':<30} {total_all:>12} {total_last:>12} {total_all - total_last:>10}")
    print(f"{'已解决(candidate_solved)':<30} {solved_all:>12} {solved_last:>12} {solved_last - solved_all:>10}")
    print(f"{'解决率':<30} {solved_all/total_all*100:>11.1f}% {solved_last/total_last*100:>11.1f}%")
    print()
    print(f"{'各status明细':<30} {'按所有run':>12} {'按最后run':>12} {'差异':>10}")
    print("-" * 66)
    all_statuses = set(list(all_runs.keys()) + list(last_runs.keys()))
    for s in sorted(all_statuses, key=lambda x: -(last_runs.get(x, 0))):
        a = all_runs.get(s, 0)
        l = last_runs.get(s, 0)
        print(f"  {s:<28} {a:>12} {l:>12} {l - a:>10}")


def print_category(last_runs, tier_label):
    """按三大类打印最后run的分类"""
    total = sum(last_runs.values())
    solved = last_runs.get("candidate_solved", 0)
    unsolved = total - solved

    def cat_sum(statuses):
        return sum(last_runs.get(s, 0) for s in statuses)

    def pct(n):
        return f"{n/unsolved*100:.1f}%" if unsolved > 0 else "0%"

    model_count = cat_sum(MODEL_FAILURE_STATUSES)
    infra_count = cat_sum(INFRA_FAILURE_STATUSES)
    data_count = cat_sum(DATA_ISSUE_STATUSES)

    print()
    print(f"=== {tier_label} 按【每题最后run】的三大类分类 ===")
    print()
    print(f"总题数: {total}")
    print(f"已解决: {solved} ({solved/total*100:.1f}%)")
    print(f"未解决: {unsolved} ({unsolved/total*100:.1f}%)")
    print()

    categories = [
        ("AI能力失败", MODEL_FAILURE_STATUSES, model_count),
        ("基础设施失败", INFRA_FAILURE_STATUSES, infra_count),
        ("数据问题", DATA_ISSUE_STATUSES, data_count),
    ]
    for label, statuses, count in categories:
        print(f"  {label}: {count} ({pct(count)})")
        for s in sorted(statuses, key=lambda x: -last_runs.get(x, 0)):
            c = last_runs.get(s, 0)
            if c > 0:
                print(f"    {s:<25} {c:>6} ({pct(c)})")
        print()


def main():
    parser = argparse.ArgumentParser(description="验证retry效果：按最后run vs 按所有run统计")
    parser.add_argument("--tier", type=int, default=None)
    parser.add_argument("--batch-id", type=str, default="pipe-runner")
    args = parser.parse_args()

    client = ArangoClient(hosts=DB_HOST, request_timeout=300)
    db = client.db(DB_NAME, username=DB_USER, password=DB_PASS)

    tier_label = f"tier={args.tier}" if args.tier else "所有tier"

    # 1. run次数分布
    print(f"=== {tier_label} 每道题的run次数分布 ===")
    retry_dist = query_retry_counts(db, args.tier, args.batch_id)
    for r in retry_dist:
        print(f"  run {r['run_count']}次: {r['num_problems']}题")
    multi_run = sum(r["num_problems"] for r in retry_dist if r["run_count"] > 1)
    total_problems = sum(r["num_problems"] for r in retry_dist)
    print(f"  多次run的题: {multi_run}/{total_problems} ({multi_run/total_problems*100:.1f}%)")
    print()

    # 2. 对比统计
    all_runs = query_all_runs_by_status(db, args.tier, args.batch_id)
    last_runs = query_last_run_by_status(db, args.tier, args.batch_id)
    print_comparison(all_runs, last_runs, tier_label)

    # 3. 按最后run的三大类分类
    print_category(last_runs, tier_label)

    # 4. 基础设施失败的题重试后的最终结局
    print(f"=== {tier_label} 基础设施失败题的重试最终结局 ===")
    print(f"（第一次run是infra_failure，最后一次run是什么status）")
    print()
    outcomes = query_infra_retry_outcome(db, args.tier, args.batch_id)
    total_infra = sum(o["count"] for o in outcomes)
    for o in outcomes:
        print(f"  {o['outcome']:<25} {o['count']:>6} ({o['count']/total_infra*100:.1f}%)")
    print(f"  总计: {total_infra}题经历过基础设施失败")


if __name__ == "__main__":
    main()
