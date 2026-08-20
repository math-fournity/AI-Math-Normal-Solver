# 项目 AGENTS.md · 解题系统（Normal Solver）

> **来源**：从 `glm5.2-math-worktree` repo 的 AGENTS.md 中拆分出来的解题系统部分（2026-08-20）。
>
> 本repo是解题系统的独立repo，包含pipe系统代码、解题系统文档、solver相关skills和rules。

---

## 角色

你是解题系统的Master Agent，工作目录`/Users/user/AI-Math-Normal-Solver/`。负责运行、监控、维护管道化解题系统（pipe系统），在数学题上构建GLM-5.2的数学能力边界Profile。

---

## repo基本信息

- 分支：`main`
- Python：依赖原repo的`.venv/`（python3.14），或自建venv
- DB：ArangoDB `localhost:8529`，`ARANGO_DB=xishujuzhen_math_glm52`
- Redis：`localhost:6379` db=0
- 运行前必须`source .env`（从原repo复制或自建）
- 题库：ArangoDB `problem_extraction_progress`集合（246万题）
- 运行记录：ArangoDB `devin_problem_runs`集合
- trajectory数据：`/Volumes/data/math-agent-glm5.2-tmux-agents-trajectory/`
- solver工作目录：`/Volumes/data/math-agent-glm5.2-tmux-agents-dir/`

---

## 铁律

1. **DB**：`ARANGO_DB=xishujuzhen_math_glm52`，运行前必须`echo $ARANGO_DB`确认。忘了source `.env`会fallback到错误数据库。
2. **Git**：显式路径add（禁`-A`/`-.`/`-u`），改前清干净改后立即commit，push需用户授权
3. **Solver隔离**：Solver的devin cli不在本repo内运行（AGENTS.md会劫持Solver行为），外部目录`/Volumes/data/math-agent-glm5.2-tmux-agents-dir/`，`--permission-mode dangerous`
4. **Solver启动**：生产用`noninteractive-solver-run` skill（`devin -p --export`），调试用`solver-tmux-launch` skill，批量用pipe系统。mitmproxy已废弃。
5. **已归档系统**：`batch_problem_runner`/`auto_runner`已被pipe系统替代，不再运行。
6. **不做清单**：①不答案泄漏 ②不用覆盖率代正确 ③不伪造CoT ④不角色混用

---

## 文档索引

**回答问题前，先判断需要加载哪些文档：**

| 你要做什么 | 加载什么 |
|---|---|
| 管道化Profile系统（pipe/5服务+Redis）的架构和运行手册 | `SolverPipeSystem.md` |
| Solver运行操作SOP（pipe启动/健康检查/异常处理/资产追溯规范/失败分类） | `SolverOpsSOP.md` |
| 查询解题结果/失败题/运行资产位置/追溯方法 | `解题系统审计方法.md` |
| 解题系统进展交接（tier=1进度/6.6检测修复/已知问题/待办） | `dev-docs/398-v0-2026-08-19-解题系统进展交接文档.md` |
| 解析--export导出的conversation.json（ATIF格式，含reasoning_content） | `devin-cli-export-conversation.md` |
| 解析sessions_db导出的trajectory.jsonl（JSONL格式） | `trajectory-schema.md` |
| conversation.json面包屑地图方案（结构未知时遍历） | `conversation-map.md` |

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

### 启停命令

```bash
# 启动
bash xishujuzhen/solver_harness/pipe/pipe_start.sh

# 优雅停止（两步）
bash xishujuzhen/solver_harness/pipe/pipe_stop.sh          # 停feeder/runner等，保留collector
bash xishujuzhen/solver_harness/pipe/pipe_stop.sh --finish  # 等running=0后，停collector

# 状态检查
PYTHONPATH=xishujuzhen/solver_harness/pipe .venv/bin/python3 xishujuzhen/solver_harness/pipe/pipe_control.py status

# 进度检查
PYTHONPATH=xishujuzhen/solver_harness/pipe .venv/bin/python3 xishujuzhen/solver_harness/pipe/scripts/check_progress.py
```

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
| `solver-batch-health-check.md` | 批量解题系统健康检查规则 |

---

## 关键经验

- **thinking spin不是卡住**：devin cli在thinking期间不输出任何内容到stdout——pane空+pipe.log空是正常状态。thinking可以持续1000+秒。判断方法：检查devin进程是否有到代理(localhost:7897)的ESTABLISHED连接。有连接=thinking spin，无连接=真卡住。
- **difficulty_tier字段不一致**：`problem_extraction_progress`中tier=1的题在`devin_problem_runs`中`difficulty_tier`被标记为`null`。统计进度时必须用`_key` JOIN `problem_id`。
- **rate limit风暴**：并发20+可能触发rate limit风暴。稳定上限是并发15。30并发在API不忙时可行。
- **pipe-runner batch无paths字段**：32,930条run没有paths字段，通过exp_id直接构造路径：`/Volumes/data/math-agent-glm5.2-tmux-agents-trajectory/<exp_id>/`

---

## 与原repo的关系

本repo从`/Users/user/glm5.2-math-worktree/`拆分而来。原repo保留数学大师系统的其他部分（Grove核心循环/题海梳理/认知图/Seven System等）。两个repo共享同一个ArangoDB和Redis实例。
