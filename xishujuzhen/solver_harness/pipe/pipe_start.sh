#!/bin/bash
# pipe_start.sh — 完整启动做题系统
#
# 用法:
#   bash pipe_start.sh              # 默认30并发
#   bash pipe_start.sh 60           # 60并发
#
# 这个脚本做6件事:
# 1. 前置检查（Redis/ArangoDB/D盘）
# 2. 恢复crash（清理zombie + 修复DB）
# 3. 设置并发数
# 4. 启动5个pipe-*服务（auto-restart模式）
# 5. 启动Monitor Pipe（持续监控+alert+AI review抽样）
# 6. 启动launchd watchdog（守护服务+定期清理zombie）
#
# 停止系统: bash pipe_stop.sh

set -e

export PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin

REPO=/Users/user/glm5.2-math-worktree
SOLVER_REPO=/Users/user/AI-Math-Normal-Solver
PY=$REPO/.venv/bin/python3
export PYTHONPATH=$SOLVER_REPO/xishujuzhen/solver_harness/pipe

CONCURRENCY=${1:-30}

echo "=========================================="
echo "  启动做题系统 (并发: $CONCURRENCY)"
echo "=========================================="

# === 1. 前置检查 ===
echo ""
echo "[1/5] 前置检查..."

# Redis
if ! docker ps | grep -q redis; then
    echo "  ❌ Redis容器未运行，启动中..."
    docker start redis-queue 2>/dev/null || docker run -d --name redis-queue -p 6379:6379 redis:7-alpine
    sleep 3
fi
if docker ps | grep -q redis; then
    echo "  ✅ Redis: 运行中"
else
    echo "  ❌ Redis启动失败，退出"
    exit 1
fi

# ArangoDB（返回401也算正常运行——只是需要认证）
if curl -s http://localhost:8529/_api/version | grep -qE "version|error"; then
    echo "  ✅ ArangoDB: 运行中"
else
    echo "  ❌ ArangoDB未运行，退出"
    exit 1
fi

# D盘
if [ -d /Volumes/data ]; then
    echo "  ✅ D盘: 已挂载"
else
    echo "  ❌ D盘未挂载，退出"
    exit 1
fi

# .env
if [ ! -f $REPO/.env ]; then
    echo "  ❌ .env文件不存在，退出"
    exit 1
fi
set -a; source $REPO/.env; set +a
if [ "$ARANGO_DB" != "xishujuzhen_math_glm52" ]; then
    echo "  ❌ ARANGO_DB=$ARANGO_DB (应为xishujuzhen_math_glm52)，退出"
    exit 1
fi
echo "  ✅ .env: ARANGO_DB=$ARANGO_DB"

# === 2. 恢复crash ===
echo ""
echo "[2/5] 恢复crash（清理zombie + 修复DB）..."
$PY $SOLVER_REPO/xishujuzhen/solver_harness/pipe/recover_from_crash.py 2>&1 | tail -8

# === 3. 设置并发数 + 记录启动时间 ===
echo ""
echo "[3/5] 设置并发数: $CONCURRENCY"
$PY -c "
from redis_queue import get_redis
import time
r = get_redis()
r.set('math:config:concurrency', $CONCURRENCY)
# 记录系统启动时间（Unix时间戳+ISO格式），供check_progress.py计算真实运行时长
start_ts = time.time()
r.set('math:system:start_time', start_ts)
from datetime import datetime, timezone
iso = datetime.fromtimestamp(start_ts, tz=timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
print(f'  ✅ 并发数已设置: {r.get(\"math:config:concurrency\")}')
print(f'  ✅ 系统启动时间已记录: {iso} (ts={start_ts})')
"

# === 4. 启动5个服务 ===
echo ""
echo "[4/5] 启动5个pipe-*服务..."

# 先杀掉可能残留的pipe-* session
for svc in pipe-feeder pipe-runner pipe-collector pipe-reporter pipe-retry pipe-monitor; do
    tmux kill-session -t $svc 2>/dev/null || true
done

tmux new-session -d -s pipe-feeder \
    "while true; do $PY $SOLVER_REPO/xishujuzhen/solver_harness/pipe/feeder.py --tier 1,2,3 --batch-size 500 --low-water-mark 1000 2>&1; echo '[auto-restart] feeder退出, 5秒后重启...'; sleep 5; done"
echo "  ✅ pipe-feeder (--tier 1,2,3 --low-water-mark 1000)"

tmux new-session -d -s pipe-runner \
    "while true; do PYTHONPATH=$PYTHONPATH $PY $SOLVER_REPO/xishujuzhen/solver_harness/pipe/runner.py --poll-interval 5 --print-mode 2>&1; echo '[auto-restart] runner退出, 5秒后重启...'; sleep 5; done"
echo "  ✅ pipe-runner (--print-mode)"

tmux new-session -d -s pipe-collector \
    "while true; do PYTHONPATH=$PYTHONPATH $PY $SOLVER_REPO/xishujuzhen/solver_harness/pipe/collector.py --poll-interval 10 --timeout 1800 --print-mode 2>&1; echo '[auto-restart] collector退出, 5秒后重启...'; sleep 5; done"
echo "  ✅ pipe-collector (--print-mode --timeout 1800)"

tmux new-session -d -s pipe-reporter \
    "while true; do PYTHONPATH=$PYTHONPATH $PY $SOLVER_REPO/xishujuzhen/solver_harness/pipe/reporter.py --interval 60 2>&1; echo '[auto-restart] reporter退出, 5秒后重启...'; sleep 5; done"
echo "  ✅ pipe-reporter (--interval 60)"

tmux new-session -d -s pipe-retry \
    "while true; do PYTHONPATH=$PYTHONPATH $PY $SOLVER_REPO/xishujuzhen/solver_harness/pipe/retry_infrastructure.py --max-retries 3 --interval 60 2>&1; echo '[auto-restart] retry退出, 5秒后重启...'; sleep 5; done"
echo "  ✅ pipe-retry (--max-retries 3)"

# === 5. 启动Monitor Pipe ===
echo ""
echo "[5/6] 启动Monitor Pipe..."
tmux new-session -d -s pipe-monitor \
    "while true; do PYTHONPATH=$PYTHONPATH $PY $SOLVER_REPO/xishujuzhen/solver_harness/pipe/monitor_pipe.py --interval 300 --concurrency $CONCURRENCY 2>&1; echo '[auto-restart] monitor退出, 5秒后重启...'; sleep 5; done"
echo "  ✅ pipe-monitor (interval=300s, concurrency=$CONCURRENCY)"
echo "     检查脚本: bash $SOLVER_REPO/xishujuzhen/solver_harness/pipe/scripts/monitor_check.sh"

# === 6. watchdog（已移除 launchd，不自动启动）===
# 2026-08-18: 用户决定 solver 系统不进入 launchd。
# 如需 watchdog，手动运行：bash $SOLVER_REPO/xishujuzhen/solver_harness/pipe/pipe_watchdog.sh
echo ""
echo "[6/6] watchdog: 已移除 launchd（solver系统不自动启动）"
echo "  如需手动运行: bash $SOLVER_REPO/xishujuzhen/solver_harness/pipe/pipe_watchdog.sh"

# === 完成 ===
echo ""
echo "=========================================="
echo "  启动完成!"
echo "=========================================="
echo ""
echo "  并发: $CONCURRENCY"
echo "  feeder: --tier 1,2,3 (246万题全自动)"
echo "  monitor: Monitor Pipe持续监控（8项自动检查+AI review抽样）"
echo "  watchdog: 守护5个服务 + 定期清理zombie"
echo ""
echo "  检查健康: PYTHONPATH=$PYTHONPATH $PY $SOLVER_REPO/xishujuzhen/solver_harness/pipe/pipe_control.py health"
echo "  Monitor检查: bash $SOLVER_REPO/xishujuzhen/solver_harness/pipe/scripts/monitor_check.sh"
echo "  停止系统: bash $SOLVER_REPO/xishujuzhen/solver_harness/pipe/pipe_stop.sh"
echo ""
