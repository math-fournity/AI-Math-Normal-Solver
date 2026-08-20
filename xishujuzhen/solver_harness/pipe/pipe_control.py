#!/usr/bin/env python3
"""pipe_control.py — 管道化系统控制工具

启动/停止/状态查询4个服务。

用法:
  python pipe_control.py start --concurrency 30
  python pipe_control.py start --concurrency 3 --dry-run  # dry-run模式
  python pipe_control.py stop
  python pipe_control.py status
  python pipe_control.py health  # 并发+网络+tmux泄漏检查
  python pipe_control.py clear  # 清空Redis队列
"""
import sys
import os
import time
import argparse
import subprocess
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))

from collector import find_ai_proof_marker
from redis_queue import (
    get_redis, pending_count, running_count, completed_count, failed_count,
    get_all_running, clear_all, ping, update_stats,
)

PIPE_DIR = Path(__file__).parent
# .venv 在原repo（/Users/user/glm5.2-math-worktree/.venv），不在本repo
VENV_PYTHON = Path(os.environ.get("VENV_PYTHON", "/Users/user/glm5.2-math-worktree/.venv/bin/python3"))

SESSIONS = {
    "feeder": {"script": "feeder.py", "args": []},
    "runner": {"script": "runner.py", "args": ["--concurrency"]},
    "collector": {"script": "collector.py", "args": []},
    "reporter": {"script": "reporter.py", "args": []},
    "monitor": {"script": "monitor_pipe.py", "args": ["--interval"]},
}

LOG_DIR = "/Volumes/data/math-agent-glm5.2-tmux-agents-trajectory/_pipe/logs"


def tmux_running(name: str) -> bool:
    try:
        r = subprocess.run(
            ["tmux", "has-session", "-t", name],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False, timeout=5
        )
        return r.returncode == 0
    except Exception:
        return False


def start_service(name: str, script: str, extra_args: list[str], dry_run: bool = False, auto_restart: bool = False):
    """启动一个服务到tmux session

    auto_restart=True: 用bash while循环包裹，服务退出后自动重启（等5秒）。
    用于collector等需要长期运行的服务——退出后自动恢复，不依赖人工重启。
    """
    session_name = f"pipe-{name}"
    if tmux_running(session_name):
        print(f"  [{name}] 已在运行, 跳过")
        return

    os.makedirs(LOG_DIR, exist_ok=True)
    log_file = os.path.join(LOG_DIR, f"{name}.log")

    cmd = [str(VENV_PYTHON), str(PIPE_DIR / script)] + extra_args
    if dry_run:
        cmd.append("--dry-run")

    cmd_str = " ".join(cmd)
    if auto_restart:
        # bash while循环：退出后等5秒自动重启
        # 不用tee -a——RotatingFileHandler已经写日志文件，tee会导致每行写两遍
        full_cmd = (
            f'while true; do {cmd_str} 2>&1; '
            f'echo "[auto-restart] {name}退出, 5秒后重启..."; '
            f'sleep 5; done'
        )
    else:
        # 不用tee -a——RotatingFileHandler已经写日志文件
        full_cmd = cmd_str + " 2>&1"

    subprocess.run(
        ["tmux", "new-session", "-d", "-s", session_name, full_cmd],
        check=False, timeout=10
    )
    print(f"  [{name}] 启动 → tmux:{session_name}" + (" (auto-restart)" if auto_restart else ""))


def stop_service(name: str, graceful: bool = True, timeout: int = 10):
    """停止一个服务

    graceful=True: 发送SIGTERM让服务优雅退出（不kill harness session）
    graceful=False: 直接kill-session（强制停止）
    """
    session_name = f"pipe-{name}"
    if not tmux_running(session_name):
        print(f"  [{name}] 未运行")
        return

    if graceful:
        # 优雅退出：通过tmux发送SIGTERM给session中的进程
        # tmux send-keys C-c 会发送Ctrl-C（SIGINT），但更可靠的方式是用kill-pane
        # 实际上tmux session中的主进程是python，用send-keys发送C-c最可靠
        subprocess.run(["tmux", "send-keys", "-t", session_name, "C-c", ""],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        print(f"  [{name}] 发送SIGINT, 等待优雅退出...")

        # 等待服务退出
        for i in range(timeout):
            time.sleep(1)
            if not tmux_running(session_name):
                print(f"  [{name}] 已优雅退出 (等待{i+1}s)")
                return

        # 超时后强制kill
        print(f"  [{name}] 优雅退出超时({timeout}s), 强制停止")
        subprocess.run(["tmux", "kill-session", "-t", session_name],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        print(f"  [{name}] 已强制停止")
    else:
        subprocess.run(["tmux", "kill-session", "-t", session_name],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        print(f"  [{name}] 已强制停止")


def cmd_start(args):
    print("启动管道化系统:")
    if not ping():
        print("  ❌ Redis连接失败，请先启动Redis容器")
        sys.exit(1)
    print("  ✅ Redis连接正常")

    runner_args = ["--concurrency", str(args.concurrency)]
    feeder_args = ["--tier", args.tier, "--batch-size", str(args.batch_size),
                   "--low-water-mark", str(args.low_water_mark)]
    collector_args = ["--poll-interval", str(args.collector_poll), "--timeout", str(args.timeout)]
    reporter_args = ["--interval", str(args.report_interval)]

    if args.clear:
        r = get_redis()
        clear_all(r)
        print("  已清空Redis队列")

    start_service("feeder", "feeder.py", feeder_args, dry_run=args.dry_run, auto_restart=True)
    time.sleep(2)
    start_service("runner", "runner.py", runner_args, dry_run=args.dry_run, auto_restart=True)
    time.sleep(1)
    start_service("collector", "collector.py", collector_args, dry_run=args.dry_run, auto_restart=True)
    time.sleep(1)
    start_service("reporter", "reporter.py", reporter_args, dry_run=args.dry_run, auto_restart=True)
    time.sleep(1)
    retry_args = ["--max-retries", "3", "--interval", "120"]
    start_service("retry", "retry_infrastructure.py", retry_args, dry_run=args.dry_run, auto_restart=True)

    # Monitor Pipe（可选，--no-monitor跳过）
    if not args.no_monitor:
        time.sleep(1)
        monitor_args = ["--interval", str(args.monitor_interval), "--concurrency", str(args.concurrency)]
        start_service("monitor", "monitor_pipe.py", monitor_args, dry_run=args.dry_run, auto_restart=True)

    print("\n所有服务已启动。用 'python pipe_control.py status' 查看状态。")


def cmd_stop(args):
    print("停止管道化系统:")
    for name in ["feeder", "runner", "collector", "reporter", "retry", "monitor"]:
        stop_service(name, graceful=not args.force, timeout=args.timeout)

    if not args.keep_harness:
        # 默认不kill harness session——它们独立运行，Collector重启后会继续处理
        # 只有明确指定--kill-harness时才kill
        pass

    if args.kill_harness:
        # kill解题系统的harness session——只匹配harness-p{uuidhex}和harness-dbmon-p{uuidhex}
        # 不kill其他AI的session（如harness-poc2_5-CC101-bare等）
        import re
        result = subprocess.run(["tmux", "list-sessions", "-F", "#{session_name}"],
                                capture_output=True, text=True, check=False)
        pipe_pattern = re.compile(r'^harness-(dbmon-)?p[a-f0-9]{20}')
        harness_sessions = [s.strip() for s in result.stdout.strip().split("\n")
                           if pipe_pattern.match(s.strip())]
        if harness_sessions:
            print(f"\n  发现{len(harness_sessions)}个解题系统harness session:")
            for s in harness_sessions:
                subprocess.run(["tmux", "kill-session", "-t", s],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
                print(f"    killed: {s}")
        else:
            print(f"\n  无解题系统harness session")

    print("\n所有服务已停止。")
    if not args.kill_harness:
        print("  注意: harness-xxx session仍在独立运行（这是正常的——解耦设计）。")
        print("  Collector重启后会继续处理running队列中的attempt。")
        print("  如需停止所有harness session: python pipe_control.py stop --kill-harness")


def cmd_concurrency(args):
    """实时调整并发数——Runner下次poll时生效（通常2-5秒内）"""
    r = get_redis()
    old = r.get("math:config:concurrency")
    old_val = int(old) if old else "?"
    r.set("math:config:concurrency", args.value)
    print(f"并发数: {old_val} → {args.value}")
    print(f"Runner会在下次poll时自动读取新值（通常2-5秒内生效）")
    print(f"当前running不会受影响——只影响后续新启动的题")
    # 如果runner没在运行，提示
    if not tmux_running("pipe-runner"):
        print(f"⚠️ Runner未运行，新值会在Runner启动时生效")


def cmd_poll_interval(args):
    """实时调整poll间隔"""
    r = get_redis()
    old = r.get("math:config:poll_interval")
    old_val = int(old) if old else "?"
    r.set("math:config:poll_interval", args.value)
    print(f"poll间隔: {old_val}s → {args.value}s")
    print(f"Runner会在下次poll时自动读取新值")


def cmd_recover(args):
    """断电恢复"""
    # 直接调用recover_from_crash.py
    cmd = [sys.executable, os.path.join(os.path.dirname(__file__), "recover_from_crash.py")]
    if args.dry_run:
        cmd.append("--dry-run")
    if args.auto_restart:
        cmd.append("--auto-restart")
    subprocess.run(cmd)


def cmd_status(args):
    print("管道化系统状态:")
    print()

    # tmux session状态
    for name in ["feeder", "runner", "collector", "reporter", "retry", "monitor"]:
        session_name = f"pipe-{name}"
        running = tmux_running(session_name)
        status = "✅ 运行中" if running else "❌ 未运行"
        print(f"  {name:12s} {status}")

    print()

    # Redis队列状态
    if not ping():
        print("  ❌ Redis连接失败")
        return

    r = get_redis()
    update_stats(r)
    pending = pending_count(r)
    running = running_count(r)
    completed = completed_count(r)
    failed = failed_count(r)
    total = pending + running + completed + failed

    print(f"  Redis队列:")
    print(f"    pending:   {pending:>8}")
    print(f"    running:   {running:>8}")
    print(f"    completed: {completed:>8}")
    print(f"    failed:    {failed:>8}")
    print(f"    total:     {total:>8}")

    # 实时配置
    conc = r.get("math:config:concurrency")
    poll = r.get("math:config:poll_interval")
    conc_str = conc.decode() if isinstance(conc, bytes) else (conc if conc else "?")
    poll_str = poll.decode() if isinstance(poll, bytes) else (poll if poll else "?")
    print(f"\n  实时配置:")
    print(f"    concurrency:   {conc_str}")
    print(f"    poll_interval: {poll_str}s")
    print(f"    (修改: python pipe_control.py concurrency 50)")

    if running > 0 and args.verbose:
        print(f"\n  Running attempts:")
        all_running = get_all_running(r)
        for exp_id, meta in list(all_running.items())[:10]:
            elapsed = time.time() - meta.get("start_time", time.time())
            print(f"    {exp_id[:20]}  {meta.get('problem_key', '')[:30]}  {elapsed:.0f}s")
        if len(all_running) > 10:
            print(f"    ... 还有 {len(all_running) - 10} 个")


def cmd_health(args):
    """完备健康检查——一次检查覆盖所有维度，无需后续深入检查

    检查维度：
    1. 并发量：设定 vs running vs harness-p vs harness-dbmon
    2. tmux泄漏：dbmon残留 + harness-p不在running队列的残留
    3. TUI状态分类：Thinking / PROOF COMPLETE / 证明完成 / Ask Devin / Response truncated / rate limit / 其他
    4. export落盘：running中PROOF COMPLETE的session是否有export
    5. 数据库字段完整度：最近candidate_solved记录的10个关键字段
    6. solve_time可信度：solve_time > runtime的比例
    7. failed队列分类统计
    8. 整体进度：completed/failed/success率/ETA
    """
    import json
    from collections import Counter
    from pathlib import Path

    TRAJ = Path("/Volumes/data/math-agent-glm5.2-tmux-agents-trajectory")

    print("=" * 70)
    print("系统完备健康检查")
    print("=" * 70)
    print()

    if not ping():
        print("  ❌ Redis连接失败")
        return
    r = get_redis()

    issues = []  # 收集所有问题

    # ============================================================
    # 1. 并发量 + tmux session统计
    # ============================================================
    conc_raw = r.get("math:config:concurrency")
    conc = int(conc_raw.decode() if isinstance(conc_raw, bytes) else conc_raw) if conc_raw else 0
    running = r.hgetall("math:running")
    running_count = len(running)

    result = subprocess.run(["tmux", "list-sessions"], capture_output=True, text=True)
    # 只统计解题系统的session（harness-p{uuidhex}格式），不统计其他AI的session
    import re
    pipe_p_pattern = re.compile(r'^harness-p([a-f0-9]{20})')
    pipe_dbmon_pattern = re.compile(r'^harness-dbmon-p([a-f0-9]{20})')
    harness_p_sessions = []
    harness_dbmon_sessions = []
    for line in result.stdout.split("\n"):
        sess_name = line.split(":")[0].strip()
        m = pipe_dbmon_pattern.match(sess_name)
        if m:
            harness_dbmon_sessions.append(m.group(1))
        else:
            m = pipe_p_pattern.match(sess_name)
            if m:
                harness_p_sessions.append(m.group(1))

    running_exp_ids = set()
    for k, v in running.items():
        data = json.loads(v)
        running_exp_ids.add(data.get("exp_id", ""))

    harness_p_set = set(harness_p_sessions)
    harness_dbmon_set = set(harness_dbmon_sessions)
    leaked_dbmon = harness_dbmon_set - running_exp_ids
    orphan_harness = harness_p_set - running_exp_ids  # harness-p不在running队列

    print(f"【1. 并发量 + tmux session】")
    print(f"  设定并发:        {conc}")
    print(f"  Redis running:   {running_count}")
    print(f"  harness-p:       {len(harness_p_sessions)}")
    print(f"  harness-dbmon:   {len(harness_dbmon_sessions)}")
    print(f"  dbmon泄漏:       {len(leaked_dbmon)}", "❌" if leaked_dbmon else "✅")
    print(f"  孤儿harness-p:   {len(orphan_harness)}", "❌" if orphan_harness else "✅")
    if leaked_dbmon:
        print(f"    泄漏dbmon: {list(leaked_dbmon)[:3]}")
        issues.append(f"dbmon泄漏{len(leaked_dbmon)}个")
    if orphan_harness:
        print(f"    孤儿harness: {list(orphan_harness)[:3]}")
        issues.append(f"孤儿harness-p {len(orphan_harness)}个")
    print()

    # ============================================================
    # 2. TUI状态分类 + 网络错误 + export落盘
    # ============================================================
    categories = {
        "thinking": [],           # 正常思考中
        "proof_complete_export": [],  # PROOF COMPLETE + 有export（等collector处理）
        "proof_complete_no_export": [],  # PROOF COMPLETE + 无export（export未写完）
        "chinese_proof": [],      # 证明完成（中文，应已被AGENTS.md模板禁止）
        "ask_devin": [],          # Ask Devin to build（devin cli完成响应，等collector处理）
        "truncated": [],          # Response truncated
        "rate_limit": [],         # rate limit / 429
        "network_error": [],      # 其他网络错误
        "empty_pane": [],         # 空pane（僵尸session）
        "other": [],              # 其他异常
    }

    error_patterns = [
        "connection error", "econnrefused", "econnreset", "socket hang up",
        "fetch failed", "network error", "network request failed", "etimedout",
    ]
    rate_limit_patterns = ["message rate limit", "429", "too many requests"]

    for k, v in running.items():
        data = json.loads(v)
        eid = data.get("exp_id", "")
        pk = data.get("problem_key", "")
        tmux_sess = f"harness-{eid}"
        res = subprocess.run(["tmux", "capture-pane", "-t", tmux_sess, "-p", "-S", "-200"],
                             capture_output=True, text=True)
        pane = res.stdout
        pane_lower = pane.lower()

        # 检查pipe.log中的rate limit（pane可能不显示）
        pipe_log = TRAJ / eid / "tmux" / "tmux_pipe.log"
        has_rate_limit = False
        if pipe_log.exists():
            content = pipe_log.read_text(errors="ignore")[-5000:].lower()
            if "message rate limit" in content:
                has_rate_limit = True

        # 分类
        if has_rate_limit or any(p in pane_lower for p in rate_limit_patterns):
            categories["rate_limit"].append((pk, eid))
        elif any(p in pane_lower for p in error_patterns):
            categories["network_error"].append((pk, eid))
        elif "Response truncated" in pane or "Send a message to continue" in pane:
            categories["truncated"].append((pk, eid))
        elif "证明完成" in pane and find_ai_proof_marker(pane)[0] is None:
            categories["chinese_proof"].append((pk, eid))
        elif find_ai_proof_marker(pane)[0] is not None:
            export_path = TRAJ / eid / "exports" / "conversation.json"
            if export_path.exists():
                categories["proof_complete_export"].append((pk, eid))
            else:
                categories["proof_complete_no_export"].append((pk, eid))
        elif "Ask Devin to build" in pane:
            categories["ask_devin"].append((pk, eid))
        elif "Thinking" in pane:
            categories["thinking"].append((pk, eid))
        elif not pane.strip():
            categories["empty_pane"].append((pk, eid))
        else:
            lines = [l for l in pane.split("\n") if l.strip()]
            last = lines[-1][:60] if lines else "(空)"
            categories["other"].append((pk, eid, last))

    print(f"【2. TUI状态分类 + 网络错误】")
    print(f"  Thinking中:              {len(categories['thinking'])} ✅")
    print(f"  PROOF COMPLETE+有export: {len(categories['proof_complete_export'])} (等collector处理)")
    print(f"  PROOF COMPLETE+无export: {len(categories['proof_complete_no_export'])}", "⚠️" if categories['proof_complete_no_export'] else "✅")
    print(f"  证明完成(中文):           {len(categories['chinese_proof'])}", "⚠️" if categories['chinese_proof'] else "✅")
    print(f"  Ask Devin(等collector):   {len(categories['ask_devin'])}")
    print(f"  Response truncated:      {len(categories['truncated'])}", "⚠️" if categories['truncated'] else "✅")
    print(f"  Rate limit/429:          {len(categories['rate_limit'])}", "❌" if categories['rate_limit'] else "✅")
    print(f"  其他网络错误:            {len(categories['network_error'])}", "❌" if categories['network_error'] else "✅")
    print(f"  空pane(僵尸):            {len(categories['empty_pane'])}", "❌" if categories['empty_pane'] else "✅")
    print(f"  其他:                    {len(categories['other'])}", "⚠️" if categories['other'] else "✅")

    if categories["rate_limit"]:
        print(f"    Rate limit详情:")
        for pk, eid in categories["rate_limit"][:3]:
            print(f"      {pk} {eid}")
        issues.append(f"rate limit {len(categories['rate_limit'])}个")
    if categories["network_error"]:
        print(f"    网络错误详情:")
        for pk, eid in categories["network_error"][:3]:
            print(f"      {pk} {eid}")
        issues.append(f"网络错误{len(categories['network_error'])}个")
    if categories["proof_complete_no_export"]:
        print(f"    PROOF COMPLETE但无export:")
        for pk, eid in categories["proof_complete_no_export"][:3]:
            print(f"      {pk} {eid}")
        issues.append(f"PROOF COMPLETE无export {len(categories['proof_complete_no_export'])}个")
    if categories["chinese_proof"]:
        print(f"    中文证明完成:")
        for pk, eid in categories["chinese_proof"][:3]:
            print(f"      {pk} {eid}")
        issues.append(f"中文证明完成{len(categories['chinese_proof'])}个")
    if categories["truncated"]:
        issues.append(f"Response truncated {len(categories['truncated'])}个")
    if categories["empty_pane"]:
        issues.append(f"空pane僵尸{len(categories['empty_pane'])}个")
    if categories["other"]:
        print(f"    其他状态:")
        for pk, eid, last in categories["other"][:3]:
            print(f"      {pk} {eid}: {last}")
        issues.append(f"其他状态{len(categories['other'])}个")
    print()

    # ============================================================
    # 3. export落盘率（最近完成的题）
    # ============================================================
    import re
    collector_log = TRAJ / "_pipe" / "logs" / "collector.log"
    if collector_log.exists():
        log_text = collector_log.read_text(errors="ignore")
        solved = re.findall(r"SOLVED \S+ exp_id=(\w+) \((\d+)s\)", log_text)[-20:]
        with_export = sum(1 for eid, _ in solved if (TRAJ / eid / "exports" / "conversation.json").exists())
        export_rate = with_export / len(solved) * 100 if solved else 0
        print(f"【3. export落盘率】")
        print(f"  最近{len(solved)}个SOLVED题: {with_export}/{len(solved)} ({export_rate:.0f}%)", "✅" if export_rate >= 90 else ("⚠️" if export_rate >= 70 else "❌"))
        if export_rate < 90:
            issues.append(f"export落盘率{export_rate:.0f}%偏低")
    print()

    # ============================================================
    # 4. 数据库字段完整度 + solve_time可信度
    # ============================================================
    try:
        from arango import ArangoClient
        client = ArangoClient(hosts="http://localhost:8529", request_timeout=300)
        db = client.db("xishujuzhen_math_glm52", username="root", password="moira123")
        cursor = db.aql.execute('''
            FOR a IN devin_problem_runs
              FILTER a.batch_id == 'pipe-runner' AND a.status == 'candidate_solved'
              SORT a.ended_at DESC
              LIMIT 20
              RETURN a
        ''', ttl=60)
        records = list(cursor)

        if records:
            fields = ['status', 'verdict', 'started_at', 'ended_at', 'runtime_seconds',
                       'solve_time_seconds', 'solve_time_source', 'pane_snapshot',
                       'exp_id', 'problem_id']
            print(f"【4. 数据库字段完整度】（最近{len(records)}条candidate_solved）")
            incomplete_fields = []
            for f in fields:
                count = sum(1 for rec in records if rec.get(f) is not None)
                pct = count / len(records) * 100
                status = "✅" if pct == 100 else ("⚠️" if pct >= 80 else "❌")
                print(f"  {status} {f:25s} {count}/{len(records)} ({pct:.0f}%)")
                if pct < 100:
                    incomplete_fields.append(f)

            # solve_time可信度
            st_gt = sum(1 for rec in records if rec.get("solve_time_seconds", 0) > rec.get("runtime_seconds", 0))
            st_zero = sum(1 for rec in records if rec.get("solve_time_seconds", 0) == 0)
            st_ok = len(records) - st_gt - st_zero
            print()
            print(f"【5. solve_time可信度】")
            print(f"  solve_time <= runtime (合理): {st_ok}/{len(records)} ({st_ok/len(records)*100:.0f}%)", "✅" if st_ok == len(records) else "⚠️")
            print(f"  solve_time > runtime (异常): {st_gt}/{len(records)} ({st_gt/len(records)*100:.0f}%)", "❌" if st_gt > 0 else "✅")
            print(f"  solve_time = 0 (缺失):       {st_zero}/{len(records)}", "❌" if st_zero > 0 else "✅")
            if st_gt > 0:
                issues.append(f"solve_time>runtime {st_gt}条")
            if st_zero > 0:
                issues.append(f"solve_time=0 {st_zero}条")
            if incomplete_fields:
                issues.append(f"DB字段不完整: {','.join(incomplete_fields)}")
        print()
    except Exception as e:
        print(f"【4-5. 数据库检查失败】: {e}")
        print()
        issues.append("数据库检查失败")

    # ============================================================
    # 6. failed队列分类
    # ============================================================
    failed = r.lrange("math:failed", 0, -1)
    verdicts = Counter()
    conn_failures = 0
    for item in failed:
        data = json.loads(item)
        v = data.get("verdict", "?")
        verdicts[v] += 1
        if v in ("failed_connection", "rate_limited"):
            conn_failures += 1

    print(f"【6. failed队列】")
    print(f"  总计:           {len(failed)}")
    print(f"  网络相关:       {conn_failures}", "❌" if conn_failures else "✅")
    for v, c in verdicts.most_common():
        print(f"    {v}: {c}")
    if conn_failures:
        issues.append(f"failed队列网络相关{conn_failures}个")
    print()

    # ============================================================
    # 7. 整体进度
    # ============================================================
    completed = r.llen("math:completed")
    pending = r.zcard("math:pending")
    total = completed + len(failed) + running_count + pending
    success_rate = completed / (completed + len(failed)) * 100 if (completed + len(failed)) > 0 else 0

    print(f"【7. 整体进度】")
    print(f"  completed: {completed}")
    print(f"  failed:    {len(failed)}")
    print(f"  running:   {running_count}")
    print(f"  pending:   {pending}")
    print(f"  total:     {total}")
    print(f"  success率: {success_rate:.1f}%")
    print()

    # ============================================================
    # 8. 整体判定
    # ============================================================
    print("=" * 70)
    if issues:
        print(f"判定: ⚠️ 有 {len(issues)} 个问题需要关注:")
        for issue in issues:
            print(f"  - {issue}")
    else:
        print(f"判定: ✅ 全部健康")
    print("=" * 70)

    # ============================================================
    # 9. 提醒：检查Monitor Pipe的输出
    # ============================================================
    print()
    print(">> ★ 提醒：还要检查Monitor Pipe的输出内容！")
    print(">>   Monitor Pipe持续监控13项自动检查+AI review抽样，alert写入pipe_monitor_alerts集合。")
    print(">>   标准化检查脚本：bash xishujuzhen/solver_harness/pipe/scripts/monitor_check.sh")
    print(">>   ")
    print(">>   检查脚本输出5项：")
    print(">>     1. Monitor Pipe pane输出（最近5轮的ALERT/AI_REVIEW/queue/throughput）")
    print(">>     2. alerts集合（新alert详情）")
    print(">>     3. 进程状态（5个pipe服务+monitor_pipe+harness-p/dbmon数）")
    print(">>     4. 进度（Redis队列+最近5分钟吞吐）")
    print(">>     5. 系统深度审查结果（alert按severity/type统计+24小时趋势）")
    print(">>   ")
    print(">>   ★ 第5项'系统深度审查结果'是给AI的核心提醒——AI必须逐项判断每个alert的处理方案")
    print(">>   ★ critical级别alert需要立即处理（pipe_service_dead/session_health/rate_limit/queue_stalled）")
    print(">>   ★ info级别alert是AI review抽样——AI必须检查抽样的proof质量（这是AI的核心职责）")
    print(">>   ★ 处理完alert后用 monitor_pipe.py --resolve-alert <key> 标记为fixed")
    print("=" * 70)


def cmd_clear(args):
    print("清空Redis队列:")
    if not ping():
        print("  ❌ Redis连接失败")
        return
    r = get_redis()
    clear_all(r)
    print("  ✅ 已清空所有队列")


def cmd_monitor(args):
    """单独启动/停止/查看Monitor Pipe"""
    if args.action == "start":
        monitor_args = ["--interval", str(args.interval), "--concurrency", str(args.concurrency)]
        start_service("monitor", "monitor_pipe.py", monitor_args, auto_restart=True)
        print(f"\nMonitor Pipe已启动（interval={args.interval}s, concurrency={args.concurrency}）")
        print(f"检查脚本: bash xishujuzhen/solver_harness/pipe/scripts/monitor_check.sh")
    elif args.action == "stop":
        stop_service("monitor", graceful=True, timeout=10)
        print("Monitor Pipe已停止")
    elif args.action == "status":
        if tmux_running("pipe-monitor"):
            print("Monitor Pipe: ✅ 运行中")
            # 显示最近几轮输出
            result = subprocess.run(
                ["tmux", "capture-pane", "-t", "pipe-monitor", "-p", "-S", "-50"],
                capture_output=True, text=True, timeout=5
            )
            lines = [l for l in result.stdout.split("\n") if l.strip()]
            print("\n最近输出:")
            for line in lines[-15:]:
                print(f"  {line}")
        else:
            print("Monitor Pipe: ❌ 未运行")
            print("  启动: python pipe_control.py monitor start --interval 300 --concurrency 20")


def main():
    parser = argparse.ArgumentParser(description="管道化系统控制工具")
    sub = parser.add_subparsers(dest="command")

    p_start = sub.add_parser("start", help="启动所有服务")
    p_start.add_argument("--concurrency", type=int, default=30)
    p_start.add_argument("--tier", type=str, default="1,2,3")
    p_start.add_argument("--batch-size", type=int, default=100)
    p_start.add_argument("--low-water-mark", type=int, default=50)
    p_start.add_argument("--collector-poll", type=int, default=10)
    p_start.add_argument("--timeout", type=int, default=1800)
    p_start.add_argument("--report-interval", type=int, default=60)
    p_start.add_argument("--clear", action="store_true", help="启动前清空Redis队列")
    p_start.add_argument("--dry-run", action="store_true", help="dry-run模式")
    p_start.add_argument("--no-monitor", action="store_true", help="不启动Monitor Pipe（默认启动）")
    p_start.add_argument("--monitor-interval", type=int, default=300, help="Monitor Pipe检查间隔秒数（默认300）")
    p_start.set_defaults(func=cmd_start)

    p_stop = sub.add_parser("stop", help="停止所有服务")
    p_stop.add_argument("--force", action="store_true", help="强制停止（不等待优雅退出）")
    p_stop.add_argument("--timeout", type=int, default=10, help="优雅退出等待超时秒数")
    p_stop.add_argument("--kill-harness", action="store_true", help="同时kill所有harness-xxx session（默认不kill）")
    p_stop.add_argument("--keep-harness", action="store_true", help="保留harness session（默认行为，这里仅为显式声明）")
    p_stop.set_defaults(func=cmd_stop)

    p_recover = sub.add_parser("recover", help="断电恢复+僵尸清理")
    p_recover.add_argument("--dry-run", action="store_true", help="只检查不修改")
    p_recover.add_argument("--auto-restart", action="store_true", help="恢复后自动重启服务")
    p_recover.set_defaults(func=cmd_recover)

    p_status = sub.add_parser("status", help="查看状态")
    p_status.add_argument("-v", "--verbose", action="store_true")
    p_status.set_defaults(func=cmd_status)

    p_health = sub.add_parser("health", help="并发+网络+tmux泄漏一键检查")
    p_health.set_defaults(func=cmd_health)

    p_clear = sub.add_parser("clear", help="清空Redis队列")
    p_clear.set_defaults(func=cmd_clear)

    p_conc = sub.add_parser("concurrency", help="实时调整并发数")
    p_conc.add_argument("value", type=int, help="新的并发数（如50）")
    p_conc.set_defaults(func=cmd_concurrency)

    p_poll = sub.add_parser("poll-interval", help="实时调整poll间隔")
    p_poll.add_argument("value", type=int, help="新的poll间隔秒数")
    p_poll.set_defaults(func=cmd_poll_interval)

    p_monitor = sub.add_parser("monitor", help="单独启动/停止Monitor Pipe")
    p_monitor.add_argument("action", choices=["start", "stop", "status"], help="start/stop/status")
    p_monitor.add_argument("--interval", type=int, default=300, help="检查间隔秒数")
    p_monitor.add_argument("--concurrency", type=int, default=20, help="预期并发数")
    p_monitor.set_defaults(func=cmd_monitor)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    args.func(args)


if __name__ == "__main__":
    main()
