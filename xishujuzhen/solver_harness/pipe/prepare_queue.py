#!/usr/bin/env python3
"""prepare_queue.py — 队列准备：从难题开始分层入队

策略：
1. 所有tier 1题都入队（包括无answer的题——我们就是让GLM-5.2做题）
2. 在DB中标记has_answer/has_solution字段，方便后续查询
3. 按难度字段排序入队——最难的先做

入队顺序（从最难开始）：
  Batch 1: deepmath score 10        (3题)
  Batch 2: deepmath score 9.5      (51题)
  Batch 3: deepmath score 9      (1356题)
  Batch 4: oda_math pass_rate=0.2 (2183题)  — 通过率最低
  Batch 5: deepmath score 8.5    (3989题)
  Batch 6: oda_math pass_rate=0.4  (658题)
  Batch 7: polymath              (11090题)  — 无内部排序
  Batch 8: deepmath score 8     (11686题)
  Batch 9: oda_math pass_rate 0.6-1.0 (4315题)
  Batch 10: 其他tier 1数据集      (~3000题)

用法:
  python prepare_queue.py --dry-run          # 只看各batch题量，不入队
  python prepare_queue.py                    # 执行入队
  python prepare_queue.py --batch 3          # 只入第3批
  python prepare_queue.py --tag-has-answer   # 给DB中所有题标记has_answer字段
"""
import sys
import os
import time
import argparse
import subprocess

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
sys.path.insert(0, os.path.dirname(__file__))

from redis_queue import get_redis, enqueue_pending, pending_count, update_stats, ping, clear_all
from arango import ArangoClient
from shared_logger import get_logger

logger = get_logger("prepare_queue")

DB_HOST = "http://localhost:8529"
DB_NAME = "xishujuzhen_math_glm52"
DB_USER = "root"
DB_PASS = "moira123"
COLLECTION = "problem_extraction_progress"


# 分批定义——从最难开始
# priority越小越先被取出（Redis zset按score升序）
# 难度越高的batch priority越小
BATCHES = [
    {
        "name": "deepmath_score_10",
        "desc": "deepmath score=10 极难中的极难",
        "aql_filter": "d.source_dataset == 'deepmath_103k' AND d.difficulty_score == 10",
        "sort": "d._key",
        "priority": 10,
        "estimated": 3,
    },
    {
        "name": "deepmath_score_9_5",
        "desc": "deepmath score=9.5",
        "aql_filter": "d.source_dataset == 'deepmath_103k' AND d.difficulty_score == 9.5",
        "sort": "d._key",
        "priority": 20,
        "estimated": 51,
    },
    {
        "name": "deepmath_score_9",
        "desc": "deepmath score=9",
        "aql_filter": "d.source_dataset == 'deepmath_103k' AND d.difficulty_score == 9",
        "sort": "d._key",
        "priority": 30,
        "estimated": 1356,
    },
    {
        "name": "oda_pass_rate_0_2",
        "desc": "oda_math pass_rate=0.2 通过率最低",
        "aql_filter": "d.source_dataset == 'oda_math_460k' AND d.pass_rate == 0.2",
        "sort": "d._key",
        "priority": 40,
        "estimated": 2183,
    },
    {
        "name": "deepmath_score_8_5",
        "desc": "deepmath score=8.5",
        "aql_filter": "d.source_dataset == 'deepmath_103k' AND d.difficulty_score == 8.5",
        "sort": "d._key",
        "priority": 50,
        "estimated": 3989,
    },
    {
        "name": "oda_pass_rate_0_4",
        "desc": "oda_math pass_rate=0.4",
        "aql_filter": "d.source_dataset == 'oda_math_460k' AND d.pass_rate == 0.4",
        "sort": "d._key",
        "priority": 60,
        "estimated": 658,
    },
    {
        "name": "polymath",
        "desc": "polymath 高难但无内部排序",
        "aql_filter": "d.source_dataset == 'polymath'",
        "sort": "d._key",
        "priority": 70,
        "estimated": 11090,
    },
    {
        "name": "deepmath_score_8",
        "desc": "deepmath score=8",
        "aql_filter": "d.source_dataset == 'deepmath_103k' AND d.difficulty_score == 8",
        "sort": "d._key",
        "priority": 80,
        "estimated": 11686,
    },
    {
        "name": "oda_pass_rate_0_6_to_1",
        "desc": "oda_math pass_rate 0.6-1.0",
        "aql_filter": "d.source_dataset == 'oda_math_460k' AND d.pass_rate >= 0.6",
        "sort": "d.pass_rate ASC, d._key",
        "priority": 90,
        "estimated": 4315,
    },
    {
        "name": "other_tier1",
        "desc": "其他tier 1数据集（fineproofs/putnam/omni/matholympiad/compfiles等）",
        "aql_filter": "d.source_dataset NOT IN ['deepmath_103k', 'oda_math_460k', 'polymath']",
        "sort": "d.source_dataset, d._key",
        "priority": 100,
        "estimated": 3000,
    },
]


def tag_has_answer(db, dry_run: bool = False):
    """给DB中所有tier 1题标记has_answer和has_solution字段"""
    logger.info("标记has_answer/has_solution字段...")

    aql = (
        f"FOR d IN {COLLECTION} "
        f"FILTER d.difficulty_tier == 1 "
        f"FILTER d.has_answer == null "
        f"RETURN {{key: d._key, answer: d.answer, solution: d.solution_text}}"
    )
    cursor = db.aql.execute(aql, ttl=300, batch_size=1000)

    tagged = 0
    has_answer_count = 0
    has_solution_count = 0
    for row in cursor:
        key = row["key"]
        has_answer = row["answer"] is not None and row["answer"] != "" and row["answer"] != "None"
        has_solution = row["solution"] is not None and row["solution"] != ""

        if has_answer:
            has_answer_count += 1
        if has_solution:
            has_solution_count += 1

        if not dry_run:
            db.collection(COLLECTION).update({
                "_key": key,
                "has_answer": has_answer,
                "has_solution": has_solution,
            })
        tagged += 1

        if tagged % 5000 == 0:
            logger.info(f"标记进度: {tagged}, has_answer={has_answer_count}, has_solution={has_solution_count}")

    logger.info(f"标记完成: total={tagged} has_answer={has_answer_count} has_solution={has_solution_count}")
    return {"total": tagged, "has_answer": has_answer_count, "has_solution": has_solution_count}


def enqueue_batch(db, r, batch: dict, dry_run: bool = False) -> int:
    """入队一个batch"""
    aql = (
        f"FOR d IN {COLLECTION} "
        f"FILTER d.extraction_status IN ['pending', 'queued'] "
        f"FILTER d.difficulty_tier == 1 "
        f"FILTER {batch['aql_filter']} "
        f"SORT {batch['sort']} ASC "
        f"RETURN {{key: d._key}}"
    )

    logger.info(f"入队batch={batch['name']}: {batch['desc']}")
    logger.debug(f"AQL: {aql}")

    cursor = db.aql.execute(aql, ttl=300, batch_size=1000)
    count = 0
    batch_priority = batch.get("priority", 100)
    for row in cursor:
        key = row["key"]

        if not dry_run:
            enqueue_pending(r, key, batch_priority)
            db.collection(COLLECTION).update({
                "_key": key,
                "extraction_status": "queued",
            })
        count += 1

        if count % 1000 == 0:
            logger.info(f"  batch={batch['name']} 已入队 {count}题")

    logger.info(f"batch={batch['name']} 完成: 入队{count}题 (预估{batch['estimated']})")
    return count


def main():
    parser = argparse.ArgumentParser(description="队列准备：从难题开始分层入队")
    parser.add_argument("--dry-run", action="store_true", help="只看各batch题量，不入队")
    parser.add_argument("--batch", type=int, help="只入第N批（1-10）")
    parser.add_argument("--tag-has-answer", action="store_true", help="给DB中所有题标记has_answer字段")
    parser.add_argument("--clear", action="store_true", help="入队前清空Redis队列")
    args = parser.parse_args()

    logger.info(f"=== 队列准备开始 (dry_run={args.dry_run}) ===")

    if not ping():
        logger.error("Redis连接失败")
        sys.exit(1)
    logger.info("Redis连接成功")

    client = ArangoClient(hosts=DB_HOST, request_timeout=300)
    db = client.db(DB_NAME, username=DB_USER, password=DB_PASS)
    logger.info("ArangoDB连接成功")

    r = get_redis()

    # 标记has_answer字段
    if args.tag_has_answer:
        tag_has_answer(db, args.dry_run)
        if not args.batch:
            logger.info("只标记has_answer，不入队")
            return

    # 清空队列
    if args.clear and not args.dry_run:
        clear_all(r)
        logger.warning("已清空Redis队列")

    # 入队
    batches = BATCHES
    if args.batch:
        if args.batch < 1 or args.batch > len(BATCHES):
            logger.error(f"batch编号无效: {args.batch}, 应为1-{len(BATCHES)}")
            sys.exit(1)
        batches = [BATCHES[args.batch - 1]]

    total = 0
    for i, batch in enumerate(batches, 1):
        print(f"\n--- Batch {i}/{len(batches)}: {batch['name']} ---")
        print(f"    {batch['desc']} (预估{batch['estimated']}题)")
        count = enqueue_batch(db, r, batch, args.dry_run)
        total += count
        print(f"    入队: {count}题")

        if not args.dry_run:
            update_stats(r)

    print(f"\n=== 总计入队: {total}题 ===")
    if not args.dry_run:
        pending = pending_count(r)
        print(f"当前Redis pending队列: {pending}题")

    logger.info(f"=== 队列准备结束, 总计{total}题 ===")


if __name__ == "__main__":
    main()
