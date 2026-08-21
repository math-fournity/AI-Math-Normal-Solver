#!/usr/bin/env python3
"""check_tier1_remaining.py — 查tier=1还有哪些题需要解决

分类：
1. 从未处理过的题（devin_problem_runs中无记录）
2. 处理过但未解决的题（按每题最后run的status）
   - AI能力失败（token_limit等）—— 是否值得重试取决于token预算是否提高
   - 基础设施失败（retry已重试3次仍失败）—— 应该重试
   - 数据问题（answer_leak）—— 不重试，题目本身有问题

用法:
  python check_tier1_remaining.py --tier 1
  python check_tier1_remaining.py --tier 1 --batch-id pipe-runner
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


def main():
    parser = argparse.ArgumentParser(description="查tier还有哪些题需要解决")
    parser.add_argument("--tier", type=int, default=1)
    parser.add_argument("--batch-id", type=str, default="pipe-runner")
    args = parser.parse_args()

    client = ArangoClient(hosts=DB_HOST, request_timeout=300)
    db = client.db(DB_NAME, username=DB_USER, password=DB_PASS)

    tier = args.tier
    batch_id = args.batch_id

    # 1. tier总题数
    aql = f"FOR p IN {PROBLEM_COLLECTION} FILTER p.difficulty_tier == {tier} COLLECT WITH COUNT INTO c RETURN c"
    total_tier = db.aql.execute(aql, ttl=300).next()

    # 2. 有run记录的题数（按每题最后run分类）
    aql = (
        f"FOR p IN {PROBLEM_COLLECTION} "
        f"FILTER p.difficulty_tier == {tier} "
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
    last_run = {r["status"]: r["count"] for r in db.aql.execute(aql, ttl=300)}
    processed = sum(last_run.values())
    solved = last_run.get("candidate_solved", 0)

    # 3. 从未处理过的题数
    never_processed = total_tier - processed

    # 4. 处理过但未解决的题，按分类
    model_fail = sum(last_run.get(s, 0) for s in MODEL_FAILURE_STATUSES)
    infra_fail = sum(last_run.get(s, 0) for s in INFRA_FAILURE_STATUSES)
    data_issue = sum(last_run.get(s, 0) for s in DATA_ISSUE_STATUSES)

    # 5. 当前running的题
    aql = (
        f"FOR p IN {PROBLEM_COLLECTION} "
        f"FILTER p.difficulty_tier == {tier} "
        f"FOR r IN {ATTEMPT_COLLECTION} "
        f"FILTER r.problem_id == p._key "
        f"FILTER r.batch_id == '{batch_id}' "
        f"FILTER r.status == 'running' "
        f"COLLECT WITH COUNT INTO c RETURN c"
    )
    running = db.aql.execute(aql, ttl=300).next()

    print(f"=== tier={tier} 题目待解决分析 ===")
    print(f"(batch_id={batch_id}, 按每题最后run统计)")
    print()
    print(f"tier={tier} 总题数:           {total_tier}")
    print(f"当前running中:               {running}")
    print(f"已处理（有终态run记录）:       {processed}")
    print(f"  已解决:                    {solved} ({solved/processed*100:.1f}%)")
    print(f"  未解决:                    {processed - solved} ({(processed-solved)/processed*100:.1f}%)")
    print(f"    AI能力失败:              {model_fail}")
    print(f"    基础设施失败(重试后仍失败): {infra_fail}")
    print(f"    数据问题:                {data_issue}")
    print(f"从未处理过:                  {never_processed}")
    print()
    print(f"=== 待解决题分类 ===")
    print()
    print(f"类别                          数量       是否应重试   原因")
    print(f"{'-'*90}")
    print(f"{'从未处理过的题':<30} {never_processed:>6}    {'是':>6}      从未运行过，应入队解决")
    print(f"{'基础设施失败(重试后仍失败)':<30} {infra_fail:>6}    {'是':>6}      retry已重试3次仍失败，需排查系统性问题后重试")
    print(f"{'AI能力失败-token_limit':<30} {last_run.get('failed_token_limit',0):>6}    {'看情况':>6}      如果提高token预算则值得重试，否则结果不变")
    print(f"{'AI能力失败-ai_gave_up':<30} {last_run.get('ai_gave_up',0):>6}    {'否':>6}      AI主动认为解不了，重试大概率同样结果")
    print(f"{'AI能力失败-其他':<30} {model_fail - last_run.get('failed_token_limit',0) - last_run.get('ai_gave_up',0):>6}    {'否':>6}      卡住/空转/无proof，边缘case")
    print(f"{'数据问题-answer_leak_in_input':<30} {last_run.get('answer_leak_in_input',0):>6}    {'否':>6}      题目文本含答案，不应入队")
    print(f"{'数据问题-answer_leak':<30} {last_run.get('answer_leak',0):>6}    {'否':>6}      AI检测到答案泄漏，不应入队")
    print()

    should_retry = never_processed + infra_fail
    maybe_retry = last_run.get("failed_token_limit", 0)
    should_not = (model_fail - last_run.get("failed_token_limit", 0)) + data_issue

    print(f"=== 汇总 ===")
    print(f"应该重试（从未处理+基础设施失败）:        {should_retry}")
    print(f"看情况重试（token_limit，取决于token预算）: {maybe_retry}")
    print(f"不应重试（AI放弃+数据问题+边缘case）:     {should_not}")
    print(f"待解决总计:                           {should_retry + maybe_retry + should_not}")


if __name__ == "__main__":
    main()
