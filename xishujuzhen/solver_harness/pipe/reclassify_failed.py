#!/usr/bin/env python3
"""reclassify_failed.py — 重新分类被误判的failed记录

当Collector的判定逻辑有bug时，一些实际上有PROOF COMPLETE的运行可能被误判为
failed_thinking_spin。本脚本读取failed队列中每条记录的pane_snapshot，
用修正后的has_real_proof重新判定，把误判的记录从failed移到completed。

用法:
  python reclassify_failed.py --dry-run    # 只检查不修改
  python reclassify_failed.py              # 执行重新分类
"""
import sys
import os
import json
import argparse
import logging
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))

from redis_queue import get_redis
from collector import has_real_proof

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

TRAJECTORY_BASE = Path("/Volumes/data/math-agent-glm5.2-tmux-agents-trajectory")


def reclassify(dry_run: bool = False):
    r = get_redis()
    failed = r.lrange("math:failed", 0, -1)
    logger.info(f"failed队列: {len(failed)}条")

    reclassified = []
    still_failed = []

    for item in failed:
        data = json.loads(item)
        exp_id = data.get("exp_id")
        problem_key = data.get("problem_key")
        verdict = data.get("verdict", "?")

        # 只重新检查failed_thinking_spin和failed_timeout
        if verdict not in ("failed_thinking_spin", "failed_timeout"):
            still_failed.append(item)
            continue

        # 读pane_snapshot
        snap = TRAJECTORY_BASE / exp_id / "collector" / "pane_snapshot.txt"
        if not snap.exists():
            logger.debug(f"  {problem_key}: 无pane_snapshot，跳过")
            still_failed.append(item)
            continue

        with open(snap) as f:
            pane = f.read()

        # 用修正后的has_real_proof重新判定
        if has_real_proof(pane):
            logger.info(f"  ✅ 重新分类: {problem_key} {verdict} → candidate_solved")
            # 转换为completed记录
            new_data = {
                "problem_key": problem_key,
                "exp_id": exp_id,
                "verdict": "candidate_solved",
                "elapsed": data.get("elapsed", 0),
                "reclassified_from": verdict,
                "reclassified_reason": "has_real_proof=True after collector bug fix",
            }
            reclassified.append((item, json.dumps(new_data)))
        else:
            still_failed.append(item)

    logger.info(f"\n=== 结果 ===")
    logger.info(f"  重新分类为candidate_solved: {len(reclassified)}")
    logger.info(f"  仍为failed: {len(still_failed)}")

    if dry_run:
        logger.info("  (dry-run模式，不修改Redis)")
        return

    if not reclassified:
        return

    # 执行重新分类
    # 1. 清空failed队列
    r.delete("math:failed")
    # 2. 写回仍为failed的记录
    for item in still_failed:
        r.rpush("math:failed", item)
    # 3. 把重新分类的记录写入completed
    for old_item, new_item in reclassified:
        r.rpush("math:completed", new_item)
        # 更新DB中的status
        try:
            from arango import ArangoClient
            client = ArangoClient(hosts="http://localhost:8529", request_timeout=300)
            db = client.db("xishujuzhen_math_glm52", username="root", password="moira123")
            # 找到attempt记录并更新
            aql = f"FOR a IN devin_problem_runs FILTER a.exp_id == '{json.loads(new_item)['exp_id']}' LIMIT 1 RETURN a._key"
            cursor = db.aql.execute(aql, ttl=60)
            keys = list(cursor)
            if keys:
                db.collection("devin_problem_runs").update({
                    "_key": keys[0],
                    "status": "candidate_solved",
                    "verdict": "candidate_solved",
                    "reclassified_from": json.loads(new_item).get("reclassified_from"),
                    "reclassified_reason": "has_real_proof=True after collector bug fix",
                })
                logger.info(f"  DB更新: {keys[0]} → candidate_solved")
        except Exception as e:
            logger.error(f"  DB更新失败: {e}")

    logger.info(f"\n重新分类完成: {len(reclassified)}条从failed→completed")


def main():
    parser = argparse.ArgumentParser(description="重新分类被误判的failed记录")
    parser.add_argument("--dry-run", action="store_true", help="只检查不修改")
    args = parser.parse_args()
    reclassify(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
