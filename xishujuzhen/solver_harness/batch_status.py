#!/usr/bin/env python3
"""批次状态查询脚本——封装所有常用查询，避免每次现写脚本。

用法：
  python batch_status.py status [--batch-id ID]          # 批次总状态
  python batch_status.py active [--batch-id ID]          # running attempt活跃度详情
  python batch_status.py errors [--batch-id ID]          # 连接错误/token_limit/异常统计
  python batch_status.py solved [--batch-id ID]          # candidate_solved列表
  python batch_status.py feed [--batch-id ID]            # feed事件历史
  python batch_status.py leak [--batch-id ID]            # 答案泄漏检查（DB marker + tmux_pipe扫描）
  python batch_status.py dead [--batch-id ID]            # 僵尸session检测（tmux pane空白+Devin CLI已退出）
  python batch_status.py all [--batch-id ID]             # 上述全部

不指定--batch-id时，自动选最新的running批次。
"""
import argparse
import os
import subprocess
import sys
from pathlib import Path

from arango import ArangoClient


def connect_db():
    host = os.environ.get("ARANGO_HOST", "http://localhost:8529")
    db_name = os.environ["ARANGO_DB"]
    user = os.environ.get("ARANGO_USER", "root")
    pwd = os.environ.get("ARANGO_PASS", "")
    c = ArangoClient(hosts=host)
    return c.db(db_name, username=user, password=pwd)


def latest_batch_id(db):
    """选最新的非completed批次。"""
    for b in db.aql.execute(
        'FOR b IN devin_batch_runs FILTER b.status != "completed" '
        'SORT b.created_at DESC LIMIT 1 RETURN b._key'
    ):
        return b
    # fallback: 最新的任意批次
    for b in db.aql.execute(
        'FOR b IN devin_batch_runs SORT b.created_at DESC LIMIT 1 RETURN b._key'
    ):
        return b
    return None


def cmd_status(db, batch_id):
    """批次总状态：按status分组计数 + 并发 + feed剩余。"""
    print(f"=== Batch: {batch_id} ===")
    batch = db.collection("devin_batch_runs").get(batch_id)
    if not batch:
        print(f"  NOT FOUND")
        return
    print(f"  concurrency: {batch.get('concurrency')}")
    print(f"  status: {batch.get('status')}")
    print(f"  created: {batch.get('created_at')}")

    print(f"\n  --- Attempt counts ---")
    total = 0
    for r in db.aql.execute(
        'FOR a IN devin_problem_runs FILTER a.batch_id == @bid '
        'COLLECT status=a.status WITH COUNT INTO cnt SORT cnt DESC '
        'RETURN {status, cnt}',
        bind_vars={"bid": batch_id},
    ):
        print(f"    {r['status']:25s}: {r['cnt']}")
        total += r["cnt"]
    print(f"    {'TOTAL':25s}: {total}")

    # feed剩余
    tier = (batch.get("selection") or {}).get("feed_tier", 1)
    remaining = db.aql.execute(
        'FOR p IN problem_extraction_progress FILTER p.difficulty_tier == @tier '
        'FILTER LENGTH(FOR r IN devin_problem_runs FILTER r.progress_key == p._key LIMIT 1 RETURN 1) == 0 '
        'COLLECT WITH COUNT INTO n RETURN n',
        bind_vars={"tier": tier},
    ).next()
    print(f"\n  --- Feed (tier={tier}) ---")
    print(f"    remaining unprocessed: {remaining}")


def cmd_active(db, batch_id):
    """running attempt活跃度详情：runtime/idle/thinking大小/markers。"""
    print(f"=== Active attempts: {batch_id} ===")
    active = 0
    stalled = 0
    for a in db.aql.execute(
        'FOR a IN devin_problem_runs FILTER a.batch_id == @bid '
        'FILTER a.status IN ["running", "stalled_warning"] '
        'SORT a.idle_seconds ASC '
        'RETURN {pid:a.problem_id, rt:a.runtime_seconds, idle:a.idle_seconds, '
        'sizes:a.observability.file_sizes, markers:a.observability.markers}',
        bind_vars={"bid": batch_id},
    ):
        s = a.get("sizes") or {}
        think_kb = (s.get("thinking_readable_path", 0) or 0) // 1024
        pipe_kb = (s.get("tmux_pipe_path", 0) or 0) // 1024
        export_kb = (s.get("export_path", 0) or 0) // 1024
        markers_dict = a.get("markers") or {}
        markers = [k for k, v in markers_dict.items() if v]
        m_str = ",".join(markers) if markers else "-"
        rt = a.get("rt") or 0
        idle = a.get("idle") or 0
        status = "ACTIVE" if idle < 120 else "STALLED"
        if status == "ACTIVE":
            active += 1
        else:
            stalled += 1
        print(
            f"  {a['pid']:30s} rt={rt:5d}s idle={idle:4d}s "
            f"think={think_kb:5d}KB pipe={pipe_kb:3d}KB exp={export_kb:4d}KB "
            f"[{status}] {m_str}"
        )
    print(f"\n  active={active}, stalled={stalled}")


def cmd_errors(db, batch_id):
    """连接错误/token_limit/异常统计 + 抽样tmux_pipe内容。"""
    print(f"=== Error analysis: {batch_id} ===")

    # marker统计
    for marker in ["connection_error", "token_limited", "rate_limited", "answer_leak"]:
        cnt = db.aql.execute(
            'FOR a IN devin_problem_runs FILTER a.batch_id == @bid '
            'FILTER a.observability.markers.@marker == true '
            'COLLECT WITH COUNT INTO n RETURN n',
            bind_vars={"bid": batch_id, "marker": marker},
        ).next()
        if cnt > 0:
            print(f"  {marker}: {cnt} attempts")

    # failed_no_proof中实际是连接错误的
    print(f"\n  --- failed_no_proof breakdown (sampling tmux_pipe) ---")
    conn_err = 0
    token_lim = 0
    real_fail = 0
    other = 0
    for a in db.aql.execute(
        'FOR a IN devin_problem_runs FILTER a.batch_id == @bid '
        'FILTER a.status == "failed_no_proof" '
        'RETURN {pid:a.problem_id, paths:a.paths}',
        bind_vars={"bid": batch_id},
    ):
        pipe = Path(a["paths"]["tmux_pipe_path"])
        if not pipe.exists():
            other += 1
            continue
        content = pipe.read_text()
        if "Connection error" in content or "cognition.ai/errorKind" in content or "unavailable" in content:
            conn_err += 1
        elif "token limit" in content.lower() or "truncated" in content.lower():
            token_lim += 1
        elif len(content.strip()) > 100:
            real_fail += 1
        else:
            other += 1
    print(f"    connection_error: {conn_err}")
    print(f"    token_limit: {token_lim}")
    print(f"    real_no_proof (has thinking but no proof): {real_fail}")
    print(f"    other (empty/missing): {other}")


def cmd_solved(db, batch_id):
    """candidate_solved列表。"""
    print(f"=== Solved: {batch_id} ===")
    count = 0
    for a in db.aql.execute(
        'FOR a IN devin_problem_runs FILTER a.batch_id == @bid '
        'FILTER a.status == "candidate_solved" '
        'SORT a.runtime_seconds ASC '
        'RETURN {pid:a.problem_id, rt:a.runtime_seconds, think:a.observability.file_sizes.thinking_readable_path}',
        bind_vars={"bid": batch_id},
    ):
        think_kb = (a["think"] or 0) // 1024
        print(f"  {a['pid']:30s} rt={a['rt']:5d}s think={think_kb:5d}KB")
        count += 1
    print(f"  total solved: {count}")


def cmd_feed(db, batch_id):
    """feed事件历史。"""
    print(f"=== Feed events: {batch_id} ===")
    for e in db.aql.execute(
        'FOR e IN devin_run_events FILTER e.batch_id == @bid '
        'FILTER e.event_type IN ["cases_added", "concurrency_changed", "feed_exhausted"] '
        'SORT e.timestamp ASC RETURN e',
        bind_vars={"bid": batch_id},
    ):
        print(f"  {e['timestamp']}: {e['event_type']} {e.get('details', {})}")


def cmd_leak(db, batch_id):
    """答案泄漏检查：DB marker + tmux_pipe/export文件扫描。

    检查三个层面：
    1. DB中answer_leak marker（Solver输出### ANSWER LEAK DETECTED时monitor设的）
    2. tmux_pipe.log中的### ANSWER LEAK DETECTED（直接扫描，不依赖marker）
    3. export/conversation.json中的ANSWER LEAK DETECTED（区分指令文本 vs 真报错）
    """
    print(f"=== Answer leak check: {batch_id} ===")

    # 1. DB marker
    cnt = db.aql.execute(
        'FOR a IN devin_problem_runs FILTER a.batch_id == @bid '
        'FILTER a.observability.markers.answer_leak == true '
        'COLLECT WITH COUNT INTO n RETURN n',
        bind_vars={"bid": batch_id},
    ).next()
    print(f"  DB answer_leak marker: {cnt}")

    if cnt > 0:
        for a in db.aql.execute(
            'FOR a IN devin_problem_runs FILTER a.batch_id == @bid '
            'FILTER a.observability.markers.answer_leak == true '
            'RETURN {pid:a.problem_id, status:a.status, paths:a.paths}',
            bind_vars={"bid": batch_id},
        ):
            print(f"    {a['pid']}: status={a['status']}")

    # 2. 扫描tmux_pipe.log中的真实报错（排除AGENTS.md指令文本）
    print(f"\n  --- tmux_pipe.log scan (real reports only) ---")
    real_leaks = 0
    instruction_matches = 0
    for a in db.aql.execute(
        'FOR a IN devin_problem_runs FILTER a.batch_id == @bid '
        'RETURN {pid:a.problem_id, paths:a.paths}',
        bind_vars={"bid": batch_id},
    ):
        pipe_path = Path(a["paths"]["tmux_pipe_path"])
        if not pipe_path.exists():
            continue
        content = pipe_path.read_text()
        if "ANSWER LEAK DETECTED" not in content:
            continue
        # 区分：指令文本中是"### ANSWER LEAK DETECTED: <brief description>"
        # 真报错是"### ANSWER LEAK DETECTED: <具体描述>"（没有<brief>占位符）
        for line in content.split("\n"):
            if "ANSWER LEAK DETECTED" in line:
                if "<brief description>" in line or "Instead output exactly" in line:
                    instruction_matches += 1
                else:
                    real_leaks += 1
                    print(f"    REAL LEAK: {a['pid']} -> {line.strip()[:100]}")

    print(f"    instruction_text_matches: {instruction_matches} (AGENTS.md指令被记录，正常)")
    print(f"    real_leak_reports: {real_leaks}")

    # 3. 全局grep export文件（跨批次，检查是否有遗漏）
    print(f"\n  --- global export scan (all batches) ---")
    result = subprocess.run(
        ["grep", "-r", "-l", "ANSWER LEAK DETECTED",
         "/Volumes/data/math-agent-glm5.2-tmux-agents-trajectory/"],
        capture_output=True, text=True, timeout=60,
    )
    export_files = [f for f in result.stdout.strip().split("\n") if f]
    print(f"    files containing 'ANSWER LEAK DETECTED': {len(export_files)}")
    for f in export_files[:5]:
        # 检查是指令文本还是真报错
        try:
            import json as _json
            d = _json.load(open(f))
            found_real = False
            def _scan(obj):
                nonlocal found_real
                if isinstance(obj, str) and "ANSWER LEAK DETECTED" in obj:
                    if "<brief description>" not in obj and "Instead output exactly" not in obj:
                        found_real = True
                elif isinstance(obj, dict):
                    for v in obj.values():
                        _scan(v)
                elif isinstance(obj, list):
                    for v in obj:
                        _scan(v)
            _scan(d)
            tag = "REAL" if found_real else "instruction_only"
        except Exception:
            tag = "?"
        print(f"    [{tag}] {Path(f).parent.name}")

    if real_leaks == 0 and cnt == 0:
        print(f"\n  RESULT: No answer leaks detected.")


def cmd_dead(db, batch_id):
    """僵尸session检测：tmux session还在但Devin CLI已退出（pane空白）。

    检测逻辑：
    1. 找所有running/stalled_warning的attempt
    2. 检查tmux pane内容——空白=Devin CLI已退出
    3. 检查tmux_pipe.log——有没有PROOF COMPLETE（已完成但monitor没判定）
    4. 检查thinking_readable_path——有没有实际产出

    输出分类：
    - DEAD: pane空白，无PROOF COMPLETE，无thinking → 僵尸session
    - PROOF_MISSED: pane空白但有PROOF COMPLETE → 已完成但monitor没判定
    - CONN_DEAD: pane空白，pipe有Connection error → 连接错误导致退出
    - ALIVE: pane有内容 → 正常运行
    """
    print(f"=== Dead session check: {batch_id} ===")

    dead = 0
    proof_missed = 0
    conn_dead = 0
    alive = 0

    for a in db.aql.execute(
        'FOR a IN devin_problem_runs FILTER a.batch_id == @bid '
        'FILTER a.status IN ["running", "stalled_warning"] '
        'RETURN {key:a._key, pid:a.problem_id, tmux:a.tmux_session, '
        'paths:a.paths, rt:a.runtime_seconds}',
        bind_vars={"bid": batch_id},
    ):
        tmux = a["tmux"]
        # 检查tmux session是否存在
        r = subprocess.run(
            ["tmux", "has-session", "-t", tmux],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
        )
        if r.returncode != 0:
            # tmux session已消失但DB还显示running——也是僵尸
            pipe = Path(a["paths"]["tmux_pipe_path"])
            pipe_text = pipe.read_text() if pipe.exists() else ""
            if "### PROOF COMPLETE" in pipe_text:
                proof_missed += 1
                print(f"  [PROOF_MISSED] {a['pid']:25s} rt={a['rt']}s (tmux gone, proof in pipe)")
            else:
                dead += 1
                print(f"  [DEAD] {a['pid']:25s} rt={a['rt']}s (tmux session gone)")
            continue

        # 捕获pane内容
        r = subprocess.run(
            ["tmux", "capture-pane", "-t", tmux, "-p"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False, timeout=10,
        )
        content = r.stdout.decode("utf-8", errors="ignore")
        stripped = "\n".join(line.strip() for line in content.split("\n") if line.strip())

        if len(stripped) < 5:
            # pane空白——Devin CLI已退出
            pipe = Path(a["paths"]["tmux_pipe_path"])
            pipe_text = pipe.read_text() if pipe.exists() else ""
            think_size = Path(a["paths"]["thinking_readable_path"]).stat().st_size if Path(a["paths"]["thinking_readable_path"]).exists() else 0

            if "### PROOF COMPLETE" in pipe_text:
                proof_missed += 1
                print(f"  [PROOF_MISSED] {a['pid']:25s} rt={a['rt']}s think={think_size//1024}KB (pane empty, proof in pipe)")
            elif "Connection error" in pipe_text or "unavailable" in pipe_text or "cognition.ai" in pipe_text:
                conn_dead += 1
                print(f"  [CONN_DEAD] {a['pid']:25s} rt={a['rt']}s think={think_size//1024}KB (pane empty, connection error)")
            else:
                dead += 1
                print(f"  [DEAD] {a['pid']:25s} rt={a['rt']}s think={think_size//1024}KB (pane empty, no output)")
        else:
            alive += 1

    print(f"\n  DEAD={dead}  PROOF_MISSED={proof_missed}  CONN_DEAD={conn_dead}  ALIVE={alive}")

    if dead + proof_missed + conn_dead > 0:
        print(f"\n  ACTION NEEDED: {dead + proof_missed + conn_dead} zombie sessions need cleanup.")
        print(f"  Run with --cleanup to auto-fix (kill tmux + update DB status).")
    else:
        print(f"\n  RESULT: No zombie sessions.")


def cmd_dead_cleanup(db, batch_id, *, all_batches=False):
    """僵尸session自动清理：检查运行时文件判定终态 + 更新DB状态。

    改进版（2026-08-12）：
    - 检查export文件（比pipe更准确——export是devin cli正式输出）
    - 检查pipe文件尾部（PROOF COMPLETE / Response truncated / Connection error）
    - 区分failed_no_proof / failed_token_limit / failed_connection / dead_session
    - all_batches=True时清理所有batch的僵尸（不限于指定batch）
    """
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()

    if all_batches:
        print("=== Dead session cleanup: ALL BATCHES ===")
        cursor = db.aql.execute(
            'FOR a IN devin_problem_runs '
            'FILTER a.status IN ["running", "stalled_warning", "launching"] '
            'RETURN a'
        )
    else:
        print(f"=== Dead session cleanup: {batch_id} ===")
        cursor = db.aql.execute(
            'FOR a IN devin_problem_runs FILTER a.batch_id == @bid '
            'FILTER a.status IN ["running", "stalled_warning", "launching"] '
            'RETURN a',
            bind_vars={"bid": batch_id},
        )

    cleaned = 0
    results = {"candidate_solved": 0, "failed_no_proof": 0, "failed_token_limit": 0,
               "failed_connection": 0, "dead_session": 0}

    for a in cursor:
        tmux = a.get("tmux_session", "")
        pid = a.get("problem_id", "?")
        run_id = a.get("run_id", 0)
        paths = a.get("paths", {})
        export_path = Path(paths.get("export_path", ""))
        pipe_path = Path(paths.get("tmux_pipe_path", ""))

        # tmux是否还活着
        tmux_alive = False
        if tmux:
            r = subprocess.run(["tmux", "has-session", "-t", tmux],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
            tmux_alive = (r.returncode == 0)

        # 如果tmux还活着，先capture pane看状态
        if tmux_alive:
            try:
                r = subprocess.run(["tmux", "capture-pane", "-t", tmux, "-p", "-S", "-50"],
                                   capture_output=True, text=True, timeout=5)
                pane = r.stdout
            except Exception:
                pane = ""
            # 如果pane有内容且在thinking，跳过——不是僵尸
            if "Thinking" in pane and "Ask Devin" not in pane:
                continue
            # pane空或IDLE——是僵尸，kill掉
            subprocess.run(["tmux", "kill-session", "-t", tmux],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)

        # 检查运行时文件判定终态
        export_size = export_path.stat().st_size if export_path.exists() else 0
        pipe_size = pipe_path.stat().st_size if pipe_path.exists() else 0

        has_proof = False
        if export_size > 0:
            try:
                content = export_path.read_text(encoding="utf-8", errors="ignore")
                if "PROOF COMPLETE" in content:
                    has_proof = True
            except: pass

        pipe_proof = False
        pipe_truncated = False
        pipe_conn_err = False
        if pipe_size > 0:
            try:
                with pipe_path.open("rb") as f:
                    f.seek(max(0, pipe_size - 10240))
                    tail = f.read().decode("utf-8", errors="ignore")
                if "PROOF COMPLETE" in tail: pipe_proof = True
                if "Response truncated" in tail: pipe_truncated = True
                if "Connection error" in tail or "unavailable" in tail: pipe_conn_err = True
            except: pass

        # 判定
        if has_proof or pipe_proof:
            status = "candidate_solved"
            reason = "zombie_cleanup_proof_found"
        elif pipe_truncated:
            status = "failed_token_limit"
            reason = "zombie_cleanup_response_truncated"
        elif pipe_conn_err:
            status = "failed_connection"
            reason = "zombie_cleanup_connection_error"
        elif export_size == 0 and pipe_size == 0:
            status = "dead_session"
            reason = "zombie_cleanup_no_data"
        elif export_size > 0:
            status = "failed_no_proof"
            reason = "zombie_cleanup_no_proof_marker"
        else:
            status = "failed_no_proof"
            reason = "zombie_cleanup_pipe_only_no_proof"

        db.collection("devin_problem_runs").update({
            "_key": a["_key"], "status": status,
            "ended_at": now, "end_reason": reason, "updated_at": now,
        })
        db.collection("devin_run_events").insert({
            "batch_id": a.get("batch_id", ""),
            "attempt_key": a["_key"],
            "event_type": "zombie_cleanup",
            "event_data": {"problem_id": pid, "run_id": run_id, "final_status": status,
                           "export_size": export_size, "pipe_size": pipe_size,
                           "tmux_was_alive": tmux_alive},
            "observed_at": now,
        })

        results[status] = results.get(status, 0) + 1
        tag = {"candidate_solved": "SOLVED", "failed_no_proof": "NOPROOF",
               "failed_token_limit": "TRUNC", "failed_connection": "CONN",
               "dead_session": "DEAD"}.get(status, status)
        print(f"  {tag} {pid} (run_id={run_id}) export={export_size}B pipe={pipe_size}B → {status}")
        cleaned += 1

    print(f"\n  cleaned: {cleaned}")
    for s, n in results.items():
        if n > 0:
            print(f"    {s}: {n}")


def cmd_scan_thinking(db, batch_id):
    """SOP 9: 扫描所有running session的thinking状态（tmux capture-pane）。"""
    import re
    sessions = subprocess.run(["tmux", "list-sessions"], capture_output=True, text=True).stdout
    harness_sessions = [line.split(":")[0] for line in sessions.strip().split("\n") if line.startswith("harness-dpb")]
    print(f"  tmux sessions: {len(harness_sessions)}\n")
    counts = {"PROOF": 0, "THINK": 0, "IDLE": 0, "OTHER": 0, "EMPTY": 0}
    for s in harness_sessions:
        pid = s.rstrip().rsplit("-", 1)[-1] if "-" in s else s
        try:
            r = subprocess.run(["tmux", "capture-pane", "-t", s, "-p", "-S", "-100"],
                               capture_output=True, text=True, timeout=5)
            pane = r.stdout
        except Exception:
            counts["EMPTY"] += 1
            continue
        ctx = ""
        m = re.search(r"Context: (\d+k / 200k)", pane)
        if m:
            ctx = m.group(1)
        if "PROOF COMPLETE" in pane:
            print(f"  {pid}: PROOF_COMPLETE  ctx={ctx}")
            counts["PROOF"] += 1
        elif re.search(r"Thinking · \d+m \d+s|\d+s", pane):
            t = re.findall(r"Thinking · \d+m \d+s|\d+s", pane)
            print(f"  {pid}: THINK({t[-1] if t else '?'})  ctx={ctx}")
            counts["THINK"] += 1
        elif "Ask Devin to build" in pane:
            print(f"  {pid}: IDLE  ctx={ctx}")
            counts["IDLE"] += 1
        elif len([l for l in pane.split("\n") if l.strip()]) < 3:
            print(f"  {pid}: EMPTY")
            counts["EMPTY"] += 1
        else:
            last = [l for l in pane.split("\n") if l.strip()][-1][:50]
            print(f"  {pid}: OTHER  ctx={ctx}  [{last}]")
            counts["OTHER"] += 1
    print(f"\n  summary: {counts}")


def cmd_landfall(db, batch_id, *, all_batches=False):
    """SOP 10: 批量落盘PROOF COMPLETE + 清理IDLE/truncated session。

    改进版（2026-08-12）：
    - 落盘前先capture_thinking保留thinking数据
    - stop_attempt让devin cli写export
    - 处理Response truncated（标记failed_token_limit）
    - 处理Connection error（标记failed_connection）
    - all_batches=True时处理所有batch
    """
    import sys
    sys.path.insert(0, str(Path(__file__).parent))
    from batch_problem_runner import capture_thinking, stop_attempt, SOLVER_BASE
    from datetime import datetime, timezone
    import time as _time
    now_ts = datetime.now(timezone.utc).isoformat()

    if all_batches:
        print("=== Landfall: ALL BATCHES ===")
        cursor = db.aql.execute(
            'FOR a IN devin_problem_runs '
            'FILTER a.status IN ["running", "stalled_warning", "launching"] '
            'RETURN a'
        )
    else:
        print(f"=== Landfall: {batch_id} ===")
        cursor = db.aql.execute(
            'FOR a IN devin_problem_runs FILTER a.batch_id == @bid '
            'FILTER a.status IN ["running", "stalled_warning", "launching"] RETURN a',
            bind_vars={"bid": batch_id}
        )

    solved = 0
    truncated = 0
    conn_err = 0
    cleaned = 0

    for a in cursor:
        tmux = a.get("tmux_session", "")
        pid = a.get("problem_id", "?")
        if not tmux:
            continue
        try:
            r = subprocess.run(["tmux", "capture-pane", "-t", tmux, "-p", "-S", "-500"],
                               capture_output=True, text=True, timeout=5)
        except Exception:
            continue
        pane = r.stdout

        # 如果还在thinking，跳过
        if "Thinking" in pane and "Ask Devin" not in pane and "PROOF COMPLETE" not in pane:
            continue

        if "PROOF COMPLETE" in pane:
            # 1. capture thinking
            try: capture_thinking(a)
            except: pass
            # 2. stop_attempt
            batch_dir = Path(a["paths"].get("solver_dir", "")).parent
            try: stop_attempt(db, a, batch_dir, decode=False)
            except: pass
            _time.sleep(3)
            # 3. kill tmux
            subprocess.run(["tmux", "kill-session", "-t", tmux],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
            # 4. 更新DB
            db.collection("devin_problem_runs").update({
                "_key": a["_key"], "status": "candidate_solved",
                "ended_at": now_ts, "end_reason": "manual_pane_proof_complete",
                "updated_at": now_ts,
            })
            db.collection("devin_run_events").insert({
                "batch_id": a.get("batch_id", ""),
                "attempt_key": a["_key"],
                "event_type": "attempt_solved",
                "event_data": {"problem_id": pid, "method": "landfall"},
                "observed_at": now_ts,
            })
            print(f"  SOLVED: {pid}")
            solved += 1
        elif "Response truncated" in pane:
            try: capture_thinking(a)
            except: pass
            batch_dir = Path(a["paths"].get("solver_dir", "")).parent
            try: stop_attempt(db, a, batch_dir, decode=False)
            except: pass
            _time.sleep(3)
            subprocess.run(["tmux", "kill-session", "-t", tmux],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
            db.collection("devin_problem_runs").update({
                "_key": a["_key"], "status": "failed_token_limit",
                "ended_at": now_ts, "end_reason": "response_truncated_max_token_limit",
                "updated_at": now_ts,
            })
            db.collection("devin_run_events").insert({
                "batch_id": a.get("batch_id", ""),
                "attempt_key": a["_key"],
                "event_type": "attempt_terminal",
                "event_data": {"problem_id": pid, "status": "failed_token_limit"},
                "observed_at": now_ts,
            })
            print(f"  TRUNCATED: {pid}")
            truncated += 1
        elif "Connection error" in pane:
            try: capture_thinking(a)
            except: pass
            batch_dir = Path(a["paths"].get("solver_dir", "")).parent
            try: stop_attempt(db, a, batch_dir, decode=False)
            except: pass
            _time.sleep(3)
            subprocess.run(["tmux", "kill-session", "-t", tmux],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
            db.collection("devin_problem_runs").update({
                "_key": a["_key"], "status": "failed_connection",
                "ended_at": now_ts, "end_reason": "connection_error_network",
                "updated_at": now_ts,
            })
            db.collection("devin_run_events").insert({
                "batch_id": a.get("batch_id", ""),
                "attempt_key": a["_key"],
                "event_type": "attempt_terminal",
                "event_data": {"problem_id": pid, "status": "failed_connection"},
                "observed_at": now_ts,
            })
            print(f"  CONN_ERR: {pid}")
            conn_err += 1
        elif "Ask Devin to build" in pane and "Thinking" not in pane:
            # IDLE但不是truncated——可能是其他原因的idle
            subprocess.run(["tmux", "kill-session", "-t", tmux],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
            db.collection("devin_problem_runs").update({
                "_key": a["_key"], "status": "failed_token_limit",
                "ended_at": now_ts, "end_reason": "idle_token_limited",
                "updated_at": now_ts,
            })
            print(f"  CLEANED: {pid}")
            cleaned += 1

    print(f"\n  solved: {solved}, truncated: {truncated}, conn_err: {conn_err}, cleaned: {cleaned}")
    print(f"  total freed: {solved + truncated + conn_err + cleaned}")


def cmd_recover_export(db, batch_id):
    """SOP 11: 从sessions.db恢复export=0B的题（metadata only）。"""
    import sqlite3
    import json
    from datetime import datetime, timezone
    sdb = sqlite3.connect(os.path.expanduser("~/.local/share/devin/cli/sessions.db"))
    recovered = 0
    for a in db.aql.execute(
        'FOR a IN devin_problem_runs FILTER a.batch_id == @bid '
        'FILTER a.status == "candidate_solved" RETURN a',
        bind_vars={"bid": batch_id}
    ):
        export_path = Path(a["paths"].get("export_path", ""))
        if export_path.exists() and export_path.stat().st_size > 1000:
            continue
        sid = a.get("devin_session_id", "")
        if not sid:
            continue
        rows = sdb.execute(
            "SELECT node_id, chat_message FROM message_nodes WHERE session_id=? ORDER BY node_id",
            (sid,)
        ).fetchall()
        messages = []
        for nid, msg_json in rows:
            try:
                msg = json.loads(msg_json)
                messages.append({"node_id": nid, "role": msg.get("role", ""),
                                 "content_preview": str(msg.get("content", ""))[:200]})
            except Exception:
                pass
        export_data = {
            "problem_id": a["problem_id"], "devin_session_id": sid,
            "source": "sessions_db_metadata_only",
            "warning": "export was 0B, proof content not recoverable",
            "message_count": len(messages), "messages": messages,
            "extracted_at": datetime.now(timezone.utc).isoformat(),
        }
        export_path.parent.mkdir(parents=True, exist_ok=True)
        export_path.write_text(json.dumps(export_data, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  {a['problem_id']}: {len(messages)} msgs (metadata only)")
        recovered += 1
    sdb.close()
    print(f"\n  recovered: {recovered}")


def main():
    parser = argparse.ArgumentParser(description="Batch status query tool")
    parser.add_argument("cmd", choices=["status", "active", "errors", "solved", "feed", "leak", "dead", "all",
                                        "scan-thinking", "landfall", "recover-export"])
    parser.add_argument("--batch-id", help="batch id (default: latest running)")
    parser.add_argument("--cleanup", action="store_true", help="auto-fix zombie sessions (for dead command)")
    parser.add_argument("--all-batches", action="store_true",
                        help="process all batches (for dead-cleanup and landfall)")
    args = parser.parse_args()

    db = connect_db()
    batch_id = args.batch_id or latest_batch_id(db)
    # --all-batches模式下不需要batch_id
    if not args.all_batches:
        if not batch_id:
            print("No batch found", file=sys.stderr)
            sys.exit(1)
        if not args.batch_id:
            print(f"(auto-selected batch: {batch_id})\n")

    if args.cmd in ("status", "all"):
        cmd_status(db, batch_id)
        print()
    if args.cmd in ("active", "all"):
        cmd_active(db, batch_id)
        print()
    if args.cmd in ("errors", "all"):
        cmd_errors(db, batch_id)
        print()
    if args.cmd in ("solved", "all"):
        cmd_solved(db, batch_id)
        print()
    if args.cmd in ("feed", "all"):
        cmd_feed(db, batch_id)
        print()
    if args.cmd in ("leak", "all"):
        cmd_leak(db, batch_id)
        print()
    if args.cmd == "dead":
        if args.cleanup:
            cmd_dead_cleanup(db, batch_id, all_batches=args.all_batches)
        else:
            cmd_dead(db, batch_id)
        print()
    if args.cmd == "scan-thinking":
        cmd_scan_thinking(db, batch_id)
        print()
    if args.cmd == "landfall":
        cmd_landfall(db, batch_id, all_batches=args.all_batches)
        print()
    if args.cmd == "recover-export":
        cmd_recover_export(db, batch_id)
        print()


if __name__ == "__main__":
    main()
