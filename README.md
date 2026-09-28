# AI-Math-Normal-Solver

> **从零恢复完整工作环境**（本仓库是仓库族之一）：按主仓库手册 [docs/RESTORE-GUIDE.md](https://github.com/math-fournity/AI-Math-Competition-Problem-Solving-System/blob/main/docs/RESTORE-GUIDE.md) 执行。

平凡解题系统（Normal Solver）——AI 数学竞赛题解题的早期世代管线代码。以**固定服务管道
（pipe）+ Redis 队列**批量跑题：一次入队数千题，devin cli 逐题解题，五服务协作完成投喂、
监控、收集、报告与崩溃恢复；配套运行 SOP（编译验证/资产追溯/失败分类）与 16 种终态
status 分类。

> 与主系统的关系：本系统与后续的持续解题系统共用同一套 ArangoDB 数据表
> （`xishujuzhen_math_glm52`，其中 `devin_problem_runs` 5 万+ 条即本系统时代的运行记录）
> 与运行现场目录。本 repo 保留的是**代码与 SOP**；运行现场归档见
> [AI-Math-Solving-Trajectories-Archive](https://github.com/math-fournity/AI-Math-Solving-Trajectories-Archive)，
> 数据表见 [AI-Math-Solving-Databases](https://github.com/math-fournity/AI-Math-Solving-Databases)，
> 后续世代主系统见
> [AI-Math-Competition-Problem-Solving-System](https://github.com/math-fournity/AI-Math-Competition-Problem-Solving-System)。

## 功能与架构

```
prepare_queue.py   队列准备（一次性入队数千题，之后可不启动 feeder）
      │
      ▼
feeder.py ──► Redis 队列 ──► monitor_pipe.py（5 服务管道监控）
                                   │  启动/看护 tmux 中的 devin cli 解题实例
                                   ▼
                             collector.py   收集每题产出（proof/对话录/轨迹）
                                   ▼
                             reporter.py    报告与进度
      旁路：pipe_watchdog.sh（看门狗自动重启）· graceful_shutdown.py（优雅停止）
            recover_from_crash.py（崩溃恢复）· reclassify_failed.py（失败重分类）
```

### 管道模块（`xishujuzhen/solver_harness/pipe/`）

| 模块 | 职责 |
|---|---|
| `prepare_queue.py` / `feeder.py` / `redis_queue.py` | 题目入队与 Redis 队列管理 |
| `monitor_pipe.py` / `pipe_control.py` / `pipe_watchdog.sh` | 管道监控、控制与看门狗 |
| `collector.py` / `reporter.py` | 产出收集与报告 |
| `audit_trace.py` / `build_profile.py` | 审计追踪与题目画像 |
| `check_answer_leak.py` | 答案泄漏检查（题面是否含答案） |
| `extract_solve_time.py` / `query_progress.py` / `query_failures.py` | 解题时长/进度/失败查询 |
| `reclassify_failed.py` / `recover_from_crash.py` / `graceful_shutdown.py` | 失败重分类、崩溃恢复、优雅停止 |

### 运行 SOP（根目录文档）

| 文档 | 内容 |
|---|---|
| `SolverPipeSystem.md` | 管道化 Profile 系统（pipe/5 服务 + Redis）完整架构与运行手册 |
| `SolverOpsSOP.md` | 运行操作 SOP：编译验证（G1）/ 资产追溯（G2）/ 失败分类（G3） |
| `解题系统审计方法.md` | 结果查询、失败题、运行资产位置与追溯方法、16 种终态 status 分类 |
| `templates/` | solver 的 AGENTS.md 提示词模板（guided/plain 两版） |
| `dev-docs/` | 演进文档：队列准备方案（3,448 题一次性入队）、tier=1 结果调查报告等 |
| `docs/dev/AI-GUIDE.md` | AI 协作内部引导地图（文档/代码索引、目录结构、操作经验） |

## 数据表

本系统写入 ArangoDB 库 `xishujuzhen_math_glm52`（与后续世代系统共享），核心表：
`devin_problem_runs`（每题运行记录）、`problems`、`math_datasets`、分析/选择结果表等。
整体导出与恢复脚本见
[AI-Math-Solving-Databases](https://github.com/math-fournity/AI-Math-Solving-Databases)。

## License

MIT License，见 [LICENSE](LICENSE)。
