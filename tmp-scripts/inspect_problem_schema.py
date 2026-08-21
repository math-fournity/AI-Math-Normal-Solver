#!/usr/bin/env python3
"""inspect_problem_schema.py — 临时调查脚本

调查目的：查看problem_extraction_progress集合的字段结构，
为设计清洗后的表结构提供依据。

使用时间：2026-08-21
调查结果记录在：dev-docs/404-v0-2026-08-21-题目清洗系统方案.md §2.6

用法:
  set -a; source /Users/user/glm5.2-math-worktree/.env; set +a
  PYTHONPATH=xishujuzhen/solver_harness/pipe \
    /Users/user/glm5.2-math-worktree/.venv/bin/python3 \
    tmp-scripts/inspect_problem_schema.py
"""
import os
from arango import ArangoClient

c = ArangoClient(hosts=os.environ['ARANGO_HOST'])
db = c.db(os.environ['ARANGO_DB'], username=os.environ['ARANGO_USER'], password=os.environ['ARANGO_PASS'])

# 1. 看一个answer_leak题的完整字段
aql = '''
FOR p IN problem_extraction_progress
  FILTER p.difficulty_tier == 1
  LET has_run = (
    FOR r IN devin_problem_runs
      FILTER r.problem_id == p._key
      FILTER r.batch_id == "pipe-runner"
      FILTER r.status != "running"
      SORT r.ended_at DESC
      LIMIT 1
      RETURN r.status
  )
  FILTER LENGTH(has_run) > 0
  FILTER has_run[0] == "answer_leak_in_input"
  LIMIT 1
  RETURN p
'''
doc = db.aql.execute(aql, ttl=300).next()
print("=== answer_leak_in_input题的完整字段 ===")
for k in sorted(doc.keys()):
    v = doc[k]
    if isinstance(v, str) and len(v) > 100:
        print(f"  {k}: ({type(v).__name__}, len={len(v)}) {v[:80]}...")
    else:
        print(f"  {k}: ({type(v).__name__}) {v}")

# 2. 看一个普通题的完整字段（对比）
print()
aql2 = '''
FOR p IN problem_extraction_progress
  FILTER p.difficulty_tier == 1
  LET has_run = (
    FOR r IN devin_problem_runs
      FILTER r.problem_id == p._key
      FILTER r.batch_id == "pipe-runner"
      FILTER r.status != "running"
      SORT r.ended_at DESC
      LIMIT 1
      RETURN r.status
  )
  FILTER LENGTH(has_run) > 0
  FILTER has_run[0] == "candidate_solved"
  LIMIT 1
  RETURN p
'''
doc2 = db.aql.execute(aql2, ttl=300).next()
print("=== 普通已解决题的完整字段 ===")
for k in sorted(doc2.keys()):
    v = doc2[k]
    if isinstance(v, str) and len(v) > 100:
        print(f"  {k}: ({type(v).__name__}, len={len(v)}) {v[:80]}...")
    else:
        print(f"  {k}: ({type(v).__name__}) {v}")

# 3. 看一个answer_leak题（AI运行中检测的）
print()
aql3 = '''
FOR p IN problem_extraction_progress
  FILTER p.difficulty_tier == 1
  LET has_run = (
    FOR r IN devin_problem_runs
      FILTER r.problem_id == p._key
      FILTER r.batch_id == "pipe-runner"
      FILTER r.status != "running"
      SORT r.ended_at DESC
      LIMIT 1
      RETURN r.status
  )
  FILTER LENGTH(has_run) > 0
  FILTER has_run[0] == "answer_leak"
  LIMIT 1
  RETURN p
'''
doc3 = db.aql.execute(aql3, ttl=300).next()
print("=== answer_leak题（AI运行中检测）的完整字段 ===")
for k in sorted(doc3.keys()):
    v = doc3[k]
    if isinstance(v, str) and len(v) > 100:
        print(f"  {k}: ({type(v).__name__}, len={len(v)}) {v[:80]}...")
    else:
        print(f"  {k}: ({type(v).__name__}) {v}")

# 4. 所有字段的并集
print()
print("=== 所有题的字段并集 ===")
all_keys = set(doc.keys()) | set(doc2.keys()) | set(doc3.keys())
for k in sorted(all_keys):
    in1 = "✓" if k in doc else " "
    in2 = "✓" if k in doc2 else " "
    in3 = "✓" if k in doc3 else " "
    print(f"  {k:<30} leak_input={in1} solved={in2} leak_runtime={in3}")
