# 旧模式 · auto_runner.py 自动化运营系统说明

> **归档说明**：本文件记录的是第二代批量解题系统（auto_runner.py + enqueue_problem.py）的完整架构说明。该系统是batch_problem_runner.py到管道化系统之间的过渡方案，已被管道化系统（pipe/5服务）完全替代，当前不再运行。保留此文档供历史参考。
> **归档时间**：2026-08-12
> **替代系统**：管道化GLM-5.2能力边界Profile系统（见AGENTS.md中对应节）

---

## 架构

```
[AI清洗题] → enqueue_problem.py → queue_in/{pid}.txt + problem_queue表
                                        ↓
[auto_runner.py] ← 取queued题 ← problem_queue
       ↓
创建devin_problem_runs attempt → 写problem.txt → launch devin cli
       ↓
refresh循环 → 判定PROOF COMPLETE / timeout / stall / dead
       ↓
更新problem_queue的queue_status → solved / failed
       ↓
队列空 → auto_runner自动退出 → AI送新题 → 重启
```

## 三个组件

| 组件 | 文件 | 职责 |
|---|---|---|
| 送题工具 | `xishujuzhen/solver_harness/enqueue_problem.py` | AI清洗题后送入queue |
| 自动运营 | `xishujuzhen/solver_harness/auto_runner.py` | 从queue取题、launch、判定、落盘、报告 |
| 题队列 | `problem_queue` DB collection | queued/running/solved/failed状态管理 |

## 送题（AI的职责）

```bash
cd /Users/user/glm5.2-math-worktree
set -a; source .env; set +a

# 单题送入
.venv/bin/python xishujuzhen/solver_harness/enqueue_problem.py \
  --problem-id <pid> --text-file <path> --tier <n> --source <name> --answer <text> --priority 1

# 从DB批量送入某tier（跳过已solved）
.venv/bin/python xishujuzhen/solver_harness/enqueue_problem.py \
  --from-progress --tier 2 --limit 1000 --skip-solved

# 从目录扫描新题文件
.venv/bin/python xishujuzhen/solver_harness/enqueue_problem.py \
  --scan-dir <dir> --tier <n> --source <name>

# 查看队列状态
.venv/bin/python xishujuzhen/solver_harness/enqueue_problem.py --status
```

## 启动runtime

```bash
# 启动auto_runner（concurrency可配置，默认100）
tmux new-session -d -s auto-runner "set -a; source .env; set +a; \
  .venv/bin/python -u xishujuzhen/solver_harness/auto_runner.py \
  --concurrency 100 --poll-seconds 30 --report-seconds 300 \
  --max-runtime-seconds 14400 --stall-seconds 900 --stop-on-stall \
  --launch-interval 1 2>&1 | tee /Volumes/data/math-agent-glm5.2-tmux-agents-dir/logs/auto_runner.log"

# 查看日志
tail -20 /Volumes/data/math-agent-glm5.2-tmux-agents-dir/logs/auto_runner.log

# 停止
tmux kill-session -t auto-runner
```

## 监控（AI的职责）

```bash
# 队列状态
.venv/bin/python xishujuzhen/solver_harness/enqueue_problem.py --status

# auto_runner日志
tail -30 /Volumes/data/math-agent-glm5.2-tmux-agents-dir/logs/auto_runner.log

# batch状态
.venv/bin/python xishujuzhen/solver_harness/batch_status.py status --batch-id dpb-auto-runner

# scan-thinking
.venv/bin/python xishujuzhen/solver_harness/batch_status.py scan-thinking --batch-id dpb-auto-runner
```

## 关键信息

**auto_runner的batch_id**：`dpb-auto-runner`（固定）

**problem_queue DB collection关键字段**：
- `queue_status`: queued / running / solved / failed / cancelled
- `problem_text`: 题目原文
- `difficulty_tier`: 难度层级
- `priority`: 优先级（1最高10最低，auto_runner按priority升序取题）
- `attempt_keys`: 关联的`devin_problem_runs`的_key列表
- `run_count`: 被运行过几次

**固定目录**：
- `queue_in/`：`/Volumes/data/math-agent-glm5.2-tmux-agents-trajectory/_queue_in/` — 送入的题目文本文件
- `queue/`：`/Volumes/data/math-agent-glm5.2-tmux-agents-trajectory/_queue/` — 队列元数据

## AI的工作循环

1. 清洗新题 → `enqueue_problem.py`送入queue
2. 启动auto_runner（如未运行）
3. 监控auto_runner运行状态（日志、队列状态、scan-thinking）
4. 处理卡死的session（发"继续"或标记failed重跑）
5. auto_runner退出后（队列空），送入新题重启

## 与旧模式的区别

- 旧模式（batch_problem_runner）：AI手动启动batch、手动feed、手动管理concurrency
- 新模式（auto_runner）：AI只负责送题和监控，auto_runner自动管理取题/launch/判定/落盘
- 旧模式的SOP 1-16仍适用于已在运行的旧batch实例
