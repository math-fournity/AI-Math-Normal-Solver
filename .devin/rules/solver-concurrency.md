---
description: >
  Solver AI并发约束。runner启动devin cli时每3秒启动1个，不可改。
  并发数通过Redis实时调整，推荐50-60。
  WHEN to use: 启动或调整Solver并发数、runner启动devin cli时。
  WHEN NOT to use: 非Solver的devin cli使用。
trigger: model_decision
---

# Solver AI 并发约束

**硬约束**：runner启动devin cli时**每3秒启动1个，不可改**。并发数通过Redis实时调整，推荐50-60。

## 铁律：3秒启动间隔不可改

**runner.py第333行的`time.sleep(3)`是铁律，不可修改。**

- 每个devin cli启动后固定等3秒再启动下一个
- 这是防止API rate limit的关键——并行启动60个session会导致58/60个遇到rate limit
- 3秒间隔意味着：填满60并发需要180秒（3分钟），填满80并发需要240秒（4分钟）
- **恢复慢是正常的**——collector清理完积压session后，runner按3秒间隔逐步填充，不是bug

## 并发上限经验（管道化系统实测，2026-08-13）

> 旧batch系统（batch_problem_runner）的并发经验已过时——60并发51%雪崩是旧系统的数据，
> 管道化系统架构不同（独立tmux session+collector轮询+3秒间隔启动），已验证60并发稳定运行。

| 并发 | 吞吐 | solved率 | dead率 | rate limit | 结论 |
|---|---|---|---|---|---|
| 40 | 257题/时 | 56% | 23% | 0 | 稳定，但吞吐低 |
| 50 | 360题/时 | 63% | 11% | 0 | 稳定 |
| 60 | 465题/时 | 65% | 9% | 0 | 稳定 |
| 80 | 待观察 | — | — | — | 当前测试中 |
| 100 | 729题/时 | 62% | 6% | 15个 | 吞吐最高但触发限流 |

## 调试反模式：看到dead_session就降并发（2026-08-13教训）

> **看到dead_session激增时，先查根因，不要急着降并发。**

2026-08-13的教训：80并发时出现大量dead_session（81个/10分钟），我反复降回60。
但根因不是80并发触发限流——是collector的`pane_is_empty`误杀初始化中的session：
- `-p`模式下devin cli连接API、初始化期间无stdout输出，pane和pipe.log都是0字节
- collector用`pane_is_empty`判dead_session，把正在初始化的session杀掉
- runner启动1个，collector杀好几个，导致并发数无法维持

**修复后80并发应该重新观察**——之前的dead_session数据是collector bug导致的，不是80并发本身的问题。

**正确做法**：
1. 看到dead_session激增 → 先查dead_session的pipe.log是否为空（空=初始化中被误杀）
2. 确认collector是否在用`pane_is_empty`判断（已改为`DEVIN_CLI_EXITED`）
3. 修复collector后再观察并发本身的表现，不要把collector bug和并发问题混为一谈

**推荐并发：60-80。** 60并发已验证稳定，80并发在collector修复后待验证。

## 并发调整

通过Redis实时调整，不需要重启runner：
```
PYTHONPATH=xishujuzhen/solver_harness/pipe .venv/bin/python3 -c "
from redis_queue import get_redis
r = get_redis()
r.set('math:config:concurrency', 60)
"
```

runner每轮poll从Redis读取`math:config:concurrency`，实时生效。

## 检查并发数

```bash
# Redis中的并发设定值
PYTHONPATH=xishujuzhen/solver_harness/pipe .venv/bin/python3 -c "from redis_queue import get_redis; r=get_redis(); print(r.get('math:config:concurrency'))"

# 实际running数
PYTHONPATH=xishujuzhen/solver_harness/pipe .venv/bin/python3 -c "from redis_queue import get_redis; r=get_redis(); print(r.hlen('math:running'))"

# tmux session数
tmux list-sessions | grep "harness-" | grep -v dbmon | wc -l
```

## 适用于

- 所有通过 `solver-harness launch` 启动的 Solver AI
- 管道化系统（feeder/runner/collector/reporter/retry五服务）
- 树生长引擎（tree_engine.py）——静态编排和动态编排都适用
- A/B 对照实验
