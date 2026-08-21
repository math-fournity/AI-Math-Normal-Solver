#!/bin/bash
# pipe_watchdog.sh — 管道化系统看门狗
# 每30秒检查5个pipe-*服务是否活着，死了就重启
# 每5分钟跑recover_from_crash.py清理zombie
#
# 由launchd plist启动（com.user.pipe-watchdog.plist）
# 也可手动运行：bash pipe_watchdog.sh

export PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin

set -a
source /Users/user/glm5.2-math-worktree/.env
set +a

REPO=/Users/user/glm5.2-math-worktree
SOLVER_REPO=/Users/user/AI-Math-Normal-Solver
PYTHONPATH=$SOLVER_REPO/xishujuzhen/solver_harness/pipe
PY=$REPO/.venv/bin/python3
LOG=/tmp/pipe-watchdog.log

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] [watchdog] $*" >> "$LOG"
}

ensure_service() {
    local name=$1
    local cmd=$2
    if ! tmux has-session -t "$name" 2>/dev/null; then
        log "$name 不存在，重启"
        tmux new-session -d -s "$name" "$cmd"
    fi
}

last_recover=0

while true; do
    # 检查5个服务
    ensure_service pipe-feeder \
        "while true; do $PY $SOLVER_REPO/xishujuzhen/solver_harness/pipe/feeder.py --tier 1,2,3 --batch-size 500 --low-water-mark 1000 2>&1; echo '[auto-restart] feeder退出, 5秒后重启...'; sleep 5; done"

    ensure_service pipe-runner \
        "while true; do PYTHONPATH=$PYTHONPATH $PY $SOLVER_REPO/xishujuzhen/solver_harness/pipe/runner.py --poll-interval 5 --print-mode 2>&1; echo '[auto-restart] runner退出, 5秒后重启...'; sleep 5; done"

    ensure_service pipe-collector \
        "while true; do PYTHONPATH=$PYTHONPATH $PY $SOLVER_REPO/xishujuzhen/solver_harness/pipe/collector.py --poll-interval 10 --timeout 1800 --print-mode 2>&1; echo '[auto-restart] collector退出, 5秒后重启...'; sleep 5; done"

    ensure_service pipe-reporter \
        "while true; do PYTHONPATH=$PYTHONPATH $PY $SOLVER_REPO/xishujuzhen/solver_harness/pipe/reporter.py --interval 60 2>&1; echo '[auto-restart] reporter退出, 5秒后重启...'; sleep 5; done"

    ensure_service pipe-retry \
        "while true; do PYTHONPATH=$PYTHONPATH $PY $SOLVER_REPO/xishujuzhen/solver_harness/pipe/retry_infrastructure.py --max-retries 3 --interval 60 2>&1; echo '[auto-restart] retry退出, 5秒后重启...'; sleep 5; done"

    # 每5分钟跑一次recover_from_crash清理zombie
    now=$(date +%s)
    if (( now - last_recover > 300 )); then
        log "定期清理zombie"
        PYTHONPATH=$PYTHONPATH $PY $SOLVER_REPO/xishujuzhen/solver_harness/pipe/recover_from_crash.py >> /tmp/pipe-recover.log 2>&1
        last_recover=$now
    fi

    sleep 30
done
