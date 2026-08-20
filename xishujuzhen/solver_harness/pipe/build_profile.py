#!/usr/bin/env python3
"""build_profile.py — GLM-5.2能力边界Profile构建

从所有有效运行中构建GLM-5.2的数学能力边界Profile。

只统计有效运行（排除基础设施失败和invalid_tool_use）：
- candidate_solved: AI做出来了
- ai_gave_up: AI承认无法做
- failed_token_limit: token超限
- failed_output_limit: 输出长度限制
- failed_thinking_spin: thinking死循环
- failed_no_proof: 没做完
- failed_stall: 卡住

用法:
  python build_profile.py --output profile.json
  python build_profile.py --by-dataset
  python build_profile.py --by-tier
  python build_profile.py --summary
"""
import sys
import os
import json
import argparse
from collections import defaultdict

sys.path.insert(0, os.path.dirname(__file__))

from arango import ArangoClient

DB_HOST = "http://localhost:8529"
DB_NAME = "xishujuzhen_math_glm52"
DB_USER = "root"
DB_PASS = "moira123"
ATTEMPT_COLLECTION = "devin_problem_runs"
PROBLEM_COLLECTION = "problem_extraction_progress"

# 有效verdict（计入Profile）
VALID_VERDICTS = {
    "candidate_solved",      # 成功
    "ai_gave_up",            # AI主动放弃
    "failed_token_limit",    # token超限
    "failed_output_limit",   # 输出长度限制
    "failed_thinking_spin",  # thinking死循环
    "failed_no_proof",       # 没做完
    "failed_stall",          # 卡住
    "answer_leak",           # 答案泄漏（题目问题，不是AI能力）
}

# 无效verdict（不计入Profile）
INVALID_VERDICTS = {
    "failed_connection",     # 基础设施失败
    "rate_limited",          # 基础设施失败
    "launch_error",          # 基础设施失败
    "dead_session",          # 基础设施失败
    "invalid_tool_use",      # 运行无效
    "failed_timeout",        # 超时（可能是基础设施）
}

# 成功verdict
SUCCESS_VERDICTS = {"candidate_solved"}


def get_all_valid_attempts(db) -> list[dict]:
    """获取所有有效运行（pipe-runner的，verdict在VALID_VERDICTS中）"""
    aql = (
        f"FOR a IN {ATTEMPT_COLLECTION} "
        f"FILTER a.batch_id == 'pipe-runner' "
        f"FILTER a.verdict != null "
        f"RETURN {{_key: a._key, problem_id: a.problem_id, exp_id: a.exp_id, "
        f"verdict: a.verdict, runtime_seconds: a.runtime_seconds, started_at: a.started_at}}"
    )
    cursor = db.aql.execute(aql, ttl=300)
    attempts = []
    for row in cursor:
        verdict = row["verdict"]
        # 处理verdict可能是dict的情况
        if isinstance(verdict, dict):
            verdict = verdict.get("auto_status") or verdict.get("runner_status") or "unknown"
        if verdict in VALID_VERDICTS:
            row["verdict"] = verdict
            attempts.append(row)
    return attempts


def get_problem_info(db, problem_keys: list[str]) -> dict:
    """批量获取题目信息"""
    if not problem_keys:
        return {}
    # 分批查询（AQL的IN有长度限制）
    result = {}
    batch_size = 1000
    for i in range(0, len(problem_keys), batch_size):
        batch = problem_keys[i:i+batch_size]
        keys_str = ", ".join(f"'{k}'" for k in batch)
        aql = (
            f"FOR p IN {PROBLEM_COLLECTION} "
            f"FILTER p._key IN [{keys_str}] "
            f"RETURN {{_key: p._key, source_dataset: p.source_dataset, "
            f"difficulty_tier: p.difficulty_tier, problem_type: p.problem_type}}"
        )
        cursor = db.aql.execute(aql, ttl=300)
        for row in cursor:
            result[row["_key"]] = row
    return result


def build_profile(db) -> dict:
    """构建完整Profile"""
    attempts = get_all_valid_attempts(db)
    problem_keys = list(set(a["problem_id"] for a in attempts if a["problem_id"]))
    problem_info = get_problem_info(db, problem_keys)

    # 总体统计
    total = len(attempts)
    by_verdict = defaultdict(int)
    for a in attempts:
        by_verdict[a["verdict"]] += 1

    success_count = by_verdict.get("candidate_solved", 0)
    success_rate = f"{success_count/total*100:.1f}%" if total > 0 else "0%"

    # 按数据集统计
    by_dataset = defaultdict(lambda: defaultdict(int))
    for a in attempts:
        pinfo = problem_info.get(a["problem_id"], {})
        dataset = pinfo.get("source_dataset", "unknown")
        by_dataset[dataset][a["verdict"]] += 1
        by_dataset[dataset]["total"] += 1

    # 按tier统计
    by_tier = defaultdict(lambda: defaultdict(int))
    for a in attempts:
        pinfo = problem_info.get(a["problem_id"], {})
        tier = str(pinfo.get("difficulty_tier", "unknown"))
        by_tier[tier][a["verdict"]] += 1
        by_tier[tier]["total"] += 1

    # 按题目类型统计
    by_type = defaultdict(lambda: defaultdict(int))
    for a in attempts:
        pinfo = problem_info.get(a["problem_id"], {})
        ptype = pinfo.get("problem_type", "unknown")
        by_type[ptype][a["verdict"]] += 1
        by_type[ptype]["total"] += 1

    # 能力边界分析
    capability_boundary = analyze_capability_boundary(by_dataset, by_tier, by_type)

    # 运行时间统计——优先用solve_time_seconds（精确），fallback到runtime_seconds（粗略）
    runtimes = []
    for a in attempts:
        st = a.get("solve_time_seconds")
        if st is None:
            st = a.get("runtime_seconds")
        if st:
            runtimes.append(float(st))
    avg_runtime = sum(runtimes) / len(runtimes) if runtimes else 0

    # 解题时间分布（P50/P90/P99）
    time_distribution = {}
    if runtimes:
        sorted_rt = sorted(runtimes)
        n = len(sorted_rt)
        time_distribution = {
            "min": round(sorted_rt[0], 1),
            "p50": round(sorted_rt[n // 2], 1),
            "p90": round(sorted_rt[int(n * 0.9)], 1),
            "p99": round(sorted_rt[int(n * 0.99)], 1),
            "max": round(sorted_rt[-1], 1),
            "avg": round(avg_runtime, 1),
            "count": n,
        }

    # 成功vs失败的解题时间对比
    success_times = []
    failure_times = []
    for a in attempts:
        st = a.get("solve_time_seconds")
        if st is None:
            st = a.get("runtime_seconds")
        if not st:
            continue
        if a["verdict"] in SUCCESS_VERDICTS:
            success_times.append(float(st))
        else:
            failure_times.append(float(st))

    time_by_outcome = {}
    if success_times:
        time_by_outcome["success_avg"] = round(sum(success_times) / len(success_times), 1)
        time_by_outcome["success_count"] = len(success_times)
    if failure_times:
        time_by_outcome["failure_avg"] = round(sum(failure_times) / len(failure_times), 1)
        time_by_outcome["failure_count"] = len(failure_times)

    return {
        "model": "glm-5.2-high",
        "total_valid_runs": total,
        "success_rate": success_rate,
        "success_count": success_count,
        "by_verdict": dict(by_verdict),
        "by_dataset": dict(by_dataset),
        "by_tier": dict(by_tier),
        "by_type": dict(by_type),
        "avg_runtime_seconds": int(avg_runtime),
        "time_distribution": time_distribution,
        "time_by_outcome": time_by_outcome,
        "capability_boundary": capability_boundary,
    }


def analyze_capability_boundary(by_dataset, by_tier, by_type) -> dict:
    """分析能力边界"""
    can_solve = []
    cannot_solve = []
    boundary_cases = []

    # 按数据集分析
    for dataset, stats in by_dataset.items():
        total = stats.get("total", 0)
        solved = stats.get("candidate_solved", 0)
        if total < 5:  # 样本太少
            continue
        rate = solved / total
        if rate >= 0.8:
            can_solve.append(f"{dataset} ({rate*100:.0f}%)")
        elif rate <= 0.2:
            cannot_solve.append(f"{dataset} ({rate*100:.0f}%)")
        else:
            boundary_cases.append(f"{dataset} ({rate*100:.0f}%)")

    # 按tier分析
    tier_boundary = []
    for tier, stats in sorted(by_tier.items()):
        total = stats.get("total", 0)
        solved = stats.get("candidate_solved", 0)
        if total < 5:
            continue
        rate = solved / total
        tier_boundary.append(f"tier {tier}: {rate*100:.0f}% ({solved}/{total})")

    return {
        "can_solve": can_solve,
        "cannot_solve": cannot_solve,
        "boundary_cases": boundary_cases,
        "by_tier_summary": tier_boundary,
    }


def main():
    parser = argparse.ArgumentParser(description="GLM-5.2能力边界Profile构建")
    parser.add_argument("--output", type=str, help="输出到JSON文件")
    parser.add_argument("--by-dataset", action="store_true")
    parser.add_argument("--by-tier", action="store_true")
    parser.add_argument("--by-type", action="store_true")
    parser.add_argument("--summary", action="store_true")
    args = parser.parse_args()

    client = ArangoClient(hosts=DB_HOST, request_timeout=300)
    db = client.db(DB_NAME, username=DB_USER, password=DB_PASS)

    profile = build_profile(db)

    if args.output:
        with open(args.output, "w") as f:
            json.dump(profile, f, indent=2, ensure_ascii=False)
        print(f"Profile已写入 {args.output}")
        print(f"总有效运行: {profile['total_valid_runs']}")
        print(f"成功率: {profile['success_rate']}")
        return

    if args.summary or (not args.by_dataset and not args.by_tier and not args.by_type):
        print(f"=== GLM-5.2 能力边界Profile ===")
        print(f"模型: {profile['model']}")
        print(f"总有效运行: {profile['total_valid_runs']}")
        print(f"成功数: {profile['success_count']}")
        print(f"成功率: {profile['success_rate']}")
        print(f"\n=== 解题时间分布 ===")
        td = profile.get("time_distribution", {})
        if td:
            print(f"  样本数:  {td.get('count', 0)}")
            print(f"  min:     {td.get('min', 'N/A')}s")
            print(f"  P50:     {td.get('p50', 'N/A')}s")
            print(f"  P90:     {td.get('p90', 'N/A')}s")
            print(f"  P99:     {td.get('p99', 'N/A')}s")
            print(f"  max:     {td.get('max', 'N/A')}s")
            print(f"  avg:     {td.get('avg', 'N/A')}s")
        else:
            print(f"  (无时间数据)")
        to = profile.get("time_by_outcome", {})
        if to:
            print(f"\n=== 成功vs失败解题时间 ===")
            if to.get("success_count"):
                print(f"  成功: avg={to['success_avg']}s (n={to['success_count']})")
            if to.get("failure_count"):
                print(f"  失败: avg={to['failure_avg']}s (n={to['failure_count']})")
        print(f"\n按verdict:")
        for v, c in sorted(profile["by_verdict"].items(), key=lambda x: -x[1]):
            print(f"  {v:25s} {c:>8}")
        print(f"\n能力边界:")
        print(f"  能做:   {profile['capability_boundary']['can_solve']}")
        print(f"  不能做: {profile['capability_boundary']['cannot_solve']}")
        print(f"  边界:   {profile['capability_boundary']['boundary_cases']}")
        print(f"\n按tier:")
        for s in profile["capability_boundary"]["by_tier_summary"]:
            print(f"  {s}")
        return

    if args.by_dataset:
        print(f"=== 按数据集统计 ===")
        for dataset, stats in sorted(profile["by_dataset"].items(), key=lambda x: -x[1].get("total", 0)):
            total = stats.get("total", 0)
            solved = stats.get("candidate_solved", 0)
            rate = f"{solved/total*100:.1f}%" if total > 0 else "0%"
            print(f"\n  {dataset} (total={total}, solved={solved}, rate={rate}):")
            for v, c in sorted(stats.items(), key=lambda x: -x[1]):
                if v != "total":
                    print(f"    {v:25s} {c:>6}")
        return

    if args.by_tier:
        print(f"=== 按难度tier统计 ===")
        for tier, stats in sorted(profile["by_tier"].items()):
            total = stats.get("total", 0)
            solved = stats.get("candidate_solved", 0)
            rate = f"{solved/total*100:.1f}%" if total > 0 else "0%"
            print(f"\n  tier {tier} (total={total}, solved={solved}, rate={rate}):")
            for v, c in sorted(stats.items(), key=lambda x: -x[1]):
                if v != "total":
                    print(f"    {v:25s} {c:>6}")
        return

    if args.by_type:
        print(f"=== 按题目类型统计 ===")
        for ptype, stats in sorted(profile["by_type"].items(), key=lambda x: -x[1].get("total", 0)):
            total = stats.get("total", 0)
            solved = stats.get("candidate_solved", 0)
            rate = f"{solved/total*100:.1f}%" if total > 0 else "0%"
            print(f"\n  {ptype} (total={total}, solved={solved}, rate={rate}):")
            for v, c in sorted(stats.items(), key=lambda x: -x[1]):
                if v != "total":
                    print(f"    {v:25s} {c:>6}")
        return


if __name__ == "__main__":
    main()
