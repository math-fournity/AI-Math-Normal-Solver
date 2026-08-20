#!/usr/bin/env python3
"""solver-harness: 在tmux中启动devin cli，自动采集完整trajectory。

v1设计（全局共享mitmproxy + 事后批量解码）：
  Solver工作目录（AI可见）:  /Volumes/data/math-agent-glm5.2-tmux-agents-dir/<exp-id>/
  Trajectory数据目录（AI不可见）: /Volumes/data/math-agent-glm5.2-tmux-agents-trajectory/<exp-id>/
  共享MITM raw目录: /Volumes/data/math-agent-glm5.2-tmux-agents-trajectory/_shared/mitm_raw/

数据流：
  1. 全局共享mitmproxy（固定18888端口，--allow-hosts限制只拦截devin host）
  2. 每实验：创建目录 + 启动devin cli（走代理）+ pipe-pane兜底 + db轮询
  3. devin cli启动后回填devin_session_id（从sessions.db查找work_dir对应session）
  4. 实验stop时调用decode-all：扫描共享raw目录，按session_id分发到各实验

用法：
  python3 solver_harness.py launch --exp-id 258-matrix-test --problem-file problem.txt
  python3 solver_harness.py status --exp-id 258-matrix-test
  python3 solver_harness.py stop --exp-id 258-matrix-test
  python3 solver_harness.py list
  python3 solver_harness.py decode-all
  python3 solver_harness.py mitm start|stop|status
"""

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# 导入protobuf解码器（模块级，供_extract_session_id使用）
# mitm_thinking_intercept 目录在原repo（/Users/user/glm5.2-math-worktree），
# 本repo不含此目录，需要fallback到原repo路径
_mitm_dir = Path(__file__).parent.parent / "mitm_thinking_intercept"
if not _mitm_dir.exists():
    _mitm_dir = Path("/Users/user/glm5.2-math-worktree/xishujuzhen/mitm_thinking_intercept")
sys.path.insert(0, str(_mitm_dir))
from decode_connect_proto import extract_streaming_data, parse_connect_stream, decode_protobuf

# ============================================================
# 路径常量
# ============================================================

SOLVER_BASE = "/Volumes/data/math-agent-glm5.2-tmux-agents-dir"
TRAJECTORY_BASE = "/Volumes/data/math-agent-glm5.2-tmux-agents-trajectory"
SHARED_RAW_DIR = os.path.join(TRAJECTORY_BASE, "_shared", "mitm_raw")
MITM_PORT = 18889
MITM_ALLOW_HOSTS = r"server\.self-serve\.windsurf\.com|api\.devin\.ai|static\.devin\.ai"
SESSIONS_DB = os.path.expanduser("~/.local/share/devin/cli/sessions.db")
SCRIPT_DIR = Path(__file__).parent.parent  # xishujuzhen/
MITM_ADDON = SCRIPT_DIR / "mitm_thinking_intercept" / "mitm_proto_capture.py"
DECODE_SCRIPT = SCRIPT_DIR / "mitm_thinking_intercept" / "decode_connect_proto.py"
TRAJECTORY_MONITOR = SCRIPT_DIR / "trajectory_monitor.py"
SOLVER_AGENTS_TEMPLATE = SCRIPT_DIR.parent / "templates" / "solver_agents_md.md"
MITM_TMUX_NAME = "harness-mitmproxy"

# ============================================================
# 工具函数
# ============================================================

def solver_dir(exp_id):
    return Path(SOLVER_BASE) / exp_id

def trajectory_dir(exp_id):
    return Path(TRAJECTORY_BASE) / exp_id

def tmux_session_name(exp_id):
    return f"harness-{exp_id}"

def ensure_dirs(exp_id):
    """创建Solver工作目录和Trajectory数据目录的完整结构。"""
    sdir = solver_dir(exp_id)
    tdir = trajectory_dir(exp_id)

    # Solver目录（AI可见）
    sdir.mkdir(parents=True, exist_ok=True)

    # Trajectory目录（AI不可见）
    (tdir / "mitm").mkdir(parents=True, exist_ok=True)
    (tdir / "sessions_db").mkdir(parents=True, exist_ok=True)
    (tdir / "tmux").mkdir(parents=True, exist_ok=True)
    (tdir / "exports").mkdir(parents=True, exist_ok=True)

    # 共享raw目录
    os.makedirs(SHARED_RAW_DIR, exist_ok=True)

    return sdir, tdir

def write_session_info(exp_id, problem_file, model, prompt, extra=None):
    """写session_info.json到trajectory目录。"""
    info = {
        "exp_id": exp_id,
        "model": model,
        "prompt": prompt,
        "problem_file": problem_file,
        "solver_dir": str(solver_dir(exp_id)),
        "trajectory_dir": str(trajectory_dir(exp_id)),
        "tmux_session": tmux_session_name(exp_id),
        "mitm_port": MITM_PORT,
        "mitm_enabled": False,
        "devin_session_id": None,
        "start_timestamp": datetime.now(timezone.utc).isoformat(),
        "status": "launching",
    }
    if extra:
        info.update(extra)

    info_path = trajectory_dir(exp_id) / "session_info.json"
    with open(info_path, "w") as f:
        json.dump(info, f, indent=2, ensure_ascii=False)
    return info

def update_session_info(exp_id, updates):
    """更新session_info.json。"""
    info_path = trajectory_dir(exp_id) / "session_info.json"
    if not info_path.exists():
        return
    with open(info_path) as f:
        info = json.load(f)
    info.update(updates)
    info["updated_at"] = datetime.now(timezone.utc).isoformat()
    with open(info_path, "w") as f:
        json.dump(info, f, indent=2, ensure_ascii=False)

def tmux_has_session(name):
    """检查tmux session是否存在。"""
    result = subprocess.run(
        ["tmux", "has-session", "-t", name],
        capture_output=True
    )
    return result.returncode == 0

# ============================================================
# 共享mitmproxy管理
# ============================================================

def is_mitmproxy_running():
    """检查mitmproxy是否在运行（检测端口，兼容tmux和launchd两种启动方式）。"""
    # 方式1：检测端口是否在监听（launchd系统服务或tmux启动都能检测到）
    try:
        result = subprocess.run(
            ["lsof", "-i", f":{MITM_PORT}", "-t"],
            capture_output=True, text=True, timeout=5
        )
        if result.stdout.strip():
            return True
    except Exception:
        pass
    # 方式2：兼容旧tmux session检测
    return tmux_has_session(MITM_TMUX_NAME)

def ensure_mitmproxy_ca_trusted():
    """确保mitmproxy CA证书在Keychain中信任。"""
    ca_cert = os.path.expanduser("~/.mitmproxy/mitmproxy-ca-cert.pem")
    if not os.path.exists(ca_cert):
        print("ERROR: mitmproxy CA cert not found at", ca_cert)
        print("Run 'mitmdump' once first to generate it.")
        return False

    # 检查是否已信任
    result = subprocess.run(
        ["security", "find-certificate", "-c", "mitmproxy",
         os.path.expanduser("~/Library/Keychains/login.keychain-db")],
        capture_output=True, text=True
    )
    if result.returncode == 0:
        return True  # 已信任

    # 添加信任
    print("Adding mitmproxy CA to Keychain (requires password)...")
    result = subprocess.run(
        ["security", "add-trusted-cert", "-d", "-r", "trustRoot",
         "-k", os.path.expanduser("~/Library/Keychains/login.keychain-db"),
         ca_cert],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        print("ERROR: Failed to add CA cert:", result.stderr)
        return False
    return True

def start_shared_mitmproxy():
    """启动全局共享mitmproxy实例。

    优先使用launchd系统服务（开机自启动，两个AI共享，不冲突）。
    如果launchd服务未运行，fallback到tmux启动。
    """
    if is_mitmproxy_running():
        # 检测是launchd服务还是tmux
        try:
            result = subprocess.run(
                ["lsof", "-i", f":{MITM_PORT}", "-t"],
                capture_output=True, text=True, timeout=5
            )
            if result.stdout.strip():
                print(f"mitmproxy already running on port {MITM_PORT} (system service)")
                return True
        except Exception:
            pass
        if tmux_has_session(MITM_TMUX_NAME):
            print(f"mitmproxy already running (tmux: {MITM_TMUX_NAME})")
            return True

    # 先尝试启动launchd服务
    plist_path = os.path.expanduser("~/Library/LaunchAgents/com.user.mitmproxy-devin.plist")
    if os.path.exists(plist_path):
        subprocess.run(["launchctl", "load", plist_path], capture_output=True)
        time.sleep(3)
        if is_mitmproxy_running():
            print(f"mitmproxy started via launchd on port {MITM_PORT}")
            print(f"  raw dir: {SHARED_RAW_DIR}")
            print(f"  addon: ~/.mitmproxy/mitm_proto_capture.py")
            return True

    # fallback：tmux启动
    if not ensure_mitmproxy_ca_trusted():
        return False

    os.makedirs(SHARED_RAW_DIR, exist_ok=True)
    stderr_log = os.path.join(TRAJECTORY_BASE, "_shared", "mitm_stderr.log")
    os.makedirs(os.path.dirname(stderr_log), exist_ok=True)

    cmd = (
        f"MITM_RAW_DIR={SHARED_RAW_DIR} "
        f"MITM_TRAJECTORY_BASE={TRAJECTORY_BASE} "
        f"mitmdump --listen-port {MITM_PORT} "
        f"--allow-hosts \"{MITM_ALLOW_HOSTS}\" "
        f"-s {MITM_ADDON} --set ssl_insecure=true --no-http2 "
        f"-w {TRAJECTORY_BASE}/_shared/mitm_flows.mitm "
        f"2>{stderr_log}"
    )

    subprocess.run(
        ["tmux", "new-session", "-d", "-s", MITM_TMUX_NAME, cmd],
        capture_output=True
    )

    time.sleep(3)

    if is_mitmproxy_running():
        print(f"mitmproxy started on port {MITM_PORT} (tmux: {MITM_TMUX_NAME})")
        print(f"  raw dir: {SHARED_RAW_DIR}")
        print(f"  allow-hosts: {MITM_ALLOW_HOSTS}")
        return True
    else:
        print("ERROR: mitmproxy failed to start")
        print(f"  check stderr: {stderr_log}")
        return False

def stop_shared_mitmproxy():
    """停止mitmproxy实例（launchd服务和tmux都停）。"""
    # 停launchd服务
    plist_path = os.path.expanduser("~/Library/LaunchAgents/com.user.mitmproxy-devin.plist")
    if os.path.exists(plist_path):
        subprocess.run(["launchctl", "unload", plist_path], capture_output=True)
    # 停tmux session
    subprocess.run(["tmux", "kill-session", "-t", MITM_TMUX_NAME], capture_output=True)
    print(f"mitmproxy stopped (launchd + tmux)")

# ============================================================
# sessions.db轮询 + devin_session_id回填
# ============================================================

def find_session_id_by_work_dir(work_dir):
    """从sessions.db中查找work_dir对应的最新session_id。"""
    if not os.path.exists(SESSIONS_DB):
        return None
    conn = sqlite3.connect(SESSIONS_DB)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id FROM sessions WHERE working_directory = ? ORDER BY created_at DESC LIMIT 1",
            (str(work_dir),),
        )
        row = cur.fetchone()
        return row[0] if row else None
    finally:
        conn.close()

def backfill_devin_session_id(exp_id, max_wait=30):
    """等待devin cli在sessions.db中出现，回填devin_session_id到session_info.json。

    devin cli启动后需要几秒才在sessions.db中创建session记录。
    最多等max_wait秒。
    """
    sdir = solver_dir(exp_id)
    print(f"Waiting for devin session to appear in sessions.db (max {max_wait}s)...")

    waited = 0
    while waited < max_wait:
        session_id = find_session_id_by_work_dir(sdir)
        if session_id:
            update_session_info(exp_id, {"devin_session_id": session_id})
            print(f"  Found devin_session_id: {session_id}")
            return session_id
        time.sleep(2)
        waited += 2

    print(f"  WARN: devin session not found in sessions.db after {max_wait}s")
    print(f"  (session_info.json devin_session_id will remain null)")
    return None

def start_db_monitor(exp_id):
    """启动sessions.db轮询进程：step级trajectory实时落盘。"""
    tdir = trajectory_dir(exp_id)
    sdir = solver_dir(exp_id)
    jsonl_path = tdir / "sessions_db" / "trajectory.jsonl"
    # 确保目录存在——否则trajectory_monitor.py的open(args.output, "a")会失败
    os.makedirs(jsonl_path.parent, exist_ok=True)

    tname = f"harness-dbmon-{exp_id}"
    subprocess.run(
        ["tmux", "new-session", "-d", "-s", tname,
         f"{sys.executable} {TRAJECTORY_MONITOR} "
         f"--work-dir {sdir} --interval 3 --max-wait 7200 "
         f"-o {jsonl_path}"],
        capture_output=True
    )
    return tname

def stop_db_monitor(exp_id):
    tname = f"harness-dbmon-{exp_id}"
    subprocess.run(["tmux", "kill-session", "-t", tname], capture_output=True)

# ============================================================
# decode-all：按session_id分发
# ============================================================

def build_session_id_to_exp_mapping():
    """构建session_id → exp_id映射表（读所有实验的session_info.json）。

    注意：MITM protobuf中的session_id是云端UUID，和sessions.db中的本地session名称不同。
    此映射表用devin_session_id（本地名称）做映射，但MITM数据匹配不用这个——
    MITM数据通过_req文件中的work_dir路径匹配实验（见_match_exp_by_req_file）。
    此函数保留用于未来可能的UUID映射。
    """
    mapping = {}
    tbase = Path(TRAJECTORY_BASE)
    if not tbase.exists():
        return mapping

    for d in tbase.iterdir():
        if not d.is_dir() or d.name == "_shared":
            continue
        info_path = d / "session_info.json"
        if not info_path.exists():
            continue
        try:
            with open(info_path) as f:
                info = json.load(f)
            sid = info.get("devin_session_id")
            if sid:
                mapping[sid] = d.name
        except (json.JSONDecodeError, KeyError):
            continue

    return mapping

def build_workdir_to_exp_mapping():
    """构建work_dir路径 → exp_id映射表。

    MITM _req文件中包含work_dir路径（如/Volumes/data/.../harness-test-001），
    通过此路径匹配实验。这是MITM数据分发的主要匹配方式。
    """
    mapping = {}
    tbase = Path(TRAJECTORY_BASE)
    if not tbase.exists():
        return mapping

    for d in tbase.iterdir():
        if not d.is_dir() or d.name == "_shared":
            continue
        info_path = d / "session_info.json"
        if not info_path.exists():
            continue
        try:
            with open(info_path) as f:
                info = json.load(f)
            solver_dir = info.get("solver_dir")
            if solver_dir:
                mapping[solver_dir] = d.name
        except (json.JSONDecodeError, KeyError):
            continue

    return mapping

def _match_exp_by_req_file(req_filepath, workdir_mapping):
    """从_req文件中提取work_dir路径，匹配实验。

    _req文件是Connect protocol的request body，包含work_dir路径。
    搜索所有mapping中的work_dir路径，找到匹配的exp_id。
    """
    try:
        with open(req_filepath, "rb") as f:
            data = f.read()
        text = data.decode("utf-8", errors="replace")

        for work_dir, exp_id in workdir_mapping.items():
            if work_dir in text:
                return exp_id
    except Exception:
        pass
    return None

def cmd_decode_all(args):
    """扫描共享raw目录，解码每个.bin，通过_req文件中的work_dir匹配实验。

    匹配策略：
    1. 每个chatmsg_NNN_*.bin有对应的chatmsg_NNN_*_req.bin（request body）
    2. _req文件中包含work_dir路径（如/Volumes/data/.../harness-test-001）
    3. 通过work_dir路径匹配到实验的exp_id
    4. 解码response .bin，写入对应实验的mitm/trajectory.jsonl
    """
    if not os.path.exists(SHARED_RAW_DIR):
        print(f"Shared raw dir not found: {SHARED_RAW_DIR}")
        return 1

    # 构建work_dir → exp_id映射表
    workdir_mapping = build_workdir_to_exp_mapping()
    if not workdir_mapping:
        print("No experiments with solver_dir found in session_info.json")
        print("Will decode all to _shared/unmatched/")
    else:
        print(f"Work-dir mapping ({len(workdir_mapping)} experiments):")
        for work_dir, exp_id in workdir_mapping.items():
            print(f"  {exp_id} ← {work_dir}")

    # 扫描所有response .bin文件（排除_req文件）
    raw_files = sorted([
        f for f in os.listdir(SHARED_RAW_DIR)
        if f.endswith(".bin") and "_req" not in f
    ])

    if not raw_files:
        print(f"No .bin files in {SHARED_RAW_DIR}")
        return 0

    print(f"\nDecoding {len(raw_files)} files...")

    unmatched_dir = os.path.join(TRAJECTORY_BASE, "_shared", "unmatched")
    os.makedirs(unmatched_dir, exist_ok=True)

    stats = {"matched": 0, "unmatched": 0, "errors": 0}

    for fname in raw_files:
        fpath = os.path.join(SHARED_RAW_DIR, fname)
        try:
            # 从文件名推导_req文件名
            # chatmsg_001_024646_324511.bin → chatmsg_001_024646_324511_req.bin
            req_fname = fname.replace(".bin", "_req.bin")
            req_fpath = os.path.join(SHARED_RAW_DIR, req_fname)

            # 通过_req文件匹配实验
            exp_id = None
            if os.path.exists(req_fpath):
                exp_id = _match_exp_by_req_file(req_fpath, workdir_mapping)

            # 解码response
            result = extract_streaming_data(fpath)
            session_id = _extract_session_id(fpath)

            if exp_id:
                target_jsonl = trajectory_dir(exp_id) / "mitm" / "trajectory.jsonl"
                os.makedirs(os.path.dirname(target_jsonl), exist_ok=True)
                stats["matched"] += 1
            else:
                target_jsonl = Path(unmatched_dir) / "trajectory.jsonl"
                stats["unmatched"] += 1

            entry = {
                "source_file": fname,
                "session_id": session_id,
                "matched_exp": exp_id,
                "decoded_at": time.time(),
                "total_messages": result["total_messages"],
                "content_thinking": result["content_thinking"],
                "content_chunks_count": result["content_chunks_count"],
                "tool_calls": result["tool_calls"],
            }

            with open(target_jsonl, "a") as out:
                out.write(json.dumps(entry, ensure_ascii=False) + "\n")

        except Exception as e:
            stats["errors"] += 1
            error_log = os.path.join(unmatched_dir, "decode_errors.jsonl")
            with open(error_log, "a") as out:
                out.write(json.dumps({"error": str(e), "file": fname}, ensure_ascii=False) + "\n")

    print(f"\nDone: {stats['matched']} matched, {stats['unmatched']} unmatched, {stats['errors']} errors")
    if stats["unmatched"] > 0:
        print(f"  unmatched → {unmatched_dir}/trajectory.jsonl")
    if stats["errors"] > 0:
        print(f"  errors → {unmatched_dir}/decode_errors.jsonl")

    return 0

def _extract_session_id(filepath):
    """从GetChatMessage响应中提取session_id（protobuf field 17）。"""
    with open(filepath, "rb") as f:
        data = f.read()

    messages = parse_connect_stream(data)

    for flags, msg_data in messages:
        decoded = decode_protobuf(msg_data)
        for field_num, type_name, value in decoded:
            if field_num == 17 and type_name == "string":
                return value
            # session_id也可能在嵌套message中
            if field_num == 17 and type_name == "message":
                for fn, ft, fv in value:
                    if ft == "string":
                        return fv

    return None

# ============================================================
# 主命令：launch
# ============================================================

def cmd_launch(args):
    exp_id = args.exp_id
    problem_file = args.problem_file
    model = args.model
    prompt = args.prompt or "请按AGENTS.md中的题目直接解答。直接在TUI中输出证明，不要写任何文件，结尾输出 ### PROOF COMPLETE"
    no_mitm = args.no_mitm
    interactive = args.interactive

    print(f"=== Launching solver-harness for {exp_id} ===")

    # 1. 创建目录
    sdir, tdir = ensure_dirs(exp_id)
    print(f"Solver dir:     {sdir}")
    print(f"Trajectory dir: {tdir}")

    # 2. 把题目直接写入AGENTS.md（Solver不需要读problem.txt，省一次read工具调用）
    agents_md = sdir / "AGENTS.md"
    if problem_file and os.path.exists(problem_file):
        problem_text = open(problem_file, encoding="utf-8").read().strip()
        # AGENTS.md = 解题指令 + 答案泄漏自检 + 题目内容，Solver启动即见
        agents_content = f"""# Solver Task

You are a mathematical problem solver. Solve the problem completely.
Do not search for this exact problem, its official answer, or its solution.
You may use computation for exploration or verification.

Output your complete proof directly in your response (in this TUI).
Do NOT write any files — do not use write/edit tools.
End your proof with a line containing exactly: ### PROOF COMPLETE
Your full reasoning and output are automatically captured by the system.

## Answer Leak Self-Check (MANDATORY before solving)

Before you start solving, check the problem text below for any leaked answers, solutions, solution sketches, or formalization notes that would give away the answer or proof strategy.

If you find ANY of the following in the problem text, do NOT solve the problem. Instead output exactly:
### ANSWER LEAK DETECTED: <brief description of what leaked>

Then stop. Do not attempt to solve a problem whose answer has been leaked.

Watch for:
- Phrases like "The proof follows...", "solution sketch", "Formalization notes"
- Official solutions or answer values embedded in the problem statement
- Lean theorem statements that reveal the answer (e.g. `determine SolutionSet := {{n | ...}}`)

## Problem

{problem_text}
"""
        agents_md.write_text(agents_content, encoding="utf-8")
        print(f"Wrote AGENTS.md with problem ({len(problem_text)} chars)")
    elif problem_file:
        print(f"ERROR: problem file not found: {problem_file}")
        return 1

    # 4. 写session_info
    write_session_info(exp_id, problem_file, model, prompt, extra={"interactive": interactive})

    # 5. 确保共享mitmproxy运行
    mitm_enabled = not no_mitm
    if not no_mitm:
        if not is_mitmproxy_running():
            if not start_shared_mitmproxy():
                print("WARN: mitmproxy failed to start, continuing without MITM")
                mitm_enabled = False
        else:
            print(f"mitmproxy already running on port {MITM_PORT}")
    else:
        print("MITM disabled (--no-mitm)")

    update_session_info(exp_id, {"mitm_enabled": mitm_enabled})

    # 6. 启动devin cli in tmux
    tname = tmux_session_name(exp_id)
    export_path = tdir / "exports" / "conversation.json"
    tmux_pipe_path = tdir / "tmux" / "tmux_pipe.log"
    tmux_log_path = tdir / "tmux" / "tmux.log"

    # 构建devin cli命令
    if interactive:
        # 交互模式（不带-p，用--分隔prompt）——支持运行时send-keys提示注入
        devin_cmd = f"devin --model {model} --respect-workspace-trust false --permission-mode dangerous --export {export_path} -- '{prompt}'"
    else:
        # 单轮模式（-p）——完成后自动退出，不支持运行时输入
        # 注意：-p模式下devin cli退出后tmux session会自动销毁
        # 需要在命令后加'; sleep'保持tmux session存活一小段时间
        # 这样collector可以通过tmux pane检测PROOF COMPLETE，通过pipe.log检测DEVIN_CLI_EXITED
        # sleep 60秒后tmux session自动消失——避免collector处理慢时僵尸session长期残留
        # collector默认10秒poll间隔，60秒足够它检测6轮
        devin_cmd = f"devin -p '{prompt}' --model {model} --respect-workspace-trust false --permission-mode dangerous --export {export_path}; echo DEVIN_CLI_EXITED code=$?; sleep 60"

    if mitm_enabled:
        # 走mitmproxy代理
        # NODE_EXTRA_CA_CERTS: devin cli是Node.js应用，不读macOS Keychain，
        # 必须通过这个环境变量指向mitmproxy CA证书，否则SSL验证失败（Connection failed）
        ca_cert_path = os.path.expanduser("~/.mitmproxy/mitmproxy-ca-cert.pem")
        env_prefix = (
            f"HTTPS_PROXY=http://localhost:{MITM_PORT} "
            f"HTTP_PROXY=http://localhost:{MITM_PORT} "
            f"NODE_EXTRA_CA_CERTS={ca_cert_path} "
        )
        full_cmd = f"cd {sdir} && {env_prefix}{devin_cmd} 2>&1 | tee {tmux_log_path}"
    else:
        full_cmd = f"cd {sdir} && {devin_cmd} 2>&1 | tee {tmux_log_path}"

    subprocess.run(
        ["tmux", "new-session", "-d", "-s", tname, full_cmd],
        capture_output=True
    )

    # 7. 启动pipe-pane（raw流，简单cat >>，不过滤）
    # PROOF COMPLETE检测改用tmux capture-pane（见batch_problem_runner observe_attempt_files）
    time.sleep(0.5)  # 缩短：0.5秒足够tmux session创建
    subprocess.run(
        ["tmux", "pipe-pane", "-t", tname, f"cat >> {tmux_pipe_path}"],
        capture_output=True
    )

    update_session_info(exp_id, {"status": "running"})

    # 8. 回填devin_session_id——管道化模式下跳过等待（非阻塞）
    # 原来同步等30秒太慢，管道化系统中collector后续会处理session数据
    if interactive:
        # 交互模式仍需session_id用于send-keys
        print(f"\nBackfilling devin_session_id...")
        backfill_devin_session_id(exp_id, max_wait=30)
    else:
        # 单轮模式（管道化）——不等，直接继续
        print(f"\nSkipping devin_session_id backfill (non-interactive mode)")

    # 9. 启动sessions.db轮询
    start_db_monitor(exp_id)
    print(f"DB monitor started (step-level trajectory)")

    print(f"\n=== Launched ===")
    print(f"tmux session: {tname}")
    print(f"Observe:      tmux attach -t {tname}")
    print(f"Stop:         python3 solver_harness.py stop --exp-id {exp_id}")
    print(f"Status:       python3 solver_harness.py status --exp-id {exp_id}")

    return 0

# ============================================================
# 主命令：status
# ============================================================

def cmd_status(args):
    exp_id = args.exp_id
    tdir = trajectory_dir(exp_id)

    if not tdir.exists():
        print(f"Experiment {exp_id} not found (no trajectory dir)")
        return 1

    # session_info
    info_path = tdir / "session_info.json"
    if info_path.exists():
        with open(info_path) as f:
            info = json.load(f)
        print(f"=== Session Info ===")
        for k, v in info.items():
            print(f"  {k}: {v}")

    # tmux sessions（只查该实验的，不查共享mitmproxy）
    print(f"\n=== tmux sessions ===")
    for tname, label in [
        (tmux_session_name(exp_id), "devin cli"),
        (f"harness-dbmon-{exp_id}", "db monitor"),
    ]:
        status = "running" if tmux_has_session(tname) else "stopped"
        print(f"  {label} ({tname}): {status}")

    # 共享mitmproxy状态
    mitm_status = "running" if is_mitmproxy_running() else "stopped"
    print(f"  shared mitmproxy ({MITM_TMUX_NAME}): {mitm_status}")

    # 数据统计
    print(f"\n=== Data ===")
    mitm_jsonl = tdir / "mitm" / "trajectory.jsonl"
    if mitm_jsonl.exists():
        with open(mitm_jsonl) as f:
            lines = f.readlines()
        print(f"  MITM trajectory.jsonl: {len(lines)} entries")
    else:
        print(f"  MITM trajectory.jsonl: not yet decoded (run 'decode-all' after stop)")

    db_jsonl = tdir / "sessions_db" / "trajectory.jsonl"
    if db_jsonl.exists():
        with open(db_jsonl) as f:
            lines = f.readlines()
        print(f"  DB trajectory.jsonl: {len(lines)} entries")

    tmux_pipe = tdir / "tmux" / "tmux_pipe.log"
    if tmux_pipe.exists():
        print(f"  tmux_pipe.log: {tmux_pipe.stat().st_size} bytes")

    export = tdir / "exports" / "conversation.json"
    if export.exists():
        print(f"  conversation.json: {export.stat().st_size} bytes")

    # 共享raw目录统计
    if os.path.exists(SHARED_RAW_DIR):
        raw_files = [f for f in os.listdir(SHARED_RAW_DIR)
                     if f.endswith(".bin") and "_req" not in f]
        print(f"  shared raw files: {len(raw_files)} (all experiments)")

    return 0

# ============================================================
# 主命令：stop
# ============================================================

def cmd_stop(args):
    exp_id = args.exp_id
    decode = not args.no_decode  # 默认stop时自动decode

    print(f"=== Stopping {exp_id} ===")

    # 停止该实验的tmux sessions（不影响共享mitmproxy）
    for tname, label in [
        (tmux_session_name(exp_id), "devin cli"),
        (f"harness-dbmon-{exp_id}", "db monitor"),
    ]:
        if tmux_has_session(tname):
            subprocess.run(["tmux", "kill-session", "-t", tname], capture_output=True)
            print(f"Stopped {label} ({tname})")
        else:
            print(f"{label} ({tname}) not running")

    update_session_info(exp_id, {"status": "stopped"})

    # 自动调用decode-all
    if decode:
        print(f"\nDecoding MITM raw data...")
        cmd_decode_all(args)

    print(f"\nExperiment {exp_id} stopped.")
    print(f"  (shared mitmproxy is still running — use 'mitm stop' to stop it)")
    return 0

# ============================================================
# 主命令：list
# ============================================================

def cmd_list(args):
    print(f"=== Solver experiments ===")
    sbase = Path(SOLVER_BASE)
    if sbase.exists():
        for d in sorted(sbase.iterdir()):
            if d.is_dir():
                print(f"  {d.name}")

    print(f"\n=== Trajectory data ===")
    tbase = Path(TRAJECTORY_BASE)
    if tbase.exists():
        for d in sorted(tbase.iterdir()):
            if d.is_dir():
                if d.name == "_shared":
                    # 共享目录显示raw文件数
                    raw_files = [f for f in d.iterdir()
                                 if f.name.endswith(".bin") and "_req" not in f.name] if (d / "mitm_raw").exists() else []
                    raw_count = len(list((d / "mitm_raw").glob("chatmsg_*.bin"))) if (d / "mitm_raw").exists() else 0
                    print(f"  _shared [mitmproxy raw: {raw_count} files]")
                    continue
                info_path = d / "session_info.json"
                status = "?"
                if info_path.exists():
                    try:
                        with open(info_path) as f:
                            info = json.load(f)
                        status = info.get("status", "?")
                    except json.JSONDecodeError:
                        pass
                print(f"  {d.name} [{status}]")

    print(f"\n=== Running tmux sessions ===")
    result = subprocess.run(["tmux", "list-sessions"], capture_output=True, text=True)
    if result.returncode == 0:
        for line in result.stdout.strip().split("\n"):
            if "harness-" in line:
                print(f"  {line}")
    else:
        print("  (no tmux sessions)")

    return 0

# ============================================================
# 主命令：mitm
# ============================================================

def cmd_mitm(args):
    sub = args.subcommand

    if sub == "start":
        start_shared_mitmproxy()
        return 0
    elif sub == "stop":
        stop_shared_mitmproxy()
        return 0
    elif sub == "status":
        if is_mitmproxy_running():
            # 检测是launchd还是tmux
            try:
                result = subprocess.run(
                    ["lsof", "-i", f":{MITM_PORT}", "-t"],
                    capture_output=True, text=True, timeout=5
                )
                port_listening = bool(result.stdout.strip())
            except Exception:
                port_listening = False
            tmux_running = tmux_has_session(MITM_TMUX_NAME)

            if port_listening and not tmux_running:
                mode = "launchd system service"
            elif tmux_running:
                mode = f"tmux: {MITM_TMUX_NAME}"
            else:
                mode = "unknown"

            print(f"mitmproxy: RUNNING ({mode}, port: {MITM_PORT})")
            print(f"  raw dir: {SHARED_RAW_DIR}")
            print(f"  allow-hosts: {MITM_ALLOW_HOSTS}")
            print(f"  addon: ~/.mitmproxy/mitm_proto_capture.py (shared)")
            if os.path.exists(SHARED_RAW_DIR):
                raw_files = [f for f in os.listdir(SHARED_RAW_DIR)
                             if f.endswith(".bin") and "_req" not in f]
                print(f"  raw files captured: {len(raw_files)}")
        else:
            print(f"mitmproxy: STOPPED")
            print(f"  start with: python3 solver_harness.py mitm start")
            print(f"  (or: launchctl load ~/Library/LaunchAgents/com.user.mitmproxy-devin.plist)")
        return 0
    else:
        print("Usage: mitm start|stop|status")
        return 1

# ============================================================
# main
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description="solver-harness: 启动devin cli并自动采集完整trajectory (v1)"
    )
    sub = parser.add_subparsers(dest="command")

    # launch
    p_launch = sub.add_parser("launch", help="启动一个实验")
    p_launch.add_argument("--exp-id", required=True, help="实验ID（如 258-matrix-test）")
    p_launch.add_argument("--problem-file", help="题目文件路径")
    p_launch.add_argument("--model", default="glm-5-2", help="模型名")
    p_launch.add_argument("--prompt", help="自定义prompt（默认读problem.txt）")
    p_launch.add_argument("--no-mitm", action="store_true", help="不启用MITM代理")
    p_launch.add_argument("--interactive", action="store_true", help="交互模式（支持运行时send-keys提示注入）")

    # status
    p_status = sub.add_parser("status", help="查看实验状态")
    p_status.add_argument("--exp-id", required=True)

    # stop
    p_stop = sub.add_parser("stop", help="停止实验（不影响共享mitmproxy）")
    p_stop.add_argument("--exp-id", required=True)
    p_stop.add_argument("--no-decode", action="store_true", help="stop时不自动decode")

    # list
    sub.add_parser("list", help="列出所有实验")

    # decode-all
    sub.add_parser("decode-all", help="解码共享raw目录，按session_id分发到各实验")

    # mitm
    p_mitm = sub.add_parser("mitm", help="管理共享mitmproxy")
    p_mitm.add_argument("subcommand", choices=["start", "stop", "status"], help="start/stop/status")

    args = parser.parse_args()

    if args.command == "launch":
        return cmd_launch(args)
    elif args.command == "status":
        return cmd_status(args)
    elif args.command == "stop":
        return cmd_stop(args)
    elif args.command == "list":
        return cmd_list(args)
    elif args.command == "decode-all":
        return cmd_decode_all(args)
    elif args.command == "mitm":
        return cmd_mitm(args)
    else:
        parser.print_help()
        return 1

if __name__ == "__main__":
    sys.exit(main() or 0)
