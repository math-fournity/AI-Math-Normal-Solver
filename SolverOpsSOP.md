# SolverOpsSOP.md — Solver运行通用SOP+旧模式归档

> **来源**：从 AGENTS.md 第206-346行外移（2026-08-19瘦身工程三期，392号方案）。
> **定位**：Solver运行的通用SOP（适用于管道化系统pipe/5服务）+ 旧模式SOP归档（batch_problem_runner/auto_runner）。
> **加载时机**：当你要运行/操作Solver（启动pipe/检查健康/处理异常/批量解题）时，必须用read工具全文加载本文件。涉及旧模式batch_problem_runner时也读本文件。不涉及Solver运行操作时不需要读。
> **AGENTS.md索引**：AGENTS.md "外部文档索引"节有指向本文件的索引行。

---

### 通用SOP（适用于管道化系统）

> **以下SOP是通用规范，适用于当前管道化系统（pipe/5服务）。旧系统（batch_problem_runner/auto_runner）的SOP已归档到dev-docs/。**

**SOP G1 · 编译验证（改代码后必须做）**

```bash
cd /Users/user/glm5.2-math-worktree
.venv/bin/python -m py_compile xishujuzhen/solver_harness/pipe/feeder.py && \
.venv/bin/python -m py_compile xishujuzhen/solver_harness/pipe/runner.py && \
.venv/bin/python -m py_compile xishujuzhen/solver_harness/pipe/collector.py && \
.venv/bin/python -m py_compile xishujuzhen/solver_harness/pipe/reporter.py && \
.venv/bin/python -m py_compile xishujuzhen/solver_harness/pipe/retry_infrastructure.py && \
.venv/bin/python -m py_compile xishujuzhen/solver_harness/pipe/pipe_control.py && \
.venv/bin/python -m py_compile xishujuzhen/solver_harness/solver_harness.py && \
echo "compile OK"
```

**SOP G2 · 资产追溯规范（2026-08-12）**

> **所有运行资产必须可追溯**——从DB记录能找到文件路径，从文件路径能找到DB记录。不允许放临时目录。

**运行ID唯一性保证**：

exp_id格式：`{batch_id}-{ordinal:02d}-p{progress_key}-r{run_id:07d}-{problem_id}`

- `run_id`是ArangoDB counter（`devin_counters` collection的`run_id`键）原子自增的全局运行ID
- 每次`create_batch`/`add_cases`/`create_attempt_from_queue`创建attempt时调`next_run_id(db)`获取，确保exp_id全局唯一
- 同一道题在不同batch中跑→run_id不同→exp_id不同✓
- 同batch内重跑→run_id不同→exp_id不同✓
- 两个runner并发→ArangoDB事务保证原子递增→run_id不冲突✓
- DB attempt记录中有`run_id`字段，便于从run_id反查attempt
- **目录名以exp_id开头，exp_id含run_id，所以目录永远唯一，不会错乱**

**资产存放位置**：

| 资产 | 位置 | 持久化 | 追溯方式 |
|---|---|---|---|
| **trajectory文件**（export/pipe/thinking_capture/tmux.log） | `/Volumes/data/math-agent-glm5.2-tmux-agents-trajectory/<exp_id>/` | 是 | DB attempt.paths指向 |
| **solver工作目录**（problem.txt/proof.md/AGENTS.md） | `/Volumes/data/math-agent-glm5.2-tmux-agents-dir/<exp_id>/` | 是 | DB attempt.paths指向 |
| **monitor日志** | `/Volumes/data/math-agent-glm5.2-tmux-agents-dir/logs/batch-<label>.log` | 是 | tmux session名对应 |
| **auto_runner日志** | `/Volumes/data/math-agent-glm5.2-tmux-agents-dir/logs/auto_runner.log` | 是 | 固定路径 |
| **DB记录**（attempt/event/batch/queue） | ArangoDB | 是 | batch_id → attempt → paths |
| **sessions.db** | `~/.local/share/devin/cli/sessions.db` | 是 | devin_session_id关联 |

**trajectory数据Schema文档**（项目根目录）：

| 文件 | 内容 | 何时读取 |
|---|---|---|
| `devin-cli-export-conversation.md` | `exports/conversation.json`的完整Schema（ATIF格式，含reasoning_content/tool_calls/observation） | 需要解析--export导出的conversation.json时 |
| `trajectory-schema.md` | `sessions_db/trajectory.jsonl`的完整Schema（JSONL格式，含thinking/tool_calls/tool行） | 需要解析sessions_db导出的trajectory.jsonl时 |
| `analysis-devin-failure-system/docs/solver-trajectory-schema.md` | harness采集的完整trajectory目录结构（9个文件，含session_info/mitm/tmux） | 需要了解完整目录结构时 |

**关键差异**：conversation.json的`observation`字段存tool_results（58%存在率，优先用）；trajectory.jsonl的`role="tool"`行存tool_results（82%存在率，兜底用）。

**面包屑地图方案**（不假设schema的遍历→地图→HANDOVER.md）：见 `conversation-map.md`（项目根目录）。当conversation.json结构未知或可能变化时，用 `scripts/conversation_mapper.py` 生成面包屑地图，交给编写HANDOVER.md的AI按地图逐条遍历，不依赖先验schema。

**从DB查运行时目录和文件位置**：
```bash
# 从problem_id查所有attempt及其文件路径
.venv/bin/python -c "
from arango import ArangoClient; import os
c = ArangoClient(hosts=os.environ.get('ARANGO_HOST','http://localhost:8529'))
db = c.db(os.environ['ARANGO_DB'], username=os.environ.get('ARANGO_USER','root'), password=os.environ.get('ARANGO_PASS',''))
for a in db.aql.execute('FOR a IN devin_problem_runs FILTER a.problem_id == @pid RETURN a', bind_vars={'pid': '<PID>'}):
    print(f'run_id={a.get(\"run_id\",0)} status={a[\"status\"]} batch={a[\"batch_id\"]}')
    print(f'  exp_id={a[\"exp_id\"]}')
    print(f'  export: {a[\"paths\"][\"export_path\"]}')
    print(f'  thinking_capture: {a[\"paths\"][\"tmux_pipe_path\"].replace(\"tmux_pipe.log\",\"thinking_capture.txt\")}')
    print(f'  solver_dir: {a[\"paths\"][\"solver_dir\"]}')
"
```

**禁止**：
- **禁止放`/tmp/`**——重启丢失，无法追溯
- **禁止只存DB不存文件**——DB只有paths指针，文件丢了paths指向空
- **禁止只存文件不存DB**——没有DB记录无法从batch_id查到attempt

**SOP G3 · AI失败题分类与Response truncated处理（2026-08-12）**

> **AI做不出来的题分4类**，全部记录到DB的`devin_problem_runs`中，`status`+`end_reason`字段标识失败类型。完整追溯文档见374号文档。

**AI自身问题导致的失败**：

| status | 含义 | end_reason | 是AI的问题？ |
|---|---|---|---|
| `failed_no_proof` | session结束但没输出`### PROOF COMPLETE`标记 | `tmux_session_ended` | 是——AI做了题但没按格式输出标记 |
| `failed_token_limit` | token用完没做出来 | `tmux_session_ended` / `idle_token_limited` / `response_truncated_max_token_limit` | 是——AI能力不足/效率不够 |
| `failed_tool_stall` | 工具调用卡住超时 | `stall_seconds` | 是——AI行为异常 |
| `answer_leak` | AI检测到答案泄漏主动拒绝 | `answer_leak_detected_by_solver` | 是——AI主动拒绝 |

**基础设施问题导致的失败（不算AI做不出来）**：

| status | 含义 | 归因 |
|---|---|---|
| `dead_session` | devin cli已退出(DEVIN_CLI_EXITED)但无proof | 基础设施 |
| `failed_connection` | 网络连接断开 | 网络 |
| `failed_network_stuck` | 网络不稳定卡死 | 网络 |
| `launch_error` | 启动失败 | 系统 |
| `stopped` | 手动停止 | 操作 |

**Response truncated的处理**：

> **Response truncated不是网络问题**——是模型输出达到max token limit被截断。TUI显示`⚠︎ Response truncated`，session空闲等待用户发消息继续。这属于AI做不出来的情况——AI的输出长度不够完成证明。

**处理流程**：
1. `scan-thinking`扫描发现IDLE的session
2. 看pane内容——`Response truncated`是token limit，`Connection error`是网络断开
3. **Response truncated**：可以发"继续"让AI继续输出，但如果反复truncated说明题对AI来说太长→最终标记`failed_token_limit`
4. **Connection error**：网络问题，发"继续"重试，反复失败则标记`failed_connection`

**手动落盘PROOF COMPLETE的session**（auto_runner的refresh循环会自动做，但如果需要手动做）：
- 先`capture_thinking(attempt)`保留thinking数据
- 再`stop_attempt(db, attempt, batch_dir, decode=False)`让devin cli写export
- 等3秒后kill tmux session
- 更新DB：`status=candidate_solved`, `end_reason=manual_pane_proof_complete`
- 记录event：`event_type=attempt_solved`

**查询所有AI失败的题**：
```bash
.venv/bin/python -c "
from arango import ArangoClient; import os
from collections import Counter
c = ArangoClient(hosts=os.environ.get('ARANGO_HOST','http://localhost:8529'))
db = c.db(os.environ['ARANGO_DB'], username=os.environ.get('ARANGO_USER','root'), password=os.environ.get('ARANGO_PASS',''))
for s, n in sorted(Counter(a['status'] for a in db.aql.execute('FOR a IN devin_problem_runs FILTER a.status IN [\"failed_no_proof\",\"failed_token_limit\",\"failed_tool_stall\",\"answer_leak\"] RETURN a')).items()):
    print(f'  {s}: {n}')
"
```

---

### 旧模式SOP（batch_problem_runner直接操作 · 已归档）

> **以下SOP来自2026-08-12的调试实战，适用于batch_problem_runner.py直接操作模式。该系统已被管道化系统完全替代，当前不再运行。**
>
> **完整操作SOP已移到**：`dev-docs/旧模式batch_problem_runner操作SOP.md`——16个SOP（SOP 1启动批次/SOP 2检查批次健康/SOP 3验证STALLED/SOP 4处理dead session/SOP 5批量落盘/SOP 6看thinking内容/SOP 7停止批次/SOP 9扫描thinking状态/SOP 10恢复export=0B/SOP 13手动feed+resume）+ 数据完整性表 + 已知根因清单。
>
> **何时需要读**：管理仍在运行的旧模式实例时（当前无）。

---
