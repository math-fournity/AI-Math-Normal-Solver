#!/usr/bin/env python3
"""auto_runner.py — 自动化运营脚本

从problem_queue队列取题，launch devin cli做题，判定落盘，定时报告。

架构：
  [enqueue_problem.py送题] → problem_queue表 + queue_in/目录
                                        ↓
  [auto_runner.py] ← 取queued题 → problem_queue
       ↓
  创建devin_problem_runs attempt → 写AGENTS.md → launch devin cli
       ↓
  refresh_attempt循环 → 判定PROOF COMPLETE / timeout / stall / dead
       ↓
  更新problem_queue的queue_status → solved / failed
       ↓
  定时报告 → 日志文件 + stdout

用法：
  python auto_runner.py --concurrency 100 --poll-seconds 30 --report-seconds 300

特性：
  - 从problem_queue取queued题，自动创建attempt并launch
  - 维持固定并发数（concurrency）
  - refresh_attempt判定PROOF COMPLETE / timeout / stall / dead_session
  - 自动更新problem_queue的queue_status
  - 定时输出状态报告到日志
  - 可配置并发数、poll间隔、报告间隔
  - stop-on-stall / max-runtime / stall-seconds可配置
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPT_DIR))

from batch_problem_runner import (
    connect_db,
    ensure_schema,
    ensure_indexes,
    next_run_id,
    make_exp_id,
    make_attempt_paths,
    make_verdict,
    insert_event,
    attempt_key,
    launch_attempt,
    refresh_attempt,
    capture_thinking,
    stop_attempt,
    observe_attempt_files,
    classify_finished,
    tmux_running,
    seconds_since,
    utc_now,
    SOLVER_BASE,
    TRAJECTORY_BASE,
    BATCH_BASE,
    BATCH_COLLECTION,
    ATTEMPT_COLLECTION,
    EVENT_COLLECTION,
    COUNTER_COLLECTION,
    TERMINAL_STATUSES,
)

QUEUE_COLLECTION = "problem_queue"
AUTO_BATCH_ID = "dpb-auto-runner"
AUTO_BATCH_DIR = BATCH_BASE / AUTO_BATCH_ID


def ensure_queue_schema(db) -> None:
    if not db.has_collection(QUEUE_COLLECTION):
        db.create_collection(QUEUE_COLLECTION)
    col = db.collection(QUEUE_COLLECTION)
    for name, fields, unique in [
        ("idx_qstatus", ["queue_status"], False),
        ("idx_problem_id", ["problem_id"], True),
        ("idx_tier", ["difficulty_tier"], False),
        ("idx_priority", ["priority"], False),
    ]:
        try:
            col.add_index({"type": "persistent", "fields": fields, "unique": unique, "name": name})
        except Exception:
            pass


def ensure_auto_batch(db, concurrency: int, max_runtime: int, stall_seconds: int) -> None:
    """确保auto-runner的batch记录存在"""
    AUTO_BATCH_DIR.mkdir(parents=True, exist_ok=True)
    (AUTO_BATCH_DIR / "problems").mkdir(exist_ok=True)
    (AUTO_BATCH_DIR / "logs").mkdir(exist_ok=True)

    existing = db.collection(BATCH_COLLECTION).get(AUTO_BATCH_ID)
    if not existing:
        db.collection(BATCH_COLLECTION).insert({
            "_key": AUTO_BATCH_ID,
            "status": "running",
            "created_at": utc_now(),
            "updated_at": utc_now(),
            "model": "glm-5-2",
            "concurrency": concurrency,
            "selected_count": 0,
            "attempt_keys": [],
            "selection": {"mode": "auto_runner_queue"},
            "paths": {
                "batch_dir": str(AUTO_BATCH_DIR),
                "problems_dir": str(AUTO_BATCH_DIR / "problems"),
                "logs_dir": str(AUTO_BATCH_DIR / "logs"),
                "solver_base": str(SOLVER_BASE),
                "trajectory_base": str(TRAJECTORY_BASE),
            },
            "timeouts": {"max_runtime_seconds": max_runtime, "stall_seconds": stall_seconds},
            "schema_version": "v0",
        })
    else:
        db.collection(BATCH_COLLECTION).update({
            "_key": AUTO_BATCH_ID,
            "status": "running",
            "concurrency": concurrency,
            "updated_at": utc_now(),
            "timeouts": {"max_runtime_seconds": max_runtime, "stall_seconds": stall_seconds},
        })


def fetch_queued_problems(db, limit: int) -> list[dict[str, Any]]:
    """从problem_queue取queued题，按priority排序"""
    return list(db.aql.execute(
        f"FOR q IN {QUEUE_COLLECTION} "
        "FILTER q.queue_status == 'queued' "
        "SORT q.priority ASC, q.enqueued_at ASC "
        "LIMIT @limit "
        "RETURN q",
        bind_vars={"limit": limit},
    ))


def create_attempt_from_queue(db, q: dict[str, Any], ordinal: int) -> dict[str, Any]:
    """从queue条目创建devin_problem_runs attempt"""
    problem_id = q["problem_id"]
    run_id = next_run_id(db)
    progress_key = q.get("metadata", {}).get("progress_key", q["problem_id"])
    global_sequence = q.get("metadata", {}).get("global_sequence", 0)
    exp_id = make_exp_id(AUTO_BATCH_ID, ordinal, progress_key, global_sequence, problem_id, run_id=run_id)
    key = attempt_key(AUTO_BATCH_ID, progress_key, ordinal)

    # 写problem文件
    problem_source_path = AUTO_BATCH_DIR / "problems" / f"{exp_id}.txt"
    problem_source_path.write_text(q["problem_text"] + "\n", encoding="utf-8")

    paths = make_attempt_paths(exp_id)
    now = utc_now()

    doc = {
        "_key": key,
        "batch_id": AUTO_BATCH_ID,
        "ordinal": ordinal,
        "run_id": run_id,
        "status": "queued",
        "created_at": now,
        "updated_at": now,
        "model": "glm-5-2",
        "concurrency": 0,  # 由batch级别管理
        "progress_key": progress_key,
        "global_sequence": global_sequence,
        "problem_id": problem_id,
        "profile_doc_id": None,
        "source_dataset": q.get("source_dataset", "queue"),
        "source_mode": "auto_runner_queue",
        "case_metadata": {"queue_key": q["_key"], "tier": q.get("difficulty_tier")},
        "difficulty_tier": q.get("difficulty_tier"),
        "priority": q.get("priority", 5),
        "exp_id": exp_id,
        "tmux_session": f"harness-{exp_id}",
        "problem_source_path": str(problem_source_path),
        "paths": paths,
        "observability": {
            "activity_signature": "",
            "last_observed_activity_at": None,
            "last_observed_at": None,
            "markers": {},
            "file_sizes": {},
        },
        "verdict": make_verdict("queued", "auto_runner queued", needs_human_math_review=False),
    }
    db.collection(ATTEMPT_COLLECTION).insert(doc)

    # 更新queue状态为running
    db.collection(QUEUE_COLLECTION).update({
        "_key": q["_key"],
        "queue_status": "running",
        "updated_at": now,
        "run_count": q.get("run_count", 0) + 1,
        "current_attempt_key": key,
    })

    return db.collection(ATTEMPT_COLLECTION).get(key)


def get_next_ordinal(db) -> int:
    """获取下一个ordinal"""
    cursor = db.aql.execute(
        f"FOR a IN {ATTEMPT_COLLECTION} "
        f"FILTER a.batch_id == '{AUTO_BATCH_ID}' "
        "COLLECT AGGREGATE max_ord = MAX(a.ordinal) "
        "RETURN max_ord"
    )
    try:
        val = next(cursor)
        return (val or 0) + 1
    except StopIteration:
        return 1


def count_running_attempts(db) -> int:
    """统计当前running/launching的attempt数"""
    return next(db.aql.execute(
        f"FOR a IN {ATTEMPT_COLLECTION} "
        f"FILTER a.batch_id == '{AUTO_BATCH_ID}' "
        "FILTER a.status IN ['running', 'launching'] "
        "COLLECT WITH COUNT INTO n RETURN n"
    ))


def load_active_attempts(db) -> list[dict[str, Any]]:
    """加载所有活跃attempt（running/launching）"""
    return list(db.aql.execute(
        f"FOR a IN {ATTEMPT_COLLECTION} "
        f"FILTER a.batch_id == '{AUTO_BATCH_ID}' "
        "FILTER a.status IN ['running', 'launching'] "
        "RETURN a"
    ))


def update_queue_from_attempt(db, attempt: dict[str, Any]) -> None:
    """attempt终态后更新problem_queue状态"""
    status = attempt.get("status", "")
    queue_key = attempt.get("case_metadata", {}).get("queue_key")
    if not queue_key:
        return

    if status == "candidate_solved":
        q_status = "solved"
    elif status in TERMINAL_STATUSES:
        q_status = "failed"
    else:
        return  # 还在running

    # 添加attempt_key到队列记录
    q = db.collection(QUEUE_COLLECTION).get(queue_key)
    if q:
        attempt_keys = q.get("attempt_keys", [])
        if attempt["_key"] not in attempt_keys:
            attempt_keys.append(attempt["_key"])
        db.collection(QUEUE_COLLECTION).update({
            "_key": queue_key,
            "queue_status": q_status,
            "updated_at": utc_now(),
            "attempt_keys": attempt_keys,
            "last_attempt_status": status,
            "last_attempt_ended_at": attempt.get("ended_at"),
        })


def generate_report(db) -> str:
    """生成状态报告"""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    # queue状态
    q_statuses = Counter()
    q_total = 0
    for q in db.aql.execute(f"FOR q IN {QUEUE_COLLECTION} RETURN q"):
        q_statuses[q.get("queue_status", "?")] += 1
        q_total += 1

    # attempt状态
    a_statuses = Counter()
    a_total = 0
    for a in db.aql.execute(
        f"FOR a IN {ATTEMPT_COLLECTION} FILTER a.batch_id == '{AUTO_BATCH_ID}' RETURN a.status"
    ):
        a_statuses[a] += 1
        a_total += 1

    running = a_statuses.get("running", 0) + a_statuses.get("launching", 0)
    solved = a_statuses.get("candidate_solved", 0)
    failed = sum(n for s, n in a_statuses.items() if s in TERMINAL_STATUSES and s != "candidate_solved")

    # tmux session数
    tmux_count = 0
    try:
        tmux_out = subprocess.run(["tmux", "list-sessions"], capture_output=True, text=True, timeout=5).stdout
        tmux_count = sum(1 for line in tmux_out.strip().split("\n") if line.startswith(f"harness-{AUTO_BATCH_ID}"))
    except Exception:
        pass

    lines = [
        f"=== auto_runner report @ {now} ===",
        f"queue: total={q_total} queued={q_statuses.get('queued',0)} running={q_statuses.get('running',0)} solved={q_statuses.get('solved',0)} failed={q_statuses.get('failed',0)}",
        f"attempts: total={a_total} running={running} solved={solved} failed={failed}",
        f"tmux sessions: {tmux_count}",
        f"solve rate: {solved}/{solved+failed} = {solved/(solved+failed)*100:.1f}%" if (solved + failed) > 0 else "solve rate: n/a",
    ]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="自动化运营脚本——从queue取题做题")
    parser.add_argument("--concurrency", type=int, default=100, help="并发数（默认100）")
    parser.add_argument("--poll-seconds", type=int, default=30, help="poll间隔秒数")
    parser.add_argument("--report-seconds", type=int, default=300, help="报告间隔秒数")
    parser.add_argument("--max-runtime-seconds", type=int, default=14400, help="单题最大运行时间（默认4小时）")
    parser.add_argument("--stall-seconds", type=int, default=900, help="stall超时秒数（默认15分钟）")
    parser.add_argument("--stop-on-stall", action="store_true", default=True, help="stall时停止")
    parser.add_argument("--launch-interval", type=float, default=1.0, help="launch间隔秒数")
    parser.add_argument("--log-file", type=str, default=str(TRAJECTORY_BASE / "logs" / "auto_runner.log"), help="日志文件")
    args = parser.parse_args()

    db = connect_db()
    ensure_schema(db)
    ensure_indexes(db)
    ensure_queue_schema(db)
    ensure_auto_batch(db, args.concurrency, args.max_runtime_seconds, args.stall_seconds)

    log_path = Path(args.log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    def log(msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {msg}"
        print(line, flush=True)
        with log_path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    log(f"auto_runner started: concurrency={args.concurrency} poll={args.poll_seconds}s report={args.report_seconds}s")
    log(f"batch_id={AUTO_BATCH_ID} log={log_path}")

    last_report = 0
    cycle = 0

    while True:
        cycle += 1
        now_ts = time.time()

        # 1. refresh所有活跃attempt
        active = load_active_attempts(db)
        if active:
            log(f"[cycle {cycle}] refreshing {len(active)} active attempts")
            for attempt in active:
                try:
                    refreshed = refresh_attempt(
                        db, attempt, AUTO_BATCH_DIR,
                        max_runtime_seconds=args.max_runtime_seconds,
                        stall_seconds=args.stall_seconds,
                        stop_on_stall=args.stop_on_stall,
                    )
                    # 如果attempt进入终态，更新queue
                    if refreshed.get("status") in TERMINAL_STATUSES or refreshed.get("status") == "candidate_solved":
                        update_queue_from_attempt(db, refreshed)
                        log(f"  [done] {refreshed['problem_id']} → {refreshed['status']}")
                except Exception as e:
                    log(f"  [error] refresh {attempt.get('problem_id','?')}: {e}")

        # 2. 检查并发数，launch新题
        running_count = count_running_attempts(db)
        slots = args.concurrency - running_count
        if slots > 0:
            queued = fetch_queued_problems(db, limit=slots)
            if queued:
                log(f"[cycle {cycle}] launching {len(queued)} new problems (slots={slots} running={running_count})")
                ordinal = get_next_ordinal(db)
                for q in queued:
                    try:
                        attempt = create_attempt_from_queue(db, q, ordinal)
                        ordinal += 1
                        launch_attempt(db, attempt, batch_dir=AUTO_BATCH_DIR)
                        time.sleep(args.launch_interval)
                    except Exception as e:
                        log(f"  [error] launch {q['problem_id']}: {e}")
                        # 回滚queue状态
                        db.collection(QUEUE_COLLECTION).update({
                            "_key": q["_key"],
                            "queue_status": "queued",
                            "updated_at": utc_now(),
                        })

        # 3. 定时报告
        if now_ts - last_report >= args.report_seconds:
            report = generate_report(db)
            log(report)
            last_report = now_ts

        # 4. 检查是否所有题都做完了
        q_queued = next(db.aql.execute(
            f"FOR q IN {QUEUE_COLLECTION} FILTER q.queue_status == 'queued' COLLECT WITH COUNT INTO n RETURN n"
        ))
        q_running = next(db.aql.execute(
            f"FOR q IN {QUEUE_COLLECTION} FILTER q.queue_status == 'running' COLLECT WITH COUNT INTO n RETURN n"
        ))
        if q_queued == 0 and q_running == 0 and running_count == 0:
            log("=== ALL DONE: queue empty, no running attempts ===")
            report = generate_report(db)
            log(report)
            break

        # 5. 等待下一轮
        time.sleep(args.poll_seconds)

    log("auto_runner exited")


if __name__ == "__main__":
    main()
