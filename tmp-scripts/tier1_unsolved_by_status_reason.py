#!/usr/bin/env python3
"""tier1_unsolved_by_status_reason.py — 临时调查脚本

调查目的：查tier=1未解决题，按status+end_reason统计数量。
这是第一次调查用的脚本，按所有run记录统计（不是按每题最后run）。
后来发现这个方法有问题——rate_limited等中间run被算进来了。
正确的统计方法见check_retry_effect.py（按每题最后run）。

使用时间：2026-08-21
调查结果记录在：dev-docs/402-v0-2026-08-21-tier1做题结果调查报告.md §0

用法:
  set -a; source /Users/user/glm5.2-math-worktree/.env; set +a
  PYTHONPATH=xishujuzhen/solver_harness/pipe \
    /Users/user/glm5.2-math-worktree/.venv/bin/python3 \
    tmp-scripts/tier1_unsolved_by_status_reason.py
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
    FILTER r.status != "candidate_solved"
    FILTER r.status != "running"
    COLLECT status = r.status, reason = r.end_reason WITH COUNT INTO c
    SORT c DESC
    RETURN {status, reason, count: c}
'''
results = list(db.aql.execute(aql, ttl=120))
total_unsolved = sum(r['count'] for r in results)
print(f'tier=1 未解决总数: {total_unsolved}')
print()
print(f'{"status":<30} {"end_reason":<50} {"count":>6}')
print('-' * 90)
for r in results:
    print(f'{r["status"]:<30} {(r["reason"] or "")[:50]:<50} {r["count"]:>6}')
