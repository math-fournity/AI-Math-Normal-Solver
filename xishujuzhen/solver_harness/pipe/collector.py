#!/usr/bin/env python3
"""collector.py — 服务3：状态收集+判定（眼见为实版）

扫描Redis running队列，用"眼见为实"原则检测devin cli终态。

核心改进：
1. 真实thinking检测——检查pane内容是否有thinking标记和数学内容
2. 真实proof验证——PROOF COMPLETE标记+proof内容≥100字符
3. AI放弃检测——检查"I CANNOT SOLVE"/"无法"等放弃标记
4. 失败分类——区分基础设施失败(重试)和模型能力失败(Profile)

用法:
  python collector.py --poll-interval 10 --timeout 1800
  python collector.py --dry-run
"""
import sys
import os
import time
import json
import re
import argparse
import subprocess
import sqlite3
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
sys.path.insert(0, os.path.dirname(__file__))

from redis_queue import (
    get_redis, get_all_running, remove_running, add_completed, add_failed,
    update_stats, ping,
)
from arango import ArangoClient
from shared_logger import get_logger
from graceful_shutdown import register_shutdown, should_stop

logger = get_logger("collector")

DB_HOST = "http://localhost:8529"
DB_NAME = "xishujuzhen_math_glm52"
DB_USER = "root"
DB_PASS = "moira123"
ATTEMPT_COLLECTION = "devin_problem_runs"
COLLECTION = "problem_extraction_progress"

TRAJECTORY_BASE = Path("/Volumes/data/math-agent-glm5.2-tmux-agents-trajectory")

# === 标记模式 ===
PROOF_COMPLETE_MARKER = "PROOF COMPLETE"
PROOF_COMPLETE_MARKERS = ["PROOF COMPLETE", "证明完成"]  # 英文+中文

# 提示词中包含"结尾输出 ### PROOF COMPLETE"——必须排除这些上下文
_PROMPT_CONTEXT_PATTERNS = ["结尾输出", "请按AGENTS", "Pro ·", "直接在TUI中输出证明"]


def find_ai_proof_marker(text: str) -> tuple[int, str] | tuple[None, None]:
    """在pane文本中找到AI真正输出的PROOF COMPLETE标记（排除提示词中的）。

    提示词模式: "❭ 请按AGENTS.md中的题目直接解答...结尾输出 ### PROOF COMPLETE"
    AI输出模式: 在reasoning/proof末尾，通常以"### PROOF COMPLETE"单独成行

    返回 (位置索引, 匹配的marker) 或 (None, None)。
    """
    for marker in PROOF_COMPLETE_MARKERS:
        start = 0
        while True:
            idx = text.find(marker, start)
            if idx == -1:
                break
            # 检查上下文——如果附近有提示词特征，跳过
            ctx_before = text[max(0, idx - 80):idx]
            ctx_after = text[idx + len(marker):idx + len(marker) + 40]
            is_prompt = any(p in ctx_before for p in _PROMPT_CONTEXT_PATTERNS) or \
                        any(p in ctx_after for p in _PROMPT_CONTEXT_PATTERNS)
            if not is_prompt:
                return idx, marker
            start = idx + len(marker)
    return None, None
ANSWER_LEAK_MARKER = "ANSWER LEAK DETECTED"
AI_GAVE_UP_PATTERNS = [
    "I CANNOT SOLVE", "I cannot solve", "i cannot solve",
    "无法解决", "无法做出", "做不出来",
    "I give up", "i give up", "I'm unable", "无法完成",
    "### I CANNOT SOLVE THIS",
]
RATE_LIMIT_PATTERNS = ["rate limit", "rate_limit", "429", "Too Many Requests"]
TOKEN_LIMIT_PATTERNS = ["token limit", "context limit", "context_length", "maximum context",
                        "Response truncated", "max output token", "Send a message to continue"]
CONNECTION_PATTERNS = ["connection error", "ECONNREFUSED", "ETIMEDOUT", "socket hang up", "fetch failed"]
THINKING_PATTERNS = ["Thinking ·", "Thinking...", "thinking", "⠐", "⠒"]  # devin cli的thinking状态标记

# ANSI转义码清理
ANSI_ESCAPE = re.compile(r'\x1b\[[0-9;]*[a-zA-Z]|\x1b\][^\x07]*\x07|\x1b\[[0-9;]*m|\[\d+m|\[0m|\[K|\[2A|\[2C|\[\?25[a-z]|\[\?2026[a-z]')


def clean_ansi(text: str) -> str:
    """清理ANSI转义码和tmux格式字符"""
    text = ANSI_ESCAPE.sub('', text)
    text = re.sub(r'[\u2800-\u28ff]', '', text)  # braille spinner
    text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', text)
    return text


# === 基础设施失败（重试，不计入Profile）===
INFRA_FAILURES = {"failed_connection", "rate_limited", "launch_error", "dead_session", "crash_recovered"}

# === 模型能力失败（Profile数据，不重试）===
MODEL_FAILURES = {
    "failed_token_limit", "failed_output_limit", "ai_gave_up",
    "failed_thinking_spin", "failed_no_proof", "failed_stall",
    "invalid_tool_use",
}


def tmux_running(session_name: str) -> bool:
    try:
        result = subprocess.run(
            ["tmux", "has-session", "-t", session_name],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False, timeout=5
        )
        running = result.returncode == 0
        logger.debug(f"tmux_running: session={session_name} running={running}")
        return running
    except Exception as e:
        logger.warning(f"tmux_running异常: session={session_name} error={e}")
        return False


def capture_pane(session_name: str, lines: int = 500) -> str:
    try:
        result = subprocess.run(
            ["tmux", "capture-pane", "-t", session_name, "-p", "-S", f"-{lines}"],
            capture_output=True, text=True, timeout=10
        )
        text = result.stdout
        logger.debug(f"capture_pane: session={session_name} lines={lines} captured_len={len(text)}")
        return text
    except Exception as e:
        logger.warning(f"capture_pane失败: session={session_name} lines={lines} error={e}")
        return ""


def pane_is_empty(session_name: str) -> bool:
    text = capture_pane(session_name, 50)
    stripped = "\n".join(l for l in text.split("\n") if l.strip())
    is_empty = len(stripped) < 20
    if is_empty:
        logger.warning(f"pane_is_empty: session={session_name} stripped_len={len(stripped)} 判定为空白")
    else:
        logger.debug(f"pane_is_empty: session={session_name} stripped_len={len(stripped)} 非空白")
    return is_empty


def is_thinking(pane_text: str) -> bool:
    """检测AI是否正在thinking——眼见为实"""
    cleaned = clean_ansi(pane_text)
    # 检查devin cli的thinking状态标记
    for pattern in THINKING_PATTERNS:
        if pattern in cleaned:
            logger.debug(f"is_thinking=True: 匹配到thinking标记 '{pattern}' pane_len={len(cleaned)}")
            return True
    # 检查是否有数学内容（LaTeX符号、推理步骤）
    math_indicators = ["\\frac", "\\sum", "\\int", "\\Rightarrow", "therefore", "hence", "thus", "since",
                       "minimize", "maximize", "constraint", "feasible", "denote", "define", "lemma"]
    math_count = sum(1 for ind in math_indicators if ind in cleaned.lower())
    if math_count >= 2:
        logger.debug(f"is_thinking=True: 数学内容匹配 count={math_count} pane_len={len(cleaned)}")
        return True
    logger.debug(f"is_thinking=False: 无thinking标记也无足够数学内容 math_count={math_count} pane_len={len(cleaned)}")
    return False


def is_devin_waiting_api(exp_id: str) -> bool:
    """检查devin cli进程是否有到代理(localhost:7897)的ESTABLISHED连接。

    thinking spin时devin cli在等待API响应，有ESTABLISHED连接到代理。
    真卡住时（API超时/断连/进程异常）没有ESTABLISHED连接。

    用于区分"正在thinking"和"真卡住"——两者在pane空+pipe.log空时表面现象一样。
    """
    try:
        # 找exp_id对应的devin进程PID
        ps_res = subprocess.run(["ps", "aux"], capture_output=True, text=True, timeout=5)
        pids = []
        for line in ps_res.stdout.split("\n"):
            if exp_id in line and "devin" in line and "grep" not in line:
                parts = line.split()
                if len(parts) >= 2:
                    pids.append(parts[1])
        if not pids:
            logger.debug(f"is_devin_waiting_api: exp_id={exp_id} 未找到devin进程")
            return False

        # 检查每个PID是否有到7897代理的ESTABLISHED连接
        for pid in pids:
            lsof_res = subprocess.run(
                ["lsof", "-p", pid, "-i", "-n", "-P"],
                capture_output=True, text=True, timeout=10
            )
            for line in lsof_res.stdout.split("\n"):
                if "7897" in line and "ESTABLISHED" in line:
                    logger.debug(f"is_devin_waiting_api=True: exp_id={exp_id} pid={pid} 有到代理的ESTABLISHED连接")
                    return True
        logger.debug(f"is_devin_waiting_api=False: exp_id={exp_id} pids={pids} 无到代理的ESTABLISHED连接")
        return False
    except Exception as e:
        logger.warning(f"is_devin_waiting_api: exp_id={exp_id} 检查异常 {e}")
        return False


def has_real_proof(pane_text: str) -> bool:
    """验证是否有真实的proof内容——眼见为实"""
    cleaned = clean_ansi(pane_text)
    # 使用find_ai_proof_marker排除提示词中的PROOF COMPLETE
    idx, found_marker = find_ai_proof_marker(cleaned)
    if found_marker is None:
        logger.debug(f"has_real_proof=False: 未检测到AI输出的PROOF COMPLETE marker（已排除提示词）")
        return False
    logger.debug(f"has_real_proof: 检测到AI输出 '{found_marker}' marker位置={idx}")

    # TUI scrollback中，PROOF COMPLETE可能出现在输出区域中间
    # 真正的proof内容可能在marker之前或之后——两边都检查
    # 过滤TUI UI元素（非proof内容）
    ui_patterns = [
        "Thinking ·", "esc twice to interrupt", "Guide Devin while it works",
        "GLM-5.2 High", "Press opt+t to cycle thinking levels",
        "Context:", "tokens (", "bypass permissions on",
        "Ask Devin to build features", "Devin CLI", "v3000.",
        "Yapping ·", "Pro ·", "lines truncated",
    ]

    def extract_proof_content(text: str) -> str:
        """从文本中提取proof内容，过滤TUI UI元素"""
        lines = [l.strip() for l in text.split("\n") if l.strip()]
        # 过滤掉纯标记行和TUI UI行
        content_lines = []
        for l in lines:
            if l.startswith("#") and not l.startswith("### "):
                continue
            # 跳过TUI UI元素行
            if any(p in l for p in ui_patterns):
                continue
            # 跳过纯分隔线
            if set(l) <= set("─━│┃┄┅┆┇┈┉┊┋┌┍┎┏┐┑┒┓└┕┖┗┘┙┚┛├┝┞┟┠┡┢┣┤┥┦┧┨┩┪┫┬┭┮┯┰┱┲┳┴┵┶┷┸┹┺┻┼┽┾┿ "):
                continue
            content_lines.append(l)
        return "\n".join(content_lines)

    # 检查marker之前和之后的内容
    before_content = extract_proof_content(cleaned[:idx])
    after_content = extract_proof_content(cleaned[idx + len(found_marker):])
    # 合并——优先用内容更长的一侧，但也检查另一侧
    proof_content = before_content if len(before_content) >= len(after_content) else after_content
    # 如果单侧不够，合并两侧
    if len(proof_content) < 100:
        proof_content = before_content + "\n" + after_content

    if len(proof_content) < 100:
        logger.debug(f"has_real_proof=False: proof内容过短 content_len={len(proof_content)} < 100 "
                     f"before={len(before_content)} after={len(after_content)}")
        return False
    # 检查是否有数学推理内容（中英文都覆盖）
    math_indicators = ["\\frac", "\\sum", "\\int", "\\Rightarrow", "therefore", "hence", "thus",
                       "prove", "proof", "since", "let", "assume", "suppose", "consider", "we have",
                       "minimize", "maximize", "constraint", "feasible", "denote", "define", "lemma",
                       "$\\blacksquare", "\\blacksquare", "QED", "q.e.d",
                       # 中文数学推理标记
                       "因此", "故", "由", "设", "令", "假设", "考虑", "可得",
                       "方程", "边界条件", "初始条件", "解", "满足", "代入",
                       "控制方程", "建立模型", "边值问题"]
    math_count = sum(1 for ind in math_indicators if ind in proof_content.lower())
    if math_count >= 2:
        logger.debug(f"has_real_proof=True: proof内容有效 content_len={len(proof_content)} math_count={math_count}")
        return True
    logger.debug(f"has_real_proof=False: proof内容无足够数学推理 math_count={math_count} "
                 f"content_len={len(proof_content)} before={len(before_content)} after={len(after_content)}")
    return False


def check_ai_gave_up(pane_text: str) -> bool:
    """检测AI是否主动放弃"""
    for pattern in AI_GAVE_UP_PATTERNS:
        if pattern in pane_text:
            logger.debug(f"check_ai_gave_up=True: 匹配到放弃标记 '{pattern}'")
            return True
    logger.debug(f"check_ai_gave_up=False: 未匹配到任何放弃标记")
    return False


def check_tool_use(exp_id: str) -> bool:
    """检查是否有工具调用——查sessions.db"""
    # trajectory目录
    traj_dir = TRAJECTORY_BASE / exp_id / "sessions_db"
    if not traj_dir.exists():
        logger.debug(f"check_tool_use: traj_dir不存在 {traj_dir} 假设无工具调用")
        return False  # 没有sessions.db，无法检查，假设无工具调用

    # 查找sessions.db文件
    db_files = list(traj_dir.glob("*.db"))
    if not db_files:
        logger.debug(f"check_tool_use: traj_dir存在但无.db文件 {traj_dir}")
        return False

    logger.debug(f"check_tool_use: exp_id={exp_id} 找到{len(db_files)}个db文件")
    for db_file in db_files:
        try:
            conn = sqlite3.connect(str(db_file))
            cursor = conn.cursor()
            # 查message_nodes表中是否有tool_call类型
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = [r[0] for r in cursor.fetchall()]
            if "message_nodes" in tables:
                cursor.execute("SELECT COUNT(*) FROM message_nodes WHERE type LIKE '%tool%' OR role LIKE '%tool%'")
                count = cursor.fetchone()[0]
                logger.debug(f"check_tool_use: db_file={db_file.name} message_nodes表存在 tool_count={count}")
                if count > 0:
                    conn.close()
                    logger.warning(f"check_tool_use: 检测到工具调用! exp_id={exp_id} db_file={db_file.name} tool_count={count}")
                    return True
            else:
                logger.debug(f"check_tool_use: db_file={db_file.name} 无message_nodes表 tables={tables}")
            conn.close()
        except Exception as e:
            logger.debug(f"check_tool_use: db_file={db_file.name} 查询异常 {e}")
            continue
    logger.debug(f"check_tool_use=False: exp_id={exp_id} 未检测到工具调用")
    return False


def classify(attempt_meta: dict, pane_text: str, pipe_text: str, is_running: bool, elapsed: float,
             timeout: int, stall_time: int, last_activity: float) -> tuple[str, dict]:
    """眼见为实的终态判定

    -p模式下所有输出都写入tmux_pipe.log，pane只有500行scrollback。
    用 pane_text + pipe_text 合并检测，pipe.log是主要检测源（完整输出），
    pane用于补充检测thinking状态（spinner等动态内容pipe.log可能不完整）。
    """
    problem_key = attempt_meta.get("problem_key", "")
    exp_id = attempt_meta.get("exp_id", "")
    tmux_session = attempt_meta.get("tmux_session", "")

    # 合并检测文本——pipe.log是主要源，pane补充
    detect_text = pipe_text + "\n" + pane_text
    detect_lower = detect_text.lower()

    pane_len = len(pane_text)
    pipe_len = len(pipe_text)
    thinking = is_thinking(pane_text) if pane_text else False
    logger.debug(f"classify开始: problem_key={problem_key} exp_id={exp_id} elapsed={elapsed:.0f}s "
                 f"is_running={is_running} pane_len={pane_len} pipe_len={pipe_len} thinking={thinking} "
                 f"timeout={timeout} stall_time={stall_time}")

    # 1. 答案泄漏（最高优先级）
    if ANSWER_LEAK_MARKER in detect_text:
        logger.warning(f"classify判定=answer_leak: problem_key={problem_key} exp_id={exp_id} "
                       f"检测到 '{ANSWER_LEAK_MARKER}' 标记 pane_len={pane_len} pipe_len={pipe_len} elapsed={elapsed:.0f}s")
        return "answer_leak", {"problem_key": problem_key, "exp_id": exp_id, "verdict": "answer_leak", "elapsed": elapsed}

    # 2. AI主动放弃——模型能力边界
    if check_ai_gave_up(detect_text):
        logger.warning(f"classify判定=ai_gave_up: problem_key={problem_key} exp_id={exp_id} "
                       f"检测到AI放弃标记 pane_len={pane_len} pipe_len={pipe_len} elapsed={elapsed:.0f}s")
        return "ai_gave_up", {"problem_key": problem_key, "exp_id": exp_id, "verdict": "ai_gave_up", "elapsed": elapsed}

    # 3. 真实PROOF COMPLETE——验证有真实proof内容
    if has_real_proof(detect_text):
        logger.info(f"classify: 检测到真实proof problem_key={problem_key} exp_id={exp_id} pane_len={pane_len} pipe_len={pipe_len}")
        # 检查是否有工具调用
        if check_tool_use(exp_id):
            logger.warning(f"classify判定=invalid_tool_use: problem_key={problem_key} exp_id={exp_id} "
                           f"有proof但检测到工具调用 elapsed={elapsed:.0f}s")
            return "invalid_tool_use", {"problem_key": problem_key, "exp_id": exp_id, "verdict": "invalid_tool_use", "elapsed": elapsed}
        logger.info(f"classify判定=candidate_solved: problem_key={problem_key} exp_id={exp_id} "
                    f"真实proof+无工具调用 elapsed={elapsed:.0f}s")
        return "candidate_solved", {"problem_key": problem_key, "exp_id": exp_id, "verdict": "candidate_solved", "elapsed": elapsed}

    # 3.5 Response truncated——AI输出达到max token limit，卡在"Send a message to continue"
    # 即使session还在运行（is_running=True），也要检测——否则会卡住直到timeout
    for p in TOKEN_LIMIT_PATTERNS:
        if p.lower() in detect_lower:
            logger.warning(f"classify判定=failed_token_limit: problem_key={problem_key} exp_id={exp_id} "
                           f"检测到token/output limit标记 '{p}' elapsed={elapsed:.0f}s is_running={is_running}")
            return "failed_token_limit", {"problem_key": problem_key, "exp_id": exp_id, "verdict": "failed_token_limit", "elapsed": elapsed}

    # 3.6 Rate limit——API限流，即使session还在运行也要检测
    # 否则rate_limited的session要等到timeout(1800s)才会被处理，浪费并发槽位
    # 用detect_text（pane+pipe.log合并）检测——pipe.log有完整输出，pane可能滚走
    for p in RATE_LIMIT_PATTERNS:
        if p.lower() in detect_lower:
            logger.warning(f"classify判定=rate_limited: problem_key={problem_key} exp_id={exp_id} "
                           f"检测到rate limit标记 '{p}' elapsed={elapsed:.0f}s is_running={is_running}")
            return "rate_limited", {"problem_key": problem_key, "exp_id": exp_id, "verdict": "rate_limited", "elapsed": elapsed}

    # 4. 超时——区分thinking spin和真超时
    if elapsed > timeout:
        if is_thinking(pane_text):
            logger.warning(f"classify判定=failed_thinking_spin: problem_key={problem_key} exp_id={exp_id} "
                           f"超时且仍在thinking elapsed={elapsed:.0f}s > timeout={timeout}s pane_len={pane_len}")
            return "failed_thinking_spin", {"problem_key": problem_key, "exp_id": exp_id, "verdict": "failed_thinking_spin", "elapsed": elapsed}
        logger.info(f"classify判定=failed_timeout: problem_key={problem_key} exp_id={exp_id} "
                    f"超时 elapsed={elapsed:.0f}s > timeout={timeout}s pane_len={pane_len}")
        return "failed_timeout", {"problem_key": problem_key, "exp_id": exp_id, "verdict": "failed_timeout", "elapsed": elapsed}

    # 5. tmux session已结束——区分基础设施失败和模型能力失败
    if not is_running:
        logger.debug(f"classify: tmux session已结束 problem_key={problem_key} exp_id={exp_id} 检查错误标记")
        # 检查基础设施错误（用detect_text=pane+pipe.log合并检测）
        for p in RATE_LIMIT_PATTERNS:
            if p.lower() in detect_lower:
                logger.warning(f"classify判定=rate_limited: problem_key={problem_key} exp_id={exp_id} "
                               f"检测到rate limit标记 '{p}' elapsed={elapsed:.0f}s")
                return "rate_limited", {"problem_key": problem_key, "exp_id": exp_id, "verdict": "rate_limited", "elapsed": elapsed}
        for p in TOKEN_LIMIT_PATTERNS:
            if p.lower() in detect_lower:
                logger.warning(f"classify判定=failed_token_limit: problem_key={problem_key} exp_id={exp_id} "
                               f"检测到token limit标记 '{p}' elapsed={elapsed:.0f}s")
                return "failed_token_limit", {"problem_key": problem_key, "exp_id": exp_id, "verdict": "failed_token_limit", "elapsed": elapsed}
        for p in CONNECTION_PATTERNS:
            if p.lower() in detect_lower:
                logger.error(f"classify判定=failed_connection: problem_key={problem_key} exp_id={exp_id} "
                             f"检测到连接错误标记 '{p}' elapsed={elapsed:.0f}s")
                return "failed_connection", {"problem_key": problem_key, "exp_id": exp_id, "verdict": "failed_connection", "elapsed": elapsed}
        # 没有错误标记——AI没做完就结束了
        logger.warning(f"classify判定=failed_no_proof: problem_key={problem_key} exp_id={exp_id} "
                       f"session结束但无错误标记也无proof elapsed={elapsed:.0f}s pane_len={pane_len}")
        return "failed_no_proof", {"problem_key": problem_key, "exp_id": exp_id, "verdict": "failed_no_proof", "elapsed": elapsed}

    # 6. dead_session——-p模式下devin cli已退出但无proof
    # -p模式是非交互的：devin cli输出到stdout，跑完echo DEVIN_CLI_EXITED后退出
    # 判断死活的正确方法：pipe.log中有DEVIN_CLI_EXITED → devin cli已退出
    # 如果已退出但前面没检测到proof/rate_limit/token_limit → dead_session
    # 不用pane_is_empty——pane空只代表还没输出，不代表死了
    if "DEVIN_CLI_EXITED" in pipe_text:
        logger.warning(f"classify判定=dead_session: problem_key={problem_key} exp_id={exp_id} "
                       f"devin cli已退出(DEVIN_CLI_EXITED)但无proof elapsed={elapsed:.0f}s")
        return "dead_session", {"problem_key": problem_key, "exp_id": exp_id, "verdict": "dead_session", "elapsed": elapsed}

    # 6.5 crash_recovered——tmux session不存在且无DEVIN_CLI_EXITED
    # 典型场景：网络切换/断电导致tmux server死亡，所有session被杀
    # pipe.log中没有DEVIN_CLI_EXITED（devin cli没正常退出），session也不存在
    # 给60秒grace period（网络恢复后tmux可能重建），超过则判定crash_recovered
    if not is_running and "DEVIN_CLI_EXITED" not in pipe_text and elapsed > 60:
        logger.warning(f"classify判定=crash_recovered: problem_key={problem_key} exp_id={exp_id} "
                       f"tmux session不存在且无DEVIN_CLI_EXITED elapsed={elapsed:.0f}s pipe_len={pipe_len} "
                       f"疑似tmux server死亡(网络切换/断电)")
        return "crash_recovered", {"problem_key": problem_key, "exp_id": exp_id, "verdict": "crash_recovered", "elapsed": elapsed}

    # 6.6 failed_stall——devin cli卡在启动阶段
    # 症状：tmux session存在（is_running=True），但pane空白+pipe.log空+elapsed>300秒
    # 注意：pane空+pipe.log空有两种情况：
    #   a) thinking spin——devin cli在等待API响应，thinking不输出到stdout，有到代理(7897)的ESTABLISHED连接
    #   b) 真卡住——API超时/断连/进程异常，无到代理的ESTABLISHED连接
    # 只有b)才判定为failed_stall，a)继续等待（thinking可以持续1000+秒）
    # 注意：pane_text可能包含空白行和换行符（len=24但strip后=0），用strip()判断
    if is_running and len(pane_text.strip()) < 20 and pipe_len == 0 and elapsed > 300:
        if is_devin_waiting_api(exp_id):
            logger.info(f"classify跳过6.6: problem_key={problem_key} exp_id={exp_id} "
                        f"devin cli正在thinking spin（有到代理的ESTABLISHED连接）elapsed={elapsed:.0f}s 继续等待")
        else:
            logger.warning(f"classify判定=failed_stall: problem_key={problem_key} exp_id={exp_id} "
                           f"devin cli真卡住（无到代理的ESTABLISHED连接）pane_len={pane_len} pipe_len={pipe_len} elapsed={elapsed:.0f}s")
            return "failed_stall", {"problem_key": problem_key, "exp_id": exp_id, "verdict": "failed_stall", "elapsed": elapsed}

    # 7. stall——区分thinking spin和真stall
    if time.time() - last_activity > stall_time:
        stall_elapsed = time.time() - last_activity
        if is_thinking(pane_text):
            logger.warning(f"classify判定=failed_thinking_spin: problem_key={problem_key} exp_id={exp_id} "
                           f"stall且仍在thinking stall_elapsed={stall_elapsed:.0f}s > stall_time={stall_time}s")
            return "failed_thinking_spin", {"problem_key": problem_key, "exp_id": exp_id, "verdict": "failed_thinking_spin", "elapsed": elapsed}
        logger.warning(f"classify判定=failed_stall: problem_key={problem_key} exp_id={exp_id} "
                       f"stall stall_elapsed={stall_elapsed:.0f}s > stall_time={stall_time}s")
        return "failed_stall", {"problem_key": problem_key, "exp_id": exp_id, "verdict": "failed_stall", "elapsed": elapsed}

    # 未结束
    logger.debug(f"classify: 未结束 problem_key={problem_key} exp_id={exp_id} elapsed={elapsed:.0f}s 继续等待")
    return None, None


def stop_tmux(session_name: str, reason: str = "", exp_id: str = ""):
    """停止tmux session——同时清理Devin CLI session和db monitor session

    每个题创建两个tmux session：
    - harness-{exp_id} —— Devin CLI
    - harness-dbmon-{exp_id} —— db monitor
    只kill一个会导致db monitor session泄漏。

    reason: 停止原因（用于审计日志）
    exp_id: 实验ID（用于审计日志）
    """
    # 记录stop原因——这是审计关键位置
    was_running = tmux_running(session_name) if session_name else False
    if was_running:
        logger.info(f"stop_tmux: KILL活进程 session={session_name} exp_id={exp_id} reason={reason}")
    else:
        logger.info(f"stop_tmux: 清理已退出session session={session_name} exp_id={exp_id} reason={reason}")

    subprocess.run(["tmux", "kill-session", "-t", session_name],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    # 同时清理对应的db monitor session
    if session_name.startswith("harness-") and not session_name.startswith("harness-dbmon-"):
        dbmon_name = f"harness-dbmon-{session_name.removeprefix('harness-')}"
        subprocess.run(["tmux", "kill-session", "-t", dbmon_name],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        logger.info(f"stop_tmux: 同时清理dbmon session={dbmon_name} exp_id={exp_id}")


def save_pane_snapshot(exp_id: str, session_name: str) -> Path | None:
    """在判定终态前，保存完整的tmux pane内容到文件——眼见为实的物理证据"""
    if not session_name:
        logger.debug(f"save_pane_snapshot: 无session_name exp_id={exp_id} 跳过")
        return None
    pane_text = capture_pane(session_name, 2000)  # 抓2000行
    if not pane_text.strip():
        logger.warning(f"save_pane_snapshot: pane内容为空 exp_id={exp_id} session={session_name} 无法保存")
        return None
    snapshot_dir = TRAJECTORY_BASE / exp_id / "collector"
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    snapshot_file = snapshot_dir / "pane_snapshot.txt"
    snapshot_file.write_text(pane_text, encoding="utf-8")
    # 同时保存清理ANSI后的版本
    cleaned = clean_ansi(pane_text)
    clean_file = snapshot_dir / "pane_snapshot_clean.txt"
    clean_file.write_text(cleaned, encoding="utf-8")
    raw_size = snapshot_file.stat().st_size
    clean_size = clean_file.stat().st_size
    line_count = pane_text.count("\n") + 1
    logger.info(f"save_pane_snapshot: exp_id={exp_id} session={session_name} "
                f"lines={line_count} raw_size={raw_size}B clean_size={clean_size}B "
                f"saved={snapshot_file}")
    return snapshot_file


def update_db_status(db, attempt_key: str, status: str, verdict: str, result: dict):
    """更新ArangoDB中attempt和problem的状态——含完整审计字段"""
    logger.debug(f"update_db_status: attempt_key={attempt_key} status={status} verdict={verdict}")
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    elapsed = result.get("elapsed", 0)
    update_doc = {
        "_key": attempt_key,
        "status": status,
        "verdict": verdict,
        "ended_at": now,
        "runtime_seconds": int(elapsed),
        "end_reason": f"collector判定: {verdict}",
    }
    # 审计字段——让事后审计能找到物理证据
    if result.get("solve_time_seconds") is not None:
        update_doc["solve_time_seconds"] = result["solve_time_seconds"]
    if result.get("solve_time_source"):
        update_doc["solve_time_source"] = result["solve_time_source"]
    if result.get("pane_snapshot"):
        update_doc["pane_snapshot"] = result["pane_snapshot"]
    if result.get("init_overhead_seconds") is not None:
        update_doc["init_overhead_seconds"] = result["init_overhead_seconds"]
    if result.get("truncated_stall_seconds") is not None:
        update_doc["truncated_stall_seconds"] = result["truncated_stall_seconds"]
    try:
        db.collection(ATTEMPT_COLLECTION).update(update_doc)
        logger.debug(f"update_db_status: attempt更新成功 attempt_key={attempt_key} status={status} "
                     f"fields={list(update_doc.keys())}")
    except Exception as e:
        logger.error(f"update_db_status: attempt更新失败 attempt_key={attempt_key} error={e}")

    # 更新problem状态
    problem_key = result.get("problem_key")
    if not problem_key:
        try:
            attempt = db.collection(ATTEMPT_COLLECTION).get(attempt_key)
            if attempt:
                problem_key = attempt.get("problem_id")
                logger.debug(f"update_db_status: 从attempt获取problem_key={problem_key}")
        except Exception as e:
            logger.error(f"update_db_status: 获取attempt失败 attempt_key={attempt_key} error={e}")

    if problem_key:
        if status == "candidate_solved":
            try:
                db.collection(COLLECTION).update({"_key": problem_key, "extraction_status": "completed"})
                logger.debug(f"update_db_status: problem标记completed problem_key={problem_key}")
            except Exception as e:
                logger.error(f"update_db_status: problem更新completed失败 problem_key={problem_key} error={e}")
        elif verdict in INFRA_FAILURES:
            # 基础设施失败——回到pending，可以重试
            try:
                db.collection(COLLECTION).update({"_key": problem_key, "extraction_status": "pending"})
                logger.debug(f"update_db_status: problem回退pending(基础设施失败) problem_key={problem_key} verdict={verdict}")
            except Exception as e:
                logger.error(f"update_db_status: problem回退pending失败 problem_key={problem_key} error={e}")
        elif verdict in MODEL_FAILURES:
            # 模型能力失败——标记为failed，不重试
            try:
                db.collection(COLLECTION).update({"_key": problem_key, "extraction_status": "failed"})
                logger.debug(f"update_db_status: problem标记failed(模型能力失败) problem_key={problem_key} verdict={verdict}")
            except Exception as e:
                logger.error(f"update_db_status: problem标记failed失败 problem_key={problem_key} error={e}")


def main():
    parser = argparse.ArgumentParser(description="Collector: 状态收集+判定（眼见为实版）")
    parser.add_argument("--poll-interval", type=int, default=10)
    parser.add_argument("--timeout", type=int, default=1800, help="超时秒数（默认30分钟）")
    parser.add_argument("--stall-time", type=int, default=300, help="stall判定秒数（默认5分钟）")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--print-mode", action="store_true", default=True, help="-p模式（默认）：devin cli输出完成后自动退出+写export")
    parser.add_argument("--no-print-mode", dest="print_mode", action="store_false", help="交互模式：用三阶段export保证逻辑")
    args = parser.parse_args()

    if not ping():
        logger.error("Redis连接失败, 退出")
        sys.exit(1)
    logger.info(f"Redis连接成功, poll={args.poll_interval}s, timeout={args.timeout}s, stall={args.stall_time}s")
    logger.info("眼见为实模式: 真实thinking检测+真实proof验证+无工具调用验证")

    client = ArangoClient(hosts=DB_HOST, request_timeout=300)
    db = client.db(DB_NAME, username=DB_USER, password=DB_PASS)
    logger.info("ArangoDB连接成功")

    r = get_redis()

    # 累计统计
    total_completed = 0
    total_failed = 0
    total_infra = 0
    round_num = 0

    # 注册优雅退出
    register_shutdown("collector")

    while True:
        # 检查优雅退出
        if should_stop():
            logger.info(f"Collector优雅退出: round={round_num} total_completed={total_completed} total_failed={total_failed}")
            logger.info(f"running队列中的attempt不受影响，harness session继续独立运行")
            logger.info(f"重启Collector后可以继续处理这些running attempt")
            break

        round_num += 1
        try:
            running = get_all_running(r)
        except Exception as e:
            logger.error(f"get_all_running失败(轮{round_num}): {e}", exc_info=True)
            time.sleep(args.poll_interval)
            continue

        if not running:
            logger.debug(f"无running attempt, 等待{args.poll_interval}s...")
            time.sleep(args.poll_interval)
            continue

        logger.info(f"第{round_num}轮扫描: {len(running)}个running attempt")
        completed_this_round = 0
        failed_this_round = 0
        infra_failures_this_round = 0

        for exp_id, meta in running.items():
            try:
                if args.dry_run:
                    result = {"problem_key": meta.get("problem_key", ""), "exp_id": exp_id, "verdict": "dry_run_complete", "elapsed": 0}
                    add_completed(r, result)
                    remove_running(r, exp_id)
                    completed_this_round += 1
                    logger.debug(f"dry_run: exp_id={exp_id} 标记完成")
                    continue

                tmux_session = meta.get("tmux_session", "")
                start_time = meta.get("start_time", time.time())
                last_activity = meta.get("last_activity", start_time)
                elapsed = time.time() - start_time

                is_running = tmux_running(tmux_session) if tmux_session else False
                pane_text = capture_pane(tmux_session) if is_running else ""

                # 读tmux_pipe.log——-p模式下所有输出都写入此文件，比pane更可靠
                # pane只有500行scrollback，pipe.log有完整输出
                pipe_log_path = TRAJECTORY_BASE / exp_id / "tmux" / "tmux_pipe.log"
                pipe_text = ""
                if pipe_log_path.exists():
                    try:
                        pipe_text = pipe_log_path.read_text(encoding="utf-8", errors="ignore")
                    except Exception as e:
                        logger.debug(f"pipe.log读取失败: exp_id={exp_id} error={e}")

                status, result = classify(
                    meta, pane_text, pipe_text, is_running, elapsed,
                    args.timeout, args.stall_time, last_activity
                )

                if status is None:
                    # 还在运行中——更新last_activity
                    meta["last_activity"] = time.time()
                    r.hset("math:running", exp_id, json.dumps(meta))
                    logger.debug(f"仍在运行 exp_id={exp_id} elapsed={elapsed:.0f}s 更新last_activity")
                    continue

                # 终态确定
                result["attempt_key"] = meta.get("attempt_key", "")
                result["tmux_session"] = tmux_session
                logger.info(f"终态确定: exp_id={exp_id} status={status} problem_key={meta.get('problem_key', '')} elapsed={elapsed:.0f}s")

                # 眼见为实：保存完整pane内容作为物理证据
                if is_running and tmux_session:
                    snapshot = save_pane_snapshot(exp_id, tmux_session)
                    if snapshot:
                        result["pane_snapshot"] = str(snapshot)

                # === export落盘保证 ===
                # -p模式（print_mode=True）：devin cli输出完成后自动退出，退出时写export
                #   只需等tmux session结束（最多120秒），然后检查export是否存在
                # 交互模式（print_mode=False）：devin cli完成后不退出，进入"Ask Devin to build"
                #   用三阶段保证：等Ask Devin → Ctrl-C → 等export
                # 基础设施失败（rate_limited/failed_connection/dead_session）跳过export等待——
                # devin cli异常退出不会写export，等120秒纯属浪费时间且阻塞主循环
                # failed_stall（6.6检测：devin cli卡在启动阶段）也跳过——卡住的devin cli不会写export
                if is_running and tmux_session and status not in INFRA_FAILURES and status != "failed_stall":
                    export_path = TRAJECTORY_BASE / exp_id / "exports" / "conversation.json"
                    if export_path.exists():
                        logger.info(f"export已存在: exp_id={exp_id} size={export_path.stat().st_size}B (无需等待)")
                    elif args.print_mode:
                        # -p模式：等devin cli退出（检测"DEVIN_CLI_EXITED"标记或tmux session结束）
                        # devin cli退出时会写export文件
                        logger.info(f"export等待(-p模式): exp_id={exp_id} 等devin cli自动退出+写export")
                        wait_start = time.time()
                        devin_exited = False
                        for _ in range(120):  # 最多等120秒
                            time.sleep(1)
                            if export_path.exists():
                                break
                            # 检测"DEVIN_CLI_EXITED"标记——solver_harness在devin cli退出后会echo这个标记
                            pane_check = capture_pane(tmux_session)
                            if "DEVIN_CLI_EXITED" in pane_check:
                                devin_exited = True
                                # devin cli已退出，再等2秒让export写完
                                time.sleep(2)
                                break
                        wait_elapsed = time.time() - wait_start
                        if export_path.exists():
                            logger.info(f"export写完(-p模式): exp_id={exp_id} 等待{wait_elapsed:.0f}s size={export_path.stat().st_size}B (devin cli已退出={devin_exited})")
                        else:
                            logger.error(f"export未写完(-p模式): exp_id={exp_id} 等待{wait_elapsed:.0f}s devin_exited={devin_exited} tmux_running={tmux_running(tmux_session)} 标记export_missing")
                            result["export_missing"] = True
                            result["verdict"] = "export_missing"
                    else:
                        # 交互模式：三阶段保证（原有逻辑保留）
                        # 阶段1：等"Ask Devin to build"出现（最多120秒）
                        wait_start = time.time()
                        ask_devin_appeared = False
                        for _ in range(120):
                            time.sleep(1)
                            pane_check = capture_pane(tmux_session)
                            if "Ask Devin to build" in pane_check:
                                ask_devin_appeared = True
                                break
                            if export_path.exists():
                                break
                        wait1_elapsed = time.time() - wait_start

                        if export_path.exists():
                            logger.info(f"export写完: exp_id={exp_id} 等待{wait1_elapsed:.0f}s (Ask Devin出现)")
                        elif ask_devin_appeared:
                            for _ in range(5):
                                time.sleep(1)
                                if export_path.exists():
                                    break
                            if export_path.exists():
                                logger.info(f"export写完: exp_id={exp_id} Ask Devin后等{time.time()-wait_start:.0f}s")
                            else:
                                logger.error(f"Ask Devin出现但export未写: exp_id={exp_id} 标记export_missing")
                                result["export_missing"] = True
                        else:
                            # 阶段2：120秒超时，发Ctrl-C让devin cli停止生成
                            logger.warning(f"120秒未出现Ask Devin: exp_id={exp_id} 发Ctrl-C停止生成")
                            subprocess.run(["tmux", "send-keys", "-t", tmux_session, "C-c"], capture_output=True)
                            # 阶段3：等export文件出现（最多30秒）
                            for _ in range(30):
                                time.sleep(1)
                                pane_check = capture_pane(tmux_session)
                                if "Ask Devin to build" in pane_check or export_path.exists():
                                    break
                            wait_total = time.time() - wait_start
                            if export_path.exists():
                                logger.info(f"export写完(Ctrl-C后): exp_id={exp_id} 总等待{wait_total:.0f}s")
                            else:
                                logger.error(f"Ctrl-C后export仍不存在: exp_id={exp_id} 标记export_missing (不重新入队，避免循环)")
                                result["export_missing"] = True
                                result["verdict"] = "export_missing"

                # 提取精确解题时间（优先从tmux_pipe.log mtime，其次conversation.json，最后trajectory.jsonl）
                # 注意：conversation.json的steps时间戳不覆盖完整解题过程，tmux_pipe.log最可靠
                # 但tmux_pipe.log的birthtime比runner.start_time晚（pipe-pane在tmux session创建0.5秒后启动），
                # 导致solve_time可能比runtime大——需要cap到runtime
                try:
                    from extract_solve_time import extract_solve_time
                    time_info = extract_solve_time(exp_id)
                    if time_info.get("solve_time"):
                        raw_solve_time = time_info["solve_time"]["solve_time_seconds"]
                        # cap到runtime——solve_time不应超过runtime
                        if raw_solve_time > elapsed:
                            logger.warning(f"solve_time > runtime: exp_id={exp_id} solve_time={raw_solve_time:.1f}s > runtime={elapsed:.0f}s, cap到runtime")
                            raw_solve_time = elapsed
                        result["solve_time_seconds"] = round(raw_solve_time, 1)
                        result["solve_time_source"] = time_info["solve_time"]["source"]
                        logger.info(f"extract_solve_time: exp_id={exp_id} source={time_info['solve_time']['source']} "
                                    f"solve_time={result['solve_time_seconds']}s")
                    if time_info.get("time_breakdown"):
                        tb = time_info["time_breakdown"]
                        if tb.get("init_overhead_seconds") is not None:
                            result["init_overhead_seconds"] = round(tb["init_overhead_seconds"], 1)
                            logger.debug(f"extract_solve_time: exp_id={exp_id} init_overhead={result['init_overhead_seconds']}s")
                except Exception as e:
                    logger.debug(f"extract_solve_time失败(首次): exp_id={exp_id} error={e}")

                # failed_token_limit特殊标注：solve_time是AI实际推理时间，
                # runtime_seconds包含卡在"Send a message to continue"的等待时间
                # 需要记录truncated_stall_seconds让事后审计能区分推理时间vs卡住时间
                if status == "failed_token_limit" and result.get("solve_time_seconds") is not None:
                    truncated_stall = elapsed - result["solve_time_seconds"]
                    result["truncated_stall_seconds"] = round(truncated_stall, 1)
                    # 标注solve_time_source为truncated场景下的估算
                    result["solve_time_source"] = result.get("solve_time_source", "") + " (truncated: 推理时间，不含卡住等待)"
                    logger.info(f"failed_token_limit时间标注: exp_id={exp_id} "
                                f"solve_time={result['solve_time_seconds']}s (推理) "
                                f"truncated_stall={truncated_stall:.0f}s (卡住等待) "
                                f"runtime={elapsed:.0f}s (总)")

                if status == "candidate_solved":
                    add_completed(r, result)
                    completed_this_round += 1
                    logger.info(f"SOLVED {meta.get('problem_key', '')} exp_id={exp_id} ({elapsed:.0f}s)")
                elif status == "answer_leak":
                    add_completed(r, result)
                    completed_this_round += 1
                    logger.warning(f"ANSWER LEAK {meta.get('problem_key', '')} exp_id={exp_id} ({elapsed:.0f}s)")
                elif status in INFRA_FAILURES:
                    add_failed(r, result)
                    infra_failures_this_round += 1
                    logger.warning(f"INFRA {status} {meta.get('problem_key', '')} exp_id={exp_id} ({elapsed:.0f}s) → 可重试")
                else:
                    add_failed(r, result)
                    failed_this_round += 1
                    logger.warning(f"MODEL {status} {meta.get('problem_key', '')} exp_id={exp_id} ({elapsed:.0f}s) → Profile数据")

                # export已在前面等待过——这里直接stop_tmux
                # -p模式下devin cli已自动退出，stop_tmux只是清理残留session
                # 交互模式下devin cli不会自然退出，stop_tmux会kill活进程

                # 停tmux——记录终态原因用于审计
                if tmux_session:
                    stop_tmux(tmux_session, reason=f"终态={status}/{result.get('verdict','')}", exp_id=exp_id)
                    logger.debug(f"stop_tmux: session={tmux_session}")

                # 先更新DB再移除Redis——确保DB更新失败时记录还在Redis running中，
                # 下一轮collector会重试更新（避免出现Redis已移除但DB没更新的孤儿记录）
                attempt_key = meta.get("attempt_key", "")
                if attempt_key:
                    update_db_status(db, attempt_key, status, result.get("verdict", ""), result)
                else:
                    logger.warning(f"attempt_key为空, 跳过DB更新: exp_id={exp_id} status={status}")

                # DB更新成功后再从Redis running移除
                remove_running(r, exp_id)

            except Exception as e:
                logger.error(f"处理attempt失败(轮{round_num}): exp_id={exp_id} error={e}", exc_info=True)
                # 不break——继续处理下一个attempt，单个失败不影响整轮

        try:
            update_stats(r)
            total_completed += completed_this_round
            total_failed += failed_this_round
            total_infra += infra_failures_this_round
            logger.info(f"第{round_num}轮扫描结束: 本轮 completed={completed_this_round} failed={failed_this_round} infra={infra_failures_this_round} | "
                        f"累计 completed={total_completed} failed={total_failed} infra={total_infra}")
        except Exception as e:
            logger.error(f"update_stats失败(轮{round_num}): {e}", exc_info=True)

        time.sleep(args.poll_interval)


if __name__ == "__main__":
    main()
