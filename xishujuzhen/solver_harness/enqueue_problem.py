#!/usr/bin/env python3
"""enqueue_problem.py — 送题工具

将清洗好的题目送入problem_queue队列和queue_in/目录。

用法1（单题送入）：
  python enqueue_problem.py --problem-id <pid> --text-file <path> [--tier <n>] [--source <name>] [--answer <text>] [--priority <n>]

用法2（从problem_extraction_progress批量送入某tier）：
  python enqueue_problem.py --from-progress --tier <n> --limit <n> [--skip-solved]

用法3（从固定目录扫描新题文件）：
  python enqueue_problem.py --scan-dir <dir> [--tier <n>] [--source <name>]

用法4（查看队列状态）：
  python enqueue_problem.py --status

架构：
  [我清洗题] → queue_in/{problem_id}.txt + DB problem_queue表 → [auto_runner.py] → AGENTS.md → devin cli → 判定落盘
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# 复用batch_problem_runner的基础设施
SCRIPT_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPT_DIR))

from batch_problem_runner import (
    connect_db,
    ensure_schema,
    ensure_indexes,
    next_run_id,
    load_problem_text_from_progress,
    SOLVER_BASE,
    TRAJECTORY_BASE,
)

QUEUE_COLLECTION = "problem_queue"
QUEUE_IN_DIR = TRAJECTORY_BASE / "_queue_in"
QUEUE_DIR = TRAJECTORY_BASE / "_queue"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_queue_schema(db) -> None:
    """确保problem_queue collection存在"""
    if not db.has_collection(QUEUE_COLLECTION):
        db.create_collection(QUEUE_COLLECTION)
    col = db.collection(QUEUE_COLLECTION)
    # 添加索引
    for name, fields, unique in [
        ("idx_qstatus", ["queue_status"], False),
        ("idx_problem_id", ["problem_id"], True),
        ("idx_tier", ["difficulty_tier"], False),
        ("idx_priority", ["priority"], False),
        ("idx_created", ["enqueued_at"], False),
    ]:
        try:
            col.add_index({"type": "persistent", "fields": fields, "unique": unique, "name": name})
        except Exception:
            pass  # 索引已存在


def enqueue_single(
    db,
    *,
    problem_id: str,
    problem_text: str,
    tier: int | None = None,
    source: str = "manual",
    answer: str = "",
    priority: int = 5,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """送入单道题到队列"""
    QUEUE_IN_DIR.mkdir(parents=True, exist_ok=True)

    # 写入queue_in目录
    text_file = QUEUE_IN_DIR / f"{problem_id}.txt"
    text_file.write_text(problem_text.strip() + "\n", encoding="utf-8")

    # 写入DB queue
    now = utc_now()
    doc = {
        "_key": problem_id,
        "problem_id": problem_id,
        "queue_status": "queued",  # queued / running / solved / failed / cancelled
        "problem_text": problem_text.strip(),
        "problem_file_path": str(text_file),
        "difficulty_tier": tier,
        "source_dataset": source,
        "expected_answer": answer,
        "priority": priority,
        "metadata": metadata or {},
        "enqueued_at": now,
        "updated_at": now,
        "attempt_keys": [],  # 关联的devin_problem_runs的_key列表
        "run_count": 0,  # 被运行过几次
    }

    # upsert
    existing = db.collection(QUEUE_COLLECTION).get(problem_id)
    if existing:
        # 已存在——如果之前solved了就不重复送入
        if existing.get("queue_status") == "solved":
            print(f"  SKIP {problem_id}: already solved", file=sys.stderr)
            return existing
        doc["run_count"] = existing.get("run_count", 0)
        doc["attempt_keys"] = existing.get("attempt_keys", [])
        db.collection(QUEUE_COLLECTION).update({"_key": problem_id, **{k: v for k, v in doc.items() if k != "_key"}})
        print(f"  UPDATE {problem_id}: re-queued (was {existing.get('queue_status')})")
    else:
        db.collection(QUEUE_COLLECTION).insert(doc)
        print(f"  ENQUEUE {problem_id}: tier={tier} source={source} priority={priority}")

    return doc


def enqueue_from_progress(db, *, tier: int, limit: int, skip_solved: bool = True) -> int:
    """从problem_extraction_progress批量送入某tier的题"""
    # 查已solved的problem_ids
    solved_ids = set()
    if skip_solved:
        for r in db.aql.execute(
            f"FOR q IN {QUEUE_COLLECTION} FILTER q.queue_status == 'solved' RETURN q.problem_id"
        ):
            solved_ids.add(r)
        # 也查devin_problem_runs中solved的
        for r in db.aql.execute(
            "FOR r IN devin_problem_runs FILTER r.status == 'candidate_solved' RETURN DISTINCT r.problem_id"
        ):
            solved_ids.add(r)

    # 查该tier的题
    candidates = list(
        db.aql.execute(
            "FOR p IN problem_extraction_progress "
            "FILTER p.difficulty_tier == @tier "
            "FILTER p.external_ref != null "
            "FILTER p.external_ref.local_path != null "
            "SORT p.global_sequence ASC "
            "LIMIT @limit "
            "RETURN p",
            bind_vars={"tier": tier, "limit": limit},
        )
    )

    enqueued = 0
    skipped = 0
    for prog in candidates:
        pid = prog.get("problem_id", f"p{prog['_key']}")
        if pid in solved_ids:
            skipped += 1
            continue
        try:
            text = load_problem_text_from_progress(prog)
            if not text or len(text.strip()) < 10:
                skipped += 1
                continue
            enqueue_single(
                db,
                problem_id=pid,
                problem_text=text,
                tier=tier,
                source=prog.get("source_dataset", "unknown"),
                priority=prog.get("priority", 5),
                metadata={"progress_key": str(prog["_key"]), "global_sequence": prog.get("global_sequence")},
            )
            enqueued += 1
        except Exception as e:
            print(f"  SKIP {pid}: {e}", file=sys.stderr)
            skipped += 1

    print(f"\nenqueued: {enqueued}, skipped: {skipped}")
    return enqueued


def enqueue_from_dir(db, *, scan_dir: str, tier: int | None = None, source: str = "manual") -> int:
    """从固定目录扫描新题文件送入队列"""
    d = Path(scan_dir)
    if not d.exists():
        print(f"ERROR: directory {d} does not exist", file=sys.stderr)
        return 0

    enqueued = 0
    for f in sorted(d.glob("*.txt")):
        pid = f.stem
        text = f.read_text(encoding="utf-8").strip()
        if len(text) < 10:
            continue
        enqueue_single(db, problem_id=pid, problem_text=text, tier=tier, source=source)
        enqueued += 1

    print(f"\nenqueued: {enqueued} from {d}")
    return enqueued


def show_status(db) -> None:
    """显示队列状态"""
    from collections import Counter

    statuses = Counter()
    tiers = Counter()
    total = 0
    for q in db.aql.execute(f"FOR q IN {QUEUE_COLLECTION} RETURN q"):
        statuses[q.get("queue_status", "?")] += 1
        tiers[q.get("difficulty_tier", "?")] += 1
        total += 1

    print(f"problem_queue total: {total}")
    print("\nby status:")
    for s, n in sorted(statuses.items(), key=lambda x: -x[1]):
        print(f"  {s}: {n}")
    print("\nby tier:")
    for t, n in sorted(tiers.items(), key=lambda x: str(x[0])):
        print(f"  tier {t}: {n}")

    # queue_in目录
    if QUEUE_IN_DIR.exists():
        files = list(QUEUE_IN_DIR.glob("*.txt"))
        print(f"\nqueue_in/ directory: {len(files)} files")


def main():
    parser = argparse.ArgumentParser(description="送题工具——将题目送入problem_queue队列")
    parser.add_argument("--problem-id", type=str, help="题目ID")
    parser.add_argument("--text-file", type=str, help="题目文本文件路径")
    parser.add_argument("--text", type=str, help="题目文本（直接传入）")
    parser.add_argument("--tier", type=int, default=None, help="难度层级")
    parser.add_argument("--source", type=str, default="manual", help="来源数据集名")
    parser.add_argument("--answer", type=str, default="", help="标准答案（如有）")
    parser.add_argument("--priority", type=int, default=5, help="优先级（1最高10最低）")

    parser.add_argument("--from-progress", action="store_true", help="从problem_extraction_progress批量送入")
    parser.add_argument("--limit", type=int, default=100, help="批量送入上限")
    parser.add_argument("--skip-solved", action="store_true", default=True, help="跳过已solved的题")

    parser.add_argument("--scan-dir", type=str, help="从目录扫描新题文件")

    parser.add_argument("--status", action="store_true", help="显示队列状态")

    args = parser.parse_args()

    db = connect_db()
    ensure_schema(db)
    ensure_indexes(db)
    ensure_queue_schema(db)

    if args.status:
        show_status(db)
        return

    if args.from_progress:
        if args.tier is None:
            print("ERROR: --tier required with --from-progress", file=sys.stderr)
            sys.exit(1)
        enqueue_from_progress(db, tier=args.tier, limit=args.limit, skip_solved=args.skip_solved)
        return

    if args.scan_dir:
        enqueue_from_dir(db, scan_dir=args.scan_dir, tier=args.tier, source=args.source)
        return

    # 单题送入
    if not args.problem_id:
        print("ERROR: --problem-id required for single enqueue", file=sys.stderr)
        sys.exit(1)

    if args.text_file:
        text = Path(args.text_file).read_text(encoding="utf-8")
    elif args.text:
        text = args.text
    else:
        print("ERROR: --text-file or --text required", file=sys.stderr)
        sys.exit(1)

    enqueue_single(
        db,
        problem_id=args.problem_id,
        problem_text=text,
        tier=args.tier,
        source=args.source,
        answer=args.answer,
        priority=args.priority,
    )


if __name__ == "__main__":
    main()
