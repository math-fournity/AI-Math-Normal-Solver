# SolverPipeSystem.md — 管道化GLM-5.2能力边界Profile系统运行手册

> **来源**：从 AGENTS.md 第2859-3515行外移（2026-08-19瘦身工程，392号方案）。
> **定位**：管道化GLM-5.2能力边界Profile系统的自包含运行手册。任何AI进入本repo做管道化解题运行时，读完本文件即可接手。
> **加载时机**：当你要运行/监控/调试管道化GLM-5.2能力边界Profile系统（xishujuzhen/solver_harness/pipe/，5服务+Monitor Pipe+Redis队列）时，必须用read工具全文加载本文件。不涉及管道化解题系统时不需要读。
> **AGENTS.md索引**：AGENTS.md "外部文档索引"节有指向本文件的索引行。
> **跨系统共享小节**：本文件包含"DB schema关键表/数据完整性表/看Solver的4种方法"三个跨系统共享小节，其他系统需要查这些信息时也读本文件。

---

## 管道化GLM-5.2能力边界Profile系统（跨Session连续运行手册）

> **本节是自包含的运行手册。任何AI进入本repo做管道化解题运行时，读完本节即可接手——不依赖Session上下文中的其他内容。**
>
> **本节被AGENTS.md always-on守护。跨压缩边界后，AI加载AGENTS.md时自动看到本节。**
>
> **详细文档**：377号（系统设计）、378号（测试策略）、379号（操作手册）在`Tell分类学研究过程文档/`中。

### 系统目标

在2.4M道竞赛级数学题上构建GLM-5.2的数学能力边界Profile。不是让AI做题，是找能力边界——区分基础设施失败（重试，不计入Profile）和模型能力失败（Profile数据，不重试）。

### 系统架构（5服务+Monitor Pipe+Redis队列）

```
ArangoDB (2.4M题) → Feeder → Redis pending队列
                                ↓
                          Runner (30并发) → 启动devin cli (harness-xxx tmux session)
                                ↓
                          Redis running队列
                                ↓
                          Collector (眼见为实判定终态)
                                ↓
                       Redis completed/failed队列
                                ↓
                          Reporter (统计+告警) + Retry (基础设施失败重试)

                          Monitor Pipe (并行监控13项检查+AI review抽样→pipe_monitor_alerts集合)
```

**代码目录**：`xishujuzhen/solver_harness/pipe/`

**服务清单**：

| 服务 | 脚本 | 职责 |
|---|---|---|
| Feeder | `feeder.py` | 从ArangoDB选题入Redis pending队列 |
| Runner | `runner.py` | 从Redis取题启动devin cli（纯启动，不判定） |
| Collector | `collector.py` | 扫描running队列，眼见为实判定终态 |
| Reporter | `reporter.py` | 定时统计报告+告警 |
| Retry | `retry_infrastructure.py` | 基础设施失败自动重试 |
| **Monitor Pipe** | **`monitor_pipe.py`** | **持续监控13项自动检查+AI review抽样，alert写入`pipe_monitor_alerts`集合** |

### 启动流程

**一键启动（推荐）**：

```bash
cd /Users/user/glm5.2-math-worktree

# 启动30并发（默认）
bash xishujuzhen/solver_harness/pipe/pipe_start.sh

# 启动60并发
bash xishujuzhen/solver_harness/pipe/pipe_start.sh 60
```

`pipe_start.sh`自动完成6步：
1. 前置检查（Redis/ArangoDB/D盘/.env的ARANGO_DB）
2. 恢复crash（清理zombie + 修复DB）
3. 设置并发数
4. 启动5个pipe-*服务（auto-restart模式）
5. 启动Monitor Pipe（持续监控13项检查+AI review抽样，auto-restart模式）
6. 启动launchd watchdog（守护服务+定期清理zombie）

**一键停止**：

```bash
# 优雅停止（默认，两步流程）：
#   第1步：停feeder/runner/reporter/retry/monitor，保留collector继续判定终态
bash xishujuzhen/solver_harness/pipe/pipe_stop.sh

#   第2步：等running=0后，停collector+watchdog
bash xishujuzhen/solver_harness/pipe/pipe_stop.sh --finish

# 立即停止所有服务（保留harness session自然完成，但collector已停，终态需下次启动时recover处理）
bash xishujuzhen/solver_harness/pipe/pipe_stop.sh --force

# 停止并kill所有harness session（强制中断所有正在做的题）
bash xishujuzhen/solver_harness/pipe/pipe_stop.sh --kill
```

**优雅停止的两步流程**（默认）：

- **第1步**（`pipe_stop.sh`）：停feeder/runner/reporter/retry/monitor，**保留collector继续运行**。collector会继续判定正在做的题的终态。不停watchdog（守护collector）。
- **第2步**（`pipe_stop.sh --finish`）：等running=0后，停collector+watchdog。如果running>0会拒绝执行。

**为什么不能一次性停所有服务**：collector被停后，正在做的题完成后没有服务判定终态，status卡在running，直到下次启动时recover_from_crash.py处理——这不是优雅停止。

**4种停止模式对比**：

| 模式 | 命令 | 行为 | 适用场景 |
|---|---|---|---|
| 优雅停止（默认） | `pipe_stop.sh` | 停feeder/runner等，保留collector | 正常关机，想让正在做的题被正确判定终态 |
| 收尾 | `pipe_stop.sh --finish` | running=0后停collector+watchdog | 优雅停止后的第2步 |
| 立即停止 | `pipe_stop.sh --force` | 停所有服务，保留harness session | 需要立即停pipe服务，harness session自然完成 |
| 强制kill | `pipe_stop.sh --kill` | 停所有服务+kill所有harness session | 紧急情况，强制中断所有正在做的题 |

**手动启动（不推荐，缺少前置检查和watchdog）**：

```bash
cd /Users/user/glm5.2-math-worktree
.venv/bin/python3 xishujuzhen/solver_harness/pipe/pipe_control.py start \
  --concurrency 30 --tier 1,2,3 --batch-size 500
```

### 停止流程（优雅停止，不kill harness session）

```bash
# 优雅停止（默认）——发送SIGINT，等待10秒，不kill harness session
.venv/bin/python3 xishujuzhen/solver_harness/pipe/pipe_control.py stop

# 强制停止
.venv/bin/python3 xishujuzhen/solver_harness/pipe/pipe_control.py stop --force

# 停止同时kill所有harness session
.venv/bin/python3 xishujuzhen/solver_harness/pipe/pipe_control.py stop --kill-harness
```

**解耦保证**：停止任何服务不影响已启动的harness-xxx session。harness session独立运行，Collector重启后从Redis running队列继续处理。

**tmux session边界规则**（重要）：解题系统的脚本只操作`harness-p{uuidhex}`和`harness-dbmon-p{uuidhex}`格式的session，不操作其他AI在这个repo中的session（如`harness-poc2_5-CC101-bare`等）。所有tmux session匹配用正则`^harness-(dbmon-)?p[a-f0-9]{20}`，不用`grep "harness-"`或`startswith("harness-")`。

### 并发量实时控制（无需重启Runner）

Runner每次poll时从Redis读取并发配置，可以实时调整，不需要重启：

```bash
# 调整并发数
.venv/bin/python3 xishujuzhen/solver_harness/pipe/pipe_control.py concurrency 50

# 调整poll间隔
.venv/bin/python3 xishujuzhen/solver_harness/pipe/pipe_control.py poll-interval 3

# 查看当前配置（status命令也显示实时配置）
.venv/bin/python3 xishujuzhen/solver_harness/pipe/pipe_control.py status

# 一键健康检查（并发量+网络连接+tmux泄漏+failed分类）
.venv/bin/python3 xishujuzhen/solver_harness/pipe/pipe_control.py health

# 或直接用redis-cli
docker exec redis-queue redis-cli SET math:config:concurrency 50
```

**生效机制**：
- Runner在下次poll时（通常2-5秒内）自动读取Redis中的新值
- **当前running的题不受影响**——只影响后续新启动的题
- 降低并发数：Runner不再启动新题，等running自然结束到低于新并发数后才开始新题
- 升高并发数：Runner立即开始启动更多题填补空槽
- Redis键：`math:config:concurrency`（并发数）、`math:config:poll_interval`（poll间隔秒数）

### 断电恢复流程

```bash
# 检查（不修改）
.venv/bin/python3 xishujuzhen/solver_harness/pipe/recover_from_crash.py --dry-run

# 执行恢复
.venv/bin/python3 xishujuzhen/solver_harness/pipe/recover_from_crash.py

# 恢复后自动重启服务
.venv/bin/python3 xishujuzhen/solver_harness/pipe/recover_from_crash.py --auto-restart
```

恢复做了3件事：
1. running队列中session不存在的记录标记`crash_recovered`，移到failed，problem恢复为pending
2. kill孤儿harness session（tmux有但Redis无记录的）
3. 修复ArangoDB中status=running但实际已结束的attempt

### 代码热替换流程

```bash
# 1. 优雅停止要替换的服务（如Collector）
tmux send-keys -t pipe-collector C-c ""
sleep 5
tmux has-session -t pipe-collector 2>/dev/null && tmux kill-session -t pipe-collector

# 2. 替换代码（编辑collector.py）

# 3. 重启服务
tmux new-session -d -s pipe-collector ".venv/bin/python3 xishujuzhen/solver_harness/pipe/collector.py --poll-interval 10 --timeout 1800 --print-mode"

# 4. Collector从Redis running队列继续处理——harness session一直在运行
```

### 监控检查命令

```bash
# 当前状态
.venv/bin/python3 xishujuzhen/solver_harness/pipe/query_progress.py

# 失败分类统计
.venv/bin/python3 xishujuzhen/solver_harness/pipe/query_failures.py --summary

# 数据完整性验证
.venv/bin/python3 xishujuzhen/solver_harness/pipe/verify_completeness.py --all

# 运行完整性验证（无工具调用+真实proof）
.venv/bin/python3 xishujuzhen/solver_harness/pipe/verify_run_integrity.py --batch

# 双向追溯
.venv/bin/python3 xishujuzhen/solver_harness/pipe/audit_trace.py --all

# Profile构建
.venv/bin/python3 xishujuzhen/solver_harness/pipe/build_profile.py --summary

# 精确解题时间提取
.venv/bin/python3 xishujuzhen/solver_harness/pipe/extract_solve_time.py --batch --update-db
```

### Monitor Pipe检查（2026-08-17新增）

**Monitor Pipe**是持续运行的监控服务（`pipe-monitor` tmux session），每5分钟一轮，13项自动检查 + AI review抽样。详见`dev-docs/391号`。

**13项自动检查**：

| 检查项 | alert_type | 检测什么 |
|---|---|---|
| A. session_health | session_health | harness-p=0但Redis running>0 → runner挂了 |
| B. queue_progress | queue_stalled/no_completions | 15分钟无变化 → stall |
| C. rate_limit_detection | rate_limit | 最近5分钟rate_limited≥3 → 需降并发 |
| D. zombie_sessions | zombie_sessions | 空pane僵尸session≥2 |
| E. export_landing | export_missing | completed题无export文件>10% |
| F. solve_time_credibility | solve_time_anomaly | solve_time>runtime>10% |
| G. failure_rate | failure_rate | 失败率>15% |
| H. throughput_trend | throughput_drop | 吞吐下降>50% |
| I. long_running_tasks | long_running_tasks | 单题运行>30分钟（可能stall） |
| J. proof_completeness | proof_too_small | export文件<1KB（proof内容不完整） |
| K. feeder_health | feeder_dead/feeder_stalled | feeder挂了或没在补充pending |
| L. collector_health | collector_dead/collector_stalled | collector挂了或没在处理completed |
| M. db_redis_consistency | db_redis_inconsistency | Redis running数 vs DB running数差异>5 |

**AI review抽样**：每3轮抽样2条candidate_solved，标记需AI检查proof数学正确性（alert_type=ai_review_sample, severity=info）。

**alert集合**：ArangoDB `pipe_monitor_alerts`（与错题分析系统的`monitor_alerts`隔离）。

**标准化检查脚本**（5项检查+对AI的核心提醒）：

```bash
# Monitor Pipe标准化检查（5项：pane输出/alerts/进程状态/进度/系统深度审查结果）
bash xishujuzhen/solver_harness/pipe/scripts/monitor_check.sh

# 查看新alerts
PYTHONPATH=xishujuzhen/solver_harness/pipe .venv/bin/python3 xishujuzhen/solver_harness/pipe/monitor_pipe.py --check-alerts

# 解决alert
PYTHONPATH=xishujuzhen/solver_harness/pipe .venv/bin/python3 xishujuzhen/solver_harness/pipe/monitor_pipe.py --resolve-alert <alert_key>

# 单轮检查（不循环）
PYTHONPATH=xishujuzhen/solver_harness/pipe .venv/bin/python3 xishujuzhen/solver_harness/pipe/monitor_pipe.py --once
```

**单独管理Monitor Pipe**（不重启其他服务）：

```bash
# 启动
PYTHONPATH=xishujuzhen/solver_harness/pipe .venv/bin/python3 xishujuzhen/solver_harness/pipe/pipe_control.py monitor start --interval 300 --concurrency 20

# 停止
PYTHONPATH=xishujuzhen/solver_harness/pipe .venv/bin/python3 xishujuzhen/solver_harness/pipe/pipe_control.py monitor stop

# 查看状态
PYTHONPATH=xishujuzhen/solver_harness/pipe .venv/bin/python3 xishujuzhen/solver_harness/pipe/pipe_control.py monitor status
```

**检查脚本对AI的核心提醒**（monitor_check.sh第5节"系统深度审查结果"）：
- ★ critical级别alert需要立即处理：pipe_service_dead/session_health critical/rate_limit critical/queue_stalled
- ★ warning级别alert需要评估：zombie_sessions/failure_rate/throughput_drop/solve_time_anomaly
- ★ info级别alert是AI review抽样——AI必须检查抽样的proof质量（这是AI的核心职责，不是脚本能做的）
- ★ 处理完alert后用`monitor_pipe.py --resolve-alert <key>`标记为fixed
- ★ 如果同一类型alert反复出现，说明根因未解决——需要切回系统开发者修复代码

**日志查看**：

```bash
tail -f /Volumes/data/math-agent-glm5.2-tmux-agents-trajectory/_pipe/logs/pipe.log
```

### 眼见为实验证原则（Collector的核心判定逻辑）

Collector判定终态时，不只看标记，要验证真实内容：

1. **真实thinking检测**：clean_ansi后检查数学内容(math_indicator≥2)和thinking标记
2. **真实proof验证**：PROOF COMPLETE标记 + proof内容≥100字符 + math_indicator≥2（中英文都覆盖）
3. **无工具调用验证**：查sessions.db中是否有tool_call类型节点
4. **AI放弃检测**：检查"I CANNOT SOLVE"/"无法"等放弃标记
5. **pane snapshot保存**：判定终态前保存完整pane内容作为物理证据

### `-p`模式核心认知（2026-08-13修正）

> **`-p`模式是非交互的——devin cli是普通进程，不是TUI应用。** 这个认知是2026-08-13调试并发无法维持问题的核心。

**`-p`模式的输出流向**：
- solver_harness用`tmux pipe-pane`把tmux session的所有stdout写入`tmux_pipe.log`
- pane只是tmux的显示区域（500行scrollback），pipe.log有完整输出
- **pane空≠dead**——devin cli连接API、初始化期间无stdout输出，pane和pipe.log都是0字节，但进程活着
- **判断devin cli死活的正确方法**：pipe.log中有`DEVIN_CLI_EXITED`标记 → 已退出；没有 → 还在运行

**`-p`模式的关键技巧**：solver_harness.py在devin cli命令后加`; echo DEVIN_CLI_EXITED code=$?; sleep 999999`——devin cli退出后echo标记+sleep保持tmux session存活（macOS不支持`sleep infinity`）。

**Collector检测源（2026-08-13修正）**：
- **用pipe.log + pane合并检测**（`detect_text = pipe_text + pane_text`），pipe.log是主要源
- pane只有500行scrollback，rate limit等标记可能滚走；pipe.log有完整输出
- thinking检测仍用pane（spinner是动态内容，pipe.log可能不完整）
- **dead_session用`DEVIN_CLI_EXITED`判断**，不用`pane_is_empty`

**2026-08-13修复的3个collector bug**：

| bug | 根因 | 修复 |
|---|---|---|
| 并发无法维持（running掉到3） | `pane_is_empty`判dead_session，初始化中的session被误杀 | 改用`DEVIN_CLI_EXITED`判断 |
| rate_limited要等30分钟timeout | 只在session结束后检测，但devin cli遇到rate limit时session还活着 | 提前到session运行中检测（3.6步） |
| 基础设施失败阻塞主循环120秒 | rate_limited等失败session也等export，但不会写export | `status not in INFRA_FAILURES`跳过export等待 |

**中文proof检测（2026-08-13修正）**：
- `math_indicators`原全是英文（therefore/hence/let/assume等），中文proof只命中`\frac`一个LaTeX标记，被判定为"无足够数学推理"
- 增加中文标记：因此/故/由/设/令/假设/考虑/可得/方程/边界条件/初始条件/解/满足/代入/控制方程/建立模型/边值问题

### 3秒启动间隔铁律（2026-08-13）

> **runner.py第333行的`time.sleep(3)`是铁律，不可修改。** 详见`.devin/rules/solver-concurrency.md`。

- 每个devin cli启动后固定等3秒再启动下一个
- 这是防止API rate limit的关键——并行启动60个session会导致58/60个遇到rate limit
- 3秒间隔意味着：填满60并发需要180秒（3分钟），填满80并发需要240秒（4分钟）
- **恢复慢是正常的**——collector清理完积压session后，runner按3秒间隔逐步填充，不是bug

### 网络切换/断电健壮性（2026-08-14）

> **系统在网络切换/断电后自动恢复，无需人工干预。** 两层保障：collector自动检测zombie + launchd watchdog守护服务。

**问题根因**：网络切换导致tmux server死亡 → 所有harness-* session和pipe-*服务全部消失。旧collector无法自动恢复zombie——session不存在但pipe.log中无`DEVIN_CLI_EXITED`（devin cli没正常退出），走到"未结束"分支永远不会被清理。

**修复A：collector自动检测zombie（collector.py 6.5步）**：

classify函数新增6.5步`crash_recovered`检测：
- 条件：`not is_running` + pipe.log中无`DEVIN_CLI_EXITED` + `elapsed > 60秒`
- 60秒grace period给网络恢复时间（tmux可能重建）
- `crash_recovered`在`INFRA_FAILURES`集合中，retry服务自动重试
- 不再需要手动跑`recover_from_crash.py`

**修复B：launchd watchdog（pipe_watchdog.sh + com.user.pipe-watchdog.plist）**：

- **脚本**：`xishujuzhen/solver_harness/pipe/pipe_watchdog.sh`
- **plist**：`~/Library/LaunchAgents/com.user.pipe-watchdog.plist`
- 每30秒检查5个pipe-*服务（feeder/runner/collector/reporter/retry），死了就重启
- 每5分钟跑`recover_from_crash.py`清理zombie（双重保险）
- `KeepAlive=true`，watchdog自身死了也自动重启
- 开机自启，不依赖终端session
- 日志在`/tmp/pipe-watchdog.log`（launchd进程无D盘写权限，用/tmp）

**验证方法**：
```bash
# 检查watchdog是否运行
launchctl list | grep pipe-watchdog
ps aux | grep pipe_watchdog | grep -v grep
cat /tmp/pipe-watchdog.log | tail -10

# 手动测试：杀掉所有pipe-*服务，30秒内watchdog自动重启
tmux kill-session -t pipe-runner; tmux kill-session -t pipe-collector
sleep 35; tmux list-sessions | grep pipe  # 应该全部恢复
```

**feeder全自动选题（2026-08-14）**：

feeder配置`--tier 1,2,3`，三个tier在同一个AQL查询里。tier=1跑完后自动取tier=2，tier=2跑完后自动取tier=3。auto-restart模式保证feeder退出后5秒重启。**246万题全部自动跑完，不需要人工干预。**

### export落盘保证（`-p`模式 vs 交互模式）

**`-p`模式（当前默认）**：devin cli输出完成后自动退出，退出时写export。collector检测到PROOF COMPLETE后等pane中出现`DEVIN_CLI_EXITED`标记（最多120秒），出现后再等2秒让export写完。export落盘率100%（2026-08-12验证，20/20）。**基础设施失败（rate_limited/failed_connection/dead_session）跳过export等待**——devin cli异常退出不会写export，等120秒纯属浪费时间且阻塞主循环。

**交互模式（`--no-print-mode`）**：devin cli完成后不退出，进入"Ask Devin to build"等待输入。collector用三阶段保证：等Ask Devin出现（120秒）→ 发Ctrl-C → 等export（30秒）。历史落盘率约70-100%，存在export丢失问题。

### stop_tmux审计日志

collector中`stop_tmux(session, reason, exp_id)`记录审计日志：
- **KILL活进程** vs **清理已退出session**——区分两种情况
- 记录终态原因（`终态=candidate_solved/failed_timeout/...`）
- 日志级别：`logger.info`（不是debug），确保生产中可见

### 答案泄漏检测（管道化系统两层）

| 层 | 谁做 | 机制 | 检测点 |
|---|---|---|---|
| **第一层** | Runner（启动前） | 检查题目文本是否包含answer字段值（>10字符）或solution片段 | 启动devin cli前，不启动直接标记`answer_leak_in_input` |
| **第二层** | Solver AI（运行时） | AGENTS.md中Answer Leak Self-Check段要求AI自检 | AI输出`### ANSWER LEAK DETECTED: <描述>` |
| **第三层** | Collector（检测后） | 扫描`ANSWER LEAK DETECTED`标记 | 标记`answer_leak`终态，不重新入队 |

**实测数据**（2026-08-12，676题完成）：
- answer_leak_in_input: 15条（Runner层检测，未启动devin cli）
- answer_leak: 2条（Solver AI自检+Collector检测）
- 答案泄漏的题不重新入队，从pending队列移除

### 失败分类（基础设施 vs 模型能力）

| 类别 | verdict | 处理 | 计入Profile |
|---|---|---|---|
| 基础设施失败 | failed_connection/rate_limited/launch_error/dead_session/crash_recovered | 自动重试 | ❌ |
| 模型能力失败 | failed_token_limit/ai_gave_up/failed_thinking_spin/failed_no_proof/failed_stall/failed_timeout | 不重试 | ✅ |
| 成功 | candidate_solved | - | ✅ |
| 无效 | invalid_tool_use/answer_leak | 不重试 | ❌ |

### 精确解题时间

`solve_time_seconds`从tmux_pipe.log的文件创建/修改时间提取（最可靠——覆盖完整解题过程）。

**数据源优先级**（2026-08-12修正）：
1. **tmux/tmux_pipe.log mtime**（首选）——从tmux session创建到结束持续写入，文件时间覆盖完整解题过程
2. exports/conversation.json的steps时间戳（备选）——注意：devin cli的export可能在session结束时一次性写入，steps时间戳只覆盖部分过程（如8个steps在3秒内完成，但实际解题126秒）
3. sessions_db/trajectory.jsonl的created_at（备选）

**历史误判修复**：2026-08-12发现conversation.json的steps时间戳不覆盖完整解题过程，导致solve_time被低估为2-8秒（实际45-574秒）。已修正优先级为tmux_pipe.log mtime首选，并批量更新了DB中46条历史记录。

时间分解：`launch_overhead + init_overhead + solve_time + tail_overhead + judge_delay`

DB记录字段：`solve_time_seconds`（核心指标）、`solve_time_source`、`prompt_tokens`、`completion_tokens`、`cached_tokens`、`total_steps`

**批量修复历史记录**：
```bash
.venv/bin/python3 xishujuzhen/solver_harness/pipe/extract_solve_time.py --batch --limit 100 --update-db
```

### 答案泄漏检测（answer_leak_in_input）

Runner在入队前检查题目文本是否包含answer/solution字段值，防止AI看到答案。

**检测逻辑**（2026-08-12修正）：
- **answer字段**：只对长度>10字符的答案做检测——短答案如`True`/`False`/`2875`等容易在题目文本中自然出现，导致误判
- **solution_text字段**：检查solution前100字符是否出现在题目文本中

**历史误判修复**：2026-08-12发现answer="False"/"True"/"2875"/"\tau_2"等短答案被误判为answer_leak，因为题目文本中自然包含这些词。已将阈值从>3字符提高到>10字符，4道误判的题已重新入队。

### 日志系统

- **目录**：`/Volumes/data/math-agent-glm5.2-tmux-agents-trajectory/_pipe/logs/`
- **单文件**：1MB分片
- **总量**：10GB循环滚动
- **文件**：runner.log/collector.log/feeder.log/reporter.log/retry.log + 统一pipe.log

### 系统运行监控SOP（AI检查者角色）

**当系统在运行时，AI是检查者，不是旁观者。** 按以下SOP检查系统健康状态：

**检查1：服务存活**（每次检查必做）
```bash
tmux list-sessions | grep pipe-    # 5个服务session必须都在
.venv/bin/python3 xishujuzhen/solver_harness/pipe/pipe_control.py status
```
- feeder/runner/collector/reporter/retry必须✅运行中
- 如果有❌：检查对应日志，可能是崩溃或优雅退出

**检查2：队列流动**（每次检查必做）
- pending应该在减少，running应该接近并发数，completed+failed应该在增长
- 如果running=0但pending>0：Runner可能挂了，或并发配置被改成0
- 如果completed和failed都不增长超过10分钟：Collector可能挂了，或所有running都在长时间解题

**检查3：失败分类**（每100个失败检查一次）
```bash
PYTHONPATH=xishujuzhen/solver_harness/pipe .venv/bin/python3 -c "
from redis_queue import get_redis
import json
from collections import Counter
r = get_redis()
failed = r.lrange('math:failed', 0, -1)
verdicts = Counter()
for item in failed:
    data = json.loads(item)
    verdicts[data.get('verdict', '?')] += 1
for v, c in verdicts.most_common():
    print(f'  {v}: {c}')
"
```
- 基础设施失败（failed_connection/dead_session/launch_error）占比高→检查API连接
- answer_leak_in_input占比高→检查是否有新的误判模式
- failed_tool_stall占比高→AI在工具调用中卡住，可能需要调整timeout

**检查4：solve_time准确性**（每50个完成检查一次）
```bash
PYTHONPATH=xishujuzhen/solver_harness/pipe .venv/bin/python3 -c "
from redis_queue import get_redis
import json
from collections import Counter
r = get_redis()
completed = r.lrange('math:completed', 0, -1)
sources = Counter()
for item in completed:
    data = json.loads(item)
    sources[data.get('solve_time_source', '?')] += 1
for s, c in sources.most_common():
    print(f'  {s}: {c}')
"
```
- tmux_pipe.log mtime应该是主要source——这是最可靠的
- 如果conversation.json占比高：说明tmux_pipe.log可能不存在，检查trajectory目录结构
- solve_time < 10秒的可疑——可能是conversation.json的steps时间戳不完整

**检查5：误判修复**（发现异常verdict时）
- answer_leak_in_input但answer是短字符串（<10字符）→误判，需要重新入队
- solve_time远小于elapsed→solve_time提取有误，用extract_solve_time --batch --update-db修复
- failed_thinking_spin但pane_snapshot中有PROOF COMPLETE→Collector误判，用reclassify_failed.py修复
- candidate_solved但export中无AI输出的PROOF COMPLETE→提示词误匹配bug，用fix_false_positive_solved.py修复
- 修复后记录到本节"历史误判修复"中

**检查5b：export内容质量验证**（每批次结束时抽查）
- 脚本：`scripts/check_export_vs_db.py`——全量检查export内容质量+与数据库交叉验证
- 检查项：
  1. export文件大小分布（0B/<10KB/正常）——0B或<10KB说明export写入不完整
  2. agent step的reasoning_content长度——0B说明AI没输出任何思考
  3. PROOF COMPLETE真伪——用`find_ai_proof_marker`区分AI输出 vs 提示词中的文本
  4. 交叉验证：DB说candidate_solved的，export中是否有真实PROOF COMPLETE
- 误判率 = DB说solved但export无真实证明的数量 / DB说solved的总数
- 正常误判率应<1%；>5%说明collector匹配逻辑有bug

**历史误判修复记录**：
- 2026-08-12 answer_leak短答案误判：阈值>3改为>10，4道题重新入队
- 2026-08-12 solve_time提取误判：conversation.json优先改为tmux_pipe.log mtime优先，46条DB记录更新
- 2026-08-12 failed_thinking_spin误判：has_real_proof只检查PROOF COMPLETE之前内容，但证明在标记之后。改为同时检查前后+过滤TUI UI元素。11条记录从failed→completed
- 2026-08-12 `-p`模式"秒退"误判：早期发现`-p`模式API响应慢时秒退，禁用`-p`改用交互模式。晚期重新测试发现秒退问题已不存在，`-p`模式输出完成后自动退出+写export（138KB）。切换回`-p`模式，export落盘率从70%提升到100%
- 2026-08-12 `-p`模式tmux session消失：`-p`模式devin cli退出后tmux session自动销毁，collector判定为dead_session。修复：命令后加`; echo DEVIN_CLI_EXITED code=$?; sleep 999999`保持session存活
- 2026-08-12 macOS sleep infinity不支持：`sleep infinity`在macOS上报错（usage: sleep number[unit]），改用`sleep 999999`
- 2026-08-12 提示词PROOF COMPLETE误匹配（重大）：collector的has_real_proof在pane中搜索"PROOF COMPLETE"时，匹配到了提示词"结尾输出 ### PROOF COMPLETE"中的文本，而非AI实际输出的标记。导致272个实际未完成的题被误判为candidate_solved（误判率27.4%）。修复：新增`find_ai_proof_marker`函数检查上下文排除提示词匹配（检查"结尾输出"/"请按AGENTS"/"Pro ·"等提示词特征）。272条DB记录重新分类（252 dead_session + 17 token_limit + 3 stall），272道题重新入队。脚本：`scripts/fix_false_positive_solved.py`，验证脚本：`scripts/check_export_vs_db.py`

**检查6：harness session健康**（每30分钟检查一次）
```bash
tmux list-sessions | grep -c harness-p    # 应该接近并发数
```
- harness session数远大于并发数→有僵尸session，运行recover
- harness session数=0但running>0→Redis running队列和tmux不同步，运行recover

### 数据存储位置

| 数据 | 位置 |
|---|---|
| ArangoDB | `xishujuzhen_math_glm52@localhost:8529` |
| Redis | `redis-queue`容器，6379端口，AOF+RDB持久化到`/Volumes/data/redis/data` |
| 题目数据 | ArangoDB `math_problems`集合 |
| 运行记录 | ArangoDB `devin_problem_runs`集合 |
| Solver工作目录 | `/Volumes/data/math-agent-glm5.2-tmux-agents-dir/<exp_id>/` |
| Trajectory数据 | `/Volumes/data/math-agent-glm5.2-tmux-agents-trajectory/<exp_id>/` |
| 日志 | `/Volumes/data/math-agent-glm5.2-tmux-agents-trajectory/_pipe/logs/` |

### DB schema关键表（跨系统共享）

| 表 | 用途 |
|---|---|
| `devin_batch_runs` | 批次记录（batch_id, concurrency, status, attempt_keys） |
| `devin_problem_runs` | 单题run记录（status, verdict, observability, paths） |
| `devin_run_events` | 事件流（concurrency_changed, cases_added, answer_leak_detected等） |
| `problem_extraction_progress` | 题库（_key, difficulty_tier, source_dataset, external_ref.local_path） |

### 数据完整性表（跨系统共享）

| 层 | 文件 | 有什么 | 没有什么 |
|---|---|---|---|
| **export** | `<exp_id>/exports/conversation.json` | 最终证明输出（ATIF steps）、system/user消息、metrics（token数） | **thinking内容**；**steps时间戳不覆盖完整解题过程**（export可能一次性写入，用tmux_pipe.log mtime提取solve_time更可靠） |
| **pipe** | `<exp_id>/tmux/tmux_pipe.log` | TUI渲染流（ANSI+braille spinner）、UI行（`Thinking · Xm Ys`）；**文件创建/修改时间=solve_time最可靠来源** | **thinking文本内容** |
| **thinking_capture** | `<exp_id>/tmux/thinking_capture.txt` | **完整thinking文本**（Ctrl+O展开后capture-pane抓取） | 无ANSI清理（raw TUI文本） |
| **sessions.db** | `~/.local/share/devin/cli/sessions.db` | 输入消息（system/user） | **assistant输出和thinking** |

> **thinking落盘机制**：`stop_attempt()`调用前自动调`capture_thinking()`——发Ctrl+O展开thinking，capture-pane抓scrollback 5000行，存到`thinking_capture.txt`。

### 看Solver的4种方法（跨系统通用）

```bash
# 方法1：tmux attach（实时看，Ctrl+B D退出）
tmux attach -t <tmux_session_name>

# 方法2：capture-pane（抓当前屏幕，-S -500抓历史500行）
tmux capture-pane -t <tmux_session_name> -p -S -500

# 方法3：看export文件（最终证明输出，ATIF JSON格式）
cat /Volumes/data/math-agent-glm5.2-tmux-agents-trajectory/<exp_id>/exports/conversation.json | python3 -m json.tool | head -100

# 方法4：看thinking_capture文件（Ctrl+O展开后的完整thinking文本）
cat /Volumes/data/math-agent-glm5.2-tmux-agents-trajectory/<exp_id>/tmux/thinking_capture.txt | tail -100
```

### 查询/验证脚本清单

| 脚本 | 用途 |
|---|---|
| `query_progress.py` | 运行进度查询 |
| `query_failures.py` | 失败分类统计 |
| `audit_trace.py` | 双向追溯验证（DB↔文件，题目↔运行） |
| `verify_completeness.py` | 数据完备性验证（remainder=0） |
| `verify_run_integrity.py` | 运行完整性验证（无工具调用+真实proof） |
| `check_answer_leak.py` | 答案泄漏检查 |
| `extract_solve_time.py` | 精确解题时间提取 |
| `build_profile.py` | GLM-5.2能力边界Profile构建 |
| `recover_from_crash.py` | 断电恢复+僵尸清理 |
| `pipe_control.py` | 启动/停止/状态管理/健康检查（status/health/concurrency） |
| `check_export_quality.py` | export文件大小+结构抽样检查 |
| `check_export_vs_db.py` | export内容质量+与数据库交叉验证（PROOF COMPLETE真伪、误判率） |
| `fix_false_positive_solved.py` | 修复collector误判的candidate_solved记录+重新入队 |

### AGENTS.md模板（给devin cli的）

Runner为每个题目生成`AGENTS.md`文件，明确禁止任何工具调用：

```markdown
# 解题任务

你是一个数学解题AI。请直接在TUI中输出证明，不要写任何文件。

## 严格规则
1. 不要使用任何工具（不写文件、不执行命令、不搜索、不浏览）
2. 直接在TUI中用thinking解题
3. 结尾输出 ### PROOF COMPLETE
4. 如果实在做不出来，输出 ### I CANNOT SOLVE THIS

## 题目
{problem_text}
```

### 未覆盖因素（待补充）

1. ~~**export导出完整性验证**~~ — ✅已解决（`-p`模式export落盘率100%，2026-08-12验证）
2. **tmux pane scrollback完整性** — 超长proof可能被截断（2000行上限）
3. **proof数学正确性验证** — 需要审稿AI审查（当前只验证有内容）
4. **跨数据集去重验证** — 近似去重防止重复运行
5. **并发健康度监控** — 实时连接错误率、吞吐量告警

### 运行实测数据（2026-08-12，`-p`模式+40并发）

| 指标 | 数值 |
|---|---|
| 已完成 | 744题（修复误判后真实值） |
| 失败 | 414题（含272道误判修复后重新分类的题） |
| 真实成功率 | 64.2%（修复272条误判前显示91.2%是虚高的） |
| 吞吐量 | ~324题/小时（40并发）/ ~270题/小时（30并发） |
| export落盘率 | 100%（20/20） |
| 数据库字段完整度 | 14/14字段全部100% |
| solve_time可信度 | 100%（20/20） |
| dead_session率 | ~15%（`-p`模式devin cli偶发0字节退出，不随并发量变化） |
| 误判修复历史 | 272条candidate_solved误判（提示词PROOF COMPLETE误匹配），已修复 |

> **注意**：修复前的"91.2%成功率"是虚高的——272个实际未完成的题被误判为已解决。修复后真实成功率64.2%。dead_session（`-p`模式偶发0字节退出）是主要失败原因，占失败的60%+。

### 连续运行SOP

**当你要接手连续运行时，按以下步骤行动**：

1. **检查系统状态**：`pipe_control.py status`看当前pending/running/completed/failed
2. **全面健康检查**：`pipe_control.py health`（7项检查：并发/队列/TUI状态/export落盘/DB字段/solve_time/failed分类）
3. **检查服务是否在运行**：`tmux list-sessions`看pipe-feeder/runner/collector/reporter/retry是否在
4. **如果服务没在运行但有running记录**：执行`recover_from_crash.py`恢复，然后重启服务
5. **如果服务在运行**：检查日志`tail -50 .../pipe.log`看是否有异常
6. **export内容质量验证**：`scripts/check_export_vs_db.py`检查PROOF COMPLETE真伪+误判率（正常<1%）
7. **如果需要热替换代码**：按"代码热替换流程"操作
8. **运行结束后**：执行`build_profile.py --summary`构建Profile，`verify_completeness.py --all`验证数据完整性，`scripts/check_export_vs_db.py`做最终export质量审计

### 关键文档索引

- **377号**：`Tell分类学研究过程文档/377-v0-2026-08-12-管道化解题系统设计-4服务分离+Redis队列.md`——完整系统设计（第8节核心验证原则、第9节未覆盖因素、第10节优雅停止+断电恢复）
- **378号**：`Tell分类学研究过程文档/378-v0-2026-08-12-管道化系统测试策略-数据管理与计算流程完备性.md`——测试策略（DM/CF/E2E/RI/FC/PB/ST/LG/UC测试项）
- **379号**：`Tell分类学研究过程文档/379-v0-2026-08-12-管道化解题系统操作手册.md`——完整操作手册

