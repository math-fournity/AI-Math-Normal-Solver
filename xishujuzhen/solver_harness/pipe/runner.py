#!/usr/bin/env python3
"""runner.py — 服务2：纯启动

从Redis pending队列取题，启动devin cli，不做任何状态判定。
独立进程，维持固定并发数。

用法:
  python runner.py --concurrency 30
  python runner.py --concurrency 3 --dry-run  # dry-run：不启动devin cli
"""
import sys
import os
import time
import json
import uuid
import argparse
import subprocess
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
sys.path.insert(0, os.path.dirname(__file__))

from redis_queue import (
    get_redis, dequeue_pending, add_running, get_all_running, running_count, update_stats, ping,
)
from arango import ArangoClient
from shared_logger import get_logger
from graceful_shutdown import register_shutdown, should_stop

logger = get_logger("runner")

DB_HOST = "http://localhost:8529"
DB_NAME = "xishujuzhen_math_glm52"
DB_USER = "root"
DB_PASS = "moira123"
COLLECTION = "problem_extraction_progress"
ATTEMPT_COLLECTION = "devin_problem_runs"

TRAJECTORY_BASE = Path("/Volumes/data/math-agent-glm5.2-tmux-agents-trajectory")
PIPE_DIR = TRAJECTORY_BASE / "_pipe"
PROBLEMS_DIR = PIPE_DIR / "problems"

# solver_harness.py的路径
HARNESS_SCRIPT = Path(__file__).parent.parent / "solver_harness.py"
# .venv 在原repo（/Users/user/glm5.2-math-worktree/.venv），不在本repo
# 优先用环境变量 VENV_PYTHON，回退到原repo的绝对路径
VENV_PYTHON = Path(os.environ.get("VENV_PYTHON", "/Users/user/glm5.2-math-worktree/.venv/bin/python3"))


def load_problem_text(db, problem_key: str) -> str:
    """从ArangoDB读取题目文本"""
    doc = db.collection(COLLECTION).get(problem_key)
    if not doc:
        return ""
    return doc.get("problem_text", "")


def write_problem_file(problem_key: str, problem_text: str, exp_id: str) -> Path:
    """写题目文件到problems目录——禁止工具调用版"""
    problem_dir = PROBLEMS_DIR / exp_id
    problem_dir.mkdir(parents=True, exist_ok=True)
    problem_file = problem_dir / "AGENTS.md"
    content = f"""# Problem

{problem_text}

## 解题约束（必须严格遵守）

1. **不要使用任何工具**——不要写文件、不要执行命令、不要搜索、不要浏览网页、不要读取文件。
   你只需要在TUI中用thinking来解题。所有推理过程在你的思维中完成。

2. **直接在TUI中输出证明**——不要创建任何文件，不要使用任何工具调用。
   完成证明后，在TUI中直接输出（必须用英文原文，不要翻译成中文）：

   ### PROOF COMPLETE

3. **如果你无法做出这道题**，直接说（必须用英文原文）：

   ### I CANNOT SOLVE THIS

4. **如果你发现题目中包含了答案**（答案泄漏），直接说：

   ### ANSWER LEAK DETECTED

以上是全部约束。现在请解题。
"""
    problem_file.write_text(content, encoding="utf-8")
    return problem_file


def launch_devin_cli(exp_id: str, problem_file: Path, model: str = "glm-5.2-high",
                     print_mode: bool = True) -> str:
    """启动devin cli——直接调用solver_harness.py launch（它自己创建harness-xxx tmux session）
    返回solver_harness创建的tmux session名（harness-{exp_id}）

    print_mode=True（默认）：用-p模式，devin cli输出完成后自动退出+写export
    print_mode=False：用交互模式（--interactive），devin cli完成后进入等待输入状态
      ——历史遗留，为HintInjector设计，当前管道化系统未使用
    """
    cmd = [
        str(VENV_PYTHON), str(HARNESS_SCRIPT),
        "launch",
        "--exp-id", exp_id,
        "--problem-file", str(problem_file),
        "--model", model,
        "--no-mitm",
    ]
    if not print_mode:
        cmd.append("--interactive")

    log_file = PROBLEMS_DIR / exp_id / "launch.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)

    logger.info(f"launch_devin_cli: exp_id={exp_id}, model={model}, problem_file={problem_file}")
    logger.debug(f"launch_devin_cli: cmd={' '.join(cmd)}")

    # 直接运行solver_harness.py launch（它是同步的，会创建harness-xxx tmux session）
    launch_start = time.time()
    with open(log_file, "w") as f:
        result = subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT, timeout=60)

    launch_duration = time.time() - launch_start
    if result.returncode != 0:
        logger.error(f"launch_devin_cli FAILED: exp_id={exp_id}, returncode={result.returncode}, duration={launch_duration:.1f}s")
        # 读取launch.log的最后几行作为错误信息
        try:
            error_tail = log_file.read_text(encoding="utf-8", errors="ignore")[-500:]
            logger.error(f"launch_devin_cli error tail: {error_tail}")
        except Exception:
            pass
    else:
        logger.info(f"launch_devin_cli OK: exp_id={exp_id}, duration={launch_duration:.1f}s")

    # solver_harness创建的session名是 harness-{exp_id}
    tmux_session = f"harness-{exp_id}"
    return tmux_session


def create_attempt_record(db, problem_key: str, exp_id: str, tmux_session: str) -> str:
    """在ArangoDB中创建attempt记录"""
    attempt_key = f"pipe_{exp_id[:40]}"
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    doc = {
        "_key": attempt_key,
        "batch_id": "pipe-runner",
        "problem_id": problem_key,
        "exp_id": exp_id,
        "tmux_session": tmux_session,
        "status": "running",
        "started_at": now,
        "model": "glm-5.2-high",
    }
    try:
        db.collection(ATTEMPT_COLLECTION).insert(doc)
        logger.debug(f"create_attempt_record: inserted {attempt_key} for {problem_key}")
    except Exception as e:
        # 已存在则更新
        logger.warning(f"create_attempt_record: {attempt_key} already exists, updating. Error: {e}")
        db.collection(ATTEMPT_COLLECTION).update({"_key": attempt_key, "status": "running", "started_at": now})
    return attempt_key


def main():
    parser = argparse.ArgumentParser(description="Runner: 纯启动服务")
    parser.add_argument("--concurrency", type=int, default=30, help="并发数")
    parser.add_argument("--poll-interval", type=int, default=5, help="检查间隔秒数")
    parser.add_argument("--model", type=str, default="glm-5.2-high", help="模型名")
    parser.add_argument("--dry-run", action="store_true", help="dry-run：不启动devin cli")
    parser.add_argument("--print-mode", action="store_true", default=True, help="用-p模式（默认）：devin cli输出完成后自动退出+写export")
    parser.add_argument("--no-print-mode", dest="print_mode", action="store_false", help="用交互模式（--interactive）：历史遗留，为HintInjector设计")
    args = parser.parse_args()

    if not ping():
        logger.error("Redis连接失败, 退出")
        sys.exit(1)
    logger.info(f"Redis连接成功, concurrency={args.concurrency}, poll_interval={args.poll_interval}s, model={args.model}, dry_run={args.dry_run}")

    client = ArangoClient(hosts=DB_HOST, request_timeout=300)
    db = client.db(DB_NAME, username=DB_USER, password=DB_PASS)
    logger.info(f"ArangoDB连接成功: {DB_NAME}@{DB_HOST}")

    r = get_redis()
    total_launched = 0
    poll_count = 0

    # 注册优雅退出
    register_shutdown("runner")

    # 初始化Redis中的并发配置（如果没设置过）
    # 用redis-cli SET math:config:concurrency 50 可以实时修改
    if r.get("math:config:concurrency") is None:
        r.set("math:config:concurrency", args.concurrency)
    if r.get("math:config:poll_interval") is None:
        r.set("math:config:poll_interval", args.poll_interval)

    last_concurrency = args.concurrency
    last_poll_interval = args.poll_interval

    while True:
        # 检查优雅退出
        if should_stop():
            logger.info(f"Runner优雅退出: total_launched={total_launched}, 当前running={running_count(r)}")
            logger.info(f"已启动的harness session不受影响，继续独立运行（Collector会处理它们）")
            break

        # 实时读取并发配置
        try:
            redis_conc = int(r.get("math:config:concurrency") or args.concurrency)
            redis_poll = int(r.get("math:config:poll_interval") or args.poll_interval)
        except (ValueError, TypeError):
            redis_conc = args.concurrency
            redis_poll = args.poll_interval

        if redis_conc != last_concurrency:
            logger.info(f"并发数实时变更: {last_concurrency} → {redis_conc}")
            last_concurrency = redis_conc
        if redis_poll != last_poll_interval:
            logger.info(f"poll间隔实时变更: {last_poll_interval}s → {redis_poll}s")
            last_poll_interval = redis_poll

        poll_count += 1
        # 统计当前running数
        current_running = running_count(r)
        slots = redis_conc - current_running

        if slots <= 0:
            logger.debug(f"poll#{poll_count}: running={current_running}, 满载(concurrency={redis_conc}), 等待{redis_poll}s")
            time.sleep(redis_poll)
            continue

        # 从Redis取题
        items = dequeue_pending(r, slots)
        if not items:
            logger.debug(f"poll#{poll_count}: pending空, 等待feeder补充, slots={slots}")
            time.sleep(redis_poll)
            continue

        logger.info(f"poll#{poll_count}: 取到{len(items)}题, slots={slots}, running={current_running}")

        # 阶段1：预处理（串行，快）——读题目、检查泄漏、写文件
        launch_tasks = []  # (exp_id, problem_key, problem_file, priority)
        for problem_key, priority in items:
            exp_id = f"p{uuid.uuid4().hex[:20]}"

            if args.dry_run:
                metadata = {
                    "problem_key": problem_key,
                    "exp_id": exp_id,
                    "tmux_session": f"dry-{exp_id[:20]}",
                    "start_time": time.time(),
                    "priority": priority,
                    "dry_run": True,
                }
                add_running(r, exp_id, metadata)
                total_launched += 1
                logger.info(f"DRY-RUN 启动 {problem_key} (exp={exp_id[:20]}), total={total_launched}")
                continue

            # 读取题目文本
            problem_text = load_problem_text(db, problem_key)
            if not problem_text.strip():
                logger.warning(f"题目文本为空: {problem_key}, 跳过")
                from redis_queue import add_failed
                add_failed(r, {"problem_key": problem_key, "error": "empty_problem_text", "exp_id": exp_id, "verdict": "empty_problem_text"})
                continue

            # 答案泄漏检测已取消（2026-08-21）
            # 调查发现：原始数据集的question字段本身就包含答案值（如deepmath的"验证2875"题），
            # 这是数据集特性不是提取bug。泄漏检测会阻止正常题目进入解题系统。
            # 详见 dev-docs/404-v0-2026-08-21-题目清洗系统方案.md 的调查结论。

            # 写题目文件
            problem_file = write_problem_file(problem_key, problem_text, exp_id)
            launch_tasks.append((exp_id, problem_key, problem_file, priority))

        # 阶段2：串行启动devin cli——每3秒一个，避免rate limit
        # 60并发并行启动导致58/60个session遇到rate limit
        for exp_id, problem_key, problem_file, priority in launch_tasks:
            try:
                tmux_session = launch_devin_cli(exp_id, problem_file, args.model, print_mode=args.print_mode)
            except Exception as e:
                logger.error(f"启动失败 {problem_key}: {e}", exc_info=True)
                from redis_queue import add_failed
                add_failed(r, {"problem_key": problem_key, "error": str(e), "exp_id": exp_id, "verdict": "launch_error"})
                continue

            # 创建attempt记录
            attempt_key = create_attempt_record(db, problem_key, exp_id, tmux_session)

            # 写入Redis running队列
            metadata = {
                "problem_key": problem_key,
                "exp_id": exp_id,
                "attempt_key": attempt_key,
                "tmux_session": tmux_session,
                "start_time": time.time(),
                "priority": priority,
            }
            add_running(r, exp_id, metadata)
            total_launched += 1
            logger.info(f"启动 {problem_key} → tmux={tmux_session} (total={total_launched})")

            # 3秒间隔——避免rate limit
            time.sleep(3)

        update_stats(r)

    # 不会到达这里


if __name__ == "__main__":
    main()
