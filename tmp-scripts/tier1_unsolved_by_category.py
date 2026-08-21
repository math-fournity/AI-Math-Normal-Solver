#!/usr/bin/env python3
"""tier1_unsolved_by_category.py — 临时调查脚本

调查目的：按status聚合统计tier=1做题结果，区分AI能力失败/基础设施失败/数据问题三大类。
这是第二次调查用的脚本，按所有run记录统计（不是按每题最后run）。
后来发现这个方法有问题——rate_limited等中间run被算进来了。
正确的统计方法见check_retry_effect.py（按每题最后run）。
这个脚本的逻辑后来被持久化到query_failures.py --by-category。

使用时间：2026-08-21
调查结果记录在：dev-docs/402-v0-2026-08-21-tier1做题结果调查报告.md §0

用法:
  set -a; source /Users/user/glm5.2-math-worktree/.env; set +a
  PYTHONPATH=xishujuzhen/solver_harness/pipe \
    /Users/user/glm5.2-math-worktree/.venv/bin/python3 \
    tmp-scripts/tier1_unsolved_by_category.py
"""
import os
from arango import ArangoClient

c = ArangoClient(hosts=os.environ['ARANGO_HOST'])
db = c.db(os.environ['ARANGO_DB'], username=os.environ['ARANGO_USER'], password=os.environ['ARANGO_PASS'])

aql = '''
FOR p IN problem_extraction_progress
  FILTER p.difficulty_tier == 1
  FOR r IN devin_problem_runs
    FILTER r.problem_id == p._key
    FILTER r.batch_id == "pipe-runner"
    FILTER r.status != "running"
    COLLECT status = r.status WITH COUNT INTO c
    SORT c DESC
    RETURN {status, count: c}
'''
results = list(db.aql.execute(aql, ttl=120))
total = sum(r['count'] for r in results)
solved = sum(r['count'] for r in results if r['status'] == 'candidate_solved')
unsolved = total - solved
print(f'tier=1 总处理题数: {total}')
print(f'已解决: {solved} ({solved/total*100:.1f}%)')
print(f'未解决: {unsolved} ({unsolved/total*100:.1f}%)')
print()

model_failures = {'failed_token_limit', 'ai_gave_up', 'failed_thinking_spin', 'failed_no_proof', 'failed_stall'}
infra_failures = {'rate_limited', 'dead_session', 'crash_recovered', 'launch_error', 'failed_connection'}
data_issues = {'answer_leak_in_input', 'answer_leak'}

model_count = sum(r['count'] for r in results if r['status'] in model_failures)
infra_count = sum(r['count'] for r in results if r['status'] in infra_failures)
data_count = sum(r['count'] for r in results if r['status'] in data_issues)

print('=== 未解决原因分类 ===')
print()
print(f'一、AI能力失败（模型做不出，是Profile数据）: {model_count} ({model_count/unsolved*100:.1f}%)')
for r in sorted([x for x in results if x['status'] in model_failures], key=lambda x: -x['count']):
    print(f'  {r["status"]:<25} {r["count"]:>6} ({r["count"]/unsolved*100:.1f}%)')
print()
print(f'二、基础设施失败（可重试，不代表AI能力）: {infra_count} ({infra_count/unsolved*100:.1f}%)')
for r in sorted([x for x in results if x['status'] in infra_failures], key=lambda x: -x['count']):
    print(f'  {r["status"]:<25} {r["count"]:>6} ({r["count"]/unsolved*100:.1f}%)')
print()
print(f'三、数据问题（题目本身有问题）: {data_count} ({data_count/unsolved*100:.1f}%)')
for r in sorted([x for x in results if x['status'] in data_issues], key=lambda x: -x['count']):
    print(f'  {r["status"]:<25} {r["count"]:>6} ({r["count"]/unsolved*100:.1f}%)')
