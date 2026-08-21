# 项目 AGENTS.md · 平凡解题系统（Normal Solver）

> **来源**：从 `glm5.2-math-worktree` repo 的 AGENTS.md 中拆分出来的解题系统部分（2026-08-20）。
>
> 本repo是平凡解题系统的独立repo，包含pipe系统代码、解题系统文档、solver相关skills和rules。任何AI进入本repo做解题系统运行/监控/调试/审计时，读完本文件即可接手。

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
- **retry自动重试**：`dead_session`/`launch_error`/`crash_recovered`属于基础设施失败（`INFRA_FAILURES`），`retry_infrastructure.py`会自动将它们重新入pending队列（最多`--max-retries 3`次）
- **模型能力失败不重试**：`failed_token_limit`/`ai_gave_up`/`failed_thinking_spin`等属于模型能力失败（`MODEL_FAILURES`），不重试，作为Profile数据保留

---

## 文档索引

**回答问题前，先判断需要加载哪些文档：**

| 你要做什么 | 加载什么 |
|---|---|
| 管道化Profile系统（pipe/5服务+Redis）的完整架构和运行手册 | `SolverPipeSystem.md` |
| Solver运行操作SOP（编译验证SOP G1/资产追溯SOP G2/失败分类SOP G3） | `SolverOpsSOP.md` |
| 查询解题结果/失败题/运行资产位置/追溯方法/16种终态status分类 | `解题系统审计方法.md` |
| 解题系统进展交接（tier=1进度/6.6检测修复/已知问题/待办） | `dev-docs/398-v0-2026-08-19-解题系统进展交接文档.md` |
| tier=1做题结果调查报告（调查方法+三大类分类统计+关键发现） | `dev-docs/402-v0-2026-08-21-tier1做题结果调查报告.md` |
| 平凡解题系统队列准备方案（一次性入队2809题，之后不启动feeder） | `dev-docs/403-v0-2026-08-21-平凡解题系统队列准备方案.md` |
| 题目清洗系统方案（**已废弃**→改为取消泄漏检测，调查发现原始数据本身就包含答案） | `dev-docs/404-v0-2026-08-21-题目清洗系统方案.md` |
| 解析--export导出的conversation.json（ATIF格式，含reasoning_content=thinking） | `devin-cli-export-conversation.md` |
| 解析sessions_db导出的trajectory.jsonl（JSONL格式，含thinking/tool_calls/tool行） | `trajectory-schema.md` |
| conversation.json面包屑地图方案（结构未知时遍历） | `conversation-map.md` |

### SolverPipeSystem.md中的关键节

| 节 | 位置 | 内容 |
|---|---|---|
| 系统架构（5服务+Monitor Pipe+Redis队列） | 开头 | 5个服务的职责和交互流程 |
| 启动流程 | 中部 | `pipe_start.sh`的完整启动过程（前置检查→恢复crash→启动5服务→启动monitor） |
| 停止流程 | 中部 | 优雅停止两步流程 |
| 并发量实时控制 | 中部 | 如何用Redis命令实时修改并发（无需重启Runner） |
| 断电恢复流程 | 中部 | `recover_from_crash.py`的工作方式 |
| DB schema关键表 | 后部 | 4个ArangoDB集合的用途和关键字段 |
| 数据完整性表 | 后部 | export/pipe/thinking_capture/sessions.db各有什么和缺什么 |
| 看Solver的4种方法 | 后部 | tmux attach/capture-pane/tail pipe.log/读export |

### SolverOpsSOP.md中的关键节

| 节 | 内容 |
|---|---|
| SOP G1 · 编译验证 | 改代码后必须做的验证步骤 |
| SOP G2 · 资产追溯规范 | exp_id格式、资产存放位置表、从DB查文件路径的方法、禁止事项 |
| SOP G3 · AI失败题分类 | 4类AI失败+3类基础设施失败的status和end_reason对照表 |

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

## 代码文件索引

### pipe系统核心代码（`xishujuzhen/solver_harness/pipe/`）

| 文件 | 用途 | 用法 |
|---|---|---|
| `feeder.py` | 服务1：从ArangoDB选题入Redis pending队列 | `python feeder.py --tier 1 --batch-size 500 --low-water-mark 1000` |
| `runner.py` | 服务2：从pending队列取题，启动devin cli | `python runner.py --print-mode` |
| `collector.py` | 服务3：轮询running队列，判定终态 | `python collector.py --poll-interval 10 --timeout 1800 --print-mode` |
| `reporter.py` | 服务4：定期统计报告 | `python reporter.py --interval 60` |
| `retry_infrastructure.py` | 服务5：基础设施失败自动重试 | `python retry_infrastructure.py --max-retries 3` |
| `monitor_pipe.py` | Monitor Pipe：持续监控+alert | `python monitor_pipe.py --interval 300 --concurrency 30` |
| `pipe_control.py` | 管道化系统控制工具（start/stop/status/health） | `python pipe_control.py status` |
| `redis_queue.py` | Redis队列操作封装（4个服务共用） | 被其他模块import，不直接运行 |
| `shared_logger.py` | 共享日志基础设施 | 被其他模块import |
| `graceful_shutdown.py` | 优雅退出支持（SIGTERM/SIGINT处理） | 被其他模块import |
| `recover_from_crash.py` | 断电恢复+僵尸清理 | `pipe_start.sh`自动调用，或手动`python recover_from_crash.py` |
| `pipe_start.sh` | 启动脚本（创建6个tmux session） | `bash pipe_start.sh [并发数]` |
| `pipe_stop.sh` | 停止脚本（4种模式） | `bash pipe_stop.sh [--finish|--force|--kill]` |
| `pipe_watchdog.sh` | watchdog脚本（已从launchd移除，手动运行） | `bash pipe_watchdog.sh` |

### pipe系统工具脚本（`xishujuzhen/solver_harness/pipe/`）

| 文件 | 用途 | 用法 |
|---|---|---|
| `audit_trace.py` | 双向追溯验证（DB记录↔物理文件） | `python audit_trace.py --all` |
| `build_profile.py` | GLM-5.2能力边界Profile构建 | `python build_profile.py --summary` |
| `check_answer_leak.py` | 答案泄漏检查 | 被feeder调用 |
| `extract_solve_time.py` | 精确解题时间提取（从tmux_pipe.log的mtime） | 被collector调用 |
| `prepare_queue.py` | 队列准备：从难题开始分层入队 | `python prepare_queue.py` |
| `query_failures.py` | 失败分类统计查询（支持`--by-category --tier N`按三大类分类） | `python query_failures.py --by-category --tier 1` |
| `query_progress.py` | 运行进度查询 | `python query_progress.py` |
| `reclassify_failed.py` | 重新分类被误判的failed记录 | `python reclassify_failed.py` |
| `verify_completeness.py` | 数据完备性验证 | `python verify_completeness.py --all` |
| `verify_run_integrity.py` | 运行完整性验证 | `python verify_run_integrity.py` |

### scripts/目录（运维脚本）

| 文件 | 用途 | 用法 |
|---|---|---|
| `check_progress.py` | 一键检查进度（Redis状态/tier分布/吞吐/rate limit预警） | `python check_progress.py [--verbose]` |
| `filter_pending_by_tier.py` | 从Redis pending队列中移除非指定tier的题 | `python filter_pending_by_tier.py --tier 1 [--dry-run]` |
| `concurrency_safety_check.py` | 并发安全检查 | `python concurrency_safety_check.py` |
| `fix_orphan_running.py` | 修复孤儿running记录 | `python fix_orphan_running.py` |
| `recover_lost_problems.py` | 恢复runner launch失败后从队列丢失的题 | `python recover_lost_problems.py [--dry-run]` |
| `check_exports.py` | 全量检查solved题的export文件存在性和大小合理性 | `python check_exports.py [--sample N|--recent N]` |
| `check_and_report.py` | 系统统计+export结构完整性检查（增量，DB标记已检查过的） | `python check_and_report.py [--recheck|--stats-only|--limit N]` |
| `check_retry_effect.py` | 验证retry效果：按每题最后run vs 按所有run统计（正确调查AI未解决问题） | `python check_retry_effect.py --tier 1` |
| `check_tier1_remaining.py` | 查tier还有哪些题需要解决（从未处理/应重试/看情况/不应重试） | `python check_tier1_remaining.py --tier 1` |
| `reenqueue_leak_problems.py` | 把被误判为answer_leak的639题重新入队（泄漏检测已取消） | `python reenqueue_leak_problems.py [--dry-run]` |
| `monitor_check.sh` | Monitor Pipe检查脚本 | `bash monitor_check.sh` |

### 已归档代码（`xishujuzhen/solver_harness/`根目录）

| 文件 | 用途 | 状态 |
|---|---|---|
| `solver_harness.py` | 原始solver-harness（在tmux中启动devin cli） | 已被pipe系统替代，保留参考 |
| `batch_problem_runner.py` | 旧模式批量runner | 已归档 |
| `auto_runner.py` | 旧模式自动运营 | 已归档 |
| `batch_status.py` | 旧模式批次状态查询 | 已归档 |
| `enqueue_problem.py` | 旧模式送题工具 | 已归档 |
| `extract_problem_text.py` | 从源文件提取题目文本 | 已归档 |

---

## 数据库表设计

> **完整表结构见 `/Users/user/database/AI-Math-Normal-Solver.md`**——写查数据库脚本前必须先读。
> 5个ArangoDB集合（problem_extraction_progress/devin_problem_runs/devin_batch_runs/devin_run_events/pipe_monitor_alerts）+ Redis队列。
> 关键JOIN：`problem_extraction_progress._key` = `devin_problem_runs.problem_id`
> ⚠️ `devin_problem_runs.difficulty_tier`通常为null，统计时必须用_key JOIN problem_id。
> ⚠️ `devin_problem_runs.paths`在pipe-runner批次中缺失，通过exp_id构造路径。

---

## 运行目录结构

### trajectory数据（`/Volumes/data/math-agent-glm5.2-tmux-agents-trajectory/`）

```
/Volumes/data/math-agent-glm5.2-tmux-agents-trajectory/
├── _pipe/
│   ├── logs/                    # 系统日志
│   │   ├── collector.log        # collector日志（终态判定过程）
│   │   ├── pipe.log             # 主日志
│   │   ├── runner.log           # runner日志
│   │   ├── feeder.log           # feeder日志
│   │   ├── reporter.log         # reporter日志
│   │   ├── retry.log            # retry日志
│   │   └── monitor.log          # monitor日志
│   └── problems/<exp_id>/AGENTS.md  # 题目文件（含题目原文+解题约束）
├── <exp_id>/                    # 每个run的trajectory目录
│   ├── session_info.json        # 元数据（exp_id/model/prompt/devin_session_id/start_timestamp）
│   ├── exports/
│   │   └── conversation.json    # --export导出的ATIF格式对话（含reasoning_content=thinking）
│   ├── sessions_db/
│   │   └── trajectory.jsonl     # sessions.db导出的trajectory（JSONL格式）
│   ├── collector/
│   │   ├── pane_snapshot.txt    # collector采集的tmux pane快照（终态时截取）
│   │   └── pane_snapshot_clean.txt  # 清理后的pane快照（去控制字符）
│   ├── mitm/                     # MITM截获数据（仅早期批次，mitmproxy已废弃）
│   │   ├── thinking_live.jsonl
│   │   ├── thinking_live.txt
│   │   ├── thinking_readable.txt
│   │   └── trajectory.jsonl
│   └── tmux/
│       ├── tmux_pipe.log        # tmux pipe-pane输出（完整TUI输出）
│       └── tmux.log             # tmux日志
```

### solver工作目录（`/Volumes/data/math-agent-glm5.2-tmux-agents-dir/`）

```
/Volumes/data/math-agent-glm5.2-tmux-agents-dir/
└── <exp_id>/
    ├── AGENTS.md                # Solver角色的AGENTS.md（数学大师Solver行为约束）
    └── problem.txt              # 题目文件（部分早期batch有）
```

### 文件存在率按终态status

| status | conversation.json | trajectory.jsonl | pane_snapshot.txt | tmux_pipe.log |
|---|---|---|---|---|
| `candidate_solved` | ✅ 100% | ✅ 100% | ✅ 100% | ✅ 100% |
| `failed_token_limit` | ✅ 100% | ✅ 100% | ✅ 100% | ✅ 100% |
| `rate_limited` | ❌ 0% | ✅ 有（小） | ❌ 无 | ✅ 有（小） |
| `dead_session` | ~40% | ✅ 100% | ~40% | ✅ 100% |
| `failed_stall` | ✅ 100% | ✅ 100% | ✅ 100% | ✅ 100% |
| `answer_leak_in_input` | ❌ 无 | ❌ 无 | ❌ 无 | ❌ 无（题没发出去） |

### 16种终态status分类

**模型能力失败**（Profile数据，不重试）：`failed_token_limit` / `ai_gave_up` / `failed_thinking_spin` / `failed_tool_stall` / `failed_no_proof` / `failed_stall`

**基础设施失败**（可重试）：`rate_limited` / `dead_session` / `crash_recovered` / `failed_connection` / `launch_error`

**其他**：`candidate_solved`（成功）/ `answer_leak`（~~AI检测到答案泄漏~~ **已废弃**）/ `answer_leak_in_input`（~~题目含答案~~ **已废弃**）/ `running` / `stopped`

> 完整的查询方法代码示例见`解题系统审计方法.md`

---

## Skills和Rules

### Skills（.devin/skills/）

| Skill | 用途 |
|---|---|
| `solver-batch-health-check` | 批量解题系统健康检查 |
| `solver-tmux-launch` | 通过solver-harness启动Solver的devin cli实例（调试用） |
| `guided-session-launch` | 引导式数学解题session启动 |
| `tree-growth-experiment` | 树生长实验操作流程 |
| `solver-monitoring` | Solver监控 |

### Rules（.devin/rules/）

| Rule | 用途 |
|---|---|
| `solver-batch-health-check.md` | 批量解题系统健康检查规则（三条铁律+7项检查清单+并发上限经验） |
| `solver-concurrency.md` | Solver并发约束（3秒启动间隔铁律+并发经验表40/50/60/80/100实测数据+Redis实时调整命令） |
| `solver-tmux-launch.md` | solver-harness调试启动规则（tmux实时观察devin cli行为，加`--no-mitm`） |

### Templates（templates/）

| 文件 | 用途 |
|---|---|
| `solver_agents_md.md` | Solver角色AGENTS.md模板（bare模式——直接做数学，无提示） |
| `solver_agents_md_guided.md` | Solver角色AGENTS.md模板（guided模式——含提示引导） |

### dev-docs（解题系统文档）

| 文档 | 用途 |
|---|---|
| `dev-docs/391-v0-2026-08-17-解题系统MonitorPipe-参考错题分析系统的持续监控方案.md` | Monitor Pipe设计方案（13项自动检查+AI review抽样） |
| `dev-docs/旧模式batch_problem_runner系统说明.md` | 第一代批量系统架构说明（已归档，pipe系统前身） |
| `dev-docs/旧模式batch_problem_runner操作SOP.md` | 第一代批量系统操作SOP（16个SOP，已归档） |
| `dev-docs/旧模式auto_runner系统说明.md` | 第二代批量系统架构说明（已归档，过渡方案） |

### 全局Skill（不在本repo中）

| Skill | 位置 | 用途 |
|---|---|---|
| `noninteractive-solver-run` | `~/.config/devin/skills/` | 非交互模式运行解题AI（生产标准，`devin -p --export`） |

---

## 关键经验

- **thinking spin不是卡住**：devin cli在thinking期间不输出任何内容到stdout——pane空+pipe.log空是正常状态。thinking可以持续1000+秒。判断方法：检查devin进程是否有到代理(localhost:7897)的ESTABLISHED连接。有连接=thinking spin，无连接=真卡住。collector.py中的`is_devin_waiting_api()`函数实现了这个检查。
- **difficulty_tier字段不一致**：`problem_extraction_progress`中tier=1的题在`devin_problem_runs`中`difficulty_tier`被标记为`null`。统计进度时必须用`_key` JOIN `problem_id`。
- **rate limit风暴**：并发20+可能触发rate limit风暴。稳定上限是并发15。30并发在API不忙时可行。出现rate_limited>50时立即降并发。
- **pipe-runner batch无paths字段**：32,000+条run没有paths字段，通过exp_id直接构造路径。
- **pipe_stop.sh --finish编码bug**：中文输出乱码导致running数比较失败。如running已=0，用`--force`替代。
- **3秒启动间隔**：Runner启动devin cli时，每次启动间隔3秒，避免同时启动多个devin cli造成资源竞争。
- **export落盘保证**：`-p`模式（当前默认）下，devin cli输出完成后自动退出，退出时写export。collector检测到PROOF COMPLETE后等pane中出现`DEVIN_CLI_EXITED`标记（最多120秒）。基础设施失败跳过export等待。

---

## 与原repo的关系

本repo从`/Users/user/glm5.2-math-worktree/`拆分而来。原repo保留数学大师系统的其他部分（Grove核心循环/题海梳理/认知图/Seven System等）。两个repo共享同一个ArangoDB和Redis实例。

**本repo依赖原repo的资源**：
- `.venv/`（Python虚拟环境）：`/Users/user/glm5.2-math-worktree/.venv/`
- `.env`（环境变量）：`/Users/user/glm5.2-math-worktree/.env`
- 全局Skill `noninteractive-solver-run`：`~/.config/devin/skills/`
