---
description: >
  调试用：通过solver-harness启动devin cli实例并用tmux实时观察其行为。
  仅用于harness设计/改进/调试场景——验证harness能否正确让AI工作。
  生产实验（采集thinking、批量解题、POC实验等）用noninteractive-solver-run skill（devin -p --export）。
  mitmproxy已废弃（2026-08-18），不再用于生产trajectory采集。
  WHEN to use: 调试solver-harness本身的设计/改进/验证时；需要tmux实时观察devin cli行为时。
  WHEN NOT to use: 生产实验运行解题AI（用noninteractive-solver-run）；批量解题（用pipe系统）。
trigger: model_decision
---

# solver-tmux-launch rule

## 定位（2026-08-18更新）

**本rule/skill仅用于调试场景**——验证solver-harness的设计、改进harness代码、观察devin cli在tmux中的实时行为。

**生产实验（POC、对照实验、批量解题等）不使用本rule**——用全局 `noninteractive-solver-run` skill（`devin -p --prompt-file ... --export ...`），conversation.json的reasoning_content包含完整thinking，不需要mitmproxy，不需要solver-harness。

## mitmproxy已废弃（2026-08-18）

**mitmproxy不再用于生产trajectory采集。** 原因：
1. `--export`的conversation.json包含`reasoning_content`（完整thinking），不需要MITM截获
2. mitmproxy代理在多AI并发时造成冲突（共享端口18889，连接竞争导致进程秒退）
3. mitmproxy增加复杂度且容易失败（代理不通、SSL验证问题）

**历史背景**：mitmproxy曾用于token级实时thinking截获（2026-08-08建立），但2026-08-12已设`mitm_enabled: False`默认值，2026-08-18正式废弃。

## 调试场景的启动方式

```bash
# 调试solver-harness时用（观察harness能否正确启动devin cli）
python3 xishujuzhen/solver_harness/solver_harness.py launch \
  --exp-id <debug-exp-id> \
  --problem-file <problem.txt> \
  --model glm-5-2 \
  --no-mitm

# 观察devin cli在tmux中的行为
tmux capture-pane -t harness-<exp-id> -p
tmux attach -t harness-<exp-id>

# 停止
python3 xishujuzhen/solver_harness/solver_harness.py stop --exp-id <debug-exp-id> --no-decode
```

## 详见

skill: `.devin/skills/solver-tmux-launch/SKILL.md`——调试工作流和注意事项。
