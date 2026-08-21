#!/usr/bin/env python3
"""recheck_final_queue.py — 验证Redis队列中的题是否都属于正确类别

从math:pending取出所有key，逐个查ArangoDB验证：
1. 确认是tier=1
2. 查是否有pipe-runner run记录
3. 如果有run记录，查最后run的status
4. 判断是否符合入队标准

应入队的类别：
- 从未处理过的题（无run记录）
- 检测误判题（最后run是answer_leak/answer_leak_in_input）
- 基础设施失败题（最后run是rate_limited/dead_session/crash_recovered/failed_connection/launch_error）

不应入队的类别（如果出现就是错误）：
- candidate_solved（已解决）
- failed_token_limit/ai_gave_up/failed_stall/failed_no_proof/failed_thinking_spin（模型能力失败）
- 非tier=1的题

用法:
  python recheck_final_queue.py              # 验证，只报告不符合的
  python recheck_final_queue.py --verbose    # 验证，报告所有分类统计
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from arango import ArangoClient
import redis
from collections import Counter

DB_HOST = "http://localhost:8529"
DB_NAME = "xishujuzhen_math_glm52"
DB_USER = "root"
DB_PASS = "moira123"
COLLECTION = "problem_extraction_progress"

PENDING_KEY = "math:pending"

VALID_LAST_STATUS = {
    "answer_leak", "answer_leak_in_input",
    "rate_limited", "dead_session", "crash_recovered",
    "failed_connection", "launch_error",
}


def main():
    verbose = "--verbose" in sys.argv

    client = ArangoClient(hosts=DB_HOST, request_timeout=300)
    db = client.db(DB_NAME, username=DB_USER, password=DB_PASS)
    r = redis.Redis(host="localhost", port=6379, db=0, decode_responses=True)

    # 1. 从Redis取出所有key
    all_keys = r.zrange(PENDING_KEY, 0, -1)
    total = len(all_keys)
    print(f"math:pending中共 {total} 题")
    print()

    # 2. 批量查ArangoDB——用AQL一次性查所有key的tier和最后run status
    # 分批查，每批500个key
    batch_size = 500
    results = []  # [{key, tier, has_run, last_status}]

    for i in range(0, total, batch_size):
        batch = all_keys[i:i + batch_size]
        # 用AQL查每个key的tier和最后run status
        aql = """
FOR key IN @keys
  LET p = DOCUMENT(CONCAT("problem_extraction_progress/", key))
  FILTER p != null
  LET last_run = (
    FOR r IN devin_problem_runs
      FILTER r.problem_id == p._key
      FILTER r.batch_id == "pipe-runner"
      FILTER r.status != "running"
      SORT r.ended_at DESC
      LIMIT 1
      RETURN r.status
  )
  RETURN {
    key: p._key,
    tier: p.difficulty_tier,
    has_run: LENGTH(last_run) > 0,
    last_status: last_run[0] || null
  }
"""
        cursor = db.aql.execute(aql, bind_vars={"keys": batch}, ttl=300, batch_size=500)
        results.extend(list(cursor))

    print(f"ArangoDB查到 {len(results)} 条记录（Redis有 {total} 个key）")
    missing = set(all_keys) - set(r["key"] for r in results)
    if missing:
        print(f"⚠️  {len(missing)} 个key在ArangoDB中找不到！")
        for k in list(missing)[:10]:
            print(f"  {k}")
    print()

    # 3. 分类验证
    valid_never = []       # 从未处理（正确）
    valid_retry = []       # 检测误判/infra失败（正确）
    invalid_solved = []    # 已解决（错误！）
    invalid_model = []     # 模型能力失败（错误！）
    invalid_tier = []      # 非tier=1（错误！）
    invalid_other = []     # 其他不符合的

    for r in results:
        key = r["key"]
        tier = r["tier"]
        has_run = r["has_run"]
        last_status = r["last_status"]

        if tier != 1:
            invalid_tier.append((key, f"tier={tier}"))
        elif not has_run:
            valid_never.append(key)
        elif last_status in VALID_LAST_STATUS:
            valid_retry.append((key, last_status))
        elif last_status == "candidate_solved":
            invalid_solved.append(key)
        elif last_status in ("failed_token_limit", "ai_gave_up", "failed_stall",
                             "failed_no_proof", "failed_thinking_spin", "invalid_tool_use"):
            invalid_model.append((key, last_status))
        else:
            invalid_other.append((key, last_status))

    # 4. 报告
    valid_count = len(valid_never) + len(valid_retry)
    invalid_count = len(invalid_solved) + len(invalid_model) + len(invalid_tier) + len(invalid_other)

    print("=== 验证结果 ===")
    print(f"  ✅ 正确（从未处理）: {len(valid_never)}")
    print(f"  ✅ 正确（检测误判/infra失败）: {len(valid_retry)}")
    print(f"  ✅ 正确合计: {valid_count}")
    print()
    print(f"  ❌ 错误（已解决candidate_solved）: {len(invalid_solved)}")
    print(f"  ❌ 错误（模型能力失败）: {len(invalid_model)}")
    print(f"  ❌ 错误（非tier=1）: {len(invalid_tier)}")
    print(f"  ❌ 错误（其他）: {len(invalid_other)}")
    print(f"  ❌ 错误合计: {invalid_count}")
    print()

    if verbose and valid_retry:
        print("=== 正确（检测误判/infra失败）按status分布 ===")
        retry_status = Counter(s for _, s in valid_retry)
        for status, count in sorted(retry_status.items()):
            print(f"  {status}: {count}")
        print()

    if invalid_solved:
        print(f"=== ❌ 已解决但入队的题（前20个）===")
        for k in invalid_solved[:20]:
            print(f"  {k}")
        if len(invalid_solved) > 20:
            print(f"  ... 共{len(invalid_solved)}题")
        print()

    if invalid_model:
        print(f"=== ❌ 模型能力失败但入队的题（前20个）===")
        for k, s in invalid_model[:20]:
            print(f"  {k} (last_status={s})")
        if len(invalid_model) > 20:
            print(f"  ... 共{len(invalid_model)}题")
        print()

    if invalid_tier:
        print(f"=== ❌ 非tier=1但入队的题 ===")
        for k, info in invalid_tier:
            print(f"  {k} ({info})")
        print()

    if invalid_other:
        print(f"=== ❌ 其他不符合的题 ===")
        for k, s in invalid_other:
            print(f"  {k} (last_status={s})")
        print()

    # 5. 结论
    if invalid_count == 0:
        print(f"✅ 全部验证通过！{valid_count} 题都属于正确类别。")
    else:
        print(f"⚠️  有 {invalid_count} 题不属于正确类别，需要从队列中移除。")


if __name__ == "__main__":
    main()
