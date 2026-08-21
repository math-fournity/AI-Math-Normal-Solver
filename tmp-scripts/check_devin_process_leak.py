#!/usr/bin/env python3
"""check_devin_process_leak.py — 临时调查脚本

调查目的：检查系统中是否有解题系统的devin cli实例泄漏。
通过检查所有devin进程的工作目录来判断——如果工作目录指向
solver工作目录（/Volumes/data/math-agent-glm5.2-tmux-agents-dir/），
说明是解题系统启动的devin cli还在运行。

使用时间：2026-08-21
调查结果：无泄漏，所有devin进程都是交互式会话，无解题系统实例。

用法:
  python3 tmp-scripts/check_devin_process_leak.py
"""
import subprocess

# 获取所有devin进程的PID
ps_output = subprocess.check_output(
    ["ps", "aux"], text=True
)
pids = []
for line in ps_output.split("\n"):
    if "devin" in line and "grep" not in line:
        parts = line.split()
        if len(parts) > 1:
            try:
                pids.append(parts[1])
            except ValueError:
                pass

for pid in pids:
    # 用lsof查工作目录
    try:
        lsof_output = subprocess.check_output(
            ["lsof", "-a", "-p", pid, "-d", "cwd"], text=True,
            stderr=subprocess.DEVNULL
        )
        lines = lsof_output.strip().split("\n")
        if len(lines) > 1:
            # 最后一行是实际cwd
            parts = lines[-1].split()
            cwd = " ".join(parts[8:]) if len(parts) > 8 else "unknown"
        else:
            cwd = "unknown"
    except subprocess.CalledProcessError:
        cwd = "unknown"

    # 查命令行
    try:
        cmd_output = subprocess.check_output(
            ["ps", "-p", pid, "-o", "args="], text=True
        ).strip()[:80]
    except subprocess.CalledProcessError:
        cmd_output = "unknown"

    print(f"PID={pid} CWD={cwd} CMD={cmd_output}")
