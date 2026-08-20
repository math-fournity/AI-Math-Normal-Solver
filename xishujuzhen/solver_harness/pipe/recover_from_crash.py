#!/usr/bin/env python3
"""recover_from_crash.py — 断电恢复 + 僵尸清理

系统断电或服务异常退出后，运行此脚本恢复一致性。

故障场景：
1. 断电：Redis有AOF持久化可以恢复队列，但tmux session全部丢失
   → running队列中的记录变成僵尸（session不存在但Redis还标记为running）
2. 服务被kill：pipe_control stop用tmux kill-session停服务
   → 服务进程被kill，harness-xxx session可能还在运行
3. 代码热替换：停Collector替换代码，重启Collector
   → running队列中的记录对应的session可能还在运行

恢复流程：
1. 扫描Redis running队列
2. 对每个running记录，检查tmux session是否存在
   - session存在 → 保留在running队列（让Collector处理）
   - session不存在 → 标记为crash_recovered，移到failed队列
3. 修复ArangoDB中status=running但实际已结束的attempt
4. 报告恢复结果

用法:
  python recover_from_crash.py --dry-run       # 只检查不修改
  python recover_from_crash.py                  # 执行恢复
  python recover_from_crash.py --auto-restart   # 恢复后自动重启所有服务
"""
import sys
import os
import time
import json
import argparse
import subprocess
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
sys.path.insert(0, os.path.dirname(__file__))

from redis_queue import get_redis, ping, add_failed, remove_running
from arango import ArangoClient
from shared_logger import get_logger

logger = get_logger("recover")

DB_HOST = "http://localhost:8529"
DB_NAME = "xishujuzhen_math_glm52"
DB_USER = "root"
DB_PASS = "moira123"
COLLECTION = "math_problems"
ATTEMPT_COLLECTION = "devin_problem_runs"
TRAJECTORY_BASE = Path("/Volumes/data/math-agent-glm5.2-tmux-agents-trajectory")


def tmux_session_exists(session_name: str) -> bool:
    """检查tmux session是否存在"""
    try:
        result = subprocess.run(
            ["tmux", "has-session", "-t", session_name],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False, timeout=5
        )
        return result.returncode == 0
    except Exception:
        return False


def list_harness_sessions() -> list:
    """列出解题系统的harness-xxx tmux session——只匹配harness-p{uuidhex}和harness-dbmon-p{uuidhex}"""
    try:
        import re
        result = subprocess.run(
            ["tmux", "list-sessions", "-F", "#{session_name}"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode != 0:
            return []
        pipe_pattern = re.compile(r'^harness-(dbmon-)?p[a-f0-9]{20}')
        return [s.strip() for s in result.stdout.strip().split("\n") if pipe_pattern.match(s.strip())]
    except Exception:
        return []


def recover_running_queue(r, db, dry_run: bool = False) -> dict:
    """恢复running队列——清理僵尸记录"""
    running_items = r.hgetall("math:running")
    logger.info(f"扫描running队列: {len(running_items)}项")

    alive = 0       # session还存在，保留
    zombies = 0     # session不存在，移到failed
    recovered_keys = []

    for exp_id_bytes, meta_bytes in running_items.items():
        exp_id = exp_id_bytes.decode("utf-8") if isinstance(exp_id_bytes, bytes) else exp_id_bytes
        meta_str = meta_bytes.decode("utf-8") if isinstance(meta_bytes, bytes) else meta_bytes

        try:
            meta = json.loads(meta_str)
        except Exception:
            logger.warning(f"running记录解析失败: exp_id={exp_id}, 跳过")
            continue

        tmux_session = meta.get("tmux_session", "")
        problem_key = meta.get("problem_key", "")
        attempt_key = meta.get("attempt_key", "")

        # dry_run模式下不启动devin cli，用dry-xxx session名
        if tmux_session.startswith("dry-"):
            logger.debug(f"跳过dry-run记录: exp_id={exp_id}")
            continue

        # 检查tmux session是否存在
        if tmux_session and tmux_session_exists(tmux_session):
            alive += 1
            logger.info(f"ALIVE: exp_id={exp_id} session={tmux_session} problem={problem_key} — 保留在running队列")
        else:
            zombies += 1
            logger.warning(f"ZOMBIE: exp_id={exp_id} session={tmux_session} problem={problem_key} — session不存在，标记为crash_recovered")

            if not dry_run:
                # 移到failed队列
                fail_result = {
                    "problem_key": problem_key,
                    "exp_id": exp_id,
                    "verdict": "crash_recovered",
                    "error": f"tmux session {tmux_session} 不存在（断电或服务崩溃）",
                    "recovered_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                }
                add_failed(r, fail_result)

                # 从running队列移除
                remove_running(r, exp_id)

                # 更新ArangoDB
                now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                if attempt_key:
                    try:
                        db.collection(ATTEMPT_COLLECTION).update({
                            "_key": attempt_key,
                            "status": "crash_recovered",
                            "verdict": "crash_recovered",
                            "ended_at": now,
                            "end_reason": f"tmux session不存在（断电或服务崩溃）",
                        })
                        logger.debug(f"DB更新: attempt={attempt_key} status=crash_recovered")
                    except Exception as e:
                        logger.error(f"DB更新失败: attempt={attempt_key} error={e}")

                # 更新problem状态回pending（可以重试）
                try:
                    db.collection(COLLECTION).update({
                        "_key": problem_key,
                        "extraction_status": "pending",  # 允许重新入队
                    })
                    logger.debug(f"Problem状态更新: {problem_key} → pending (可重新入队)")
                except Exception as e:
                    logger.error(f"Problem状态更新失败: {problem_key} error={e}")

                recovered_keys.append(exp_id)

    result = {
        "total_running": len(running_items),
        "alive": alive,
        "zombies": zombies,
        "recovered": len(recovered_keys),
        "recovered_keys": recovered_keys,
    }
    logger.info(f"running队列恢复完成: total={result['total_running']} alive={alive} zombies={zombies} recovered={result['recovered']}")
    return result


def recover_orphan_harness_sessions(r, db, dry_run: bool = False) -> dict:
    """恢复孤儿harness session——tmux session存在但Redis running队列中没有记录"""
    harness_sessions = list_harness_sessions()
    running_items = r.hgetall("math:running")

    # 提取running队列中所有的tmux_session名
    running_sessions = set()
    for meta_bytes in running_items.values():
        meta_str = meta_bytes.decode("utf-8") if isinstance(meta_bytes, bytes) else meta_bytes
        try:
            meta = json.loads(meta_str)
            ts = meta.get("tmux_session", "")
            if ts:
                running_sessions.add(ts)
        except Exception:
            continue

    orphans = [s for s in harness_sessions if s not in running_sessions]
    logger.info(f"扫描孤儿harness session: harness总数={len(harness_sessions)} running记录中session数={len(running_sessions)} 孤儿={len(orphans)}")

    killed = 0
    for session in orphans:
        # 从session名提取exp_id
        exp_id = session.replace("harness-", "") if session.startswith("harness-") else session
        logger.warning(f"ORPHAN: session={session} exp_id={exp_id} — tmux session存在但Redis running队列无记录")

        if not dry_run:
            # 检查这个session是否已经完成了（有PROOF COMPLETE或export文件）
            export_file = TRAJECTORY_BASE / exp_id / "exports" / "conversation.json"
            if export_file.exists():
                logger.info(f"ORPHAN已有export: exp_id={exp_id} — kill session，等待Collector下次处理")
            else:
                logger.info(f"ORPHAN无export: exp_id={exp_id} — kill session（无法恢复）")

            subprocess.run(["tmux", "kill-session", "-t", session],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
            killed += 1

    result = {
        "total_harness": len(harness_sessions),
        "orphan_sessions": len(orphans),
        "killed": killed,
    }
    logger.info(f"孤儿session清理完成: total_harness={result['total_harness']} orphans={len(orphans)} killed={killed}")
    return result


def recover_arango_attempts(db, dry_run: bool = False) -> dict:
    """修复ArangoDB中status=running但实际已结束的attempt"""
    aql = (
        f"FOR a IN {ATTEMPT_COLLECTION} "
        f"FILTER a.status == 'running' "
        f"RETURN {{_key: a._key, exp_id: a.exp_id, problem_id: a.problem_id, started_at: a.started_at}}"
    )
    cursor = db.aql.execute(aql, ttl=300)
    running_attempts = list(cursor)
    logger.info(f"扫描ArangoDB中status=running的attempt: {len(running_attempts)}条")

    fixed = 0
    for att in running_attempts:
        exp_id = att.get("exp_id", "")
        if not exp_id:
            continue

        # 检查是否有export文件（说明已结束）
        export_file = TRAJECTORY_BASE / exp_id / "exports" / "conversation.json"
        # 检查trajectory目录是否存在
        traj_dir = TRAJECTORY_BASE / exp_id
        # 检查tmux session
        tmux_session = f"harness-{exp_id}"
        session_exists = tmux_session_exists(tmux_session)

        if export_file.exists() and not session_exists:
            # 有export但session不存在——已结束但状态没更新
            logger.warning(f"STALE: attempt={att['_key']} exp_id={exp_id} — 有export但session不存在，修复为completed")
            if not dry_run:
                now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                try:
                    db.collection(ATTEMPT_COLLECTION).update({
                        "_key": att["_key"],
                        "status": "crash_recovered",
                        "verdict": "crash_recovered_with_export",
                        "ended_at": now,
                        "end_reason": "断电恢复时发现有export文件",
                    })
                    fixed += 1
                except Exception as e:
                    logger.error(f"DB修复失败: attempt={att['_key']} error={e}")
        elif not session_exists and not traj_dir.exists():
            # session不存在且trajectory目录不存在——完全丢失
            logger.warning(f"LOST: attempt={att['_key']} exp_id={exp_id} — session和trajectory都不存在，标记为lost")
            if not dry_run:
                now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                try:
                    db.collection(ATTEMPT_COLLECTION).update({
                        "_key": att["_key"],
                        "status": "crash_recovered",
                        "verdict": "crash_lost",
                        "ended_at": now,
                        "end_reason": "断电恢复时session和trajectory都不存在",
                    })
                    fixed += 1
                except Exception as e:
                    logger.error(f"DB修复失败: attempt={att['_key']} error={e}")
        else:
            logger.debug(f"OK: attempt={att['_key']} exp_id={exp_id} session_exists={session_exists} — 保留running状态")

    result = {
        "total_running_attempts": len(running_attempts),
        "fixed": fixed,
    }
    logger.info(f"ArangoDB attempt修复完成: total={result['total_running_attempts']} fixed={fixed}")
    return result


def auto_restart_services(concurrency: int = 30, tier: str = "1"):
    """恢复后自动重启所有服务"""
    logger.info(f"自动重启服务: concurrency={concurrency} tier={tier}")

    # 不清空Redis——保留已有的pending/completed/failed
    services = [
        ("runner", f"pipe-runner", f".venv/bin/python3 xishujuzhen/solver_harness/pipe/runner.py --concurrency {concurrency}"),
        ("collector", "pipe-collector", f".venv/bin/python3 xishujuzhen/solver_harness/pipe/collector.py --poll-interval 10 --timeout 1800"),
        ("reporter", "pipe-reporter", f".venv/bin/python3 xishujuzhen/solver_harness/pipe/reporter.py --interval 60"),
    ]

    for name, session, cmd in services:
        try:
            subprocess.run(["tmux", "kill-session", "-t", session],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        except Exception:
            pass
        subprocess.run(["tmux", "new-session", "-d", "-s", session, cmd],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        logger.info(f"启动服务: {name} → tmux:{session}")
        time.sleep(1)

    # Feeder只在pending不足时才需要启动
    logger.info("Feeder需要手动启动（如果pending不足）")


def main():
    parser = argparse.ArgumentParser(description="断电恢复 + 僵尸清理")
    parser.add_argument("--dry-run", action="store_true", help="只检查不修改")
    parser.add_argument("--auto-restart", action="store_true", help="恢复后自动重启所有服务")
    parser.add_argument("--concurrency", type=int, default=30, help="自动重启时的并发数")
    parser.add_argument("--tier", type=str, default="1", help="自动重启时的tier")
    args = parser.parse_args()

    logger.info(f"=== 断电恢复开始 (dry_run={args.dry_run}) ===")

    if not ping():
        logger.error("Redis连接失败，无法恢复")
        sys.exit(1)
    logger.info("Redis连接成功")

    client = ArangoClient(hosts=DB_HOST, request_timeout=300)
    db = client.db(DB_NAME, username=DB_USER, password=DB_PASS)
    logger.info("ArangoDB连接成功")

    r = get_redis()

    # 1. 恢复running队列
    running_result = recover_running_queue(r, db, args.dry_run)

    # 2. 清理孤儿harness session
    orphan_result = recover_orphan_harness_sessions(r, db, args.dry_run)

    # 3. 修复ArangoDB中stale的attempt
    arango_result = recover_arango_attempts(db, args.dry_run)

    # 汇总
    print(f"\n=== 恢复结果汇总 ===")
    print(f"  Running队列: total={running_result['total_running']} alive={running_result['alive']} zombies={running_result['zombies']} recovered={running_result['recovered']}")
    print(f"  孤儿Session: total_harness={orphan_result['total_harness']} orphans={orphan_result['orphan_sessions']} killed={orphan_result['killed']}")
    print(f"  ArangoDB:    total_running_attempts={arango_result['total_running_attempts']} fixed={arango_result['fixed']}")

    if args.dry_run:
        print(f"\n  (DRY-RUN模式，未实际修改)")
    else:
        print(f"\n  恢复完成")

    if args.auto_restart and not args.dry_run:
        print(f"\n=== 自动重启服务 ===")
        auto_restart_services(args.concurrency, args.tier)

    logger.info(f"=== 断电恢复结束 ===")


if __name__ == "__main__":
    main()
