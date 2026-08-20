#!/usr/bin/env python3
"""concurrency_safety_check.py — 管道化系统并发安全性专项检查

检查多进程同时操作共享资源时的竞态条件和资源泄漏。
pipe_control.py health检查运行时健康，本脚本检查并发安全性。

检查维度：
  A. Redis操作原子性——dequeue+add_running间隙的丢题风险
  B. DB-Redis一致性——DB running数 vs Redis running数，孤儿记录
  C. 重复attempt检测——同一problem_key被多个attempt同时running
  D. 并发上限实际验证——设定并发 vs Redis running vs harness session数
  E. devin进程数 vs 并发数——僵尸devin进程堆积
  F. collector处理速率 vs runner启动速率——速率比，running是否堆积
  G. auto-restart验证——服务退出后是否真的5秒内重启
  H. 日志重复写入——tee -a + RotatingFileHandler导致每行写两遍
  I. 网络连接健康度——API可达性、HTTP错误率、rate limit
  J. devin异常退出率——dead_session占比、solved率、趋势

用法:
  cd /Users/user/glm5.2-math-worktree
  set -a; source .env; set +a
  PYTHONPATH=xishujuzhen/solver_harness/pipe .venv/bin/python3 \\
    xishujuzhen/solver_harness/pipe/scripts/concurrency_safety_check.py
"""
import json
import os
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from redis_queue import get_redis, get_all_running, running_count, pending_count

from arango import ArangoClient

TRAJ = Path("/Volumes/data/math-agent-glm5.2-tmux-agents-trajectory")
LOG_DIR = TRAJ / "_pipe" / "logs"


def check_a_redis_atomicity(r):
    """A. Redis操作原子性——检查dequeue+add_running间隙的丢题风险"""
    print("【A. Redis操作原子性】")
    # zpopmin是原子的，但dequeue_pending→add_running之间crash会丢题
    # 检查方法：看pending中是否有刚被dequeue但没add_running的题
    # 间接检查：running中的attempt是否都在DB中有对应记录
    print("  zpopmin是原子操作 ✅")
    print("  dequeue→add_running间隙crash会丢题（已知风险，auto-restart兜底）")
    # 检查running中是否有attempt_key为空的记录（dequeue后没来得及写DB）
    running = get_all_running(r)
    no_attempt_key = sum(1 for v in running.values() if not v.get("attempt_key"))
    print(f"  running中attempt_key为空: {no_attempt_key}", "❌" if no_attempt_key else "✅")
    return no_attempt_key


def check_b_db_redis_consistency(db, r):
    """B. DB-Redis一致性——DB running数 vs Redis running数"""
    print("【B. DB-Redis一致性】")
    redis_running = r.hlen("math:running")
    redis_running_keys = set(r.hkeys("math:running"))

    db_running = list(db.aql.execute('''
        FOR r IN devin_problem_runs
            FILTER r.exp_id LIKE "p%"
            FILTER r.status == "running"
            RETURN r.exp_id
    '''))
    db_running_set = set(db_running)

    print(f"  Redis running: {redis_running}")
    print(f"  DB running:    {len(db_running)}")

    # Redis有但DB没有——runner还没来得及写DB
    redis_not_db = redis_running_keys - db_running_set
    # DB有但Redis没有——孤儿记录（collector先remove_running后update_db_status的旧bug）
    db_not_redis = db_running_set - redis_running_keys

    print(f"  Redis有DB无: {len(redis_not_db)}", "⚠️" if redis_not_db else "✅", "(runner正在写DB)")
    print(f"  DB有Redis无(孤儿): {len(db_not_redis)}", "❌" if db_not_redis else "✅")
    if db_not_redis:
        print(f"    孤儿示例: {list(db_not_redis)[:3]}")
    return len(db_not_redis)


def check_c_duplicate_attempt(r):
    """C. 重复attempt检测——同一problem_key被多个attempt同时running"""
    print("【C. 重复attempt检测】")
    running = get_all_running(r)
    pks = [v.get("problem_key", "") for v in running.values()]
    dupes = {k: v for k, v in Counter(pks).items() if v > 1}
    print(f"  running中attempt数: {len(pks)}")
    print(f"  唯一problem_key数: {len(set(pks))}")
    print(f"  重复problem_key: {len(dupes)}", "❌" if dupes else "✅")
    if dupes:
        for pk, cnt in dupes.items():
            print(f"    {pk}: {cnt}次")
    return len(dupes)


def check_d_concurrency_limit(r):
    """D. 并发上限实际验证——设定并发 vs Redis running vs harness session数"""
    print("【D. 并发上限实际验证】")
    conc_raw = r.get("math:config:concurrency")
    conc = int(conc_raw) if conc_raw else 0
    redis_running = r.hlen("math:running")

    result = subprocess.run(["tmux", "list-sessions"], capture_output=True, text=True)
    # 只统计解题系统的session（harness-p{uuidhex}格式），不统计其他AI的session
    import re
    pipe_p_pattern = re.compile(r'^harness-p[a-f0-9]{20}')
    pipe_dbmon_pattern = re.compile(r'^harness-dbmon-p[a-f0-9]{20}')
    harness_p = sum(1 for line in result.stdout.split("\n") if pipe_p_pattern.match(line.split(":")[0].strip()))
    harness_dbmon = sum(1 for line in result.stdout.split("\n") if pipe_dbmon_pattern.match(line.split(":")[0].strip()))

    print(f"  设定并发:        {conc}")
    print(f"  Redis running:   {redis_running}")
    print(f"  harness-p:       {harness_p}")
    print(f"  harness-dbmon:   {harness_dbmon}")

    # running不应超过并发+5（允许少量超出，runner取题到add_running有间隙）
    over_limit = redis_running > conc + 5
    print(f"  running超并发:   {'❌' if over_limit else '✅'} (允许+5)")
    return 1 if over_limit else 0


def check_e_devin_process_count():
    """E. devin进程数 vs 并发数——僵尸devin进程堆积"""
    print("【E. devin进程数 vs 并发数】")
    result = subprocess.run(["ps", "aux"], capture_output=True, text=True)
    devin_procs = [line for line in result.stdout.split("\n") if "devin" in line and "grep" not in line and "concurrency_safety" not in line]
    # 过滤掉devin cli自身的管理进程（只看node进程）
    node_procs = [p for p in devin_procs if "node" in p]

    print(f"  devin相关进程: {len(devin_procs)}")
    print(f"  其中node进程:  {len(node_procs)}")

    # 正常情况：每个并发1个devin cli进程 + 1个node子进程
    # 僵尸进程 = 总进程数 - 2*并发数
    return len(devin_procs)


def check_f_rate_balance(r):
    """F. collector处理速率 vs runner启动速率"""
    print("【F. collector处理速率 vs runner启动速率】")
    # 从collector日志中提取最近几轮的completed/failed数
    collector_log = LOG_DIR / "collector.log"
    if not collector_log.exists():
        print("  collector.log不存在")
        return 0

    import re
    log_text = collector_log.read_text(errors="ignore")
    rounds = re.findall(r"第(\d+)轮扫描结束: 本轮 completed=(\d+) failed=(\d+) infra=(\d+)", log_text)
    if len(rounds) < 2:
        print(f"  collector日志中只有{len(rounds)}轮，不足以计算速率")
        return 0

    recent_rounds = rounds[-5:] if len(rounds) >= 5 else rounds
    total_processed = sum(int(c) + int(f) + int(i) for _, c, f, i in recent_rounds)
    avg_per_round = total_processed / len(recent_rounds)
    print(f"  最近{len(recent_rounds)}轮平均每轮处理: {avg_per_round:.1f}题")

    # runner每3秒启动1个，collector每poll_interval秒扫一轮
    # 如果collector每轮处理 < runner每轮启动，running会堆积
    print(f"  runner每3秒启动1题 = 每分钟20题")
    print(f"  collector每轮处理{avg_per_round:.1f}题")
    if avg_per_round < 20:
        print(f"  ⚠️ collector速率可能跟不上runner（每轮{avg_per_round:.1f} < 每分钟20）")
        return 1
    else:
        print(f"  ✅ collector速率跟上runner")
        return 0


def check_g_auto_restart():
    """G. auto-restart验证——服务是否在auto-restart模式下运行

    检查方法：看tmux session的主进程是否是bash while循环（auto-restart），
    而不是直接python进程。用ps检查session对应的进程树。
    """
    print("【G. auto-restart验证】")
    result = subprocess.run(["tmux", "list-sessions"], capture_output=True, text=True)
    services = ["feeder", "runner", "collector", "reporter", "retry"]
    issues = 0
    for svc in services:
        session_name = f"pipe-{svc}"
        if session_name not in result.stdout:
            print(f"  {svc}: ❌ 未运行")
            issues += 1
            continue

        # 检查tmux session的pane进程——auto-restart模式下主进程是zsh/bash（while循环）
        # 非auto-restart模式下主进程是python
        pane_pid_result = subprocess.run(
            ["tmux", "list-panes", "-t", session_name, "-F", "#{pane_pid}"],
            capture_output=True, text=True
        )
        pane_pid = pane_pid_result.stdout.strip().split("\n")[0] if pane_pid_result.stdout.strip() else ""

        # 检查pane_pid的子进程是否包含while循环
        # 用ps看pane_pid的完整进程树
        ps_result = subprocess.run(
            ["ps", "-o", "pid,ppid,command", "-p", pane_pid],
            capture_output=True, text=True
        )
        # 看pane_pid的子进程
        children = subprocess.run(
            ["pgrep", "-P", pane_pid],
            capture_output=True, text=True
        ).stdout.strip().split("\n") if pane_pid else []

        # 检查子进程中是否有python（说明while循环在运行python）
        # 或检查pane_pid的command是否包含while
        has_auto_restart = False
        if pane_pid:
            # 看pane进程的命令行
            cmd_result = subprocess.run(
                ["ps", "-o", "command=", "-p", pane_pid],
                capture_output=True, text=True
            )
            cmd = cmd_result.stdout.strip()
            if "while true" in cmd or "auto-restart" in cmd:
                has_auto_restart = True
            # 检查子进程链——auto-restart的while循环会fork出zsh→python
            if not has_auto_restart and children and children[0]:
                for child_pid in children:
                    if not child_pid:
                        continue
                    child_cmd = subprocess.run(
                        ["ps", "-o", "command=", "-p", child_pid],
                        capture_output=True, text=True
                    ).stdout.strip()
                    if "while true" in child_cmd or "auto-restart" in child_cmd:
                        has_auto_restart = True
                        break

        print(f"  {svc}: {'✅ auto-restart' if has_auto_restart else '⚠️ 无auto-restart（手动启动）'}")
        if not has_auto_restart:
            issues += 1
    return issues


def check_h_log_duplication():
    """H. 日志重复写入——tee -a + RotatingFileHandler导致每行写两遍"""
    print("【H. 日志重复写入】")
    collector_log = LOG_DIR / "collector.log"
    if not collector_log.exists():
        print("  collector.log不存在")
        return 0

    lines = collector_log.read_text(errors="ignore").strip().split("\n")
    if len(lines) < 10:
        print(f"  日志行数太少({len(lines)})，无法判断")
        return 0

    # 检查最近20行中是否有连续重复行
    recent = lines[-20:]
    dupes = sum(1 for i in range(1, len(recent)) if recent[i] == recent[i-1])
    dupe_rate = dupes / len(recent) * 100
    print(f"  最近20行中连续重复: {dupes}行 ({dupe_rate:.0f}%)")
    if dupe_rate > 50:
        print(f"  ⚠️ 日志重复写入严重（tee -a + RotatingFileHandler双写）")
        return 1
    elif dupe_rate > 10:
        print(f"  ⚠️ 日志有少量重复")
        return 0
    else:
        print(f"  ✅ 日志无重复")
        return 0


def check_i_network_health(r):
    """I. 网络连接健康度——API可达性、TCP连接数、HTTP错误率、rate limit

    检查方法：
    1. 从running的devin进程提取实际连接的远端IP，测试连通性
    2. 统计所有devin进程的ESTABLISHED TCP连接数，对比并发数
    3. 从collector日志统计最近网络错误数
    4. 从failed队列统计网络相关失败占比
    """
    print("【I. 网络连接健康度】")

    issues = 0

    # 1. 从running的devin进程提取实际连接的远端IP
    ps_result = subprocess.run(["ps", "aux"], capture_output=True, text=True)
    devin_pids = [line.split()[1] for line in ps_result.stdout.split("\n")
                  if "devin" in line and "grep" not in line and "concurrency_safety" not in line
                  and line.strip()]

    # 收集所有devin进程的TCP连接
    established_conns = 0
    remote_ips = set()
    for pid in devin_pids:
        lsof = subprocess.run(
            ["lsof", "-i", "-a", "-p", pid],
            capture_output=True, text=True, timeout=5
        )
        for line in lsof.stdout.split("\n"):
            if "ESTABLISHED" in line:
                established_conns += 1
                # 提取远端地址
                parts = line.split("->")
                if len(parts) > 1:
                    remote = parts[1].split(":")[0].strip()
                    remote_ips.add(remote)

    conc_raw = r.get("math:config:concurrency")
    conc = int(conc_raw) if conc_raw else 0
    # 每个并发约1-2个TCP连接（API + WebSocket）
    expected_conns = conc * 2
    print(f"  devin进程TCP连接数: {established_conns} (共{len(devin_pids)}个进程)")
    print(f"  预期连接数: ~{expected_conns} (并发{conc}×2)")
    if established_conns < conc * 0.5:
        print(f"  ❌ TCP连接数远低于并发数，网络可能断连")
        issues += 1
    elif established_conns < conc:
        print(f"  ⚠️ TCP连接数低于并发数")
    else:
        print(f"  ✅ TCP连接数正常")

    # 2. 测试所有远端IP的连通性（只要有一个可达就算正常）
    if remote_ips:
        reachable_ips = []
        unreachable_ips = []
        for ip in remote_ips:
            curl = subprocess.run(
                ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code} %{time_total}",
                 "--max-time", "10", "-k", f"https://{ip}"],
                capture_output=True, text=True, timeout=15
            )
            parts = curl.stdout.strip().split()
            http_code = parts[0] if parts else "000"
            latency = float(parts[1]) if len(parts) > 1 else 0
            # HTTP 404/401/403都说明TCP+TLS可达（只是没有对应路由）
            reachable = http_code in ("200", "404", "401", "403", "405")
            if reachable:
                reachable_ips.append((ip, http_code, latency))
            else:
                unreachable_ips.append((ip, http_code, latency))

        for ip, code, lat in reachable_ips:
            print(f"  ✅ {ip} (HTTP {code}, {lat:.2f}s)")
        for ip, code, lat in unreachable_ips:
            print(f"  ❌ {ip} (HTTP {code}, {lat:.2f}s)")

        if reachable_ips:
            print(f"  API endpoint连通性: ✅ {len(reachable_ips)}/{len(remote_ips)}个IP可达")
        else:
            print(f"  API endpoint连通性: ❌ 所有{len(remote_ips)}个IP不可达")
            issues += 1
    else:
        print(f"  ⚠️ 未检测到devin进程的TCP连接")

    # 3. 从collector日志统计最近网络错误
    collector_log = LOG_DIR / "collector.log"
    network_errors = 0
    rate_limits = 0
    if collector_log.exists():
        log_text = collector_log.read_text(errors="ignore")
        recent_lines = log_text.split("\n")[-500:]
        network_patterns = [
            "connection error", "econnrefused", "econnreset", "socket hang up",
            "fetch failed", "network error", "network request failed", "etimedout",
            "unavailable", "errorkind",
        ]
        rate_limit_patterns = ["message rate limit", "http 429", "status 429", "error 429", "too many requests"]
        for line in recent_lines:
            line_lower = line.lower()
            if any(p in line_lower for p in network_patterns):
                network_errors += 1
            if any(p in line_lower for p in rate_limit_patterns):
                rate_limits += 1

    print(f"  collector最近500行中网络错误: {network_errors}", "❌" if network_errors > 5 else ("⚠️" if network_errors > 0 else "✅"))
    print(f"  collector最近500行中rate limit: {rate_limits}", "❌" if rate_limits > 5 else ("⚠️" if rate_limits > 0 else "✅"))
    if network_errors > 5:
        issues += 1

    # 4. 从failed队列统计网络相关失败
    failed = r.lrange("math:failed", 0, -1)
    network_verdicts = ["rate_limited", "failed_connection", "dead_session"]
    network_failed = 0
    for item in failed:
        data = json.loads(item)
        if data.get("verdict", "") in network_verdicts:
            network_failed += 1

    network_rate = network_failed / len(failed) * 100 if failed else 0
    print(f"  failed队列中网络相关: {network_failed}/{len(failed)} ({network_rate:.1f}%)")
    if network_rate > 20:
        print(f"  ❌ 网络相关失败率>{network_rate:.0f}%，API不稳定")
        issues += 1
    elif network_rate > 5:
        print(f"  ⚠️ 网络相关失败率{network_rate:.0f}%，需关注")
    else:
        print(f"  ✅ 网络相关失败率低")

    return issues


def check_j_devin_exit_health(db, r):
    """J. devin异常退出率——dead_session占比、趋势

    检查方法：
    1. 从DB统计最近30分钟的终态分布，计算dead_session率
    2. dead_session率>20%说明devin cli频繁异常退出（API问题或启动失败）
    3. 对比candidate_solved率，判断系统整体健康度
    """
    print("【J. devin异常退出率】")
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    thirty_min_ago = (now - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M")
    now_str = now.strftime("%Y-%m-%dT%H:%M")

    # 最近30分钟终态分布（排除fix_orphan修复的记录）
    recent = list(db.aql.execute('''
        FOR r IN devin_problem_runs
            FILTER r.exp_id LIKE "p%"
            FILTER r.ended_at != null
            FILTER r.ended_at >= @start
            FILTER r.ended_at <= @end
            FILTER r.fixed_by == null
            COLLECT status = r.status WITH COUNT INTO c
            SORT c DESC
            RETURN {status, count: c}
    ''', bind_vars={"start": thirty_min_ago, "end": now_str}))

    total = sum(s["count"] for s in recent)
    if total == 0:
        print(f"  最近30分钟无终态产出（collector可能还在处理中）")
        return 0

    status_map = {s["status"]: s["count"] for s in recent}
    solved = status_map.get("candidate_solved", 0)
    dead = status_map.get("dead_session", 0)
    token_limit = status_map.get("failed_token_limit", 0)
    export_missing = status_map.get("export_missing", 0)

    dead_rate = dead / total * 100
    solved_rate = solved / total * 100

    print(f"  最近30分钟总产出: {total}题")
    print(f"  candidate_solved: {solved} ({solved_rate:.1f}%)")
    print(f"  dead_session:     {dead} ({dead_rate:.1f}%)")
    print(f"  failed_token_limit: {token_limit} ({token_limit/total*100:.1f}%)")
    print(f"  export_missing:   {export_missing} ({export_missing/total*100:.1f}%)")

    # 判定标准
    issues = 0
    if dead_rate > 20:
        print(f"  ❌ dead_session率{dead_rate:.0f}%>20%，devin cli频繁异常退出")
        issues += 1
    elif dead_rate > 10:
        print(f"  ⚠️ dead_session率{dead_rate:.0f}%，需关注")
    else:
        print(f"  ✅ dead_session率{dead_rate:.0f}%正常")

    if solved_rate < 30:
        print(f"  ❌ solved率{solved_rate:.0f}%<30%，系统产出质量低")
        issues += 1
    elif solved_rate < 50:
        print(f"  ⚠️ solved率{solved_rate:.0f}%，偏低")
    else:
        print(f"  ✅ solved率{solved_rate:.0f}%健康")

    return issues


def main():
    print("=" * 70)
    print("管道化系统并发安全性专项检查")
    print(f"时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 70)
    print()

    r = get_redis()

    # 连接DB
    c = ArangoClient(hosts=os.environ.get("ARANGO_HOST", "http://localhost:8529"))
    db = c.db(
        os.environ["ARANGO_DB"],
        username=os.environ.get("ARANGO_USER", "root"),
        password=os.environ.get("ARANGO_PASS", ""),
    )

    issues = []
    issues += [("A", check_a_redis_atomicity(r))]
    print()
    issues_b = check_b_db_redis_consistency(db, r)
    issues.append(("B", issues_b))
    print()
    issues_c = check_c_duplicate_attempt(r)
    issues.append(("C", issues_c))
    print()
    issues_d = check_d_concurrency_limit(r)
    issues.append(("D", issues_d))
    print()
    devin_count = check_e_devin_process_count()
    conc_raw = r.get("math:config:concurrency")
    conc = int(conc_raw) if conc_raw else 0
    expected_devin = conc * 2
    zombie_devin = max(0, devin_count - expected_devin - 10)
    if zombie_devin > 0:
        print(f"  僵尸devin进程: ~{zombie_devin}个 (进程{devin_count} - 预期{expected_devin}) ❌")
        issues.append(("E", zombie_devin))
    else:
        print(f"  僵尸devin进程: 0 (进程{devin_count} ≈ 预期{expected_devin}) ✅")
    print()
    issues_f = check_f_rate_balance(r)
    issues.append(("F", issues_f))
    print()
    issues_g = check_g_auto_restart()
    issues.append(("G", issues_g))
    print()
    issues_h = check_h_log_duplication()
    issues.append(("H", issues_h))
    print()
    issues_i = check_i_network_health(r)
    issues.append(("I", issues_i))
    print()
    issues_j = check_j_devin_exit_health(db, r)
    issues.append(("J", issues_j))

    # 总结
    print()
    print("=" * 70)
    real_issues = [(k, v) for k, v in issues if v and v != 0]
    if real_issues:
        print(f"判定: ⚠️ 有 {len(real_issues)} 个问题需要关注:")
        for k, v in real_issues:
            print(f"  - [{k}] {v}")
    else:
        print("判定: ✅ 全部通过")
    print("=" * 70)


if __name__ == "__main__":
    main()
