# 项目 AGENTS.md · 平凡解题系统（Normal Solver）

> **来源**：从 `glm5.2-math-worktree` repo 的 AGENTS.md 中拆分出来的解题系统部分（2026-08-20）。
>
> 本repo是平凡解题系统的独立repo，包含pipe系统代码、解题系统文档、solver相关skills和rules。任何AI进入本repo做解题系统运行/监控/调试/审计时，读完本文件即可接手。
>
> **详细结构信息（文档索引、代码索引、目录结构、skills/rules、操作经验）见 `README.md`。**

---

## 角色

你是平凡解题系统的Master Agent，工作目录`/Users/user/AI-Math-Normal-Solver/`。负责运行、监控、维护管道化解题系统（pipe系统），在数学题上构建GLM-5.2的数学能力边界Profile。

---

## 平凡解题系统的概念

### 什么是平凡解题系统

平凡解题系统是一个**在固定token预算下批量解题**的管道化系统。它用pipe系统（feeder/runner/collector/reporter/retry/monitor 6个服务）以非交互模式（`devin -p`）批量运行GLM-5.2解数学题，采集每道题的完整推理过程（thinking/trajectory/proof），构建GLM-5.2的数学能力边界Profile。

### 平凡解题系统处理什么题

| 题目状态 | 数量（tier=1） | 平凡解题系统是否处理 | 说明 |
|---|---|---|---|
| 从未处理过的题 | 2,565 | **处理** | feeder自动入队 |
| 基础设施失败（重试后仍失败） | 244 | **处理** | 排查系统性问题后手动重新入队 |
| token_limit失败 | 9,336 | **不处理** | 需要更大token预算，交给其他系统 |
| ai_gave_up | 498 | **不处理** | AI主动认为解不了，需要不同解题策略，交给其他系统 |
| 卡住/空转/无proof | 235 | **不处理** | 边缘case，是Profile数据，重试结果不变 |
| 数据问题（题目含答案/答案泄漏） | 639 | **不处理** | 题目本身有问题 |

### 平凡解题系统的边界

**平凡解题系统只负责"在固定token预算下能解决的题"。** 以下两类题不在平凡解题系统的处理范围内，交给其他系统：

1. **token_limit题**：AI有思路但推理链太长被token预算截断。在相同token预算下重试结果不变，需要更大token预算才能有不同结果。token_limit题是GLM-5.2在当前token预算下的能力边界Profile数据。

2. **ai_gave_up题**：AI主动输出`### I CANNOT SOLVE THIS`，认为解不了。在相同条件下重试大概率得到同样的判断，需要不同的解题策略（如引导式推理、提示词调整等）才能有不同结果。

> 详见 `dev-docs/402-v0-2026-08-21-tier1做题结果调查报告.md` 第5节。

---

## repo基本信息

- 分支：`main`
- Python：依赖原repo的`.venv/`（python3.14），路径`/Users/user/glm5.2-math-worktree/.venv/bin/python3`
- Python依赖：`arango`（ArangoDB客户端）、`redis`（Redis客户端）。标准库：json/os/sys/time/subprocess/sqlite3/pathlib/argparse
- DB：ArangoDB `localhost:8529`，`ARANGO_DB=xishujuzhen_math_glm52`
- Redis：`localhost:6379` db=0（Redis配置硬编码在`redis_queue.py`中，非.env）
- **.env文件**：本repo不含.env，运行前必须`source /Users/user/glm5.2-math-worktree/.env`。.env内容：
  ```
  export ARANGO_HOST="http://localhost:8529"
  export ARANGO_DB="xishujuzhen_math_glm52"
  export ARANGO_USER="root"
  export ARANGO_PASS="moira123"
  ```
- 题库：ArangoDB `problem_extraction_progress`集合（246万题）
- 运行记录：ArangoDB `devin_problem_runs`集合
- trajectory数据：`/Volumes/data/math-agent-glm5.2-tmux-agents-trajectory/`
- solver工作目录：`/Volumes/data/math-agent-glm5.2-tmux-agents-dir/`
- 系统日志：`/Volumes/data/math-agent-glm5.2-tmux-agents-trajectory/_pipe/logs/`

---

## 铁律

1. **DB**：`ARANGO_DB=xishujuzhen_math_glm52`，运行前必须`echo $ARANGO_DB`确认。忘了source `.env`会fallback到错误数据库。
2. **Git**：显式路径add（禁`-A`/`-.`/`-u`），改前清干净改后立即commit，push需用户授权
3. **Solver隔离**：Solver的devin cli不在本repo内运行（AGENTS.md会劫持Solver行为），外部目录`/Volumes/data/math-agent-glm5.2-tmux-agents-dir/`，`--permission-mode dangerous`
4. **Solver启动**：生产用`noninteractive-solver-run` skill（在全局`~/.config/devin/skills/`中，不在本repo），调试用`solver-tmux-launch` skill（在本repo`.devin/skills/`中），批量用pipe系统。mitmproxy已废弃。
5. **已归档系统**：`batch_problem_runner`/`auto_runner`已被pipe系统替代，不再运行。代码保留在`xishujuzhen/solver_harness/`根目录供参考。
6. **不做清单**：①不答案泄漏 ②不用覆盖率代正确 ③不伪造CoT ④不角色混用
7. **tmux优先**：pipe系统5个服务必须在tmux中运行（`pipe_start.sh`自动创建tmux session）。长时间运行的命令也必须在tmux中。
8. **禁止inline脚本**：超过3行的Python/bash逻辑必须写成文件，放到`xishujuzhen/solver_harness/pipe/scripts/`目录。

---

## 运维认知

以下认知在监控/健康检查时容易误判，必须牢记：

### 空pane是thinking spin的正常现象

devin cli以非交互模式（`-p`）启动后，首先进入thinking spin阶段——AI在思维链中推理，不输出任何内容到TUI pane。此时`tmux capture-pane`抓到的是空pane，`tmux_pipe.log`是0字节。这是**正常行为**，不是卡住。

health检查中的"空pane僵尸"告警，绝大多数是处于thinking spin阶段的正常session。判定session是否真正死亡的正确方法：看`pipe.log`中是否有`DEVIN_CLI_EXITED`标记（collector的判定逻辑，见`collector.py`第428-436行）。pane空只代表还没输出，不代表死了。

### devin cli刚启动就退出的应对

devin cli偶尔会出现刚启动就退出的情况（如API连接失败、进程crash等）。pipe系统已完整应对：

- **collector判定**：devin cli退出但无proof → 判定为`dead_session`（`collector.py`第428-436行）；tmux session不存在且无`DEVIN_CLI_EXITED`且elapsed>60s → 判定为`crash_recovered`（第442-444行）
- **retry自动重试**：`dead_session`/`launch_error`/`crash_recovered`/`failed_connection`/`rate_limited`属于基础设施失败（`INFRA_FAILURES`），`retry_infrastructure.py`会自动将它们重新入pending队列（最多`--max-retries`次）
- **模型能力失败不重试**：`failed_token_limit`/`ai_gave_up`/`failed_thinking_spin`等属于模型能力失败（`MODEL_FAILURES`），不重试，作为Profile数据保留

### collector分类顺序：CONNECTION必须优先于TOKEN_LIMIT（2026-08-22修复）

**历史bug**：collector的`classify_termination`函数中，`TOKEN_LIMIT_PATTERNS`检查在`CONNECTION_PATTERNS`之前。而`TOKEN_LIMIT_PATTERNS`含`"Send a message to continue"`，API连接错误的pane含`"send a message to continue retrying"`，导致连接错误被误匹配为`failed_token_limit`（模型能力失败），retry不会重试。

**影响**：1,111题被误分类，从未被retry重试，没有thinking数据。已重新入队重跑。

**修复**：`CONNECTION_PATTERNS`检查移到`TOKEN_LIMIT_PATTERNS`之前（两处：session运行时和session结束时）。

**详见**：`dev-docs/406-v0-2026-08-22-tier1留存结果完整性调查报告.md`§7

---

## 系统架构概要

```
ArangoDB (246万题) → Feeder → Redis pending队列
                                ↓
                          Runner (30并发) → 启动devin cli (harness-xxx tmux session)
                                ↓
                          Redis running队列
                                ↓
                          Collector → 判定终态 → ArangoDB devin_problem_runs
                                ↓
                          Redis completed/failed队列
                                ↓
                          Reporter → 统计报告
                          Retry → 基础设施失败重试
                          Monitor Pipe → 告警系统
```

### 5个服务（pipe系统核心）

| 服务 | 代码 | tmux session | 职责 |
|---|---|---|---|
| Feeder | `feeder.py` | `pipe-feeder` | 从ArangoDB选题入Redis pending队列（按tier选题，low-water-mark补充） |
| Runner | `runner.py` | `pipe-runner` | 从pending队列取题，启动devin cli（tmux session） |
| Collector | `collector.py` | `pipe-collector` | 轮询running队列，判定终态（candidate_solved/failed_token_limit等），写ArangoDB |
| Reporter | `reporter.py` | `pipe-reporter` | 定期统计报告 |
| Retry | `retry_infrastructure.py` | `pipe-retry` | 基础设施失败自动重试 |
| Monitor Pipe | `monitor_pipe.py` | `pipe-monitor` | 持续监控13项检查+AI review抽样，alert写入ArangoDB |

### 启停命令

```bash
# 前置：必须先source .env
set -a; source /Users/user/glm5.2-math-worktree/.env; set +a

# 启动（自动创建6个tmux session，默认并发30）
bash xishujuzhen/solver_harness/pipe/pipe_start.sh

# 启动时指定并发
bash xishujuzhen/solver_harness/pipe/pipe_start.sh 15    # 并发15

# 最终队列模式启动（不启动feeder，只处理pending队列中已有的3,448题）
# 用户要求仅处理tier=1的最终队列，不从ArangoDB取新题。队列消费完后系统自然停止。
# 详见 dev-docs/403-v0-2026-08-21-平凡解题系统队列准备方案.md
bash xishujuzhen/solver_harness/pipe/pipe_start.sh --no-feeder --max-retries 6

# 优雅停止（两步）
bash xishujuzhen/solver_harness/pipe/pipe_stop.sh          # 停feeder/runner等，保留collector
bash xishujuzhen/solver_harness/pipe/pipe_stop.sh --finish  # 等running=0后，停collector
# ⚠️ --finish模式有编码bug（中文输出乱码导致running数比较失败），如running已=0请用--force

# 强制停止（停所有服务，保留harness session自然完成）
bash xishujuzhen/solver_harness/pipe/pipe_stop.sh --force

# 强制kill（停所有服务+kill所有harness session）
bash xishujuzhen/solver_harness/pipe/pipe_stop.sh --kill

# 状态检查
PYTHONPATH=xishujuzhen/solver_harness/pipe /Users/user/glm5.2-math-worktree/.venv/bin/python3 xishujuzhen/solver_harness/pipe/pipe_control.py status

# 进度检查
PYTHONPATH=xishujuzhen/solver_harness/pipe /Users/user/glm5.2-math-worktree/.venv/bin/python3 xishujuzhen/solver_harness/pipe/scripts/check_progress.py

# 健康检查
PYTHONPATH=xishujuzhen/solver_harness/pipe /Users/user/glm5.2-math-worktree/.venv/bin/python3 xishujuzhen/solver_harness/pipe/pipe_control.py health

# Monitor检查
bash xishujuzhen/solver_harness/pipe/scripts/monitor_check.sh
```

### 并发量实时控制（无需重启Runner）

```bash
# 设置并发（Runner下次poll时生效，约5秒内）
redis-cli SET math:config:concurrency 20

# 查看当前并发
redis-cli GET math:config:concurrency
```

### feeder的tier控制

feeder启动参数`--tier`控制选题范围：
- `--tier 1,2,3`（默认）：tier=1跑完后自动取tier=2，再取tier=3
- `--tier 1`：只跑tier=1，跑完后feeder空转等待

修改tier需要重启feeder tmux session（不需要停整个系统）：
```bash
tmux send-keys -t pipe-feeder C-c ""
sleep 3
tmux kill-session -t pipe-feeder
# 用新参数重启（参考pipe_start.sh中的feeder启动命令）
```

---

## 数据库表设计

> **完整表结构见 `/Users/user/database/AI-Math-Normal-Solver.md`**——写查数据库脚本前必须先读。
> 5个ArangoDB集合（problem_extraction_progress/devin_problem_runs/devin_batch_runs/devin_run_events/pipe_monitor_alerts）+ Redis队列。
> 关键JOIN：`problem_extraction_progress._key` = `devin_problem_runs.problem_id`
> ⚠️ `devin_problem_runs.difficulty_tier`通常为null，统计时必须用_key JOIN problem_id。
> ⚠️ `devin_problem_runs.paths`在pipe-runner批次中缺失，通过exp_id构造路径。
