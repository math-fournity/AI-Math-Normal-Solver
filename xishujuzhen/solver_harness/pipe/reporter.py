#!/usr/bin/env python3
"""reporter.py — 服务4：统计报告

定时从Redis读统计，输出进度报告和告警。
独立进程，轻量级。

用法:
  python reporter.py --interval 60
"""
import sys
import os
import time
import argparse

sys.path.insert(0, os.path.dirname(__file__))

from redis_queue import (
    get_redis, pending_count, running_count, completed_count, failed_count,
    update_stats, get_stats, ping,
)
from shared_logger import get_logger
from graceful_shutdown import register_shutdown, should_stop

logger = get_logger("reporter")

LOG_DIR = "/Volumes/data/math-agent-glm5.2-tmux-agents-trajectory/_pipe/logs"


def main():
    parser = argparse.ArgumentParser(description="Reporter: 统计报告")
    parser.add_argument("--interval", type=int, default=60, help="报告间隔秒数")
    args = parser.parse_args()

    if not ping():
        logger.error("Redis连接失败")
        sys.exit(1)
    logger.info(f"Redis连接成功, interval={args.interval}s")

    os.makedirs(LOG_DIR, exist_ok=True)
    log_file = os.path.join(LOG_DIR, "reporter.log")

    r = get_redis()
    start_time = time.time()
    prev_stats = {"pending": None, "running": None, "completed": None, "failed": None}

    # 注册优雅退出
    register_shutdown("reporter")

    while True:
        # 检查优雅退出
        if should_stop():
            logger.info("Reporter优雅退出")
            break

        try:
            update_stats(r)
            pending = pending_count(r)
            running = running_count(r)
            completed = completed_count(r)
            failed = failed_count(r)
            total = pending + running + completed + failed

            elapsed = time.time() - start_time
            if completed + failed > 0:
                success_rate = completed / (completed + failed) * 100
            else:
                success_rate = 0

            # 估算完成时间
            done = completed + failed
            if done > 0 and elapsed > 0:
                rate = done / elapsed  # 题/秒
                if rate > 0 and pending > 0:
                    eta_seconds = pending / rate
                    eta_str = f"{eta_seconds/3600:.1f}h"
                else:
                    eta_str = "N/A"
            else:
                rate = 0
                eta_str = "N/A"

            # 数值变化检测（DEBUG级）
            changes = []
            for name, val in [("pending", pending), ("running", running), ("completed", completed), ("failed", failed)]:
                prev = prev_stats[name]
                if prev is not None and val != prev:
                    changes.append(f"{name}: {prev}->{val} (delta={val-prev:+d})")
                prev_stats[name] = val
            if changes:
                logger.debug(f"数值变化: {'; '.join(changes)}")

            logger.debug(f"吞吐量计算: done={done} elapsed={elapsed:.1f}s rate={rate:.4f}题/秒 pending={pending} eta={eta_str}")

            report = (
                f"{'='*60}\n"
                f"[reporter] {time.strftime('%H:%M:%S')} (运行 {elapsed/3600:.1f}h)\n"
                f"  pending:   {pending:>8}\n"
                f"  running:   {running:>8}\n"
                f"  completed: {completed:>8}\n"
                f"  failed:    {failed:>8}\n"
                f"  total:     {total:>8}\n"
                f"  success率: {success_rate:.1f}%\n"
                f"  完成速率:  {rate:.2f} 题/秒 ({done}/{int(elapsed)}s)\n"
                f"  ETA:       {eta_str}\n"
                f"{'='*60}"
            )

            logger.info(f"统计报告: pending={pending} running={running} completed={completed} failed={failed} total={total} success率={success_rate:.1f}% 速率={rate:.2f}题/秒 ETA={eta_str} 运行={elapsed/3600:.1f}h")

            # 写日志
            with open(log_file, "a") as f:
                f.write(report + "\n")

            # 告警检测
            if running == 0 and pending > 0:
                logger.warning(f"告警触发: running=0 但pending={pending}, Runner可能挂了 (条件: running==0 and pending>0)")
            if failed > 0 and success_rate < 50 and done > 10:
                logger.warning(f"告警触发: 失败率>{100-success_rate:.0f}% (failed={failed} success_rate={success_rate:.1f}% done={done}>10), 检查连接 (条件: failed>0 and success_rate<50 and done>10)")

        except Exception as e:
            logger.error(f"Reporter轮次失败: {e}", exc_info=True)

        time.sleep(args.interval)


if __name__ == "__main__":
    main()
