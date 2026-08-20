#!/usr/bin/env python3
"""check_answer_leak.py — 答案泄漏检查

检查题目文本中是否因清洗疏忽包含了答案/solution，导致AI上下文中有答案。

用法:
  python check_answer_leak.py --problem-key <key>
  python check_answer_leak.py --batch --tier 1 [--limit 1000]
  python check_answer_leak.py --pending  # 检查Redis pending队列中的题
"""
import sys
import os
import argparse

sys.path.insert(0, os.path.dirname(__file__))

from arango import ArangoClient
from redis_queue import get_redis, ping

DB_HOST = "http://localhost:8529"
DB_NAME = "xishujuzhen_math_glm52"
DB_USER = "root"
DB_PASS = "moira123"
COLLECTION = "problem_extraction_progress"


def check_leak(problem_text: str, answer: str, solution: str) -> dict:
    """检查题目文本是否包含答案/solution片段"""
    leaks = []
    text_lower = problem_text.lower().strip()

    # 检查1: answer字段的值是否出现在problem_text中
    # 只对长答案（>10字符）做检测——短答案如True/False/2875等容易在题目文本中自然出现，导致误判
    if answer and len(str(answer).strip()) > 10:
        ans = str(answer).strip()
        # 精确匹配（去除空格和LaTeX标记后）
        ans_clean = ans.replace(" ", "").replace("\\", "").replace("$", "").lower()
        text_clean = text_lower.replace(" ", "").replace("\\", "").replace("$", "")
        if ans_clean in text_clean:
            leaks.append({"type": "answer_in_text", "value": ans[:50]})

    # 检查2: solution_text的前200字符是否出现在problem_text中
    if solution and len(solution.strip()) > 20:
        sol_prefix = solution.strip()[:200].lower()
        if sol_prefix[:100] in text_lower:
            leaks.append({"type": "solution_in_text", "value": sol_prefix[:80]})

    # 检查3: 常见答案标记模式——排除题目格式说明中的占位符
    # AMO Bench等数据集的题目格式包含"### The final answer is: $\boxed{<your answer>}$"
    # 这是题目要求格式，不是答案泄漏。只有当marker后面有具体数值（非占位符）时才算泄漏
    answer_markers = ["the answer is", "final answer is", "答案是", "解为"]
    placeholders = ["<your answer>", "<answer>", "your answer", "填入", "此处"]
    for marker in answer_markers:
        if marker in text_lower:
            idx = text_lower.index(marker)
            after = text_lower[idx:idx+100]
            # 排除占位符模式
            is_placeholder = any(p in after for p in placeholders)
            if not is_placeholder:
                # 检查marker后面是否有具体数值（非占位符）
                if any(c.isdigit() or c in "\\frac{}" for c in after):
                    leaks.append({"type": "answer_marker_in_text", "value": after[:80]})
                    break

    return {"has_leak": len(leaks) > 0, "leaks": leaks}


def check_single(db, problem_key: str) -> dict:
    """检查单题"""
    doc = db.collection(COLLECTION).get(problem_key)
    if not doc:
        return {"problem_key": problem_key, "error": "not_found"}
    result = check_leak(
        doc.get("problem_text", ""),
        doc.get("answer", ""),
        doc.get("solution_text", ""),
    )
    result["problem_key"] = problem_key
    return result


def check_batch(db, tiers: list[int], limit: int) -> list[dict]:
    """批量检查某tier的题"""
    tier_filter = ", ".join(str(t) for t in tiers)
    aql = (
        f"FOR d IN {COLLECTION} "
        f"FILTER d.difficulty_tier IN [{tier_filter}] "
        f"FILTER d.extraction_status IN ['pending', 'queued'] "
        f"SORT d._key ASC "
        f"LIMIT {limit} "
        f"RETURN {{key: d._key, problem_text: d.problem_text, answer: d.answer, solution_text: d.solution_text}}"
    )
    cursor = db.aql.execute(aql, ttl=300)
    results = []
    leak_count = 0
    for row in cursor:
        result = check_leak(row["problem_text"], row.get("answer", ""), row.get("solution_text", ""))
        if result["has_leak"]:
            leak_count += 1
            results.append({
                "problem_key": row["key"],
                "has_leak": True,
                "leaks": result["leaks"],
            })
    return {"total_checked": len(results), "leak_count": leak_count, "leaks": results}


def check_pending(db) -> list[dict]:
    """检查Redis pending队列中的所有题"""
    r = get_redis()
    items = r.zrange("math:pending", 0, -1)
    results = []
    leak_count = 0
    for member in items:
        problem_key = member if isinstance(member, str) else member.decode()
        result = check_single(db, problem_key)
        if result.get("has_leak"):
            leak_count += 1
            results.append(result)
    return {"total_checked": len(items), "leak_count": leak_count, "leaks": results}


def main():
    parser = argparse.ArgumentParser(description="答案泄漏检查")
    parser.add_argument("--problem-key", type=str, help="检查单题")
    parser.add_argument("--batch", action="store_true", help="批量检查")
    parser.add_argument("--tier", type=str, default="1,2,3", help="tier筛选")
    parser.add_argument("--limit", type=int, default=1000, help="批量检查上限")
    parser.add_argument("--pending", action="store_true", help="检查Redis pending队列")
    args = parser.parse_args()

    client = ArangoClient(hosts=DB_HOST, request_timeout=300)
    db = client.db(DB_NAME, username=DB_USER, password=DB_PASS)

    if args.problem_key:
        result = check_single(db, args.problem_key)
        if result.get("has_leak"):
            print(f"❌ 泄漏: {args.problem_key}")
            for leak in result["leaks"]:
                print(f"  {leak['type']}: {leak['value']}")
        elif result.get("error"):
            print(f"❌ {result['error']}: {args.problem_key}")
        else:
            print(f"✅ 无泄漏: {args.problem_key}")
        return

    if args.batch:
        tiers = [int(t) for t in args.tier.split(",")]
        result = check_batch(db, tiers, args.limit)
        print(f"检查了 {result['total_checked']} 题, 泄漏 {result['leak_count']} 题")
        for leak in result["leaks"]:
            print(f"  ❌ {leak['problem_key']}: {leak['leaks'][0]['type']}")
        return

    if args.pending:
        if not ping():
            print("❌ Redis连接失败")
            return
        result = check_pending(db)
        print(f"检查了 {result['total_checked']} 题, 泄漏 {result['leak_count']} 题")
        for leak in result["leaks"]:
            print(f"  ❌ {leak['problem_key']}: {leak['leaks'][0]['type']}")
        return

    parser.print_help()


if __name__ == "__main__":
    main()
