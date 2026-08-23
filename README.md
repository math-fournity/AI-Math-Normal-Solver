# README.md · 平凡解题系统（Normal Solver）

> **定位**：repo的AI工作引导地图。详细结构信息（文档索引、代码索引、目录结构、skills/rules、操作经验）都在这里。
> **加载方式**：按需加载（不占always-on上下文）。AI从本文件出发，判断当前任务需要加载哪些文档/代码。
> **铁律**：在`AGENTS.md`中（always-on），不在此文件中。

---

## 1. 文档索引

**回答问题前，先判断需要加载哪些文档：**

| 你要做什么 | 加载什么 |
|---|---|
| 管道化Profile系统（pipe/5服务+Redis）的完整架构和运行手册 | `SolverPipeSystem.md` |
| Solver运行操作SOP（编译验证SOP G1/资产追溯SOP G2/失败分类SOP G3） | `SolverOpsSOP.md` |
| 查询解题结果/失败题/运行资产位置/追溯方法/16种终态status分类 | `解题系统审计方法.md` |
| 解题系统进展交接（tier=1进度/6.6检测修复/已知问题/待办） | `dev-docs/398-v0-2026-08-19-解题系统进展交接文档.md` |
| tier=1做题结果调查报告（调查方法+三大类分类统计+关键发现） | `dev-docs/402-v0-2026-08-21-tier1做题结果调查报告.md` |
| 平凡解题系统队列准备方案（一次性入队3,448题，之后不启动feeder） | `dev-docs/403-v0-2026-08-21-平凡解题系统队列准备方案.md` |
| 题目清洗系统方案（**已废弃**→改为取消泄漏检测，调查发现原始数据本身就包含答案） | `dev-docs/404-v0-2026-08-21-题目清洗系统方案.md` |
| tier=1需要另外系统处理的题目查询报告（10,069题，查询方法+AQL+分类统计+交叉表） | `dev-docs/405-v0-2026-08-21-tier1需要另外系统处理的题目查询报告.md` |
| tier=1留存结果完整性调查报告（1,323项缺失，按status×文件交叉分析） | `dev-docs/406-v0-2026-08-22-tier1留存结果完整性调查报告.md` |
| tier=1重跑与"应重试"池关系调查报告（1,175题重跑vs607题应重试的交叉分析+452题empty_problem_text发现） | `dev-docs/407-v0-2026-08-23-tier1重跑与应重试关系调查报告.md` |
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

### dev-docs（解题系统文档）

| 文档 | 用途 |
|---|---|
| `dev-docs/391-v0-2026-08-17-解题系统MonitorPipe-参考错题分析系统的持续监控方案.md` | Monitor Pipe设计方案（13项自动检查+AI review抽样） |
| `dev-docs/旧模式batch_problem_runner系统说明.md` | 第一代批量系统架构说明（已归档，pipe系统前身） |
| `dev-docs/旧模式batch_problem_runner操作SOP.md` | 第一代批量系统操作SOP（16个SOP，已归档） |
| `dev-docs/旧模式auto_runner系统说明.md` | 第二代批量系统架构说明（已归档，过渡方案） |

---

## 2. 代码文件索引

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
| `prepare_final_queue.py` | 最终队列准备：清空pending/failed/completed + 一次性入队3,448题 | `python prepare_final_queue.py [--dry-run]` |
| `recheck_final_queue.py` | 验证math:pending中的题是否都属于正确类别 | `python recheck_final_queue.py [--verbose]` |
| `check_system_health.py` | 系统运行落盘一致性检查（Redis队列+ArangoDB+硬盘交叉验证+文件完整性） | `python check_system_health.py [--start-ts N]` |
| `check_tier1_retention.py` | 检查全部tier=1题目的留存结果完整性（DB run记录+硬盘文件缺失） | `python check_tier1_retention.py [--verbose] [--export FILE]` |
| `reenqueue_misclassified_connection.py` | 把1,111题误分类的连接错误重新入队（读pane确认+入pending+更新DB） | `python reenqueue_misclassified_connection.py [--dry-run]` |
| `reenqueue_failed_no_proof.py` | 把64题failed_no_proof无export的题重新入队（确认无export+入pending+更新DB） | `python reenqueue_failed_no_proof.py [--dry-run]` |
| `analyze_rerun_vs_remaining.py` | 分析某次重跑后tier=1"应重试"池的变化（重跑名单vs应重试池交叉分析+empty_problem_text检测） | `python analyze_rerun_vs_remaining.py --tier 1 --rerun-since 2026-08-22T09:00:00Z [--verbose] [--export FILE]` |
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

## 3. 运行目录结构

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

## 4. Skills和Rules

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

### 全局Skill（不在本repo中）

| Skill | 位置 | 用途 |
|---|---|---|
| `noninteractive-solver-run` | `~/.config/devin/skills/` | 非交互模式运行解题AI（生产标准，`devin -p --export`） |

---

## 5. 关键经验

- **thinking spin不是卡住**：devin cli在thinking期间不输出任何内容到stdout——pane空+pipe.log空是正常状态。thinking可以持续1000+秒。判断方法：检查devin进程是否有到代理(localhost:7897)的ESTABLISHED连接。有连接=thinking spin，无连接=真卡住。collector.py中的`is_devin_waiting_api()`函数实现了这个检查。
- **difficulty_tier字段不一致**：`problem_extraction_progress`中tier=1的题在`devin_problem_runs`中`difficulty_tier`被标记为`null`。统计进度时必须用`_key` JOIN `problem_id`。
- **rate limit风暴**：并发20+可能触发rate limit风暴。稳定上限是并发15。30并发在API不忙时可行。出现rate_limited>50时立即降并发。
- **pipe-runner batch无paths字段**：32,000+条run没有paths字段，通过exp_id直接构造路径。
- **pipe_stop.sh --finish编码bug**：中文输出乱码导致running数比较失败。如running已=0，用`--force`替代。
- **3秒启动间隔**：Runner启动devin cli时，每次启动间隔3秒，避免同时启动多个devin cli造成资源竞争。
- **export落盘保证**：`-p`模式（当前默认）下，devin cli输出完成后自动退出，退出时写export。collector检测到PROOF COMPLETE后等pane中出现`DEVIN_CLI_EXITED`标记（最多120秒）。基础设施失败跳过export等待。

---

## 6. 与原repo的关系

本repo从`/Users/user/glm5.2-math-worktree/`拆分而来。原repo保留数学大师系统的其他部分（Grove核心循环/题海梳理/认知图/Seven System等）。两个repo共享同一个ArangoDB和Redis实例。

**本repo依赖原repo的资源**：
- `.venv/`（Python虚拟环境）：`/Users/user/glm5.2-math-worktree/.venv/`
- `.env`（环境变量）：`/Users/user/glm5.2-math-worktree/.env`
- 全局Skill `noninteractive-solver-run`：`~/.config/devin/skills/`
