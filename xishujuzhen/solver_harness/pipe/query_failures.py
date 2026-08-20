#!/usr/bin/env python3
"""query_failures.py — 失败分类统计查询

查询所有失败attempt的verdict分类，统计数量和比例。

用法:
  python query_failures.py --summary
  python query_failures.py --by-verdict
  python query_failures.py --by-dataset
  python query_failures.py --verdict failed_connection [--limit 20]
  python query_failures.py --export --output failures.json
"""
import sys
import os
import json
import argparse

sys.path.insert(0, os.path.dirname(__file__))

from arango import ArangoClient

DB_HOST = "http://localhost:8529"
DB_NAME = "xishujuzhen_math_glm52"
DB_USER = "root"
DB_PASS = "moira123"
ATTEMPT_COLLECTION = "devin_problem_runs"
PROBLEM_COLLECTION = "problem_extraction_progress"

# 所有合法的verdict值
VALID_VERDICTS = [
    "candidate_solved", "answer_leak", "answer_leak_in_input",
    "rate_limited", "failed_token_limit", "failed_connection",
    "failed_timeout", "failed_stall", "dead_session",
    "failed_no_proof", "launch_error",
]


def query_summary(db) -> dict:
    """汇总统计"""
    aql = f"FOR a IN {ATTEMPT_COLLECTION} COLLECT status = a.status WITH COUNT INTO c RETURN {{status, count: c}}"
    cursor = db.aql.execute(aql, ttl=300)
    by_status = {row["status"]: row["count"] for row in cursor}

    aql = f"FOR a IN {ATTEMPT_COLLECTION} FILTER a.verdict != null COLLECT verdict = a.verdict WITH COUNT INTO c SORT c DESC RETURN {{verdict, count: c}}"
    cursor = db.aql.execute(aql, ttl=300)
    by_verdict = {row["verdict"]: row["count"] for row in cursor}

    total = sum(by_status.values())
    return {"total": total, "by_status": by_status, "by_verdict": by_verdict}


def query_by_verdict(db) -> list[dict]:
    """按verdict分类统计——处理verdict可能是dict或string的情况"""
    aql = f"FOR a IN {ATTEMPT_COLLECTION} FILTER a.verdict != null RETURN a.verdict"
    cursor = db.aql.execute(aql, ttl=300)
    from collections import Counter
    verdict_counts = Counter()
    for v in cursor:
        if isinstance(v, dict):
            # 旧系统格式：取auto_status或runner_status
            status = v.get("auto_status") or v.get("runner_status") or "unknown"
        elif isinstance(v, str):
            status = v
        else:
            status = "unknown"
        verdict_counts[status] += 1
    results = [{"verdict": k, "count": v} for k, v in verdict_counts.most_common()]
    total = sum(r["count"] for r in results)
    for r in results:
        r["percentage"] = f"{r['count']/total*100:.1f}%" if total > 0 else "0%"
    return results


def query_by_dataset(db) -> list[dict]:
    """按数据集分类统计（需要join到problem_extraction_progress）"""
    aql = (
        f"FOR a IN {ATTEMPT_COLLECTION} "
        f"FILTER a.verdict != null "
        f"FOR p IN {PROBLEM_COLLECTION} FILTER p._key == a.problem_id "
        f"COLLECT dataset = p.source_dataset, verdict = a.verdict WITH COUNT INTO c "
        f"SORT dataset, c DESC "
        f"RETURN {{dataset, verdict, count: c}}"
    )
    cursor = db.aql.execute(aql, ttl=300)
    return list(cursor)


def query_verdict_detail(db, verdict: str, limit: int) -> list[dict]:
    """查看某类失败的详情"""
    aql = (
        f"FOR a IN {ATTEMPT_COLLECTION} "
        f"FILTER a.verdict == '{verdict}' "
        f"SORT a.ended_at DESC "
        f"LIMIT {limit} "
        f"RETURN {{_key: a._key, problem_id: a.problem_id, exp_id: a.exp_id, "
        f"ended_at: a.ended_at, runtime_seconds: a.runtime_seconds, "
        f"end_reason: a.end_reason}}"
    )
    cursor = db.aql.execute(aql, ttl=300)
    return list(cursor)


def check_verdict_completeness(db) -> dict:
    """检查verdict完整性——是否有空或unknown的verdict"""
    aql = f"FOR a IN {ATTEMPT_COLLECTION} FILTER a.status IN ['failed_timeout','failed_stall','dead_session','failed_no_proof','failed_connection','failed_token_limit','rate_limited','launch_error','answer_leak','answer_leak_in_input'] FILTER a.verdict == null OR a.verdict == '' OR a.verdict == 'unknown' COLLECT WITH COUNT INTO c RETURN c"
    cursor = db.aql.execute(aql, ttl=300)
    count = cursor.next()
    return {"incomplete_verdict_count": count, "pass": count == 0}


def main():
    parser = argparse.ArgumentParser(description="失败分类统计查询")
    parser.add_argument("--summary", action="store_true")
    parser.add_argument("--by-verdict", action="store_true")
    parser.add_argument("--by-dataset", action="store_true")
    parser.add_argument("--verdict", type=str, help="查看某类失败详情")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--export", action="store_true")
    parser.add_argument("--output", type=str, default="failures.json")
    parser.add_argument("--check-completeness", action="store_true", help="检查verdict完整性")
    args = parser.parse_args()

    client = ArangoClient(hosts=DB_HOST, request_timeout=300)
    db = client.db(DB_NAME, username=DB_USER, password=DB_PASS)

    if args.check_completeness:
        result = check_verdict_completeness(db)
        print(f"Verdict完整性检查:")
        print(f"  不完整verdict数: {result['incomplete_verdict_count']}")
        print(f"  VERDICT: {'PASS' if result['pass'] else 'FAIL'}")
        return

    if args.summary:
        s = query_summary(db)
        print(f"=== 运行汇总 ===")
        print(f"总attempt数: {s['total']}")
        print(f"\n按status:")
        for status, count in sorted(s["by_status"].items(), key=lambda x: -x[1]):
            print(f"  {status:25s} {count:>8}")
        print(f"\n按verdict:")
        for verdict, count in sorted(s["by_verdict"].items(), key=lambda x: -x[1]):
            print(f"  {verdict:25s} {count:>8}")
        return

    if args.by_verdict:
        results = query_by_verdict(db)
        print(f"=== 按verdict分类 ===")
        for r in results:
            print(f"  {r['verdict']:25s} {r['count']:>8}  ({r['percentage']})")
        return

    if args.by_dataset:
        results = query_by_dataset(db)
        print(f"=== 按数据集×verdict分类 ===")
        current_dataset = ""
        for r in results:
            if r["dataset"] != current_dataset:
                current_dataset = r["dataset"]
                print(f"\n  {current_dataset}:")
            print(f"    {r['verdict']:25s} {r['count']:>6}")
        return

    if args.verdict:
        results = query_verdict_detail(db, args.verdict, args.limit)
        print(f"=== {args.verdict} 详情 (前{args.limit}条) ===")
        for r in results:
            print(f"  {r['_key']:45s} {r.get('problem_id',''):30s} {r.get('runtime_seconds',0):>6}s  {r.get('end_reason','')}")
        return

    if args.export:
        s = query_summary(db)
        bv = query_by_verdict(db)
        bd = query_by_dataset(db)
        output = {"summary": s, "by_verdict": bv, "by_dataset": bd}
        with open(args.output, "w") as f:
            json.dump(output, f, indent=2, ensure_ascii=False)
        print(f"导出到 {args.output}")
        return

    parser.print_help()


if __name__ == "__main__":
    main()
