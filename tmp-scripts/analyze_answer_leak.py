#!/usr/bin/env python3
"""analyze_answer_leak.py — 临时调查脚本

调查目的：搞清楚两种answer_leak的区别和来源
- answer_leak_in_input: runner在启动前检测到题目文本含答案，题没发出去（在runner.py中检测）
- answer_leak: collector在devin cli运行中检测到AI输出"ANSWER LEAK DETECTED"标记（AI自己检测到答案泄漏）

调查内容：
1. 两种answer_leak的数量
2. answer_leak_in_input的题是否在problem_extraction_progress中有answer/solution_text字段
3. 这些题的extraction_status分布
4. 这些题是否在题库中还有answer/solution_text字段（可以提前清理）

使用时间：2026-08-21
调查结果记录在：对话记录

用法:
  set -a; source /Users/user/glm5.2-math-worktree/.env; set +a
  PYTHONPATH=xishujuzhen/solver_harness/pipe \
    /Users/user/glm5.2-math-worktree/.venv/bin/python3 \
    tmp-scripts/analyze_answer_leak.py
"""
import os
from arango import ArangoClient

c = ArangoClient(hosts=os.environ['ARANGO_HOST'])
db = c.db(os.environ['ARANGO_DB'], username=os.environ['ARANGO_USER'], password=os.environ['ARANGO_PASS'])

# 1. 两种answer_leak的数量（按每题最后run）
aql = '''
FOR p IN problem_extraction_progress
  FILTER p.difficulty_tier == 1
  FOR r IN devin_problem_runs
    FILTER r.problem_id == p._key
    FILTER r.batch_id == "pipe-runner"
    FILTER r.status != "running"
    SORT r.problem_id, r.ended_at DESC
    COLLECT problem_id = r.problem_id INTO runs = r.status
    LET last_status = runs[0]
    FILTER last_status IN ["answer_leak_in_input", "answer_leak"]
    COLLECT status = last_status WITH COUNT INTO c
    RETURN {status, count: c}
'''
print("=== 两种answer_leak的数量（按每题最后run） ===")
for r in db.aql.execute(aql, ttl=300):
    print(f"  {r['status']}: {r['count']}")
print()

# 2. answer_leak_in_input的题的详情：是否有answer/solution_text字段
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
  FILTER has_run[0] == "answer_leak_in_input"
  RETURN {
    key: p._key,
    has_answer: p.answer != null AND p.answer != "",
    has_solution: p.solution_text != null AND p.solution_text != "",
    answer_len: LENGTH(p.answer || ""),
    solution_len: LENGTH(p.solution_text || ""),
    extraction_status: p.extraction_status,
    source_dataset: p.source_dataset
  }
'''
print("=== answer_leak_in_input题的详情 ===")
leak_input = list(db.aql.execute(aql2, ttl=300))
print(f"总数: {len(leak_input)}")
has_answer = sum(1 for r in leak_input if r['has_answer'])
has_solution = sum(1 for r in leak_input if r['has_solution'])
has_both = sum(1 for r in leak_input if r['has_answer'] and r['has_solution'])
print(f"  有answer字段: {has_answer}")
print(f"  有solution_text字段: {has_solution}")
print(f"  两者都有: {has_both}")
print()

# 按source_dataset分布
from collections import Counter
datasets = Counter(r['source_dataset'] for r in leak_input)
print("按source_dataset分布:")
for ds, cnt in datasets.most_common():
    print(f"  {ds}: {cnt}")
print()

# extraction_status分布
statuses = Counter(r['extraction_status'] for r in leak_input)
print("按extraction_status分布:")
for s, cnt in statuses.most_common():
    print(f"  {s}: {cnt}")
print()

# 3. answer_leak的题的详情
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
  RETURN {
    key: p._key,
    has_answer: p.answer != null AND p.answer != "",
    has_solution: p.solution_text != null AND p.solution_text != "",
    answer_len: LENGTH(p.answer || ""),
    solution_len: LENGTH(p.solution_text || ""),
    extraction_status: p.extraction_status,
    source_dataset: p.source_dataset
  }
'''
print("=== answer_leak题的详情（AI运行中检测到） ===")
leak_runtime = list(db.aql.execute(aql3, ttl=300))
print(f"总数: {len(leak_runtime)}")
has_answer = sum(1 for r in leak_runtime if r['has_answer'])
has_solution = sum(1 for r in leak_runtime if r['has_solution'])
has_both = sum(1 for r in leak_runtime if r['has_answer'] and r['has_solution'])
print(f"  有answer字段: {has_answer}")
print(f"  有solution_text字段: {has_solution}")
print(f"  两者都有: {has_both}")
print()
datasets2 = Counter(r['source_dataset'] for r in leak_runtime)
print("按source_dataset分布:")
for ds, cnt in datasets2.most_common():
    print(f"  {ds}: {cnt}")
print()

# 4. 关键问题：这些题是否在题库中还有answer/solution_text字段？
# 如果有，说明可以在入队前提前清理（从problem_text中删除答案部分）
print("=== 关键问题：题库中这些题的problem_text是否还包含答案？ ===")
# 抽样检查5个answer_leak_in_input的题
print("\nanswer_leak_in_input抽样5个（检查problem_text是否含answer值）：")
from check_answer_leak import check_leak
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'xishujuzhen', 'solver_harness', 'pipe'))
sample_keys = [r['key'] for r in leak_input[:5]]
for key in sample_keys:
    doc = db.collection('problem_extraction_progress').get(key)
    if doc:
        result = check_leak(
            doc.get('problem_text', ''),
            doc.get('answer', ''),
            doc.get('solution_text', ''),
        )
        print(f"  {key}: has_leak={result['has_leak']} leaks={result['leaks'][:2]}")
