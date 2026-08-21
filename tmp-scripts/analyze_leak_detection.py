#!/usr/bin/env python3
"""analyze_leak_detection.py — 分析answer_leak_in_input题是哪种检测触发的

分析runner.py的两种泄漏检测：
1. answer检测：answer去掉空格/反斜杠/美元符号后，在problem_text中
2. solution检测：solution_text前100字符在problem_text中

统计每种检测触发了多少题，为修复检测逻辑提供依据。
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "xishujuzhen", "solver_harness", "pipe"))

from arango import ArangoClient

client = ArangoClient(hosts="http://localhost:8529", request_timeout=300)
db = client.db("xishujuzhen_math_glm52", username="root", password="moira123")

# 取所有answer_leak_in_input题
aql = """
FOR r IN devin_problem_runs
  FILTER r.batch_id == "pipe-runner"
  FILTER r.status == "answer_leak_in_input"
  LET p = DOCUMENT(CONCAT("problem_extraction_progress/", r.problem_id))
  RETURN {
    key: p._key,
    source: p.source_dataset,
    answer: p.answer,
    problem_text: p.problem_text,
    solution_text: p.solution_text
  }
"""
cursor = db.aql.execute(aql, ttl=300)
rows = list(cursor)
print(f"answer_leak_in_input总数: {len(rows)}")
print()

# 模拟runner的检测逻辑
def detect_leak(answer, problem_text, solution_text):
    """返回 (leak_type, detail)"""
    # 1. answer检测
    if answer and len(str(answer).strip()) > 10:
        ans_clean = str(answer).strip().replace(" ", "").replace("\\", "").replace("$", "").lower()
        text_clean = problem_text.lower().replace(" ", "").replace("\\", "").replace("$", "")
        if ans_clean in text_clean:
            return ("answer", f"answer='{answer[:30]}' (clean_len={len(ans_clean)})")

    # 2. solution检测
    if solution_text and len(solution_text.strip()) > 20:
        sol_prefix = solution_text.strip()[:100].lower()
        if sol_prefix in problem_text.lower():
            return ("solution", f"solution前100字符在problem_text中")

    return (None, "未触发任何检测（可能是误标记或检测逻辑已变更）")

# 按数据集和检测类型统计
from collections import defaultdict
stats = defaultdict(lambda: defaultdict(int))
undetected = []

for row in rows:
    leak_type, detail = detect_leak(
        row.get("answer", ""),
        row.get("problem_text", ""),
        row.get("solution_text", "")
    )
    stats[row["source"]][leak_type or "undetected"] += 1
    if leak_type is None:
        undetected.append(row["key"])

print("=== 按数据集×检测类型统计 ===")
for dataset in sorted(stats.keys()):
    print(f"  {dataset}:")
    for leak_type in ["answer", "solution", "undetected"]:
        if stats[dataset][leak_type] > 0:
            print(f"    {leak_type}: {stats[dataset][leak_type]}")

print()
print(f"=== 未检测到的题: {len(undetected)} ===")
if undetected:
    for key in undetected[:10]:
        print(f"  {key}")
    if len(undetected) > 10:
        print(f"  ... 共{len(undetected)}题")
