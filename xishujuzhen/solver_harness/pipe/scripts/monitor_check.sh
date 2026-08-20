#!/bin/bash
# monitor_check.sh — 解题系统Monitor Pipe标准化检查脚本
# 用法: ./scripts/monitor_check.sh [monitor_tmux_session] [interval_seconds]
# 输出5项检查结果: Monitor Pipe pane输出 / alerts / 进程状态 / 进度 / 系统深度审查结果
#
# 这个脚本参考错题分析系统的monitor_check.sh，适配解题系统（pipe/5服务）。
# 每个检查项后附带"需要检查"提示，提醒AI逐项检查而非只看数字。
# 最后一节"系统深度审查结果"是给AI的核心提醒——Monitor Pipe发现的异常需要AI判断。

set -uo pipefail

MONITOR_SESSION="${1:-pipe-monitor}"
INTERVAL="${2:-300}"
PY="/Users/user/glm5.2-math-worktree/.venv/bin/python3"
PROJ_ROOT="/Users/user/glm5.2-math-worktree"
PIPE_DIR="$PROJ_ROOT/xishujuzhen/solver_harness/pipe"

echo "============================================"
echo "解题系统Monitor Pipe标准化检查 @ $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
echo "  monitor_session: $MONITOR_SESSION"
echo "  interval: ${INTERVAL}s"
echo "============================================"

# --- 检查1: Monitor Pipe的tmux pane输出（最近轮次+ALERT+AI_REVIEW）---
echo ""
echo "=== 1. Monitor Pipe pane输出（最近5轮）==="
if tmux has-session -t "$MONITOR_SESSION" 2>/dev/null; then
    tmux capture-pane -t "$MONITOR_SESSION" -p -S -300 2>/dev/null | grep -E "监控轮次|ALERT|AI_REVIEW|queue|throughput|status|alerts|CRITICAL" | tail -30
else
    echo "  [ERROR] tmux session '$MONITOR_SESSION' 不存在！Monitor Pipe可能已退出。"
    echo "  恢复: cd $PIPE_DIR && $PY monitor_pipe.py --interval $INTERVAL --concurrency 20"
fi
echo ""
echo "  >> 需要检查："
echo "     - ★ 逐轮分析每个ALERT——不只是看有没有新ALERT，还要分析每轮ALERT的根因和处理结果"
echo "     - ★ 检查最新轮的内容——monitor_check.sh只取最近300行pane，最新轮可能被截断"
echo "     -   必要时直接运行: tmux capture-pane -t pipe-monitor -p -S -100"
echo "     - ★ 对比趋势——本轮vs上轮的alert数变化，判断根因修复是否生效"
echo "     - 每轮是否有新ALERT？alert类型是什么（critical/warning/info）？"
echo "     - AI_REVIEW抽样的2条结果——proof是否正确？是否有幻觉或答案泄漏？"
echo "     - throughput是否在推进？如果停滞，检查runner/collector日志"
echo "     - 轮次间隔是否正常（应约${INTERVAL}秒一轮）？如果间隔过长，Monitor Pipe可能卡住"
echo "     - queue的running数是否接近设定并发？running=0且pending>0 = runner可能挂了"
echo "     - CRITICAL级别的alert需要立即处理（pipe_service_dead/session_health critical）"

# --- 检查2: alerts集合（新alert）---
echo ""
echo "=== 2. alerts集合（新alert）==="
cd "$PROJ_ROOT"
set -a; source .env 2>/dev/null; set +a
PYTHONPATH="$PIPE_DIR" $PY "$PIPE_DIR/monitor_pipe.py" --check-alerts 2>&1
echo ""
echo "  >> 需要检查："
echo "     - rate_limit: 如果有，立即降并发（pipe_control.py concurrency <更低值>）"
echo "     - zombie_sessions: 空pane僵尸——等collector处理或sleep 60后自动消失（已修复根因）"
echo "     - orphan_cleaned: Monitor Pipe自动清理了孤儿记录（tmux已消失但Redis有记录）——无需处理"
echo "     - export_missing: 如果有，检查collector是否正确落盘export"
echo "     - solve_time_anomaly: 如果有，检查extract_solve_time.py逻辑"
echo "     - failure_rate: 检查failure_breakdown中哪个status占比最高"
echo "     - throughput_drop: 吞吐下降>50%时检查是否有rate limit或服务异常"
echo "     - pipe_service_dead: pipe-runner停止——检查是否手动停止或崩溃"
echo "     - long_running_tasks: 某些题运行>30分钟——检查是否stall，可能需手动kill"
echo "     - proof_too_small: export文件<1KB——proof内容可能不完整，检查AI输出质量"
echo "     - feeder_dead/feeder_stalled: feeder挂了或没在补充pending——重启feeder"
echo "     - collector_dead/collector_stalled: collector挂了或没在处理completed——重启collector"
echo "     - db_redis_inconsistency: Redis和DB的running数不一致——检查是否有并发写入问题"
echo "     - ai_review_sample: 检查抽样proof的数学正确性（这是AI的核心职责）"
echo "     - 处理完alert后用 --resolve-alert <key> 标记为fixed"

# --- 检查3: 进程状态（pipe服务 + monitor_pipe）---
echo ""
echo "=== 3. 进程状态 ==="
for svc in feeder runner collector reporter retry; do
    if tmux has-session -t "pipe-$svc" 2>/dev/null; then
        echo "  pipe-$svc: ✅ 运行中"
    else
        echo "  pipe-$svc: ❌ 未运行"
    fi
done

MONITOR_PID=$(pgrep -f "monitor_pipe.py" | head -1 || true)
if [ -n "$MONITOR_PID" ]; then
    ps -p "$MONITOR_PID" -o pid,pcpu,etime,stat 2>/dev/null | tail -1 | awk '{print "  monitor_pipe: PID="$1" CPU="$2"% ELAPSED="$3" STAT="$4}'
else
    echo "  monitor_pipe: [NOT RUNNING] — Monitor Pipe进程不存在"
fi

# 解题系统session数——只匹配harness-p{uuidhex}格式，不匹配其他AI的session
HARNESS_P_COUNT=$(tmux list-sessions 2>/dev/null | grep -E "^harness-p[a-f0-9]{20}" | wc -l | tr -d ' ')
HARNESS_DBMON_COUNT=$(tmux list-sessions 2>/dev/null | grep -E "^harness-dbmon-p[a-f0-9]{20}" | wc -l | tr -d ' ')
echo "  tmux harness-p: $HARNESS_P_COUNT"
echo "  tmux harness-dbmon: $HARNESS_DBMON_COUNT"
echo ""
echo "  >> 需要检查："
echo "     - 5个pipe服务是否都在运行？任何NOT RUNNING = 需要重启（pipe_control.py start）"
echo "     - monitor_pipe是否在运行？NOT RUNNING = 需要重启Monitor Pipe"
echo "     - harness-p数应≈并发数；为0可能是session刚完成正在启动下一个（正常）"
echo "     - harness-p持续为0 = runner可能挂了或feeder没feed"
echo "     - harness-dbmon应≈harness-p（每个solver session有一个dbmon）"

# --- 检查4: 进度（Redis队列 + DB状态分布 + 吞吐）---
echo ""
echo "=== 4. 进度 ==="
cd "$PROJ_ROOT"
PYTHONPATH="$PIPE_DIR" $PY -c "
import sys, os, json
sys.path.insert(0, '$PIPE_DIR')
from redis_queue import get_redis
r = get_redis()
pending = r.zcard('math:pending')
running = r.hlen('math:running')
completed = r.llen('math:completed')
failed = r.llen('math:failed')
conc = r.get('math:config:concurrency')
print(f'  Redis队列:')
print(f'    pending:   {pending}')
print(f'    running:   {running}')
print(f'    completed: {completed}')
print(f'    failed:    {failed}')
print(f'    concurrency: {conc}')
" 2>&1

# 最近5分钟吞吐
set -a; source .env 2>/dev/null; set +a
$PY -c "
import os
from datetime import datetime, timedelta, timezone
from arango import ArangoClient
c = ArangoClient(hosts=os.environ.get('ARANGO_HOST','http://localhost:8529'))
db = c.db(os.environ['ARANGO_DB'], username=os.environ.get('ARANGO_USER','root'), password=os.environ.get('ARANGO_PASS',''))
now = datetime.now(timezone.utc)
start = now - timedelta(minutes=5)
rows = list(db.aql.execute(
    'FOR r IN devin_problem_runs FILTER r.ended_at >= @s FILTER r.fixed_by == null '
    'COLLECT status = r.status WITH COUNT INTO cnt SORT cnt DESC RETURN {status, cnt}',
    bind_vars={'s': start.strftime('%Y-%m-%dT%H:%M')}))
total = sum(r['cnt'] for r in rows)
solved = sum(r['cnt'] for r in rows if r['status'] == 'candidate_solved')
rl = sum(r['cnt'] for r in rows if r['status'] == 'rate_limited')
print(f'  最近5分钟吞吐: {total}题 = {total*12}题/时')
if total: print(f'    solved: {solved} | rate_limited: {rl} | solved率: {solved/total*100:.0f}%')
for r in rows[:5]: print(f'    {r[\"status\"]:25s} {r[\"cnt\"]:>4}')
" 2>&1
echo ""
echo "  >> 需要检查："
echo "     - pending是否在减少？对比上次检查的pending数"
echo "     - running是否接近并发数？running远低于并发 = runner启动慢或有session异常退出"
echo "     - 吞吐是否正常？20并发预期约400-600题/时"
echo "     - rate_limited是否有新增？有则需降并发"
echo "     - solved率是否>60%？<60%可能是题目难度或AI状态问题"

# --- 检查5: 系统深度审查结果（Monitor Pipe的深度检查汇总）---
echo ""
echo "=== 5. 系统深度审查结果（Monitor Pipe深度检查）==="
cd "$PROJ_ROOT"
PYTHONPATH="$PIPE_DIR" $PY -c "
import sys, os, json
sys.path.insert(0, '$PIPE_DIR')
from monitor_pipe import _connect_db, MONITOR_ALERTS_COLLECTION
db = _connect_db()
# 按severity统计所有alert
aql = f'FOR a IN {MONITOR_ALERTS_COLLECTION} COLLECT severity = a.severity WITH COUNT INTO c RETURN {{severity, count: c}}'
rows = list(db.aql.execute(aql, ttl=60))
print(f'  Alert总数（按severity）:')
for r in rows:
    print(f'    {r[\"severity\"]:10s} {r[\"count\"]:>4}')

# 按alert_type统计新alert
aql2 = f'FOR a IN {MONITOR_ALERTS_COLLECTION} FILTER a.status == \"new\" COLLECT type = a.alert_type WITH COUNT INTO c SORT c DESC RETURN {{type, count: c}}'
rows2 = list(db.aql.execute(aql2, ttl=60))
if rows2:
    print(f'  新alert（按type）:')
    for r in rows2:
        print(f'    {r[\"type\"]:25s} {r[\"count\"]:>4}')
else:
    print(f'  新alert: 0个 ✅')

# 最近24小时的alert趋势
from datetime import datetime, timezone, timedelta
now = datetime.now(timezone.utc)
start = now - timedelta(hours=24)
aql3 = f'FOR a IN {MONITOR_ALERTS_COLLECTION} FILTER a.created_at >= @s COLLECT type = a.alert_type, severity = a.severity WITH COUNT INTO c SORT c DESC RETURN {{type, severity, count: c}}'
rows3 = list(db.aql.execute(aql3, bind_vars={'s': start.isoformat()}, ttl=60))
if rows3:
    print(f'  最近24小时alert趋势:')
    for r in rows3[:10]:
        print(f'    [{r[\"severity\"]:8s}] {r[\"type\"]:25s} {r[\"count\"]:>4}')
" 2>&1
echo ""
echo "  >> 需要检查（AI核心提醒）："
echo "     ★ 这是Monitor Pipe的系统深度审查结果——AI必须逐项判断每个alert的严重性和处理方案"
echo "     ★ critical级别的alert需要立即处理："
echo "       - pipe_service_dead → 检查pipe服务是否崩溃，重启pipe_control.py start"
echo "       - session_health critical → runner挂了或feeder没feed，检查对应服务日志"
echo "       - rate_limit critical → 立即降并发（pipe_control.py concurrency <更低值>）"
echo "       - queue_stalled → 检查collector是否在处理completed队列"
echo "       - feeder_dead → pipe-feeder挂了，重启feeder"
echo "       - collector_dead → pipe-collector挂了，重启collector"
echo "     ★ warning级别的alert需要评估是否需要处理："
echo "       - zombie_sessions → 等watchdog清理或手动kill"
echo "       - failure_rate → 检查failure_breakdown，判断是AI能力问题还是基础设施问题"
echo "       - throughput_drop → 对比历史吞吐，判断是正常波动还是异常"
echo "       - solve_time_anomaly → 检查extract_solve_time.py逻辑是否有bug"
echo "       - long_running_tasks → 某些题运行>30分钟，检查是否stall，可能需手动kill"
echo "       - proof_too_small → export文件<1KB，proof内容可能不完整，检查AI输出质量"
echo "       - feeder_stalled → feeder没在补充pending，检查feeder日志"
echo "       - collector_stalled → collector没在处理completed，检查collector日志"
echo "       - db_redis_inconsistency → Redis和DB的running数不一致，检查并发写入问题"
echo "     ★ info级别的alert是AI review抽样——AI必须检查抽样的proof质量"
echo "     ★ 处理完alert后用 monitor_pipe.py --resolve-alert <key> 标记为fixed"
echo "     ★ 如果同一类型alert反复出现，说明根因未解决——需要切回系统开发者修复代码"

echo ""
echo "============================================"
echo "检查完成 @ $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
echo ""
echo ">> 行动清单（按顺序执行）："
echo "   1. 仔细阅读第1项Monitor Pipe pane输出中的每轮ALERT和AI_REVIEW"
echo "   2. 有新alert时逐个recheck（第2项），处理完用--resolve-alert标记"
echo "   3. pipe服务NOT RUNNING时重启（第3项）：pipe_control.py start --concurrency 20"
echo "   4. monitor_pipe NOT RUNNING时重启：tmux new -d -s pipe-monitor 'cd $PIPE_DIR && $PY monitor_pipe.py --interval $INTERVAL --concurrency 20'"
echo "   5. 吞吐下降或rate_limited新增时降并发（第4项）"
echo "   6. ★ 系统深度审查结果（第5项）——AI必须逐项判断每个alert的处理方案"
echo "   7. ★ AI_REVIEW抽样的proof必须由AI检查数学正确性（这是AI的核心职责，不是脚本能做的）"
echo "============================================"
