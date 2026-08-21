#!/usr/bin/env python3
"""tier1_never_processed_priority.py — 临时调查脚本

调查目的：用LEFT JOIN方式查tier=1中从未处理过的题的总数和priority分布。
确认这2565题全部priority=1，入队时score统一。

使用时间：2026-08-21
调查结果记录在：dev-docs/403-v0-2026-08-21-平凡解题系统队列准备方案.md §1.2
关键发现：2565题全部priority=1。

用法:
  set -a; source /Users/user/glm5.2-math-worktree/.env; set +a
  PYTHONPATH=xishujuzhen/solver_harness/pipe \
    /Users/user/glm5.2-math-worktree/.venv/bin/python3 \
    tmp-scripts/tier1_never_processed_priority.py
"""
import os
from arango import ArangoClient

c = ArangoClient(hosts=os.environ['ARANGO_HOST'])
db = c.db(os.environ['ARANGO_DB'], username=os.environ['ARANGO_USER'], password=os.environ['ARANGO_PASS'])

# 总数
aql_total = '''
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
  COLLECT WITH COUNT INTO c RETURN c
'''
total = db.aql.execute(aql_total, ttl=300).next()
print(f'=== 从未处理过的题总数: {total} ===')

# priority分布
aql_priority = '''
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
  COLLECT priority = p.priority WITH COUNT INTO c
  SORT priority ASC
  RETURN {priority, count: c}
'''
print()
print('=== 从未处理过的题的priority分布 ===')
for r in db.aql.execute(aql_priority, ttl=300):
    print(f'  priority={r["priority"]}: {r["count"]}')
