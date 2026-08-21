#!/usr/bin/env python3
"""tier1_final_queue_audit.py — 从tier-1整体角度审视最后队列该包含哪些题

把所有tier=1题作为一个整体，按"最后run的status"分类：
- 从未处理过的题（无run记录）
- 处理过的题（按最后run的status分类）

统计每类的数量，判断哪些该入最后的队列、哪些不该入。
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "xishujuzhen", "solver_harness", "pipe"))

from arango import ArangoClient
from collections import defaultdict

client = ArangoClient(hosts="http://localhost:8529", request_timeout=300)
db = client.db("xishujuzhen_math_glm52", username="root", password="moira123")

# 1. tier=1总题数
aql_total = """
FOR p IN problem_extraction_progress
  FILTER p.difficulty_tier == 1
  COLLECT WITH COUNT INTO c
  RETURN c
"""
total = db.aql.execute(aql_total, ttl=300).next()
print(f"=== tier=1 总题数: {total} ===")
print()

# 2. 从未处理过的题（无pipe-runner run记录）
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
  COLLECT WITH COUNT INTO c
  RETURN c
"""
never_processed = db.aql.execute(aql_never, ttl=300).next()
print(f"从未处理过的题（无run记录）: {never_processed}")
print()

# 3. 处理过的题——按最后run的status分类
aql_by_status = """
FOR p IN problem_extraction_progress
  FILTER p.difficulty_tier == 1
  LET runs = (
    FOR r IN devin_problem_runs
      FILTER r.problem_id == p._key
      FILTER r.batch_id == "pipe-runner"
      FILTER r.status != "running"
      SORT r.ended_at DESC
      LIMIT 1
      RETURN r.status
  )
  FILTER LENGTH(runs) > 0
  COLLECT last_status = runs[0] WITH COUNT INTO c
  SORT c DESC
  RETURN {last_status, count: c}
"""
cursor = db.aql.execute(aql_by_status, ttl=300)
status_counts = list(cursor)

print("=== 处理过的题——按最后run的status分类 ===")
processed_total = 0
for row in status_counts:
    print(f"  {row['last_status']}: {row['count']}")
    processed_total += row['count']
print(f"  处理过的小计: {processed_total}")
print(f"  从未处理 + 处理过 = {never_processed + processed_total}")
print()

# 4. 按是否该入最后队列分类
# 入队标准：
# - 从未处理过的题 → 入队
# - 最后run是基础设施失败 → 入队（重试）
# - 最后run是answer_leak/answer_leak_in_input → 入队（检测已取消）
# - 最后run是candidate_solved → 不入队（已解决）
# - 最后run是模型能力失败 → 不入队（Profile数据）
# - 最后run是running → 需检查（可能还在跑或卡住）

INFRA_FAILURES = {"rate_limited", "dead_session", "crash_recovered", "failed_connection", "launch_error"}
MODEL_FAILURES = {"failed_token_limit", "ai_gave_up", "failed_thinking_spin", "failed_stall", "failed_no_proof", "invalid_tool_use"}
DEPRECATED = {"answer_leak", "answer_leak_in_input"}

should_enqueue = never_processed  # 从未处理
should_not = 0
needs_check = 0

print("=== 最后队列入队判断 ===")
print(f"  从未处理过: {never_processed} → 入队")
for row in status_counts:
    s = row["last_status"]
    c = row["count"]
    if s == "candidate_solved":
        print(f"  {s}: {c} → 不入队（已解决）")
        should_not += c
    elif s in INFRA_FAILURES:
        print(f"  {s}: {c} → 入队（基础设施失败，重试）")
        should_enqueue += c
    elif s in DEPRECATED:
        print(f"  {s}: {c} → 入队（检测已取消，重新处理）")
        should_enqueue += c
    elif s in MODEL_FAILURES:
        print(f"  {s}: {c} → 不入队（模型能力失败，Profile数据）")
        should_not += c
    elif s == "running":
        print(f"  {s}: {c} → 需检查（可能还在跑或卡住）")
        needs_check += c
    else:
        print(f"  {s}: {c} → *** 未知status，需判断 ***")
        needs_check += c

print()
print(f"=== 总结 ===")
print(f"  应入队: {should_enqueue}")
print(f"  不入队: {should_not}")
print(f"  需检查: {needs_check}")
print(f"  合计: {should_enqueue + should_not + needs_check}")
