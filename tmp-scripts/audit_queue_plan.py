#!/usr/bin/env python3
"""audit_queue_plan.py — 临时审计脚本

审计目的：验证队列填充方案中的2809题是否满足两个条件：
1. 队列中的所有题目都是tier=1的，没有被平凡解题系统给出过正确答案（candidate_solved）的题目
2. 所有数据库那边tier=1的，没有被AI给出过正确答案的题目（排除token_limit）都放入了队列

审计逻辑：
- 条件1：从方案要入队的2809题中，查是否有题的最后一次run是candidate_solved
- 条件2：从tier=1所有题中，找出"未被AI给出过正确答案"的题（按每题最后run不是candidate_solved），
  排除token_limit题，检查这些题是否都在2809题集合中

使用时间：2026-08-21
审计对象：dev-docs/403-v0-2026-08-21-平凡解题系统队列准备方案.md 中的2809题方案

用法:
  set -a; source /Users/user/glm5.2-math-worktree/.env; set +a
  PYTHONPATH=xishujuzhen/solver_harness/pipe \
    /Users/user/glm5.2-math-worktree/.venv/bin/python3 \
    tmp-scripts/audit_queue_plan.py
"""
import os
from arango import ArangoClient

c = ArangoClient(hosts=os.environ['ARANGO_HOST'])
db = c.db(os.environ['ARANGO_DB'], username=os.environ['ARANGO_USER'], password=os.environ['ARANGO_PASS'])

BATCH_ID = "pipe-runner"
TIER = 1

# ============================================================
# 第一步：重建方案中的2809题集合
# ============================================================
print("=" * 70)
print("审计第一步：重建方案中的2809题集合")
print("=" * 70)

# A. 从未处理过的题（devin_problem_runs中无pipe-runner batch的run记录）
aql_never = '''
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
  RETURN p._key
'''
never_processed = set(db.aql.execute(aql_never, ttl=300))
print(f"从未处理过的题: {len(never_processed)}")

# B. 基础设施失败题（按每题最后run的status是infra失败）
aql_infra = '''
FOR p IN problem_extraction_progress
  FILTER p.difficulty_tier == 1
  FOR r IN devin_problem_runs
    FILTER r.problem_id == p._key
    FILTER r.batch_id == "pipe-runner"
    FILTER r.status != "running"
    SORT r.problem_id, r.ended_at DESC
    COLLECT problem_id = r.problem_id INTO runs = r.status
    LET last_status = runs[0]
    FILTER last_status IN ["rate_limited","dead_session","crash_recovered","failed_connection","launch_error"]
    RETURN problem_id
'''
infra_failed = set(db.aql.execute(aql_infra, ttl=300))
print(f"基础设施失败题: {len(infra_failed)}")

# 合并
queue_set = never_processed | infra_failed
print(f"方案队列合计: {len(queue_set)}")
print(f"  从未处理 + infra失败 = {len(never_processed)} + {len(infra_failed)} = {len(never_processed) + len(infra_failed)}")
print(f"  交集（同时满足两个条件）: {len(never_processed & infra_failed)}")
print(f"  并集: {len(queue_set)}")
print()

# ============================================================
# 条件1：队列中的题是否都是tier=1且未被AI给出过正确答案
# ============================================================
print("=" * 70)
print("审计条件1：队列中的题是否都是tier=1且从未被candidate_solved")
print("=" * 70)

# 1a. 检查队列中的题是否都是tier=1（AQL已按tier=1过滤，但再验证一次）
aql_tier_check = '''
FOR p IN problem_extraction_progress
  FILTER p._key IN @keys
  FILTER p.difficulty_tier != 1
  RETURN {key: p._key, tier: p.difficulty_tier}
'''
wrong_tier = list(db.aql.execute(aql_tier_check, bind_vars={"keys": list(queue_set)}, ttl=300))
if wrong_tier:
    print(f"  ❌ 发现非tier=1的题: {len(wrong_tier)}个")
    for w in wrong_tier[:10]:
        print(f"    {w['key']} tier={w['tier']}")
else:
    print(f"  ✅ 队列中所有题都是tier=1")

# 1b. 检查队列中的题是否从未被candidate_solved（任何一次run）
aql_solved_check = '''
FOR r IN devin_problem_runs
  FILTER r.problem_id IN @keys
  FILTER r.batch_id == "pipe-runner"
  FILTER r.status == "candidate_solved"
  RETURN DISTINCT r.problem_id
'''
solved_in_queue = set(db.aql.execute(aql_solved_check, bind_vars={"keys": list(queue_set)}, ttl=300))
if solved_in_queue:
    print(f"  ❌ 队列中有题曾被candidate_solved: {len(solved_in_queue)}个")
    for k in list(solved_in_queue)[:10]:
        print(f"    {k}")
else:
    print(f"  ✅ 队列中没有任何题曾被candidate_solved")
print()

# ============================================================
# 条件2：tier=1中所有未被AI给出正确答案的题（排除token_limit）是否都在队列中
# ============================================================
print("=" * 70)
print("审计条件2：tier=1中所有未被AI解决且非token_limit的题是否都在队列中")
print("=" * 70)

# 取tier=1中每题最后run的status
aql_last_run = '''
FOR p IN problem_extraction_progress
  FILTER p.difficulty_tier == 1
  FOR r IN devin_problem_runs
    FILTER r.problem_id == p._key
    FILTER r.batch_id == "pipe-runner"
    FILTER r.status != "running"
    SORT r.problem_id, r.ended_at DESC
    COLLECT problem_id = r.problem_id INTO runs = r.status
    LET last_status = runs[0]
    RETURN {problem_id, last_status}
'''
last_runs = list(db.aql.execute(aql_last_run, ttl=300))
last_run_map = {r["problem_id"]: r["last_status"] for r in last_runs}
print(f"tier=1中有run记录的题: {len(last_run_map)}")

# tier=1总题数
aql_total = '''
FOR p IN problem_extraction_progress
  FILTER p.difficulty_tier == 1
  COLLECT WITH COUNT INTO c RETURN c
'''
total_tier1 = db.aql.execute(aql_total, ttl=300).next()
print(f"tier=1总题数: {total_tier1}")

# 所有tier=1题的集合
aql_all_tier1 = '''
FOR p IN problem_extraction_progress
  FILTER p.difficulty_tier == 1
  RETURN p._key
'''
all_tier1 = set(db.aql.execute(aql_all_tier1, ttl=300))

# 未被AI给出正确答案的题 = 从未处理过的题 + 有run但最后run不是candidate_solved的题
never_run_set = all_tier1 - set(last_run_map.keys())
not_solved_with_run = {k for k, v in last_run_map.items() if v != "candidate_solved"}
all_not_solved = never_run_set | not_solved_with_run
print(f"未被AI给出正确答案的题: {len(all_not_solved)}")
print(f"  从未处理过: {len(never_run_set)}")
print(f"  有run但最后run不是solved: {len(not_solved_with_run)}")

# 排除token_limit题
token_limit_set = {k for k, v in last_run_map.items() if v == "failed_token_limit"}
should_be_in_queue = all_not_solved - token_limit_set
print(f"排除token_limit后应入队的题: {len(should_be_in_queue)}")
print(f"  排除的token_limit题: {len(token_limit_set)}")
print()

# 检查这些题是否都在队列中
in_queue = should_be_in_queue & queue_set
not_in_queue = should_be_in_queue - queue_set
extra_in_queue = queue_set - should_be_in_queue

print(f"应入队且已在队列中: {len(in_queue)}")
print(f"应入队但不在队列中: {len(not_in_queue)}")
print(f"在队列中但不应入队: {len(extra_in_queue)}")
print()

if not_in_queue:
    print(f"  ❌ 有{len(not_in_queue)}题应入队但不在队列中！按最后run的status分类：")
    from collections import Counter
    missing_status = Counter()
    for k in not_in_queue:
        s = last_run_map.get(k, "never_processed")
        missing_status[s] += 1
    for s, cnt in missing_status.most_common():
        print(f"    {s}: {cnt}")
    # 列出前10个
    print(f"  前10个缺失的题:")
    for k in list(not_in_queue)[:10]:
        s = last_run_map.get(k, "never_processed")
        print(f"    {k} last_status={s}")

if extra_in_queue:
    print(f"  ⚠️ 有{len(extra_in_queue)}题在队列中但按条件不应入队，按最后run的status分类：")
    from collections import Counter
    extra_status = Counter()
    for k in extra_in_queue:
        s = last_run_map.get(k, "never_processed")
        extra_status[s] += 1
    for s, cnt in extra_status.most_common():
        print(f"    {s}: {cnt}")

if not not_in_queue and not extra_in_queue:
    print("  ✅ 完全匹配！应入队的题全部在队列中，队列中没有多余的题")
print()

# ============================================================
# 总结
# ============================================================
print("=" * 70)
print("审计总结")
print("=" * 70)
print(f"方案队列题数: {len(queue_set)}")
print(f"条件1（都是tier=1且从未solved）: {'✅ 通过' if not wrong_tier and not solved_in_queue else '❌ 失败'}")
print(f"条件2（应入队的题都在队列中）: {'✅ 通过' if not not_in_queue else '❌ 失败'}")
print(f"  应入队题数: {len(should_be_in_queue)}")
print(f"  队列中题数: {len(queue_set)}")
print(f"  差异: {len(queue_set) - len(should_be_in_queue)}")
if extra_in_queue:
    print(f"  队列中多出的题: {len(extra_in_queue)}（需分析原因）")
