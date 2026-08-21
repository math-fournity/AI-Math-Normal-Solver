#!/usr/bin/env python3
"""query_tier1_for_other_system.py — 查询tier=1中需要另外一套系统处理的题目

"需要另外一套系统处理"= 平凡解题系统不处理的题：
- failed_token_limit：需要更大token预算
- ai_gave_up：需要不同解题策略
- failed_stall/failed_thinking_spin/failed_no_proof：边缘case，Profile数据
- invalid_tool_use：有proof但检测到工具调用

不包括：
- candidate_solved（已解决）
- 从未处理过的题（在最终队列中）
- 检测误判题（已在最终队列中）
- 基础设施失败题（已在最终队列中）

查询方法：按每题最后run的status判断（不能用extraction_status，因为分布杂乱）。
关键JOIN：problem_extraction_progress._key = devin_problem_runs.problem_id
注意：devin_problem_runs.difficulty_tier通常为null，必须用_key JOIN problem_id再查tier。

用法:
  python query_tier1_for_other_system.py              # 查询并报告
  python query_tier1_for_other_system.py --export     导出problem_key列表到文件
"""
import sys
import os
import json

from arango import ArangoClient
from collections import Counter

DB_HOST = "http://localhost:8529"
DB_NAME = "xishujuzhen_math_glm52"
DB_USER = "root"
DB_PASS = "moira123"

# 需要另外一套系统处理的终态
OTHER_SYSTEM_STATUSES = {
    "failed_token_limit",      # 需要更大token预算
    "ai_gave_up",              # 需要不同解题策略
    "failed_stall",            # 卡住（边缘case）
    "failed_thinking_spin",    # thinking空转（边缘case）
    "failed_no_proof",         # 无proof输出（边缘case）
    "invalid_tool_use",        # 有proof但检测到工具调用
}


def main():
    export = "--export" in sys.argv

    client = ArangoClient(hosts=DB_HOST, request_timeout=300)
    db = client.db(DB_NAME, username=DB_USER, password=DB_PASS)

    # === 1. 查询tier=1中最后run为模型能力失败的题 ===
    aql = """
FOR p IN problem_extraction_progress
  FILTER p.difficulty_tier == 1
  LET last_run = (
    FOR r IN devin_problem_runs
      FILTER r.problem_id == p._key
      FILTER r.batch_id == "pipe-runner"
      FILTER r.status != "running"
      SORT r.ended_at DESC
      LIMIT 1
      RETURN {status: r.status, exp_id: r.exp_id, ended_at: r.ended_at, runtime: r.runtime_seconds}
  )
  FILTER LENGTH(last_run) > 0
  FILTER last_run[0].status IN [
    "failed_token_limit", "ai_gave_up",
    "failed_stall", "failed_thinking_spin", "failed_no_proof",
    "invalid_tool_use"
  ]
  RETURN {
    key: p._key,
    source_dataset: p.source_dataset,
    last_status: last_run[0].status,
    last_exp_id: last_run[0].exp_id,
    last_ended_at: last_run[0].ended_at,
    last_runtime: last_run[0].runtime,
    answer: p.answer,
    has_solution: p.has_solution
  }
"""
    cursor = db.aql.execute(aql, ttl=300, batch_size=1000)
    problems = list(cursor)

    print(f"=== tier=1 需要另外一套系统处理的题目 ===")
    print(f"总数: {len(problems)}")
    print()

    # === 2. 按last_status分类统计 ===
    status_counts = Counter(p["last_status"] for p in problems)
    print("按终态status分类:")
    for status, count in sorted(status_counts.items(), key=lambda x: -x[1]):
        print(f"  {status}: {count}")
    print()

    # === 3. 按source_dataset分类统计 ===
    dataset_counts = Counter(p["source_dataset"] for p in problems)
    print("按数据集分类:")
    for dataset, count in sorted(dataset_counts.items(), key=lambda x: -x[1]):
        print(f"  {dataset}: {count}")
    print()

    # === 4. 交叉表：status × dataset ===
    print("交叉表 (status × dataset):")
    all_statuses = sorted(status_counts.keys())
    all_datasets = sorted(dataset_counts.keys())
    # 表头
    header = f"  {'status':<25} " + " ".join(f"{d:>20}" for d in all_datasets) + f" {'合计':>8}"
    print(header)
    for status in all_statuses:
        row_counts = []
        for dataset in all_datasets:
            c = sum(1 for p in problems if p["last_status"] == status and p["source_dataset"] == dataset)
            row_counts.append(c)
        row_total = sum(row_counts)
        row = f"  {status:<25} " + " ".join(f"{c:>20}" for c in row_counts) + f" {row_total:>8}"
        print(row)
    # 列合计
    col_totals = []
    for dataset in all_datasets:
        col_totals.append(sum(1 for p in problems if p["source_dataset"] == dataset))
    print(f"  {'合计':<25} " + " ".join(f"{c:>20}" for c in col_totals) + f" {sum(col_totals):>8}")
    print()

    # === 5. 运行时间分布 ===
    runtimes = [p["last_runtime"] for p in problems if p["last_runtime"]]
    if runtimes:
        print(f"运行时间分布 (秒):")
        print(f"  最短: {min(runtimes):.0f}")
        print(f"  最长: {max(runtimes):.0f}")
        print(f"  平均: {sum(runtimes)/len(runtimes):.0f}")
        print(f"  中位数: {sorted(runtimes)[len(runtimes)//2]:.0f}")
        print()

    # === 6. 有答案/有解答的比例 ===
    has_answer = sum(1 for p in problems if p["answer"])
    has_solution = sum(1 for p in problems if p["has_solution"])
    print(f"答案/解答可用性:")
    print(f"  有答案: {has_answer} ({has_answer*100//len(problems)}%)")
    print(f"  有解答: {has_solution} ({has_solution*100//len(problems)}%)")
    print()

    # === 7. 导出 ===
    if export:
        export_path = os.path.join(os.path.dirname(__file__), "..", "..", "..", "tmp-scripts", "tier1_for_other_system.json")
        export_data = {
            "total": len(problems),
            "status_counts": dict(status_counts),
            "dataset_counts": dict(dataset_counts),
            "problems": problems,
        }
        with open(export_path, "w") as f:
            json.dump(export_data, f, ensure_ascii=False, indent=2)
        print(f"已导出到: {export_path}")

    # === 8. 查询方法说明 ===
    print()
    print("=== 查询方法说明 ===")
    print()
    print("1. 关键JOIN: problem_extraction_progress._key = devin_problem_runs.problem_id")
    print("2. 不能用extraction_status判断（分布杂乱），必须查devin_problem_runs中最后run的status")
    print("3. devin_problem_runs.difficulty_tier通常为null，必须先从problem_extraction_progress过滤tier=1")
    print("4. batch_id='pipe-runner'过滤pipe系统的run（排除其他批次）")
    print("5. status != 'running'排除正在运行的（非终态）")
    print("6. 按ended_at DESC排序取LIMIT 1得到最后run的status")
    print()
    print("AQL查询:")
    print(aql)


if __name__ == "__main__":
    main()
