---
name: solver-tmux-launch
description: >
  调试用：通过solver-harness启动devin cli实例并用tmux实时观察其行为。
  仅用于harness设计/改进/调试场景——验证harness能否正确让AI工作。
  生产实验（采集thinking、批量解题、POC实验等）用noninteractive-solver-run skill（devin -p --export）。
  mitmproxy已废弃（2026-08-18），不再用于生产trajectory采集。
  WHEN to use: 调试solver-harness本身的设计/改进/验证时；需要tmux实时观察devin cli行为时。
  WHEN NOT to use: 生产实验运行解题AI（用noninteractive-solver-run）；批量解题（用pipe系统）。
---

# solver-tmux-launch skill

## 定位（2026-08-18更新）

**本skill仅用于调试场景。**

- **生产实验**（POC、对照实验、批量解题等）→ 用全局 `noninteractive-solver-run` skill
  - `devin -p --prompt-file input.txt --model glm-5-2 --export conversation.json`
  - conversation.json 的 `reasoning_content` 字段包含完整 thinking
  - 不需要 mitmproxy，不需要 solver-harness
- **调试 harness**（验证 harness 设计、改进 harness 代码）→ 用本 skill
  - 通过 solver-harness 启动，用 tmux 实时观察 devin cli 行为
  - 加 `--no-mitm` 避免代理问题

## mitmproxy已废弃（2026-08-18）

**mitmproxy不再用于生产trajectory采集。** 原因：
1. `--export` 的 conversation.json 包含 `reasoning_content`（完整thinking），不需要MITM截获
2. mitmproxy代理在多AI并发时造成冲突（共享端口18889，连接竞争导致进程秒退）
3. mitmproxy增加复杂度且容易失败（代理不通、SSL验证问题）

**历史背景**：mitmproxy曾用于token级实时thinking截获（2026-08-08建立），但2026-08-12已设`mitm_enabled: False`默认值，2026-08-18正式废弃。

## 调试工作流

### 步骤1：启动调试实验

```bash
python3 xishujuzhen/solver_harness/solver_harness.py launch \
  --exp-id <debug-exp-id> \
  --problem-file <problem.txt> \
  --model glm-5-2 \
  --no-mitm
```

自动完成：
1. 创建Solver工作目录（`/Volumes/data/math-agent-glm5.2-tmux-agents-dir/<exp-id>/`）
2. 创建Trajectory数据目录
3. 复制AGENTS.md模板和problem.txt到Solver目录
4. 启动devin cli in tmux（`-p`非交互模式 + `--export`）
5. 启动pipe-pane兜底记录
6. 启动sessions.db轮询进程

### 步骤2：实时观察

```bash
# 查看tmux session输出
tmux capture-pane -t harness-<exp-id> -p

# 持续观察
tmux attach -t harness-<exp-id>

# 查看实验状态
python3 xishujuzhen/solver_harness/solver_harness.py status --exp-id <exp-id>
```

### 步骤3：停止

```bash
python3 xishujuzhen/solver_harness/solver_harness.py stop --exp-id <exp-id> --no-decode
```

## 数据产物

```
/Volumes/data/math-agent-glm5.2-tmux-agents-trajectory/<exp-id>/
├── session_info.json          # 实验元信息
├── sessions_db/
│   └── trajectory.jsonl       # step级（db monitor实时轮询，含thinking）
├── tmux/
│   ├── tmux.log               # tee输出
│   └── tmux_pipe.log          # pipe-pane兜底
└── exports/
    └── conversation.json      # devin cli --export（含reasoning_content=thinking）
```

## 注意事项

1. **调试时加 `--no-mitm`**——mitmproxy已废弃，不加会尝试走代理可能失败
2. **tmux session名中的点号会被转为下划线**——exp-id中的`.`在tmux中变成`_`，pipe-pane/capture-pane命令可能因名字不匹配而失败。建议exp-id用下划线不用点号
3. **`-p`模式下devin完成后自动退出**——tmux session在devin退出后sleep 60秒自动消失
4. **conversation.json的reasoning_content就是thinking**——不需要MITM，不需要sessions.db提取

## 历史经验（保留供参考）

### 经验1：conversation.json的reasoning_content就是thinking

**发现日期**：2026-08-17

agent step结构：`{step_id, timestamp, source, message, model_name, reasoning_content, tool_calls, observation, metrics, extra}`。`reasoning_content`字段包含完整thinking。

### 经验2：交互模式的tmux_pipe.log不可靠

tmux_pipe.log有ANSI码污染，terminal重绘导致大量重复内容，提取的thinking碎片化。不要依赖pipe.log做thinking分析。

### 反模式：用mitmproxy采集thinking

`--export`的conversation.json已包含reasoning_content（完整thinking），不需要MITM。MITM增加复杂度且容易失败。
