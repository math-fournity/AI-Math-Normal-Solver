#!/usr/bin/env python3
"""feeder.py — 服务1：选题入队

从ArangoDB的problem_extraction_progress表按tier选题，写入Redis pending队列。
独立进程，持续运行直到所有题入队或达到limit。

用法:
  python feeder.py --tier 1 --batch-size 100 --limit 500
  python feeder.py --tier 1,2,3 --batch-size 1000 --low-water-mark 100
"""
import sys
import os
import time
import argparse

# 添加solver_harness到path
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
sys.path.insert(0, os.path.dirname(__file__))

from redis_queue import (
    get_redis, enqueue_pending, pending_count, update_stats, clear_all, ping,
)
from arango import ArangoClient
from shared_logger import get_logger
from graceful_shutdown import register_shutdown, should_stop

logger = get_logger("feeder")

DB_HOST = "http://localhost:8529"
DB_NAME = "xishujuzhen_math_glm52"
DB_USER = "root"
DB_PASS = "moira123"
COLLECTION = "problem_extraction_progress"


def feed_batch(db, r, tiers: list[int], batch_size: int) -> int:
    """从ArangoDB取一批题，写入Redis pending队列"""
    tier_filter = ", ".join(str(t) for t in tiers)
    aql = (
        f"FOR d IN {COLLECTION} "
        f"FILTER d.extraction_status == 'pending' "
        f"FILTER d.difficulty_tier IN [{tier_filter}] "
        f"SORT d.priority ASC, d._key ASC "
        f"LIMIT {batch_size} "
        f"RETURN {{key: d._key, tier: d.difficulty_tier, priority: d.priority}}"
    )
    logger.debug(f"feed_batch: 执行AQL, tiers={tiers} batch_size={batch_size}")
    logger.debug(f"feed_batch: AQL={aql}")
    aql_start = time.time()
    cursor = db.aql.execute(aql, ttl=300)
    aql_elapsed = time.time() - aql_start
    logger.info(f"feed_batch: AQL执行完成, 耗时={aql_elapsed:.3f}s, tiers={tiers}, batch_size={batch_size}")
    count = 0
    for row in cursor:
        key = row["key"]
        priority = row["priority"]
        tier = row["tier"]
        logger.debug(f"feed_batch: 入队 key={key} tier={tier} priority={priority}")
        enqueue_pending(r, key, priority)
        # 更新ArangoDB状态为queued
        db.collection(COLLECTION).update({
            "_key": key,
            "extraction_status": "queued",
        })
        logger.debug(f"feed_batch: DB状态更新为queued, key={key}")
        count += 1
    logger.info(f"feed_batch: 本批入队完成, count={count}, aql耗时={aql_elapsed:.3f}s")
    return count


def main():
    parser = argparse.ArgumentParser(description="Feeder: 选题入队")
    parser.add_argument("--tier", type=str, default="1,2,3", help="难度tier，逗号分隔")
    parser.add_argument("--batch-size", type=int, default=100, help="每批从DB取多少题")
    parser.add_argument("--limit", type=int, default=0, help="总入队上限（0=无限）")
    parser.add_argument("--low-water-mark", type=int, default=50, help="pending低于此值时补充")
    parser.add_argument("--poll-interval", type=int, default=5, help="检查间隔秒数")
    parser.add_argument("--clear", action="store_true", help="清空Redis队列后重新开始")
    parser.add_argument("--dry-run", action="store_true", help="dry-run模式：不入库，只打印")
    args = parser.parse_args()

    tiers = [int(t) for t in args.tier.split(",")]
    logger.info(f"启动Feeder, tier={tiers} batch_size={args.batch_size} limit={args.limit} low_water_mark={args.low_water_mark} poll_interval={args.poll_interval} clear={args.clear} dry_run={args.dry_run}")

    # 检查Redis
    if not ping():
        logger.error("Redis连接失败, 退出")
        sys.exit(1)
    logger.info("Redis连接成功")

    if args.clear:
        r = get_redis()
        clear_all(r)
        logger.warning("已清空Redis队列")

    # 连接ArangoDB
    logger.info(f"连接ArangoDB: host={DB_HOST} db={DB_NAME} user={DB_USER}")
    client = ArangoClient(hosts=DB_HOST, request_timeout=300)
    db = client.db(DB_NAME, username=DB_USER, password=DB_PASS)
    logger.info("ArangoDB连接成功")

    r = get_redis()
    total_enqueued = 0

    # 注册优雅退出
    register_shutdown("feeder")

    while True:
        # 检查优雅退出
        if should_stop():
            logger.info(f"Feeder优雅退出: total_enqueued={total_enqueued}")
            break

        try:
            # 检查是否达到limit
            if args.limit > 0 and total_enqueued >= args.limit:
                logger.info(f"达到limit, total_enqueued={total_enqueued} >= limit={args.limit}, 停止")
                break

            # 检查pending水位
            pending = pending_count(r)
            logger.debug(f"水位检查: pending={pending} low_water_mark={args.low_water_mark}")
            if pending >= args.low_water_mark:
                logger.info(f"pending={pending} >= low_water_mark={args.low_water_mark}, 等待{args.poll_interval}s")
                time.sleep(args.poll_interval)
                continue

            # 计算这批取多少
            need = args.low_water_mark - pending
            if args.limit > 0:
                need = min(need, args.limit - total_enqueued)
            batch = min(need, args.batch_size)
            logger.debug(f"计算批量: pending={pending} need={need} batch={batch} total_enqueued={total_enqueued} limit={args.limit}")

            if args.dry_run:
                # dry-run：只查DB数量，不入队
                tier_filter = ", ".join(str(t) for t in tiers)
                aql = (
                    f"FOR d IN {COLLECTION} "
                    f"FILTER d.extraction_status == 'pending' "
                    f"FILTER d.difficulty_tier IN [{tier_filter}] "
                    f"COLLECT WITH COUNT INTO c RETURN c"
                )
                count = db.aql.execute(aql, ttl=300).next()
                logger.info(f"DRY-RUN: 待入库题数={count} (tier={tiers})")
                break

            # 取题入队
            count = feed_batch(db, r, tiers, batch)
            total_enqueued += count
            update_stats(r)
            logger.info(f"入队 {count}题 (总计 {total_enqueued}, pending={pending_count(r)})")

            if count == 0:
                # 没有更多题了
                logger.info("没有更多pending题, 停止")
                break

        except Exception as e:
            logger.error(f"Feeder轮次失败: {e}", exc_info=True)

        time.sleep(args.poll_interval)

    update_stats(r)
    logger.info(f"Feeder完成, 总共入队 {total_enqueued}题")


if __name__ == "__main__":
    main()
