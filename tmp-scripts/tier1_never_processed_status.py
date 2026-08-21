#!/usr/bin/env python3
"""tier1_never_processed_status.py — 临时调查脚本

调查目的：查tier=1中从未处理过的题的extraction_status分布，
以及244基础设施失败题的extraction_status分布，
以及Redis pending队列中现有题的tier分布（抽样）。
用于判断feeder能否自动把这些题入队（feeder只查extraction_status=='pending'的题）。

使用时间：2026-08-21
调查结果记录在：dev-docs/403-v0-2026-08-21-平凡解题系统队列准备方案.md §1.2
关键发现：2565从未处理题的extraction_status分布杂乱（queued/pending/completed都有），
不能依赖extraction_status判断题是否该入队，必须用devin_problem_runs中是否有run记录判断。

用法:
  set -a; source /Users/user/glm5.2-math-worktree/.env; set +a
  PYTHONPATH=xishujuzhen/solver_harness/pipe \
    /Users/user/glm5.2-math-worktree/.venv/bin/python3 \
    tmp-scripts/tier1_never_processed_status.py
"""
import os
import redis
from arango import ArangoClient

c = ArangoClient(hosts=os.environ['ARANGO_HOST'])
db = c.db(os.environ['ARANGO_DB'], username=os.environ['ARANGO_USER'], password=os.environ['ARANGO_PASS'])

# 1. 从未处理过的题的extraction_status分布
aql = '''
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
  COLLECT status = p.extraction_status WITH COUNT INTO c
  SORT c DESC
  RETURN {status, count: c}
'''
print('=== 从未处理过的题的extraction_status分布 ===')
for r in db.aql.execute(aql, ttl=300):
    print(f'  {r["status"]}: {r["count"]}')

# 2. 244 infra失败题的extraction_status分布
aql2 = '''
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
    FOR p2 IN problem_extraction_progress
      FILTER p2._key == problem_id
      COLLECT status = p2.extraction_status WITH COUNT INTO c
      SORT c DESC
      RETURN {status, count: c}
'''
print()
print('=== 244 infra失败题的extraction_status分布 ===')
for r in db.aql.execute(aql2, ttl=300):
    print(f'  {r["status"]}: {r["count"]}')

# 3. Redis pending中现有题的tier分布（抽样前50个）
print()
print('=== Redis pending队列抽样tier分布 ===')
r = redis.Redis(host='localhost', port=6379, db=0, decode_responses=True)
keys = r.zrange('math:pending', 0, 50)
tiers = {}
for k in keys:
    doc = db.collection('problem_extraction_progress').get(k)
    if doc:
        t = doc.get('difficulty_tier', 'unknown')
        tiers[t] = tiers.get(t, 0) + 1
print(f'  抽样{len(keys)}个key的tier分布: {tiers}')
