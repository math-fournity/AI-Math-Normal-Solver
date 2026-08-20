# 旧模式 · batch_problem_runner.py 批次系统说明

> **归档说明**：本文件记录的是第一代批量解题系统（batch_problem_runner.py）的完整架构说明。该系统已被管道化系统（pipe/5服务）完全替代，当前不再运行。保留此文档供历史参考。
> **归档时间**：2026-08-12
> **替代系统**：管道化GLM-5.2能力边界Profile系统（见AGENTS.md中对应节）

---

## 核心脚本

**核心脚本**：`xishujuzhen/solver_harness/batch_problem_runner.py`
**配套脚本**：`xishujuzhen/solver_harness/extract_problem_text.py`（预处理选题）、`xishujuzhen/solver_harness/solver_harness.py`（单Solver启动）

## 运行模式

| 命令 | 用途 | 说明 |
|---|---|---|
| `run-continuous` | **持续喂入模式（默认推荐）** | 保持N并发槽位满载，完成一个补一个，直到指定tier的题全部跑完 |
| `run-files` | 指定题目跑 | 用`--case`指定具体题目文件 |
| `run` | 按profile选题跑 | 从problem_profiles表选题（要求有profile） |
| `set-concurrency` | **动态调并发** | 运行中随时改并发量，monitor下一轮poll生效 |
| `add-cases` | 运行中追加题目 | 新题queued，monitor自动launch |
| `stop` | 停止批次 | 所有非终态attempt停止 |
| `status` | 查看状态 | |

**持续喂入模式启动**：
```bash
set -a; source .env; set +a
.venv/bin/python xishujuzhen/solver_harness/batch_problem_runner.py run-continuous \
  --feed-tier 1 --concurrency 10 --poll-seconds 60 \
  --max-runtime-seconds 14400 --stall-seconds 900 --stop-on-stall \
  --launch-interval-seconds 5 --label continuous-tier1-10c
```

> **并发上限**：实测10并发稳定。30-40并发曾经稳定但那是mitmproxy启用时代；mitmproxy禁用后未测高并发。不要超过30。

**动态调并发**（运行中随时执行）：
```bash
.venv/bin/python xishujuzhen/solver_harness/batch_problem_runner.py set-concurrency \
  --batch-id <batch_id> --concurrency 40
```

## TUI直出模式（减少工具调用，提高并发）

**Solver不写proof.md，直接在TUI输出证明**：
- AGENTS.md中指令：`Output your complete proof directly in your response. Do NOT write any files.`
- 结束标记：`### PROOF COMPLETE`
- 题目直接嵌入AGENTS.md（Solver不需要读problem.txt，省一次read工具调用）
- **PROOF COMPLETE检测**：monitor用`tmux capture-pane -t <session> -p -S -500`抓pane内容，匹配`PROOF COMPLETE`（不带`### `前缀——TUI渲染会把`### `打碎）。比扫pipe更可靠——pane是当前显示内容，不受ANSI/UTF-8截断影响
- **pipe-pane**：raw `cat >>`不过滤（曾试perl过滤但`-CSD`UTF-8模式会丢数据，`-C0`字节模式braille匹配不全，最终回退raw）。pipe数据用于activity_signature和事后审计，不用于PROOF COMPLETE判定
- trajectory/export仍由`--export`自动保存，不受mitmproxy影响（mitmproxy已禁用）

## Devin CLI启动模式（`-p`模式 vs 交互模式）

**两种模式对比**（2026-08-12完整验证）：

| 维度 | `-p`模式（print） | 交互模式（`--interactive`） |
|---|---|---|
| 启动命令 | `devin -p 'prompt' --model ... --export ...` | `devin --model ... -- 'prompt'` |
| 输出完成后 | **自动退出**（exit 0）+ 写export | 不退出，进入"Ask Devin to build"等待输入 |
| export写入 | 退出时自动写入（可靠） | 需要等"Ask Devin to build"出现后才写（不可靠） |
| tmux session | devin cli退出后session自动销毁 | devin cli不退出，session持续存活 |
| 运行时输入 | 不支持 | 支持（tmux send-keys注入提示） |

**历史演变**：
- 最初用交互模式——为了支持HintInjector（运行时通过send-keys注入提示）
- 2026-08-12早期发现`-p`模式"秒退"——API响应慢时devin cli超时退出（0字节输出），于是禁用`-p`模式
- 2026-08-12晚期重新测试`-p`模式——发现秒退问题已不存在，`-p`模式输出完成后自动退出+写export（138KB），exit code 0
- **当前管道化系统使用`-p`模式**（runner.py和collector.py默认`--print-mode`）

**`-p`模式的关键技巧——sleep 999999保持tmux session**：

`-p`模式下devin cli退出后tmux session自动销毁，collector无法通过tmux pane检测PROOF COMPLETE。解决方案：在devin cli命令后加`; echo DEVIN_CLI_EXITED code=$?; sleep 999999`：
- devin cli退出后echo `DEVIN_CLI_EXITED`标记（collector可检测）
- `sleep 999999`保持tmux session存活（macOS不支持`sleep infinity`）
- collector检测到`DEVIN_CLI_EXITED`后等2秒让export写完

**`-p`模式export落盘保证**（collector中实现）：
1. 检测到PROOF COMPLETE → 等pane中出现`DEVIN_CLI_EXITED`标记（最多120秒）
2. 出现后再等2秒让export写完
3. export仍不存在 → 标记`export_missing`（不重新入队，避免循环）

**交互模式export落盘三阶段保证**（历史逻辑，`--no-print-mode`时使用）：
1. 等"Ask Devin to build"出现（最多120秒）
2. 120秒超时则发Ctrl-C让devin cli停止生成
3. 等export文件出现（最多30秒），仍不存在则标记`export_missing`

**开关形式**：runner.py和collector.py都有`--print-mode`（默认）/`--no-print-mode`开关。原有交互模式代码完整保留。

**batch_problem_runner仍硬编码`--interactive`**：batch_problem_runner是旧的批量系统，未切换到`-p`模式。管道化系统（pipe/runner.py）默认用`-p`模式。

## 答案泄漏防护（三层）

| 层 | 谁做 | 机制 | 确定性 |
|---|---|---|---|
| **第一层a** | 脚本（提取时截断） | `load_problem_text_from_progress()`截断关键词：`Formalization notes`/`## Formalization`/`solution sketch`/`The proof follows` | 确定性 |
| **第一层b** | 脚本（提取边界） | 只取.lean文件`/-!-/`块注释内内容；`determine`/`theorem`等Lean代码在块外，不会混入 | 确定性 |
| **第二层** | Solver AI（运行时自检） | AGENTS.md中`Answer Leak Self-Check`段要求Solver检查题目是否有泄漏，发现则输出`### ANSWER LEAK DETECTED: <描述>`并停下 | 非确定性（依赖AI自觉） |
| **第三层** | monitor（检测后停机） | 扫描`### ANSWER LEAK DETECTED`标记 → `stop_attempt` → status=`answer_leak`（终态）→ 释放槽位 → feed补新题 | 确定性 |

**已验证**：157道compfiles tier 1题，提取层0泄漏（解答/Lean代码/solution sketch均不会混入）。

**如果泄漏发生**：Solver输出`### ANSWER LEAK DETECTED` → monitor扫描到 → 立即停tmux → 标记`answer_leak`终态 → DB记录`answer_leak_detected`事件 → 释放并发槽位 → feed自动补下一题。该run不计入solved/failed统计。

## 选题与feed机制

- **持续喂入**：`select_by_progress_for_feed()`从`problem_extraction_progress`表选未跑过的tier题，不要求有profile
- **题面提取**：`load_problem_text_from_progress()`从源文件提取题面，支持parquet/jsonl/.json/.lean四种格式。.json支持FATE格式（`informal_statement`字段）和MathArena格式（`columns.problem`字段）
- **feed触发条件**：每轮poll检查`running + queued < concurrency`时，自动选题、提取题面、追加到批次
- **feed源耗尽判定**：当指定tier的所有题都有devin_problem_runs记录时，批次完成
- **不会枯竭**：tier 1共452道，当前已跑约111道，剩余约341道

## 限流感知与并发上限

- monitor每轮poll检查running中attempt是否有`rate_limited` marker
- 检测到限流时暂停feed和launch（`rate_limit_pause`）
- 限流缓解后自动恢复
- `set-concurrency`可随时降并发应对限流

**并发上限经验（实测）**：
- **10并发**：稳定（mitmproxy禁用后，交互模式，2026-08-12验证）
- **30并发**：稳定（`-p`模式，2026-08-12验证，export落盘率100%）
- **40并发**：当前运行中（`-p`模式，2026-08-12验证中）
- **60并发**：打爆API，57题中29个（51%）因`cognition.ai/errorKind: unavailable`连接错误终止
- **推荐并发**：30-40（`-p`模式）。60并发打爆API，不要超过40。

**连接错误 vs 真做不出来**：
- `failed_no_proof`不一定是真做不出来——可能是API连接错误
- 需要检查tmux_pipe.log中是否有`Connection error`/`cognition.ai/errorKind`/`unavailable`
- `CONNECTION_PATTERNS`已覆盖这些模式，会自动判定为`failed_connection`而非`failed_no_proof`
- 真做不出来的题：有thinking产出但没输出`### PROOF COMPLETE`

## DB schema关键表

| 表 | 用途 |
|---|---|
| `devin_batch_runs` | 批次记录（batch_id, concurrency, status, attempt_keys） |
| `devin_problem_runs` | 单题run记录（status, verdict, observability, paths） |
| `devin_run_events` | 事件流（concurrency_changed, cases_added, answer_leak_detected等） |
| `problem_extraction_progress` | 题库（_key, difficulty_tier, source_dataset, external_ref.local_path） |

## 批次状态查询脚本（不要现写查询脚本）

**脚本位置**：`xishujuzhen/solver_harness/batch_status.py`

**用法**（不指定`--batch-id`时自动选最新running批次）：
```bash
set -a; source .env; set +a

# 批次总状态（attempt计数 + 并发 + feed剩余）
.venv/bin/python xishujuzhen/solver_harness/batch_status.py status

# running attempt活跃度详情（runtime/idle/thinking大小/markers/ACTIVE|STALLED判定）
.venv/bin/python xishujuzhen/solver_harness/batch_status.py active

# 错误分析（connection_error/token_limited marker统计 + failed_no_proof拆分）
.venv/bin/python xishujuzhen/solver_harness/batch_status.py errors

# candidate_solved列表
.venv/bin/python xishujuzhen/solver_harness/batch_status.py solved

# feed事件历史
.venv/bin/python xishujuzhen/solver_harness/batch_status.py feed

# 答案泄漏检查（DB marker + tmux_pipe扫描 + export全局扫描，区分指令文本vs真报错）
.venv/bin/python xishujuzhen/solver_harness/batch_status.py leak

# 上述全部
.venv/bin/python xishujuzhen/solver_harness/batch_status.py all

# 指定批次
.venv/bin/python xishujuzhen/solver_harness/batch_status.py status --batch-id dpb-xxxx
```

**新增查询需求时**：更新本脚本，不要另写新脚本。在脚本中加新的cmd函数 + argparse choice。
