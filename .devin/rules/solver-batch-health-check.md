---
description: >
  Solver批量集群健康检查铁律。用户说"检查进度"、"看看有没有问题"、"检查链接问题"、
  "检查并发"时；批量Solver集群运行时；新session接手批量系统时。
  WHEN to use: 批量Solver集群运行时；用户要求检查进度/并发/链接问题时。
  WHEN NOT to use: 非批量Solver集群场景。
trigger: model_decision
---

# Solver批量集群健康检查铁律

**触发条件**：用户说"检查进度"、"看看有没有问题"、"检查链接问题"、"检查并发"时；批量Solver集群运行时；新session接手批量系统时。

**核心约束**：检查批量集群时必须到最前线——不能只看DB status数字，必须看tmux pane内容、文件实际大小、进程实际状态。

## 管道化系统检查铁律（当前生效）

### 铁律0：检查不用长sleep等待——简单查询立即执行

**这是最重要的检查纪律。** 检查系统状态时：

- **简单查询立即执行**——`pipe_control.py status`、`tmux list-sessions`、看日志tail等，都是秒级返回的命令，不需要任何sleep前置
- **禁止用sleep 60/120/300等长等待**——检查命令本身是即时返回的，加sleep只是浪费时间
- **要看趋势变化时用短间隔多次检查**——需要对比前后状态时，做两次即时检查（中间可以做别的事），不要用一次长sleep
- **等待新完成出现时用get_output轮询**——如果确实需要等后台命令完成，用get_output with timeout，不要用sleep阻塞
- **反模式**：`sleep 300 && pipe_control.py status`——为什么要等5分钟才查状态？状态查询是即时的

### 铁律1：用pipe_control.py，不现写脚本

管道化系统用`pipe_control.py`检查，不另写脚本：

- `pipe_control.py status`——总状态（服务存活+队列+实时配置）
- `pipe_control.py status -v`——含running attempt详情
- `pipe_control.py health`——全面健康检查（7项+末尾提醒检查Monitor Pipe）
- `pipe_control.py concurrency 50`——实时调并发
- `pipe_control.py recover --dry-run`——断电恢复检查
- `pipe_control.py monitor start/stop/status`——单独管理Monitor Pipe

**Monitor Pipe检查**（`pipe_control.py health`输出末尾会提醒）：

- `bash xishujuzhen/solver_harness/pipe/scripts/monitor_check.sh`——标准化检查脚本（5项检查+对AI的核心提醒）
- Monitor Pipe持续监控13项自动检查+AI review抽样，alert写入`pipe_monitor_alerts`集合
- 详见AGENTS.md中"### Monitor Pipe检查"小节和`dev-docs/391号`

新增查询需求时更新pipe_control.py，不另写脚本。

### 铁律2：DB status数字会骗人，必须到最前线

DB显示`running`不代表Solver在工作。必须验证三个层面：

1. **tmux pane内容**——`tmux capture-pane -t <session> -p`，pane空白=Devin CLI已退出（僵尸session）
2. **文件实际大小**——thinking_readable_path和tmux_pipe_path的大小，0KB=没产出
3. **idle时间真实性**——activity_signature可能因tmux_log_path变化永远在变，导致idle永远=0

### 铁律3：发现问题先修检查工具再修服务代码

检查脚本和监控代码是两层。pipe_control.py是检查工具，runner/collector是服务代码。发现问题后：
1. 先在pipe_control.py中加检测命令（让下次能一键发现）
2. 再在runner/collector中修服务逻辑（让服务能自动处理）

## 旧batch系统检查（已废弃，仅参考）

旧batch系统用batch_status.py，管道化系统已替代它。旧命令保留供参考：
- `batch_status.py status/active/errors/solved/feed/leak/dead`——旧batch系统检查

## 并发上限经验（管道化系统实测，2026-08-13）

> **旧batch系统（batch_problem_runner）的并发经验已过时**——60并发51%雪崩是旧系统的数据，
> 管道化系统架构不同（独立tmux session+collector轮询），已验证60并发稳定运行。

| 并发 | 吞吐 | solved率 | dead率 | rate limit | 结论 |
|---|---|---|---|---|---|
| 40 | 257题/时 | 56% | 23% | 0 | 稳定，但吞吐低 |
| 50 | 360题/时 | 63% | 11% | 0 | 稳定 |
| 60 | 465题/时 | 65% | 9% | 0 | **最优** |
| 100 | 729题/时 | 62% | 6% | 15个 | 吞吐最高但触发限流 |

**推荐并发：50-60。60并发是最优平衡点（吞吐高+无限流+solved率最高）。**

## 已知问题清单（每次检查时对照）

1. **僵尸session**：Devin CLI退出后tmux session不消失，pane空白但tmux_running=True。已修复：observe_attempt_files中加pane_empty检测。
2. **activity_signature永远在变**：tmux_log_path被tmux自己写，导致idle永远=0，stall检测不触发。已修复：signature排除tmux_log_path和session_info_path。
3. **rate_limited误报**：RATE_LIMIT_PATTERNS匹配到"Thinking Round 1429"中的"429"。已修复：用具体模式"http 429"/"status 429"/"error 429"。
4. **failed_no_proof实为connection_error**：CONNECTION_PATTERNS没覆盖cognition.ai/errorKind。已修复：加connection error/unavailable/errorKind模式。
5. **FATE .json格式不支持**：original_index是problem_id编号不是数组索引。已修复：用original_id_in_source匹配json的id字段。
6. **monitor漏判PROOF COMPLETE**：Solver输出了但monitor还没poll到。正常——等下一轮poll会判定。
7. **答案泄漏检查的假阳性**：export/conversation.json中出现ANSWER LEAK DETECTED是指令文本本身，不是真报错。已处理：leak命令区分指令文本vs真报错。
