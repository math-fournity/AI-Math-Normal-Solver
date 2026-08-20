#!/bin/bash
# pipe_stop.sh — 完整停止做题系统
#
# 用法:
#   bash pipe_stop.sh              # 优雅停止（默认）：停feeder/runner，保留collector等running题自然完成
#   bash pipe_stop.sh --finish     # 收尾停止：running题都完成后，停collector等剩余服务
#   bash pipe_stop.sh --force      # 立即停止所有服务（保留harness session，它们自然完成）
#   bash pipe_stop.sh --kill       # 立即停止并kill所有harness session（强制中断所有正在做的题）
#
# 优雅停止的两步流程：
#   第1步：bash pipe_stop.sh           → 停feeder/runner/reporter/retry/monitor，保留collector
#                                        collector继续判定正在做的题的终态
#   第2步：bash pipe_stop.sh --finish   → 等running=0后，停collector+watchdog
#
# 为什么不能一次性停所有服务：
#   collector被停后，正在做的题完成后没有服务判定终态，status卡在running
#   直到下次启动时recover_from_crash.py处理——这不是优雅停止

set -e

export PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin

REPO=/Users/user/glm5.2-math-worktree
PY=$REPO/.venv/bin/python3
export PYTHONPATH=$REPO/xishujuzhen/solver_harness/pipe

MODE="${1:-graceful}"

echo "=========================================="
echo "  停止做题系统 (模式: $MODE)"
echo "=========================================="

# 加载环境变量
set -a; source $REPO/.env 2>/dev/null; set +a

if [ "$MODE" = "--kill" ]; then
    # === 强制kill模式：停所有服务+kill所有harness session ===
    echo ""
    echo "[1/3] 停止launchd watchdog..."
    PLIST=~/Library/LaunchAgents/com.user.pipe-watchdog.plist
    launchctl unload $PLIST 2>/dev/null || true
    # 永久禁用——即使系统重启也不会自动加载
    launchctl disable "gui/$(id -u)/com.user.pipe-watchdog" 2>/dev/null || true
    # 同时kill手动运行的watchdog进程（不是通过launchd启动的）
    pkill -f "pipe_watchdog.sh" 2>/dev/null || true
    echo "  ✅ watchdog已停止（unload + disable + kill进程）"

    echo ""
    echo "[2/3] 停止6个pipe-*服务..."
    for svc in pipe-feeder pipe-runner pipe-collector pipe-reporter pipe-retry pipe-monitor; do
        if tmux has-session -t $svc 2>/dev/null; then
            tmux send-keys -t $svc C-c ""
            sleep 2
            tmux kill-session -t $svc 2>/dev/null || true
            echo "  ✅ $svc 已停止"
        fi
    done

    echo ""
    echo "[3/3] kill解题系统harness session（harness-p{uuid}格式）..."
    $PY $REPO/xishujuzhen/solver_harness/pipe/pipe_control.py stop --kill-harness 2>&1 | grep -E "killed|停止|服务" || true
    # 只kill解题系统的session（harness-p{uuidhex}格式），不kill其他AI的session
    for s in $(tmux list-sessions 2>/dev/null | grep -E "^harness-(p|dbmon-p)[a-f0-9]{20}" | awk -F: '{print $1}'); do
        tmux kill-session -t "$s" 2>/dev/null || true
    done
    echo "  ✅ 解题系统harness session已kill"

elif [ "$MODE" = "--finish" ]; then
    # === 收尾模式：running题都完成后，停collector等剩余服务 ===
    echo ""
    echo "检查running队列..."

    RUNNING=$($PY -c "
from redis_queue import get_redis
r = get_redis()
print(r.hlen('math:running'))
" 2>/dev/null || echo "unknown")

    if [ "$RUNNING" != "0" ]; then
        echo "  ⚠️ running=$RUNNING，还有题在做！"
        echo "  请等running=0后再执行 --finish"
        echo "  或用 --force 强制停止（保留harness session自然完成）"
        echo "  或用 --kill 强制kill所有harness session"
        exit 1
    fi

    echo "  ✅ running=0，所有题已完成"

    echo ""
    echo "[1/2] 停止launchd watchdog..."
    PLIST=~/Library/LaunchAgents/com.user.pipe-watchdog.plist
    launchctl unload $PLIST 2>/dev/null || true
    # 永久禁用——即使系统重启也不会自动加载
    launchctl disable "gui/$(id -u)/com.user.pipe-watchdog" 2>/dev/null || true
    # 同时kill手动运行的watchdog进程
    pkill -f "pipe_watchdog.sh" 2>/dev/null || true
    echo "  ✅ watchdog已停止（unload + disable + kill进程）"

    echo ""
    echo "[2/2] 停止collector等剩余服务..."
    for svc in pipe-collector pipe-reporter pipe-retry pipe-monitor; do
        if tmux has-session -t $svc 2>/dev/null; then
            tmux send-keys -t $svc C-c ""
            sleep 2
            tmux kill-session -t $svc 2>/dev/null || true
            echo "  ✅ $svc 已停止"
        fi
    done

    echo ""
    echo "=========================================="
    echo "  收尾完成! 所有服务已停止"
    echo "=========================================="
    echo ""
    echo "  恢复: bash $REPO/xishujuzhen/solver_harness/pipe/pipe_start.sh"
    echo ""
    exit 0

elif [ "$MODE" = "--force" ]; then
    # === 立即停止模式：停所有服务，保留harness session自然完成 ===
    echo ""
    echo "[1/3] 停止launchd watchdog..."
    PLIST=~/Library/LaunchAgents/com.user.pipe-watchdog.plist
    launchctl unload $PLIST 2>/dev/null || true
    # 永久禁用——即使系统重启也不会自动加载
    launchctl disable "gui/$(id -u)/com.user.pipe-watchdog" 2>/dev/null || true
    # 同时kill手动运行的watchdog进程
    pkill -f "pipe_watchdog.sh" 2>/dev/null || true
    echo "  ✅ watchdog已停止（unload + disable + kill进程）"

    echo ""
    echo "[2/3] 停止6个pipe-*服务（含collector）..."
    for svc in pipe-feeder pipe-runner pipe-collector pipe-reporter pipe-retry pipe-monitor; do
        if tmux has-session -t $svc 2>/dev/null; then
            tmux send-keys -t $svc C-c ""
            sleep 2
            tmux kill-session -t $svc 2>/dev/null || true
            echo "  ✅ $svc 已停止"
        fi
    done

    echo ""
    echo "[3/3] 保留harness session（它们会自然完成当前题，但collector已停，终态需下次启动时recover处理）"
    echo "  如需kill: bash pipe_stop.sh --kill"

else
    # === 默认：优雅停止模式 ===
    # 停feeder/runner/reporter/retry/monitor，保留collector继续判定终态
    # 不停watchdog——watchdog守护collector

    echo ""
    echo "[1/2] 停止feeder/runner/reporter/retry/monitor（保留collector）..."
    for svc in pipe-feeder pipe-runner pipe-reporter pipe-retry pipe-monitor; do
        if tmux has-session -t $svc 2>/dev/null; then
            tmux send-keys -t $svc C-c ""
            sleep 2
            tmux kill-session -t $svc 2>/dev/null || true
            echo "  ✅ $svc 已停止"
        fi
    done

    # 检查running数
    RUNNING=$($PY -c "
from redis_queue import get_redis
r = get_redis()
print(r.hlen('math:running'))
" 2>/dev/null || echo "unknown")

    echo ""
    echo "[2/2] collector继续运行，等running题自然完成..."
    echo "  当前running: $RUNNING"
    echo "  collector会继续判定正在做的题的终态"
    echo "  watchdog也继续运行——守护collector"
    echo ""
    echo "  ★ 等running=0后，执行收尾（停collector+watchdog）："
    echo "    bash $REPO/xishujuzhen/solver_harness/pipe/pipe_stop.sh --finish"
    echo ""
    echo "  ★ 如需立即停止（collector+watchdog也停，终态下次启动时recover处理）："
    echo "    bash $REPO/xishujuzhen/solver_harness/pipe/pipe_stop.sh --force"
    echo ""
    echo "  ★ 如需强制kill所有harness session："
    echo "    bash $REPO/xishujuzhen/solver_harness/pipe/pipe_stop.sh --kill"
    echo ""
    echo "  ⚠️ 注意：--finish和--force和--kill都会自动停止watchdog（unload plist + kill进程）"
    echo "     如果只执行了默认模式（本模式），watchdog仍在运行——这是设计意图（守护collector）"
    echo "     但如果不再需要collector，请务必执行--finish或--force来停watchdog"
fi

echo ""
echo "=========================================="
echo "  停止完成!"
echo "=========================================="
echo ""
echo "  恢复: bash $REPO/xishujuzhen/solver_harness/pipe/pipe_start.sh"
echo ""
