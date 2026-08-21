# DATABASE.md · 解题系统数据库表结构

> **数据库**：ArangoDB `localhost:8529`，DB名 `xishujuzhen_math_glm52`
> **用户**：root / moira123（从 `/Users/user/glm5.2-math-worktree/.env` 读取）
> **维护规则**：任何表结构变更必须同步更新本文件并commit（见全局rule `database-schema-doc`）
> **查询前必读**：写查数据库脚本前，先读本文件确认字段名、类型、含义

---

## 1. problem_extraction_progress（题库）

**用途**：题库主表，246万题。每道题一条记录，包含题目文本、答案、解答、难度、来源等信息。

### 1.1 字段

| 字段 | 类型 | 必填 | 默认值 | 含义 |
|---|---|---|---|---|
| `_key` | str | 是 | — | 题目唯一标识（如`deepmath_103k_00001774`） |
| `_id` | str | 是 | 自动 | ArangoDB内部ID（`problem_extraction_progress/<_key>`） |
| `_rev` | str | 是 | 自动 | ArangoDB版本号 |
| `problem_text` | str | 是 | — | 题目文本（LaTeX格式）。**注意**：部分题此字段含答案（answer_leak），清洗后此字段为纯净题目 |
| `answer` | str | 否 | — | 答案（LaTeX格式） |
| `solution_text` | str | 否 | — | 解答文本（可能很长，如几万字符） |
| `difficulty_score` | int/float | 否 | — | 难度分数（0-10） |
| `difficulty_tier` | int | 否 | — | 难度档位（1/2/3） |
| `extraction_status` | str | 是 | — | 提取状态（见§1.3枚举） |
| `has_answer` | bool | 是 | — | 是否有答案 |
| `has_solution` | bool | 是 | — | 是否有解答 |
| `ingest_date` | str | 是 | — | 入库日期（`YYYY-MM-DD`） |
| `local_path` | str | 是 | — | 原始数据文件路径（parquet文件路径） |
| `pass_rate` | int | 否 | — | 通过率（部分数据集有，如oda_math_460k） |
| `priority` | int | 否 | 1 | 优先级（feeder按此排序入队） |
| `problem_hash` | str | 否 | — | 题目哈希（MD5） |
| `skip_reason` | str/None | 否 | null | 跳过原因（null=未跳过） |
| `source_dataset` | str | 是 | — | 数据集来源（见§1.4数据集列表） |
| `source_subset` | str | 否 | — | 数据子集 |
| `subject` | str | 否 | — | 学科（部分数据集有，如oda_math_460k） |
| `topic` | str | 否 | — | 主题分类（如`Mathematics -> Algebra -> ...`） |

### 1.2 清洗后新增字段（题目清洗系统，见`dev-docs/404-v0-2026-08-21-题目清洗系统方案.md`）

| 字段 | 类型 | 必填 | 何时写入 | 含义 |
|---|---|---|---|---|
| `original_problem_text` | str | 否 | 阶段3验证通过时 | 原始problem_text（含答案，保留备查/回滚） |
| `original_answer` | str | 否 | 阶段3验证通过时 | 原始answer |
| `cleaning_status` | str | 否 | 阶段1/3完成时 | 清洗状态（见§1.5枚举） |
| `cleaning_stage1_exp_id` | str | 否 | 阶段1完成时 | 阶段1提取run的exp_id |
| `cleaning_stage3_exp_id` | str | 否 | 阶段3完成时 | 阶段3验证run的exp_id |
| `cleaning_at` | str | 否 | 验证通过时 | 清洗完成时间（ISO格式） |
| `cleaning_review_reason` | str | 否 | 验证不通过时 | 验证不通过原因 |
| `cleaning_review_detail` | object | 否 | 验证不通过时 | AI验证详情（含question-detail/answer-detail） |

### 1.3 extraction_status枚举

| 值 | 含义 |
|---|---|
| `pending` | 待处理（feeder可选题入队） |
| `queued` | 已入队Redis pending |
| `completed` | 已完成（已处理过） |
| `failed` | 处理失败 |
| `skipped` | 跳过（skip_reason有值） |

### 1.4 source_dataset主要值

| 值 | 题量级 | 说明 |
|---|---|---|
| `deepmath_103k` | ~10万 | DeepMath数据集 |
| `oda_math_460k` | ~46万 | ODA-Math数据集 |
| `polymath` | ~数千 | Polymath数据集 |
| `omni_math2` | — | OmniMath数据集 |
| `formal_conjectures` | ~数千 | 形式化猜想 |
| `anti_guessing_olympiad` | — | 反猜测奥赛题 |

### 1.5 cleaning_status枚举（清洗后新增）

| 值 | 含义 | 后续动作 |
|---|---|---|
| `extracted` | 阶段1提取完成，待阶段2提取结果 | 阶段2脚本提取XML |
| `verified` | 阶段3验证通过 | 入库，可重新进入解题系统 |
| `needs_review` | 阶段3验证不通过 | Master Agent人工检查 |
| `cleaned_failed` | 阶段1提取失败（AI输出extraction-failed） | 不入库，记录原因 |
| `cleaned_token_limit` | 阶段1或阶段3 token用完 | 可重试 |
| `cleaned_rate_limited` | 阶段1或阶段3 API限流 | retry自动重试 |

### 1.6 索引

| 索引字段 | 类型 |
|---|---|
| `difficulty_tier` | 单字段 |
| `extraction_status` | 单字段 |
| `[difficulty_tier, extraction_status]` | 复合 |

### 1.7 已知问题

- **extraction_status分布杂乱**：从未处理过的题的extraction_status可能是queued/pending/completed等任意值，不能依赖extraction_status判断题是否该入队。正确方法是查`devin_problem_runs`中是否有对应run记录。
- **部分题problem_text含答案**：451题（tier=1）的problem_text包含answer值（answer_leak_in_input），需要清洗系统处理。

---

## 2. devin_problem_runs（运行记录）

**用途**：每次devin cli运行的记录。一道题可能有多条记录（重试时）。

### 2.1 字段

| 字段 | 类型 | 必填 | 含义 |
|---|---|---|---|
| `_key` | str | 是 | run唯一标识（如`pipe_<exp_id前40字符>`） |
| `exp_id` | str | 是 | 实验ID（对应trajectory目录名） |
| `problem_id` | str | 是 | 题目ID（JOIN `problem_extraction_progress._key`） |
| `batch_id` | str | 是 | 批次ID（`pipe-runner`=pipe系统，`pipe-cleaning`=清洗系统） |
| `status` | str | 是 | 运行终态（见§2.3枚举） |
| `ended_at` | str | 否 | 结束时间（ISO格式） |
| `runtime_seconds` | float | 否 | 总运行时间（秒） |
| `solve_time_seconds` | float | 否 | 纯解题时间（秒，排除卡住等待） |
| `end_reason` | str | 否 | 结束原因（人类可读描述） |
| `pane_snapshot` | str | 否 | TUI pane快照（最后状态） |
| `verdict` | str | 否 | 判定结论（同status） |
| `started_at` | str | 否 | 开始时间 |
| `difficulty_tier` | int/None | 否 | 难度档位（**⚠️通常为null，见§2.4**） |
| `paths` | object/None | 否 | 文件路径（**⚠️pipe-runner批次缺失，见§2.4**） |

### 2.2 索引

| 索引字段 | 类型 |
|---|---|
| `batch_id` | 单字段 |
| `progress_key` | 单字段 |
| `problem_id` | 单字段 |
| `status` | 单字段 |
| `exp_id` | 单字段 |

### 2.3 status枚举（16种终态）

**成功**：
| status | 含义 |
|---|---|
| `candidate_solved` | AI输出PROOF COMPLETE且有真实proof内容 |

**AI能力失败（不重试）**：
| status | 含义 |
|---|---|
| `failed_token_limit` | token用完，推理链被截断 |
| `ai_gave_up` | AI主动输出I CANNOT SOLVE THIS |
| `failed_thinking_spin` | 超时且仍在thinking（空转） |
| `failed_stall` | 超时且卡住不动（非thinking） |
| `failed_no_proof` | session结束但无proof输出 |
| `answer_leak` | AI检测到答案泄漏（运行中） |
| `answer_leak_in_input` | runner启动前检测到题目含答案 |
| `invalid_tool_use` | 有proof但检测到工具调用 |

**基础设施失败（自动重试）**：
| status | 含义 |
|---|---|
| `rate_limited` | API限流 |
| `dead_session` | devin cli退出但无proof |
| `crash_recovered` | tmux session不存在且无DEVIN_CLI_EXITED |
| `failed_connection` | 连接失败 |
| `launch_error` | 启动失败 |

**其他**：
| status | 含义 |
|---|---|
| `running` | 正在运行（未终态） |

### 2.4 已知问题

- **⚠️ difficulty_tier字段不一致**：`problem_extraction_progress`中tier=1的题在`devin_problem_runs`中`difficulty_tier`被标记为`null`。统计进度时必须用`_key` JOIN `problem_id`，不能按`difficulty_tier`过滤`devin_problem_runs`。
- **⚠️ paths字段缺失**：`batch_id=pipe-runner`的run（32,000+条）没有`paths`字段。通过`exp_id`直接构造路径：`/Volumes/data/math-agent-glm5.2-tmux-agents-trajectory/<exp_id>/`

### 2.5 关键JOIN

```
problem_extraction_progress._key  =  devin_problem_runs.problem_id
```

---

## 3. devin_batch_runs（批次记录）

**用途**：批次级别的记录，记录并发量、状态等。

### 3.1 字段

| 字段 | 类型 | 含义 |
|---|---|---|
| `batch_id` | str | 批次ID |
| `concurrency` | int | 并发量 |
| `status` | str | 批次状态 |
| `attempt_keys` | list | 该批次包含的run key列表 |

---

## 4. devin_run_events（事件流）

**用途**：系统运行过程中的事件流记录。

### 4.1 字段

| 字段 | 类型 | 含义 |
|---|---|---|
| `concurrency_changed` | object | 并发量变更事件 |
| `cases_added` | object | 新增case事件 |
| `answer_leak_detected` | object | 答案泄漏检测事件 |

---

## 5. pipe_monitor_alerts（Monitor Pipe告警）

**用途**：Monitor Pipe持续监控产生的告警。

### 5.1 字段

| 字段 | 类型 | 含义 |
|---|---|---|
| `alert_key` | str | 告警唯一标识 |
| `status` | str | 告警状态（open/resolved） |

---

## 6. Redis队列

**用途**：pipe系统的任务队列。Redis `localhost:6379` db=0。

| 键 | 类型 | 用途 |
|---|---|---|
| `math:pending` | sorted set | 待处理队列（score=priority） |
| `math:running` | hash | 正在运行的run（field=exp_id, value=JSON） |
| `math:completed` | list | 已完成队列 |
| `math:failed` | list | 失败队列 |
| `math:config:concurrency` | string | 并发配置 |

### 清洗系统队列（规划中，见`dev-docs/404-v0-2026-08-21-题目清洗系统方案.md`）

| 键 | 类型 | 用途 |
|---|---|---|
| `math:cleaning:pending` | sorted set | 待清洗队列 |
| `math:cleaning:running` | hash | 正在清洗的run |
| `math:cleaning:completed` | list | 阶段1提取完成 |
| `math:cleaning:verify:pending` | sorted set | 待验证队列 |
| `math:cleaning:verify:running` | hash | 正在验证的run |
| `math:cleaning:failed` | list | 清洗失败 |

---

## 变更历史

| 日期 | 变更 | 文档 |
|---|---|---|
| 2026-08-21 | 创建DATABASE.md，记录5个ArangoDB集合+Redis队列 | 本文件 |
| 2026-08-21 | 新增清洗后字段（original_*/cleaning_*） | `dev-docs/404-v0-2026-08-21-题目清洗系统方案.md` §2.6 |
