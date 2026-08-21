#!/usr/bin/env python3
"""analyze_leak_patterns.py — 临时调查脚本

调查目的：分析451个answer_leak_in_input题的泄漏模式，
确认check_answer_leak.py检测到的泄漏类型分布，
为清理problem_text做准备。

使用时间：2026-08-21
调查结果记录在：对话记录

用法:
  set -a; source /Users/user/glm5.2-math-worktree/.env; set +a
  PYTHONPATH=xishujuzhen/solver_harness/pipe \
    /Users/user/glm5.2-math-worktree/.venv/bin/python3 \
    tmp-scripts/analyze_leak_patterns.py
"""
import os
import sys
from collections import Counter
from arango import ArangoClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'xishujuzhen', 'solver_harness', 'pipe'))
from check_answer_leak import check_leak

c = ArangoClient(hosts=os.environ['ARANGO_HOST'])
db = c.db(os.environ['ARANGO_DB'], username=os.environ['ARANGO_USER'], password=os.environ['ARANGO_PASS'])

# 取451个answer_leak_in_input题
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
  RETURN {
    key: p._key,
    problem_text: p.problem_text,
    answer: p.answer,
    solution_text: p.solution_text,
    source_dataset: p.source_dataset
  }
'''
leak_problems = list(db.aql.execute(aql, ttl=300))
print(f"总数: {len(leak_problems)}")
print()

# 分析泄漏类型分布
leak_types = Counter()
for p in leak_problems:
    result = check_leak(p['problem_text'], p.get('answer', ''), p.get('solution_text', ''))
    for leak in result['leaks']:
        leak_types[leak['type']] += 1

print("=== 泄漏类型分布 ===")
for t, cnt in leak_types.most_common():
    print(f"  {t}: {cnt}")
print()

# 抽样看answer_in_text的泄漏——answer值在problem_text的什么位置
print("=== answer_in_text抽样（answer值在problem_text中的位置） ===")
count = 0
for p in leak_problems:
    result = check_leak(p['problem_text'], p.get('answer', ''), p.get('solution_text', ''))
    has_answer_in_text = any(l['type'] == 'answer_in_text' for l in result['leaks'])
    if has_answer_in_text:
        answer = p.get('answer', '').strip()
        text = p['problem_text']
        # 找answer在text中的位置
        ans_clean = answer.replace(" ", "").replace("\\", "").replace("$", "").lower()
        text_clean = text.lower().replace(" ", "").replace("\\", "").replace("$", "")
        idx = text_clean.find(ans_clean)
        if idx >= 0:
            # 在原文中找大致位置
            print(f"\n  {p['key']} (dataset={p['source_dataset']}):")
            print(f"    answer = {answer[:80]}")
            print(f"    answer在clean text中的位置: {idx}/{len(text_clean)} ({idx/len(text_clean)*100:.0f}%)")
            # 看answer前面是什么内容
            context_start = max(0, idx - 100)
            context_end = min(len(text_clean), idx + len(ans_clean) + 100)
            print(f"    前后context: ...{text_clean[context_start:context_end]}...")
            count += 1
            if count >= 5:
                break

print()
print("=== solution_in_text抽样 ===")
count = 0
for p in leak_problems:
    result = check_leak(p['problem_text'], p.get('answer', ''), p.get('solution_text', ''))
    has_sol_in_text = any(l['type'] == 'solution_in_text' for l in result['leaks'])
    if has_sol_in_text:
        solution = p.get('solution_text', '').strip()
        text = p['problem_text']
        sol_prefix = solution[:100].lower()
        if sol_prefix[:50] in text.lower():
            idx = text.lower().find(sol_prefix[:50])
            print(f"\n  {p['key']} (dataset={p['source_dataset']}):")
            print(f"    solution前100字符 = {solution[:100]}")
            print(f"    solution在text中的位置: {idx}/{len(text)} ({idx/len(text)*100:.0f}%)")
            context_start = max(0, idx - 50)
            context_end = min(len(text), idx + 200)
            print(f"    前后context: ...{text[context_start:context_end]}...")
            count += 1
            if count >= 3:
                break

print()
print("=== answer_marker_in_text抽样 ===")
count = 0
for p in leak_problems:
    result = check_leak(p['problem_text'], p.get('answer', ''), p.get('solution_text', ''))
    has_marker = any(l['type'] == 'answer_marker_in_text' for l in result['leaks'])
    if has_marker:
        print(f"\n  {p['key']} (dataset={p['source_dataset']}):")
        for l in result['leaks']:
            if l['type'] == 'answer_marker_in_text':
                print(f"    marker内容 = {l['value']}")
        count += 1
        if count >= 3:
            break
